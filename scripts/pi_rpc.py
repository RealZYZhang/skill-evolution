#!/usr/bin/env python3
"""Run Pi as a subprocess and communicate through its JSONL RPC protocol."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prompt_approval import (
    SKILL_CONTENT_PLACEHOLDER,
    load_approved_prompt,
    render_skill_prompt,
)


JsonObject = dict[str, Any]
RpcRecordObserver = Callable[[str, str, JsonObject | None, str | None], None]
StderrObserver = Callable[[str], None]


class PiRpcError(RuntimeError):
    """Base error raised by the Pi RPC client."""


class PiNotFoundError(PiRpcError):
    """Raised when no Pi executable or installed package can be found."""


class PiProcessExitedError(PiRpcError):
    """Raised when Pi exits before completing an RPC request."""


class PiProtocolError(PiRpcError):
    """Raised when Pi emits a record that violates the JSONL object protocol."""


class PiRequestTimeoutError(PiRpcError):
    """Raised when an RPC request does not receive a response in time."""


def _split_command(value: str) -> list[str]:
    return shlex.split(value, posix=os.name != "nt")


def resolve_pi_command(explicit: Sequence[str] | str | None = None) -> list[str]:
    """Resolve a command that starts the Pi CLI.

    Resolution order is an explicit value, ``PI_CODING_AGENT_COMMAND``, the
    current ``PATH``, and known packages under ``npm root -g``.
    """

    if explicit:
        command = (
            _split_command(explicit)
            if isinstance(explicit, str)
            else list(explicit)
        )
        if not command:
            raise PiNotFoundError("The explicit Pi command is empty.")
        return command

    from_environment = os.environ.get("PI_CODING_AGENT_COMMAND")
    if from_environment:
        command = _split_command(from_environment)
        if command:
            return command

    on_path = shutil.which("pi")
    if on_path:
        return [on_path]

    npm = shutil.which("npm")
    if npm:
        try:
            result = subprocess.run(
                [npm, "root", "-g"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            result = None

        if result:
            npm_root = Path(result.stdout.strip())
            package_names = (
                "@earendil-works/pi-coding-agent",
                "@mariozechner/pi-coding-agent",
            )
            for package_name in package_names:
                cli = npm_root / package_name / "dist" / "cli.js"
                if not cli.is_file():
                    continue
                if os.access(cli, os.X_OK):
                    return [str(cli)]
                node = shutil.which("node")
                if node:
                    return [node, str(cli)]

    raise PiNotFoundError(
        "Could not find Pi. Install pi-coding-agent, put `pi` on PATH, "
        "pass pi_command=..., or set PI_CODING_AGENT_COMMAND."
    )


class PiRpcClient:
    """Manage one Pi RPC subprocess with correlated responses and event streaming."""

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        pi_command: Sequence[str] | str | None = None,
        pi_args: Sequence[str] = (),
        no_session: bool = True,
        approve_project: bool | None = None,
        env: Mapping[str, str] | None = None,
        replace_environment: bool = False,
        pass_fds: Sequence[int] = (),
        stderr_limit: int = 200,
        rpc_record_observer: RpcRecordObserver | None = None,
        stderr_observer: StderrObserver | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.pi_command = pi_command
        self.pi_args = list(pi_args)
        self.no_session = no_session
        self.approve_project = approve_project
        if env is None:
            self.env = None
        elif replace_environment:
            self.env = dict(env)
        else:
            self.env = {**os.environ, **env}
        if any(
            isinstance(file_descriptor, bool)
            or not isinstance(file_descriptor, int)
            or file_descriptor < 0
            for file_descriptor in pass_fds
        ):
            raise ValueError("pass_fds must contain non-negative integers")
        self.pass_fds = tuple(pass_fds)
        self.rpc_record_observer = rpc_record_observer
        self.stderr_observer = stderr_observer
        self._process: subprocess.Popen[bytes] | None = None
        self._pending: dict[str, queue.Queue[JsonObject | BaseException]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._events: queue.Queue[JsonObject] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=stderr_limit)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._observer_errors: deque[str] = deque()

    @property
    def process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise PiRpcError("Pi RPC process has not been started.")
        return self._process

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """Return a bounded snapshot of Pi's recent stderr lines."""

        return tuple(self._stderr)

    @property
    def observer_errors(self) -> tuple[str, ...]:
        """Return errors raised by optional recording observers."""

        return tuple(self._observer_errors)

    def _observe_rpc_record(
        self,
        direction: str,
        raw: str,
        parsed: JsonObject | None,
        parse_error: str | None = None,
    ) -> None:
        observer = self.rpc_record_observer
        if observer is None:
            return
        try:
            observer(direction, raw, parsed, parse_error)
        except Exception as error:  # Observability must not break Pi transport.
            self._observer_errors.append(
                f"RPC record observer failed: {type(error).__name__}: {error}"
            )

    def _observe_stderr(self, line: str) -> None:
        observer = self.stderr_observer
        if observer is None:
            return
        try:
            observer(line)
        except Exception as error:  # Observability must not break Pi transport.
            self._observer_errors.append(
                f"stderr observer failed: {type(error).__name__}: {error}"
            )

    def build_command(self) -> list[str]:
        """Build the final Pi CLI command without starting it."""

        command = resolve_pi_command(self.pi_command)
        arguments = [*command, *self.pi_args]
        has_mode = "--mode" in arguments or any(
            argument.startswith("--mode=") for argument in arguments
        )
        if not has_mode:
            arguments.extend(["--mode", "rpc"])
        if self.no_session and "--no-session" not in arguments:
            arguments.append("--no-session")
        if self.approve_project is True and "--approve" not in arguments:
            arguments.append("--approve")
        if self.approve_project is False and "--no-approve" not in arguments:
            arguments.append("--no-approve")
        return arguments

    def start(self) -> PiRpcClient:
        """Start Pi and its stdout/stderr reader threads."""

        if self._process is not None:
            if self._process.poll() is None:
                return self
            raise PiProcessExitedError("This PiRpcClient process has already exited.")

        command = self.build_command()
        try:
            self._process = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=self.pass_fds,
            )
        except OSError as error:
            raise PiRpcError(f"Failed to start Pi RPC process: {error}") from error

        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            name="pi-rpc-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="pi-rpc-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        return self

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        try:
            # Binary readline honors Pi's strict LF record delimiter and does
            # not split JSON strings containing Unicode U+2028 or U+2029.
            for raw_record in iter(stdout.readline, b""):
                if not raw_record.endswith(b"\n"):
                    raw = raw_record.decode("utf-8", errors="replace")
                    error = "RPC record ended without an LF delimiter"
                    self._observe_rpc_record("pi_to_client", raw, None, error)
                    self._events.put(
                        {
                            "type": "client_protocol_error",
                            "error": error,
                            "raw": raw,
                        }
                    )
                    continue
                record = raw_record[:-1]
                if record.endswith(b"\r"):
                    record = record[:-1]
                if not record:
                    continue
                raw = record.decode("utf-8", errors="replace")
                try:
                    decoded = json.loads(record.decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise ValueError("top-level JSON value is not an object")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    self._observe_rpc_record(
                        "pi_to_client",
                        raw,
                        None,
                        str(error),
                    )
                    self._events.put(
                        {
                            "type": "client_protocol_error",
                            "error": str(error),
                            "raw": raw,
                        }
                    )
                    continue

                self._observe_rpc_record("pi_to_client", raw, decoded)
                request_id = decoded.get("id")
                if decoded.get("type") == "response" and isinstance(request_id, str):
                    with self._pending_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        waiter.put(decoded)
                        continue
                self._events.put(decoded)
        finally:
            if not self._stopping.is_set():
                code = self.process.poll()
                error = PiProcessExitedError(
                    f"Pi RPC stdout closed before shutdown (exit code {code})."
                )
                with self._pending_lock:
                    waiters = list(self._pending.values())
                for waiter in waiters:
                    waiter.put(error)

    def _read_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        for raw_line in iter(stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            self._stderr.append(line)
            self._observe_stderr(line)

    def send(self, message: Mapping[str, Any]) -> None:
        """Send one JSON object without waiting for a correlated response."""

        if not isinstance(message, Mapping):
            raise TypeError("RPC message must be a mapping.")
        process = self.process
        if process.poll() is not None:
            raise PiProcessExitedError(
                f"Pi RPC process already exited with code {process.returncode}."
            )
        stdin = process.stdin
        assert stdin is not None
        encoded = (
            json.dumps(
                dict(message),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with self._write_lock:
                raw = encoded[:-1].decode("utf-8")
                self._observe_rpc_record(
                    "client_to_pi",
                    raw,
                    dict(message),
                )
                stdin.write(encoded)
                stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise PiProcessExitedError("Could not write to Pi RPC stdin.") from error

    def request(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float = 30.0,
    ) -> JsonObject:
        """Send a command and wait for the response with the same request ID."""

        message = dict(command)
        request_id = message.get("id")
        if request_id is None:
            request_id = str(uuid.uuid4())
            message["id"] = request_id
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("RPC request id must be a non-empty string.")

        waiter: queue.Queue[JsonObject | BaseException] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if request_id in self._pending:
                raise ValueError(f"RPC request id is already pending: {request_id}")
            self._pending[request_id] = waiter
        try:
            self.send(message)
            try:
                result = waiter.get(timeout=timeout)
            except queue.Empty as error:
                raise PiRequestTimeoutError(
                    f"Timed out after {timeout:g}s waiting for request {request_id}."
                ) from error
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def next_event(self, *, timeout: float | None = None) -> JsonObject:
        """Return the next asynchronous Pi event."""

        try:
            return self._events.get(timeout=timeout)
        except queue.Empty as error:
            raise PiRequestTimeoutError("Timed out waiting for a Pi event.") from error

    def events_until(
        self,
        predicate: Callable[[JsonObject], bool],
        *,
        timeout: float,
    ) -> Iterator[JsonObject]:
        """Yield events through and including the first event matching predicate."""

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiRequestTimeoutError(
                    f"Timed out after {timeout:g}s waiting for Pi to settle."
                )
            event = self.next_event(timeout=remaining)
            yield event
            if predicate(event):
                return

    def close(self, *, timeout: float = 5.0) -> None:
        """Close stdin, then terminate Pi if it does not exit promptly."""

        process = self._process
        if process is None:
            return
        self._stopping.set()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout)
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> PiRpcClient:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, indent=2))


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="Pi working directory")
    parser.add_argument(
        "--pi-command",
        help="Pi executable command; defaults to PATH/npm discovery",
    )
    parser.add_argument(
        "--pi-arg",
        action="append",
        default=[],
        help="Additional Pi argument; repeat and use --pi-arg=--flag",
    )
    parser.add_argument(
        "--session",
        action="store_true",
        help="Enable Pi's own session persistence",
    )
    trust = parser.add_mutually_exclusive_group()
    trust.add_argument("--approve-project", action="store_true")
    trust.add_argument("--deny-project", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("state", help="Print Pi's current RPC state")
    prompt_parser = subparsers.add_parser("prompt", help="Run one prompt")
    prompt_parser.add_argument(
        "--prompt-file",
        required=True,
        help="Versioned prompt file with an approved sidecar",
    )
    prompt_parser.add_argument(
        "--skill",
        help="Skill directory used to render a skill prompt template",
    )
    prompt_parser.add_argument("--timeout", type=float, default=300.0)
    raw_parser = subparsers.add_parser("raw", help="Send one raw JSON command")
    raw_parser.add_argument("json_command")
    raw_parser.add_argument("--timeout", type=float, default=30.0)
    options = parser.parse_args(arguments)

    approve_project = (
        True
        if options.approve_project
        else False if options.deny_project else None
    )
    try:
        raw_command: JsonObject | None = None
        approved_prompt = None
        prompt_text: str | None = None
        if options.action == "raw":
            decoded = json.loads(options.json_command)
            if not isinstance(decoded, dict):
                raise ValueError("Raw command must be a JSON object.")
            if decoded.get("type") == "prompt":
                raise ValueError(
                    "Use the prompt action with an approved prompt file."
                )
            raw_command = decoded
        elif options.action == "prompt":
            approved_prompt = load_approved_prompt(options.prompt_file)
            if SKILL_CONTENT_PLACEHOLDER in approved_prompt.text:
                if not options.skill:
                    raise ValueError(
                        "--skill is required for a skill prompt template."
                    )
                prompt_text = render_skill_prompt(
                    approved_prompt,
                    options.skill,
                ).text
            else:
                prompt_text = approved_prompt.text

        with PiRpcClient(
            cwd=options.cwd,
            pi_command=options.pi_command,
            pi_args=options.pi_arg,
            no_session=not options.session,
            approve_project=approve_project,
        ) as client:
            if options.action == "state":
                _print_json(client.request({"type": "get_state"}))
                return 0
            if options.action == "raw":
                if raw_command is None:
                    raise ValueError("Raw command was not prepared.")
                _print_json(
                    client.request(raw_command, timeout=options.timeout)
                )
                return 0

            if approved_prompt is None or prompt_text is None:
                raise ValueError("Approved prompt was not prepared.")
            response = client.request(
                {"type": "prompt", "message": prompt_text},
                timeout=min(options.timeout, 30.0),
            )
            if not response.get("success"):
                _print_json(response)
                return 1
            for event in client.events_until(
                lambda item: item.get("type") == "agent_settled",
                timeout=options.timeout,
            ):
                if event.get("type") != "message_update":
                    continue
                delta = event.get("assistantMessageEvent", {})
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    print(delta.get("delta", ""), end="", flush=True)
            print()
            return 0
    except (PiRpcError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"pi-rpc: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_cli())
