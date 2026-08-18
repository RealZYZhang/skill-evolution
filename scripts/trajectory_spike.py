#!/usr/bin/env python3
"""Run one Pi skill and capture an ordered action-level trajectory."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import threading
import time
from typing import Any, Mapping, Sequence
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pi_rpc import JsonObject, PiRpcClient, PiRpcError
from scripts.prompt_approval import (
    load_approved_prompt,
    render_execution_prompt,
)
from scripts.task_case import (
    DEFAULT_EXPECTED_ARTIFACTS,
    DELIVERY_FILE,
    TaskCase,
    load_task_case,
)
from skill_evolution.analysis import load_approved_skill_contract
from skill_evolution.hierarchy import (
    ExecutionRecord,
    SkillHierarchyRepository,
    execution_manifest_from_payload,
)


JOURNAL_SCHEMA = "trajectory.actions.v1"
SENSITIVE_ARGUMENT_MARKERS = (
    "api-key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)
PI_BUILTIN_TOOL_REGISTRY = {
    "filesystem.read": "read",
    "filesystem.write": "write",
    "process.execute": "bash",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _skill_inventory(skill_path: Path) -> list[JsonObject]:
    inventory: list[JsonObject] = []
    for path in sorted(skill_path.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        inventory.append(
            {
                "path": str(path.relative_to(skill_path)),
                "bytes": path.stat().st_size,
            }
        )
    return inventory


def _sanitize_command(arguments: Sequence[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            sanitized.append("[REDACTED]")
            redact_next = False
            continue
        lowered = argument.lower()
        marker_present = any(
            marker in lowered for marker in SENSITIVE_ARGUMENT_MARKERS
        )
        if marker_present and "=" in argument:
            sanitized.append(argument.split("=", 1)[0] + "=[REDACTED]")
        else:
            sanitized.append(argument)
            redact_next = marker_present
    return sanitized


def _file_record(path: Path, run_directory: Path) -> JsonObject:
    relative_path = str(path.relative_to(run_directory))
    if not path.is_file():
        return {"path": relative_path, "exists": False}
    return {
        "path": relative_path,
        "exists": True,
        "bytes": path.stat().st_size,
    }


def _reported_command_path(command: Mapping[str, Any]) -> Path | None:
    value = command.get("path")
    source_info = command.get("sourceInfo")
    if value is None and isinstance(source_info, Mapping):
        value = source_info.get("path")
    if not isinstance(value, str) or not value:
        return None
    return Path(value).resolve()


def _resolve_task_case(
    task_case: TaskCase | None,
    source_path: str | os.PathLike[str] | None,
) -> TaskCase:
    if task_case is not None and source_path is not None:
        raise ValueError("Provide task_case or source_path, not both")
    if task_case is not None:
        return task_case
    if source_path is None:
        raise ValueError("task_case or source_path is required")
    return TaskCase.for_file(source_path)


def _prepare_task_input(
    task_case: TaskCase,
    *,
    artifacts_directory: Path,
    run_directory: Path,
) -> tuple[str | None, JsonObject]:
    if task_case.delivery != DELIVERY_FILE:
        assert task_case.inline_text is not None
        return None, {
            "delivery": task_case.delivery,
            "path": None,
            "exists": True,
            "bytes": len(task_case.inline_text.encode("utf-8")),
        }

    assert task_case.source_path is not None
    assert task_case.source_name is not None
    input_directory = artifacts_directory / "input"
    input_directory.mkdir()
    destination = input_directory / task_case.source_name
    shutil.copy2(task_case.source_path, destination)
    return (
        str(destination.relative_to(run_directory)),
        {
            "delivery": task_case.delivery,
            "source_path": str(task_case.source_path),
            **_file_record(destination, run_directory),
        },
    )


@dataclass(frozen=True)
class TrajectoryResult:
    """Result returned after a trajectory journal has been sealed."""

    run_directory: Path
    outcome: JsonObject
    execution_directory: Path | None = None
    execution_manifest: JsonObject | None = None


@dataclass(frozen=True)
class TrajectoryExecutionPolicy:
    """Pi tool boundary for a trajectory capture.

    The default host policy preserves the manual sampling behavior. Automatic
    candidate comparison must use ``docker_tool_router`` with an exact
    pre-created attempt directory and a trusted extension.
    """

    mode: str = "host_builtin"
    extension_path: Path | None = None
    environment: Mapping[str, str] | None = None
    exact_run_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"host_builtin", "docker_tool_router"}:
            raise ValueError("Unsupported trajectory execution policy")
        if self.mode == "host_builtin":
            if any(
                item is not None
                for item in (
                    self.extension_path,
                    self.environment,
                    self.exact_run_directory,
                )
            ):
                raise ValueError(
                    "Host policy cannot carry sandbox configuration"
                )
            return
        if self.extension_path is None or not self.extension_path.is_file():
            raise ValueError(
                "Docker tool policy requires a trusted extension file"
            )
        if self.exact_run_directory is None:
            raise ValueError(
                "Docker tool policy requires an exact attempt directory"
            )
        environment = self.environment
        if not isinstance(environment, Mapping):
            raise ValueError(
                "Docker tool policy requires a tool environment"
            )
        for key in (
            "SKILL_EVOLUTION_DOCKER_CONTAINER",
            "SKILL_EVOLUTION_DOCKER_COMMAND",
        ):
            if not isinstance(environment.get(key), str) or not environment[key]:
                raise ValueError(
                    f"Docker tool policy is missing {key}"
                )


class TrajectoryJournal:
    """Serialize complete actions and framework state through one writer."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._file = path.open("w", encoding="utf-8")
        self._lock = threading.RLock()
        self._sequence = 0
        self._started_monotonic = time.monotonic()
        self._active_message: JsonObject | None = None
        self._active_tools: dict[str, JsonObject] = {}
        self.event_types: Counter[str] = Counter()

    @property
    def record_count(self) -> int:
        return self._sequence

    def _append_locked(
        self,
        *,
        source: str,
        record_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        self._sequence += 1
        record: JsonObject = {
            "schema": JOURNAL_SCHEMA,
            "run_id": self.run_id,
            "seq": self._sequence,
            "observed_at": _utc_now(),
            "elapsed_ms": round(
                (time.monotonic() - self._started_monotonic) * 1000
            ),
            "source": source,
            "type": record_type,
            "payload": dict(payload or {}),
        }
        self._file.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        self._file.flush()
        return record

    def append(
        self,
        *,
        source: str,
        record_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """Append one framework or runtime record and return its envelope."""

        with self._lock:
            return self._append_locked(
                source=source,
                record_type=record_type,
                payload=payload,
            )

    def _save_interrupted_message_locked(self, reason: str) -> None:
        active = self._active_message
        if active is None:
            return
        self._append_locked(
            source="framework",
            record_type="action_interrupted",
            payload={
                "action_type": "message",
                "reason": reason,
                "role": active.get("role"),
                "content_persisted": False,
            },
        )
        self._active_message = None

    def _record_message_start_locked(self, parsed: Mapping[str, Any]) -> None:
        if self._active_message is not None:
            self._save_interrupted_message_locked(
                "superseded_by_message_start"
            )
        message = parsed.get("message")
        self._active_message = {
            "role": (
                message.get("role")
                if isinstance(message, Mapping)
                else None
            ),
        }

    def _record_message_update_locked(self, parsed: Mapping[str, Any]) -> None:
        if self._active_message is None:
            message = parsed.get("message")
            self._active_message = {
                "role": (
                    message.get("role")
                    if isinstance(message, Mapping)
                    else None
                ),
            }

    def _record_message_end_locked(self, parsed: Mapping[str, Any]) -> None:
        message = parsed.get("message")
        if not isinstance(message, Mapping):
            self._append_locked(
                source="framework",
                record_type="action_interrupted",
                payload={
                    "action_type": "message",
                    "reason": "message_end_without_complete_message",
                    "role": (
                        self._active_message.get("role")
                        if self._active_message is not None
                        else None
                    ),
                    "content_persisted": False,
                },
            )
            self._active_message = None
            return
        self._append_locked(
            source="pi_rpc",
            record_type="message_action",
            payload={
                "status": "completed",
                "message": dict(message),
            },
        )
        self._active_message = None

    def _record_tool_start_locked(self, parsed: Mapping[str, Any]) -> None:
        tool_call_id = str(parsed.get("toolCallId", ""))
        if tool_call_id:
            self._active_tools[tool_call_id] = {
                "toolCallId": tool_call_id,
                "toolName": parsed.get("toolName"),
                "arguments": parsed.get("args"),
                "started_at": _utc_now(),
                "started_monotonic": time.monotonic(),
            }

    def _record_tool_update_locked(
        self,
        _parsed: Mapping[str, Any],
    ) -> None:
        return

    def _record_tool_end_locked(self, parsed: Mapping[str, Any]) -> None:
        tool_call_id = str(parsed.get("toolCallId", ""))
        active = self._active_tools.pop(tool_call_id, None)
        started_monotonic = (
            active.get("started_monotonic")
            if active is not None
            else None
        )
        duration_ms = (
            round((time.monotonic() - started_monotonic) * 1000)
            if isinstance(started_monotonic, float)
            else None
        )
        self._append_locked(
            source="pi_rpc",
            record_type="tool_action",
            payload={
                "tool_call_id": tool_call_id or None,
                "tool_name": (
                    parsed.get("toolName")
                    or (active or {}).get("toolName")
                ),
                "arguments": (active or {}).get("arguments"),
                "status": (
                    "failed" if parsed.get("isError") else "succeeded"
                ),
                "result": parsed.get("result"),
                "started_at": (active or {}).get("started_at"),
                "ended_at": _utc_now(),
                "duration_ms": duration_ms,
            },
        )

    def _compact_event_locked(self, parsed: Mapping[str, Any]) -> JsonObject:
        event_type = parsed.get("type")
        compact: JsonObject = {"event_type": event_type}
        if event_type == "turn_end":
            tool_results = parsed.get("toolResults")
            if isinstance(tool_results, list):
                compact["tool_result_count"] = len(tool_results)
        elif event_type == "agent_end":
            messages = parsed.get("messages")
            if isinstance(messages, list):
                compact["message_count"] = len(messages)
        return compact

    def record_rpc(
        self,
        direction: str,
        raw: str,
        parsed: JsonObject | None,
        parse_error: str | None,
    ) -> None:
        """Normalize one RPC record into the ordered trajectory journal."""

        with self._lock:
            if parsed is None:
                self._append_locked(
                    source="pi_rpc",
                    record_type="rpc_protocol_error",
                    payload={
                        "direction": direction,
                        "raw": raw,
                        "error": parse_error,
                    },
                )
                return

            event_type = parsed.get("type")
            if direction == "client_to_pi":
                return

            if isinstance(event_type, str):
                self.event_types[event_type] += 1
            if event_type == "response":
                return
            elif event_type == "message_start":
                self._record_message_start_locked(parsed)
            elif event_type == "message_update":
                self._record_message_update_locked(parsed)
            elif event_type == "message_end":
                self._record_message_end_locked(parsed)
            elif event_type == "tool_execution_start":
                self._record_tool_start_locked(parsed)
            elif event_type == "tool_execution_update":
                self._record_tool_update_locked(parsed)
            elif event_type == "tool_execution_end":
                self._record_tool_end_locked(parsed)
            else:
                self._append_locked(
                    source="pi_rpc",
                    record_type=str(event_type or "pi_event"),
                    payload=self._compact_event_locked(parsed),
                )

    def record_stderr(self, line: str) -> None:
        """Append one Pi stderr line to the same ordered journal."""

        self.append(
            source="pi_process",
            record_type="process_stderr",
            payload={"line": line},
        )

    def capture_incomplete_state(self, reason: str) -> None:
        """Record interrupted actions without persisting partial content."""

        with self._lock:
            self._save_interrupted_message_locked(reason)
            for tool_call_id, active in list(self._active_tools.items()):
                self._append_locked(
                    source="framework",
                    record_type="tool_action",
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_name": active.get("toolName"),
                        "arguments": active.get("arguments"),
                        "status": "interrupted",
                        "result": None,
                        "error": {"reason": reason},
                        "started_at": active.get("started_at"),
                        "ended_at": _utc_now(),
                        "duration_ms": round(
                            (
                                time.monotonic()
                                - active["started_monotonic"]
                            )
                            * 1000
                        ),
                    },
                )
            self._active_tools.clear()

    def close(self) -> None:
        """Flush and close the journal."""

        with self._lock:
            self._file.close()


def _find_session_source(
    runtime_session_directory: Path,
    session_file: str | None,
) -> Path | None:
    candidates: list[Path] = []
    if session_file:
        candidates.append(Path(session_file))
    candidates.extend(sorted(runtime_session_directory.rglob("*.jsonl")))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _capture_session(
    *,
    run_directory: Path,
    runtime_session_directory: Path,
    session_file: str | None,
    agent_settled: bool,
) -> JsonObject:
    destination = run_directory / "pi-session.jsonl"
    source = _find_session_source(runtime_session_directory, session_file)
    if source is None:
        destination.touch(exist_ok=True)
        entries: list[JsonObject] = []
        invalid_lines = 0
    else:
        shutil.copy2(source, destination)
        entries = []
        invalid_lines = 0
        with destination.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    decoded = json.loads(line)
                    if not isinstance(decoded, dict):
                        raise ValueError("session line is not a JSON object")
                    entries.append(decoded)
                except (json.JSONDecodeError, ValueError):
                    invalid_lines += 1

    session_messages = [
        entry["message"]
        for entry in entries
        if entry.get("type") == "message"
        and isinstance(entry.get("message"), Mapping)
    ]
    if source is None:
        session_status = "missing"
    elif agent_settled:
        session_status = "complete"
    else:
        session_status = "partial"

    result: JsonObject = {
        "path": "pi-session.jsonl",
        "status": session_status,
        "line_count": len(entries) + invalid_lines,
        "message_count": len(session_messages),
        "invalid_line_count": invalid_lines,
    }
    if destination.is_file():
        result["bytes"] = destination.stat().st_size
    return result


def run_trajectory_spike(
    *,
    skill_path: str | os.PathLike[str],
    source_path: str | os.PathLike[str] | None = None,
    task_case: TaskCase | None = None,
    prompt: str,
    output_root: str | os.PathLike[str] = ".skill-evolution/trajectories",
    timeout: float = 900.0,
    pi_command: Sequence[str] | str | None = None,
    extra_pi_args: Sequence[str] = (),
    execution_policy: TrajectoryExecutionPolicy | None = None,
    hierarchy_root: str | os.PathLike[str] | None = None,
    execution_origin: str = "direct",
    execution_set_id: str | None = None,
    comparison_id: str | None = None,
) -> TrajectoryResult:
    """Run one skill through Pi RPC and seal an ordered trajectory journal."""

    resolved_skill = Path(skill_path).resolve()
    resolved_task_case = _resolve_task_case(task_case, source_path)
    if not (resolved_skill / "SKILL.md").is_file():
        raise FileNotFoundError(f"Skill entrypoint not found: {resolved_skill}")
    skill_contract = load_approved_skill_contract(
        resolved_skill / "skill_contract.json"
    )
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    unknown_tools = sorted(
        set(skill_contract["runtime"]["allowed_tools"])
        - set(PI_BUILTIN_TOOL_REGISTRY)
    )
    if unknown_tools:
        raise ValueError(
            "Skill Contract allows tools unsupported by the Pi adapter: "
            + ", ".join(unknown_tools)
        )

    policy = execution_policy or TrajectoryExecutionPolicy()
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    hierarchy_repository: SkillHierarchyRepository | None = None
    hierarchy_execution: ExecutionRecord | None = None
    if hierarchy_root is not None:
        if policy.exact_run_directory is not None:
            raise ValueError(
                "Hierarchy capture cannot reuse an exact legacy run directory"
            )
        hierarchy_repository = SkillHierarchyRepository(hierarchy_root)
        revision = hierarchy_repository.register_revision(resolved_skill)
        hierarchy_execution = hierarchy_repository.prepare_execution(
            skill_id=str(revision.manifest["skill_id"]),
            revision_id=str(revision.manifest["revision_id"]),
            origin=execution_origin,
            execution_set_id=execution_set_id,
            comparison_id=comparison_id,
        )
        run_id = str(hierarchy_execution.manifest["execution_id"])
        run_directory = hierarchy_execution.payload_directory
    elif policy.exact_run_directory is None:
        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        run_directory = Path(output_root).resolve() / run_id
    else:
        run_directory = policy.exact_run_directory.resolve()
        run_id = run_directory.name
        if run_directory == Path(run_directory.anchor):
            raise ValueError("A filesystem root cannot be a trajectory run")
        if not run_directory.is_dir():
            raise FileNotFoundError(
                f"Sandbox attempt directory not found: {run_directory}"
            )
        if (run_directory / "trajectory.jsonl").exists():
            raise FileExistsError(
                f"Sandbox attempt is already populated: {run_directory}"
            )
    artifacts_directory = run_directory / "artifacts"
    skill_snapshot = artifacts_directory / "skill"
    runtime_session_directory = run_directory / "runtime" / "pi-session"
    if artifacts_directory.exists() and any(artifacts_directory.iterdir()):
        raise FileExistsError(
            f"Artifact workspace is not empty: {artifacts_directory}"
        )
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    runtime_session_directory.mkdir(parents=True)
    run_relative_input, input_record = _prepare_task_input(
        resolved_task_case,
        artifacts_directory=artifacts_directory,
        run_directory=run_directory,
    )
    shutil.copytree(
        resolved_skill,
        skill_snapshot,
        ignore=shutil.ignore_patterns(".DS_Store"),
    )

    if policy.mode == "host_builtin":
        pi_tools = [
            PI_BUILTIN_TOOL_REGISTRY[tool]
            for tool in skill_contract["runtime"]["allowed_tools"]
        ]
        pi_args = [
            "--session-dir",
            str(runtime_session_directory),
            "--name",
            f"trajectory-{run_id}",
            "--no-extensions",
            "--no-prompt-templates",
            "--no-skills",
            "--skill",
            str(skill_snapshot),
            "--no-context-files",
            "--tools",
            ",".join(pi_tools),
            *extra_pi_args,
            "--thinking",
            "off",
        ]
    else:
        assert policy.extension_path is not None
        pi_args = [
            "--session-dir",
            str(runtime_session_directory),
            "--name",
            f"trajectory-{run_id}",
            "--no-builtin-tools",
            "--no-extensions",
            "--extension",
            str(policy.extension_path.resolve()),
            "--no-prompt-templates",
            "--no-skills",
            "--no-context-files",
            *extra_pi_args,
            "--thinking",
            "off",
        ]
    manifest: JsonObject = {
        "schema": JOURNAL_SCHEMA,
        "run_id": run_id,
        "started_at": started_at,
        "task_case": {
            "prompt": prompt,
            **resolved_task_case.manifest_payload(
                run_relative_input=run_relative_input
            ),
        },
        "skill": {
            "source_path": str(resolved_skill),
            "snapshot_path": "artifacts/skill",
            "contract": {
                "path": "artifacts/skill/skill_contract.json",
                "schema": skill_contract["schema"],
                "skill_id": skill_contract["skill_id"],
                "version": skill_contract["version"],
                "approved_by": skill_contract["approved_by"],
                "approved_at": skill_contract["approved_at"],
            },
            "inventory": _skill_inventory(skill_snapshot),
        },
        "source": input_record,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "working_directory": str(artifacts_directory),
            "pi_args": _sanitize_command(pi_args),
            "tool_boundary": {
                "mode": policy.mode,
                "built_in_tools": policy.mode == "host_builtin",
                "extension": (
                    policy.extension_path.name
                    if policy.extension_path is not None
                    else None
                ),
                "host_fallback_allowed": (
                    policy.mode == "host_builtin"
                ),
                "contract_runtime": skill_contract["runtime"],
            },
        },
    }

    journal = TrajectoryJournal(run_directory / "trajectory.jsonl", run_id)
    journal.append(
        source="framework",
        record_type="trajectory_started",
        payload={"manifest": manifest},
    )
    journal.append(
        source="framework",
        record_type="artifact_registered",
        payload={
            "artifact_role": "input",
            "artifact": input_record,
        },
    )

    status = "running"
    failure_stage: str | None = None
    error_record: JsonObject | None = None
    skill_loaded = False
    session_file: str | None = None
    client: PiRpcClient | None = None
    process_exit_code: int | None = None

    try:
        failure_stage = "start"
        client = PiRpcClient(
            cwd=artifacts_directory,
            pi_command=pi_command,
            pi_args=pi_args,
            no_session=False,
            approve_project=policy.mode == "host_builtin",
            env=policy.environment,
            rpc_record_observer=journal.record_rpc,
            stderr_observer=journal.record_stderr,
        )
        command = client.build_command()
        journal.append(
            source="framework",
            record_type="pi_process_starting",
            payload={"command": _sanitize_command(command)},
        )
        client.start()
        journal.append(
            source="framework",
            record_type="pi_process_started",
            payload={"pid": client.process.pid},
        )

        failure_stage = "inspect_runtime"
        state_before = client.request({"type": "get_state"}, timeout=30)
        state_data = state_before.get("data")
        if isinstance(state_data, Mapping):
            candidate = state_data.get("sessionFile")
            if isinstance(candidate, str):
                session_file = candidate
            journal.append(
                source="framework",
                record_type="runtime_observed",
                payload={
                    "model": state_data.get("model"),
                    "thinking_level": state_data.get("thinkingLevel"),
                    "session_id": state_data.get("sessionId"),
                    "session_name": state_data.get("sessionName"),
                },
            )

        if policy.mode == "host_builtin":
            commands_response = client.request(
                {"type": "get_commands"},
                timeout=30,
            )
            commands_data = commands_response.get("data")
            commands = (
                commands_data.get("commands", [])
                if isinstance(commands_data, Mapping)
                else []
            )
            skill_entry = skill_snapshot / "SKILL.md"
            skill_loaded = any(
                isinstance(command_item, Mapping)
                and command_item.get("source") == "skill"
                and _reported_command_path(command_item) == skill_entry
                for command_item in commands
            )
            delivery = "pi_skill"
        else:
            skill_loaded = True
            delivery = "approved_prompt_snapshot"
        journal.append(
            source="framework",
            record_type="skill_resolved",
            payload={
                "loaded": skill_loaded,
                "path": "artifacts/skill/SKILL.md",
                "delivery": delivery,
            },
        )
        if not skill_loaded:
            raise RuntimeError(
                "Pi did not report the requested skill in get_commands."
            )

        failure_stage = "prompt"
        prompt_response = client.request(
            {"type": "prompt", "message": prompt},
            timeout=30,
        )
        if not prompt_response.get("success"):
            raise RuntimeError(
                f"Pi rejected the prompt: {prompt_response.get('error')}"
            )

        failure_stage = "agent_execution"
        for _ in client.events_until(
            lambda event: event.get("type") == "agent_settled",
            timeout=timeout,
        ):
            pass

        failure_stage = "inspect_result"
        final_state = client.request({"type": "get_state"}, timeout=30)
        final_data = final_state.get("data")
        if isinstance(final_data, Mapping):
            candidate = final_data.get("sessionFile")
            if isinstance(candidate, str):
                session_file = candidate
        status = "succeeded"
        failure_stage = None
    except Exception as error:
        status = "failed"
        error_record = {
            "type": type(error).__name__,
            "message": str(error),
        }
    finally:
        if client is not None:
            client.close()
            try:
                process_exit_code = client.process.returncode
            except PiRpcError:
                process_exit_code = None
            observer_errors = list(client.observer_errors)
        else:
            observer_errors = []
        journal.append(
            source="framework",
            record_type="pi_process_exited",
            payload={"exit_code": process_exit_code},
        )
        journal.capture_incomplete_state(
            "agent_settled" if journal.event_types["agent_settled"] else "run_ended"
        )

    agent_settled = journal.event_types["agent_settled"] > 0
    session_record = _capture_session(
        run_directory=run_directory,
        runtime_session_directory=runtime_session_directory,
        session_file=session_file,
        agent_settled=agent_settled,
    )
    journal.append(
        source="framework",
        record_type="session_captured",
        payload=session_record,
    )

    output_records = [
        _file_record(
            artifacts_directory / relative_path,
            run_directory,
        )
        for relative_path in resolved_task_case.expected_artifacts
    ]
    for artifact_index, output_record in enumerate(output_records):
        journal.append(
            source="framework",
            record_type="artifact_registered",
            payload={
                "artifact_role": "output",
                "artifact_index": artifact_index,
                "artifact": output_record,
            },
        )

    missing_artifacts = [
        record["path"]
        for record in output_records
        if not record["exists"]
    ]
    if status == "succeeded" and missing_artifacts:
        status = "failed"
        failure_stage = "inspect_result"
        error_record = {
            "type": "RuntimeError",
            "message": (
                "Pi settled without creating expected artifacts: "
                + ", ".join(missing_artifacts)
            ),
        }
    if status == "succeeded" and observer_errors:
        status = "failed"
        failure_stage = "capture_events"
        error_record = {
            "type": "RuntimeError",
            "message": "One or more trajectory observers failed.",
        }

    ended_at = _utc_now()
    outcome: JsonObject = {
        "status": status,
        "failure_stage": failure_stage,
        "error": error_record,
        "skill_loaded": skill_loaded,
        "agent_settled": agent_settled,
        "process_exit_code": process_exit_code,
        "session": session_record,
        "artifact": output_records[0],
        "artifacts": output_records,
        "observer_errors": observer_errors,
        "pi_event_type_counts": dict(journal.event_types),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": round(
            (time.monotonic() - started_monotonic) * 1000
        ),
    }
    journal.append(
        source="framework",
        record_type="trajectory_finished",
        payload={"outcome": outcome},
    )
    journal.append(
        source="framework",
        record_type="trajectory_sealed",
        payload={
            "status": status,
            "record_count": journal.record_count + 1,
        },
    )
    journal.close()
    execution_directory: Path | None = None
    execution_manifest: JsonObject | None = None
    if hierarchy_repository is not None and hierarchy_execution is not None:
        execution_directory = hierarchy_execution.directory
        execution_manifest = execution_manifest_from_payload(
            execution_directory=execution_directory,
            skill_id=str(hierarchy_execution.manifest["skill_id"]),
            revision_id=str(hierarchy_execution.manifest["revision_id"]),
            execution_id=str(hierarchy_execution.manifest["execution_id"]),
            origin=execution_origin,
            execution_set_id=execution_set_id,
            comparison_id=comparison_id,
        )
        hierarchy_repository.finalize_execution(
            str(execution_manifest["skill_id"]),
            str(execution_manifest["execution_id"]),
            execution_manifest,
        )
    return TrajectoryResult(
        run_directory=run_directory,
        outcome=outcome,
        execution_directory=execution_directory,
        execution_manifest=execution_manifest,
    )


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Skill directory")
    task_input = parser.add_mutually_exclusive_group(required=True)
    task_input.add_argument(
        "--source",
        help="Legacy file input; converted to task.case.v1",
    )
    task_input.add_argument(
        "--task-case",
        help="Path to a task.case.v1 JSON file",
    )
    parser.add_argument(
        "--expected-artifact",
        action="append",
        default=[],
        help=(
            "Expected path relative to the run workspace; repeat for multiple "
            "artifacts (only with --source)"
        ),
    )
    parser.add_argument(
        "--prompt-file",
        required=True,
        help="Versioned prompt file with an approved sidecar",
    )
    parser.add_argument(
        "--output-root",
        help=(
            "Deprecated legacy trajectory root; providing it disables the "
            "Skill-first hierarchy for compatibility"
        ),
    )
    parser.add_argument(
        "--runtime-root",
        default=".skill-evolution",
        help="Skill-first runtime root",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Seconds to wait for agent_settled",
    )
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
    options = parser.parse_args(arguments)
    if options.task_case and options.expected_artifact:
        parser.error("--expected-artifact cannot be used with --task-case")
    if options.task_case:
        task_case = load_task_case(options.task_case)
    else:
        task_case = TaskCase.for_file(
            options.source,
            expected_artifacts=(
                options.expected_artifact
                or DEFAULT_EXPECTED_ARTIFACTS
            ),
        )
    approved_prompt = load_approved_prompt(options.prompt_file)
    rendered_prompt = render_execution_prompt(
        approved_prompt,
        options.skill,
        task_case.prompt_payload(),
    )

    result = run_trajectory_spike(
        skill_path=options.skill,
        task_case=task_case,
        output_root=options.output_root or ".skill-evolution/trajectories",
        prompt=rendered_prompt.text,
        timeout=options.timeout,
        pi_command=options.pi_command,
        extra_pi_args=options.pi_arg,
        hierarchy_root=(
            None if options.output_root is not None else options.runtime_root
        ),
    )
    print(result.execution_directory or result.run_directory)
    return 0 if result.outcome["status"] == "succeeded" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
