"""Execute and seal the deterministic multi-Trajectory Harness acceptance suite."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any

from scripts.trajectory_spike import TrajectoryJournal
from skill_evolution.agents import ModelConfiguration
from skill_evolution.research_agent_runtime import (
    RESEARCH_HARNESS_FAUX_MODEL,
    RESEARCH_HARNESS_FAUX_PROVIDER,
    ResearchPiAgentRuntime,
    load_approved_research_harness_context,
)
from skill_evolution.research_capability import (
    ResearchCapabilityError,
    attest_pi_execution_identity,
    build_research_execution_identity,
    research_execution_identity_digest,
    validate_research_execution_identity,
)
from skill_evolution.research_corpus import (
    ResearchCorpusError,
    ResearchCorpusVerification,
    verify_research_corpus,
)
from skill_evolution.research_results import (
    ResearchResultError,
    validate_research_result,
    validate_research_result_evidence,
)
from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxPreflightResult,
    research_evidence_tree_digest,
    validate_research_sandbox_context,
)
from skill_evolution.storage import JsonObject, StorageError, atomic_write_json


HARNESS_ACCEPTANCE_SCHEMA = "research.harness_acceptance.v2"
HARNESS_VALIDATOR_VERSION = "research-harness-validator-2026-08-14.v2"
HARNESS_DRIVER_PROTOCOL_VERSION = "deterministic-research-driver.v1"
HARNESS_CHECKS = (
    "corpus_preflight",
    "navigation_index",
    "evidence_roundtrip",
    "fake_agent_research_loop",
    "sandbox_isolation",
    "resource_limits",
    "structured_submission",
)
HARNESS_SUBCHECKS: dict[str, tuple[str, ...]] = {
    "corpus_preflight": (
        "pristine_corpus_accepted",
        "corrupt_declared_file_rejected",
        "missing_declared_file_rejected",
        "unsupported_schema_rejected",
    ),
    "navigation_index": (
        "all_trajectories_navigable",
        "action_entries_unique",
        "script_records_navigable",
    ),
    "evidence_roundtrip": (
        "all_index_locators_resolve",
        "all_trajectory_records_indexed",
    ),
    "fake_agent_research_loop": (
        "production_extensions_loaded",
        "full_text_search",
        "evidence_read",
        "structured_filter",
        "script_extraction",
        "trajectory_window",
        "work_file_write",
        "program_execution",
        "work_file_read",
        "terminating_submission",
    ),
    "sandbox_isolation": (
        "docker_network_none",
        "loopback_only",
        "evidence_readonly",
        "host_write_denied",
        "symlink_escape_denied",
        "credentials_absent",
    ),
    "resource_limits": (
        "active_limits_match",
        "tool_budget_exhausted",
        "timeout_terminated",
        "timeout_residual_absent",
        "output_bounded",
        "output_residual_absent",
    ),
    "structured_submission": (
        "valid_production_submission",
        "false_reference_rejected",
        "single_trajectory_repeat_rejected",
        "wrong_denominator_rejected",
        "missing_counterexample_search_rejected",
        "unknown_derivation_rejected",
        "duplicate_submission_rejected",
        "post_submission_action_rejected",
    ),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class HarnessAcceptanceError(RuntimeError):
    """Raised when an acceptance report cannot be produced or trusted."""


@dataclass(frozen=True)
class HarnessAcceptanceVerification:
    """A strict report reload together with its immutable file identity."""

    path: Path
    report: JsonObject
    file_sha256: str

    @property
    def passed(self) -> bool:
        """Return whether all fixed checks passed."""

        return self.report["status"] == "passed"

    @property
    def content_sha256(self) -> str:
        """Return the exact report-file digest used by workflow references."""

        return self.file_sha256


@dataclass(frozen=True)
class _ProbeResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_limit_exceeded: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True)
class _OutputBoundary:
    destination: Path
    trust_anchor: Path
    anchor_device: int
    anchor_inode: int
    parent_device: int
    parent_inode: int


@dataclass(frozen=True)
class _AuditBoundary:
    output: _OutputBoundary
    working_directory: Path
    final_directory: Path
    working_name: str
    device: int
    inode: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_flags() -> int:
    """Return flags that open one real directory without following its link."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0)


def _read_fd_bytes(file_fd: int) -> bytes:
    """Read one already-open file descriptor exactly once."""

    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
) -> tuple[bytes, os.stat_result]:
    """Read and pin one regular child through an already-trusted directory."""

    if Path(name).name != name or name in {"", ".", ".."}:
        raise HarnessAcceptanceError(f"{label} name is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessAcceptanceError(f"{label} is not a regular file")
        content = _read_fd_bytes(file_fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise HarnessAcceptanceError(f"{label} changed while being read")
        return content, metadata
    except OSError as error:
        raise HarnessAcceptanceError(
            f"{label} is missing or unsafe: {error}"
        ) from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _open_directory_chain(
    trust_anchor: Path,
    relative_parts: Sequence[str],
    *,
    create: bool,
    expected_anchor: tuple[int, int] | None = None,
    label: str,
) -> tuple[int, os.stat_result]:
    """Open every directory below a trust anchor with descriptor-relative I/O."""

    flags = _directory_flags()
    if not trust_anchor.is_absolute() or not trust_anchor.anchor:
        raise HarnessAcceptanceError(f"{label} trust root is not absolute")
    try:
        current_fd = os.open(Path(trust_anchor.anchor), flags)
        for part in trust_anchor.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except OSError as error:
        if "current_fd" in locals():
            os.close(current_fd)
        raise HarnessAcceptanceError(
            f"Could not open {label} trust root: {error}"
        ) from error
    anchor_metadata = os.fstat(current_fd)
    if expected_anchor is not None and (
        anchor_metadata.st_dev,
        anchor_metadata.st_ino,
    ) != expected_anchor:
        os.close(current_fd)
        raise HarnessAcceptanceError(f"{label} trust root identity changed")
    try:
        for part in relative_parts:
            if Path(part).name != part or part in {"", ".", ".."}:
                raise HarnessAcceptanceError(f"{label} path is unsafe")
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError as error:
                if not create:
                    raise HarnessAcceptanceError(
                        f"{label} is missing: {part}"
                    ) from error
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as error:
                    raise HarnessAcceptanceError(
                        f"{label} could not be created safely: {error}"
                    ) from error
            except OSError as error:
                raise HarnessAcceptanceError(
                    f"{label} contains a symlink or unsafe component: "
                    f"{part}: {error}"
                ) from error
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, anchor_metadata
    except BaseException:
        os.close(current_fd)
        raise


def _canonical_trust_root(
    value: str | os.PathLike[str],
) -> Path:
    """Require an absolute, canonical, non-symlink output trust root."""

    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise HarnessAcceptanceError(
            "Harness output trust root must be a canonical absolute path"
        )
    try:
        metadata = raw.lstat()
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness output trust root is unavailable: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise HarnessAcceptanceError(
            "Harness output trust root must be a real directory"
        )
    if raw != resolved:
        raise HarnessAcceptanceError(
            "Harness output trust root must not contain symlink components"
        )
    return resolved


def _reject_symlink_components(
    path: Path,
    *,
    label: str,
    trust_anchor: Path | None = None,
) -> None:
    """Reject links from the research-owned anchor through the target parent."""

    anchor = trust_anchor or path
    try:
        relative_parts = path.relative_to(anchor).parts
    except ValueError as error:
        raise HarnessAcceptanceError(f"{label} escapes its trust anchor") from error
    current = anchor
    for part in ("", *relative_parts):
        if part:
            current /= part
        if not os.path.lexists(current):
            continue
        if stat.S_ISLNK(current.lstat().st_mode):
            raise HarnessAcceptanceError(
                f"{label} contains a symlink component: {current}"
            )


def _prepare_output_destination(
    value: str | os.PathLike[str],
    *,
    trusted_directory: str | os.PathLike[str],
) -> _OutputBoundary:
    raw = Path(value)
    if ".." in raw.parts or raw.name in {"", ".", ".."}:
        raise HarnessAcceptanceError("Harness report path is unsafe")
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    trusted = Path(trusted_directory)
    resolved_trusted = _canonical_trust_root(trusted)
    if absolute.is_relative_to(trusted):
        relative = absolute.relative_to(trusted)
    elif absolute.is_relative_to(resolved_trusted):
        relative = absolute.relative_to(resolved_trusted)
    else:
        raise HarnessAcceptanceError(
            "Harness report path is outside its declared output trust root"
        )
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HarnessAcceptanceError("Harness report path is unsafe")
    parent_fd, anchor_metadata = _open_directory_chain(
        resolved_trusted,
        relative.parts[:-1],
        create=True,
        label="Harness report parent",
    )
    try:
        metadata = os.fstat(parent_fd)
        try:
            os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HarnessAcceptanceError(
                "Harness acceptance report already exists"
            )
    finally:
        os.close(parent_fd)
    absolute = resolved_trusted.joinpath(*relative.parts)
    if not stat.S_ISDIR(metadata.st_mode):
        raise HarnessAcceptanceError("Harness report parent is not a directory")
    return _OutputBoundary(
        absolute,
        resolved_trusted,
        anchor_metadata.st_dev,
        anchor_metadata.st_ino,
        metadata.st_dev,
        metadata.st_ino,
    )


def _existing_output_destination(
    value: str | os.PathLike[str],
    *,
    trusted_directory: str | os.PathLike[str],
) -> _OutputBoundary:
    """Resolve one existing file through a declared directory descriptor."""

    raw = Path(value)
    if ".." in raw.parts or raw.name in {"", ".", ".."}:
        raise HarnessAcceptanceError("Harness report path is unsafe")
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    trusted = Path(trusted_directory)
    resolved_trusted = _canonical_trust_root(trusted)
    if absolute.is_relative_to(trusted):
        relative = absolute.relative_to(trusted)
    elif absolute.is_relative_to(resolved_trusted):
        relative = absolute.relative_to(resolved_trusted)
    else:
        raise HarnessAcceptanceError(
            "Harness report path is outside its declared output trust root"
        )
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HarnessAcceptanceError("Harness report path is unsafe")
    parent_fd, anchor_metadata = _open_directory_chain(
        resolved_trusted,
        relative.parts[:-1],
        create=False,
        label="Harness report parent",
    )
    try:
        parent_metadata = os.fstat(parent_fd)
        metadata = os.stat(
            relative.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness acceptance report is missing or unsafe: {error}"
        ) from error
    finally:
        os.close(parent_fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise HarnessAcceptanceError("Harness report is not a regular file")
    return _OutputBoundary(
        resolved_trusted.joinpath(*relative.parts),
        resolved_trusted,
        anchor_metadata.st_dev,
        anchor_metadata.st_ino,
        parent_metadata.st_dev,
        parent_metadata.st_ino,
    )


def _reverify_output_boundary(
    boundary: _OutputBoundary,
    *,
    destination_must_be_absent: bool,
) -> None:
    parent_fd = _open_pinned_output_parent(boundary)
    try:
        try:
            metadata = os.stat(
                boundary.destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            metadata = None
        if destination_must_be_absent and metadata is not None:
            raise HarnessAcceptanceError(
                "Harness acceptance report appeared during acceptance"
            )
        if not destination_must_be_absent and (
            metadata is None or not stat.S_ISREG(metadata.st_mode)
        ):
            raise HarnessAcceptanceError(
                "Harness acceptance report is missing or unsafe after write"
            )
    finally:
        os.close(parent_fd)


def _assert_output_file_identity(
    boundary: _OutputBoundary,
    expected: os.stat_result,
) -> None:
    """Reject replacement of a report while its contents are being verified."""

    parent_fd = _open_pinned_output_parent(boundary)
    try:
        current = os.stat(
            boundary.destination.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness acceptance report changed during verification: {error}"
        ) from error
    finally:
        os.close(parent_fd)
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino)
        != (expected.st_dev, expected.st_ino)
    ):
        raise HarnessAcceptanceError(
            "Harness acceptance report changed during verification"
        )


def _write_output_json(
    boundary: _OutputBoundary,
    value: Mapping[str, Any],
) -> str:
    """Create the report relative to a verified parent directory descriptor."""

    parent_fd = _open_pinned_output_parent(boundary)
    temporary_name = (
        f".{boundary.destination.name}.{os.urandom(12).hex()}.tmp"
    )
    created = False
    try:
        try:
            os.stat(
                boundary.destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise HarnessAcceptanceError(
                "Harness acceptance report appeared before atomic write"
            )
        encoded = (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(
            temporary_name,
            file_flags,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        try:
            with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            boundary.destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        created = False
        os.fsync(parent_fd)
        return hashlib.sha256(encoded).hexdigest()
    finally:
        if created:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _fact(assertion: str, observed: str, *, digest: str | None = None) -> JsonObject:
    return {
        "kind": "observed_fact",
        "assertion": assertion,
        "observed": observed[:2000],
        "sha256": digest,
    }


def _subchecks(name: str, status: str) -> list[JsonObject]:
    result: list[JsonObject] = []
    for index, item in enumerate(HARNESS_SUBCHECKS[name]):
        observed = "failed" if status == "failed" and index == 0 else status
        if status == "failed" and index > 0:
            observed = "not_run"
        result.append(
            {
                "name": item,
                "status": observed,
                "evidence_sha256": None,
                "error": "Parent check failed" if observed == "failed" else None,
            }
        )
    return result


def _passed(name: str, evidence: Sequence[Mapping[str, Any]]) -> JsonObject:
    normalized_evidence = [dict(item) for item in evidence]
    if len(normalized_evidence) != len(HARNESS_SUBCHECKS[name]):
        raise HarnessAcceptanceError(
            f"Passed Harness check {name} must provide one fact per subcheck"
        )
    subchecks = _subchecks(name, "passed")
    for item, fact in zip(subchecks, normalized_evidence, strict=True):
        item["evidence_sha256"] = hashlib.sha256(
            _canonical_bytes({"evidence": [fact]})
        ).hexdigest()
    return {
        "name": name,
        "status": "passed",
        "subchecks": subchecks,
        "evidence": normalized_evidence,
        "error": None,
    }


def _failed(
    name: str,
    error: BaseException | str,
    evidence: Sequence[Mapping[str, Any]] = (),
) -> JsonObject:
    message = str(error).strip() or type(error).__name__
    normalized_evidence = [dict(item) for item in evidence]
    if not normalized_evidence:
        normalized_evidence = [
            _fact(
                f"{subcheck} did not pass",
                message if index == 0 else "not run after parent failure",
            )
            for index, subcheck in enumerate(HARNESS_SUBCHECKS[name])
        ]
    if len(normalized_evidence) != len(HARNESS_SUBCHECKS[name]):
        raise HarnessAcceptanceError(
            f"Failed Harness check {name} must provide one fact per subcheck"
        )
    return {
        "name": name,
        "status": "failed",
        "subchecks": _subchecks(name, "failed"),
        "evidence": normalized_evidence,
        "error": message[:2000],
    }


def _run_container_command(
    context: Mapping[str, Any],
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> _ProbeResult:
    """Run one bounded command through Docker exec, never through a host shell."""

    environment = validate_research_sandbox_context(context)
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise HarnessAcceptanceError("Probe bounds must be positive")
    docker = environment["SKILL_EVOLUTION_DOCKER_COMMAND"]
    container = environment["SKILL_EVOLUTION_RESEARCH_CONTAINER"]
    docker_command = [
        docker,
        "exec",
        "-i",
        "--workdir",
        "/work",
        container,
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        f"{timeout_seconds}s",
        *command,
    ]
    try:
        process = subprocess.Popen(
            docker_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Could not start isolation probe: {error}"
        ) from error
    assert process.stdout is not None and process.stderr is not None
    selected = selectors.DefaultSelector()
    selected.register(process.stdout, selectors.EVENT_READ, "stdout")
    selected.register(process.stderr, selectors.EVENT_READ, "stderr")
    output: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    digests = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
    stored = 0
    output_limit_exceeded = False
    host_timed_out = False
    deadline = time.monotonic() + timeout_seconds + 2.0
    try:
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                host_timed_out = True
                process.terminate()
                break
            for key, _mask in selected.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selected.unregister(key.fileobj)
                    continue
                stream = str(key.data)
                sizes[stream] += len(chunk)
                digests[stream].update(chunk)
                room = max(0, max_output_bytes - stored)
                if room:
                    kept = chunk[:room]
                    output[stream].append(kept)
                    stored += len(kept)
                if len(chunk) > room:
                    output_limit_exceeded = True
                    process.terminate()
                    break
            if output_limit_exceeded:
                break
        try:
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=2)
    finally:
        selected.close()
        process.stdout.close()
        process.stderr.close()
    return _ProbeResult(
        exit_code=return_code,
        stdout=b"".join(output["stdout"]).decode("utf-8", errors="replace"),
        stderr=b"".join(output["stderr"]).decode("utf-8", errors="replace"),
        timed_out=host_timed_out or return_code == 124,
        output_limit_exceeded=output_limit_exceeded,
        stdout_bytes=sizes["stdout"],
        stderr_bytes=sizes["stderr"],
        stdout_sha256=digests["stdout"].hexdigest(),
        stderr_sha256=digests["stderr"].hexdigest(),
    )


def _probe_python(
    context: Mapping[str, Any],
    source: str,
    *,
    timeout_seconds: float = 10,
    max_output_bytes: int = 65536,
) -> _ProbeResult:
    return _run_container_command(
        context,
        ["python3", "-c", source],
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def _run_bounded_host_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> _ProbeResult:
    """Run one host control command with live time and output enforcement."""

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    selected = selectors.DefaultSelector()
    selected.register(process.stdout, selectors.EVENT_READ, "stdout")
    selected.register(process.stderr, selectors.EVENT_READ, "stderr")
    output: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    digests = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
    stored = 0
    exceeded = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.terminate()
                break
            for key, _mask in selected.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selected.unregister(key.fileobj)
                    continue
                stream = str(key.data)
                sizes[stream] += len(chunk)
                digests[stream].update(chunk)
                room = max(0, max_output_bytes - stored)
                if room:
                    kept = chunk[:room]
                    output[stream].append(kept)
                    stored += len(kept)
                if len(chunk) > room:
                    exceeded = True
                    process.terminate()
                    break
            if exceeded:
                break
        try:
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=2)
    finally:
        selected.close()
        process.stdout.close()
        process.stderr.close()
    return _ProbeResult(
        exit_code=return_code,
        stdout=b"".join(output["stdout"]).decode("utf-8", errors="replace"),
        stderr=b"".join(output["stderr"]).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_limit_exceeded=exceeded,
        stdout_bytes=sizes["stdout"],
        stderr_bytes=sizes["stderr"],
        stdout_sha256=digests["stdout"].hexdigest(),
        stderr_sha256=digests["stderr"].hexdigest(),
    )


def _verify_disabled_container_logs(context: Mapping[str, Any]) -> JsonObject:
    """Prove PID 1 output cannot become an unbounded Docker log side channel."""

    environment = validate_research_sandbox_context(context)
    marker = f"research-log-sink-probe-{os.urandom(12).hex()}"
    source = (
        "import json\n"
        f"marker = {marker!r}.encode()\n"
        "try:\n"
        "    with open('/proc/1/fd/1', 'wb', buffering=0) as stream:\n"
        "        stream.write(marker + b'\\n')\n"
        "except OSError:\n"
        "    outcome = 'pid1_fd_inaccessible'\n"
        "else:\n"
        "    outcome = 'write_discarded_by_disabled_log_driver'\n"
        "print(json.dumps({'outcome': outcome}))\n"
    )
    write_probe = _probe_python(
        context,
        source,
        timeout_seconds=5,
        max_output_bytes=4096,
    )
    if write_probe.exit_code != 0 or write_probe.timed_out:
        raise HarnessAcceptanceError("PID 1 log-sink probe did not complete")
    try:
        outcome = json.loads(write_probe.stdout)
    except json.JSONDecodeError as error:
        raise HarnessAcceptanceError("PID 1 log-sink probe was invalid") from error
    if not isinstance(outcome, Mapping) or outcome.get("outcome") not in {
        "pid1_fd_inaccessible",
        "write_discarded_by_disabled_log_driver",
    }:
        raise HarnessAcceptanceError("PID 1 log-sink probe was inconclusive")
    command = [
        environment["SKILL_EVOLUTION_DOCKER_COMMAND"],
        "logs",
        "--tail",
        "1",
        environment["SKILL_EVOLUTION_RESEARCH_CONTAINER"],
    ]
    try:
        completed = _run_bounded_host_command(
            command,
            timeout_seconds=5,
            max_output_bytes=65536,
        )
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Could not verify the disabled Docker log sink: {error}"
        ) from error
    combined = completed.stdout + completed.stderr
    if completed.timed_out or completed.output_limit_exceeded:
        raise HarnessAcceptanceError("Docker log-sink probe exceeded its bounds")
    if completed.exit_code == 0 or marker in combined:
        raise HarnessAcceptanceError("Docker container logs remain readable")
    return {
        "pid1_write_outcome": outcome["outcome"],
        "docker_logs_exit_code": completed.exit_code,
        "marker_absent": True,
    }


def _trajectory_sequences(corpus: ResearchCorpusVerification) -> dict[str, set[int]]:
    sequences: dict[str, set[int]] = {}
    raw_runs = corpus.manifest.get("runs")
    if not isinstance(raw_runs, list):
        raise HarnessAcceptanceError("Corpus run inventory is missing")
    for run in raw_runs:
        if not isinstance(run, Mapping):
            raise HarnessAcceptanceError("Corpus run inventory is invalid")
        run_id = run.get("execution_id")
        trajectory = run.get("trajectory", run.get("trace"))
        if not isinstance(run_id, str) or not isinstance(trajectory, Mapping):
            raise HarnessAcceptanceError("Corpus run identity is invalid")
        relative = trajectory.get("path")
        expected_records = trajectory.get("records")
        if not isinstance(relative, str) or not isinstance(expected_records, int):
            raise HarnessAcceptanceError("Corpus trajectory inventory is invalid")
        path = corpus.directory / relative
        found: set[int] = set()
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HarnessAcceptanceError(
                        f"Trajectory {run_id} line {line_number} is invalid"
                    ) from error
                sequence = record.get("seq") if isinstance(record, Mapping) else None
                if not isinstance(sequence, int) or sequence < 1 or sequence in found:
                    raise HarnessAcceptanceError(
                        f"Trajectory {run_id} sequences are invalid"
                    )
                found.add(sequence)
        if len(found) != expected_records:
            raise HarnessAcceptanceError(f"Trajectory {run_id} record count differs")
        sequences[run_id] = found
    if set(sequences) != set(corpus.execution_ids):
        raise HarnessAcceptanceError("Trajectory inventory differs from corpus identity")
    return sequences


def _validate_navigation(
    corpus: ResearchCorpusVerification,
) -> tuple[list[tuple[str, int]], list[JsonObject]]:
    navigation = corpus.navigation_index
    if set(navigation) != {"schema", "entries", "scripts"}:
        raise HarnessAcceptanceError("Navigation index fields differ from contract")
    entries = navigation.get("entries")
    scripts = navigation.get("scripts")
    if not isinstance(entries, list) or not entries:
        raise HarnessAcceptanceError("Navigation index has no action entries")
    if not isinstance(scripts, list):
        raise HarnessAcceptanceError("Navigation script index is invalid")
    locators: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    covered: set[str] = set()
    for label, values in (("entries", entries), ("scripts", scripts)):
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise HarnessAcceptanceError(f"{label}[{index}] is not an object")
            run_id = item.get("run_id")
            sequence = item.get("seq")
            evidence = item.get("evidence")
            if (
                not isinstance(run_id, str)
                or not isinstance(sequence, int)
                or sequence < 1
                or not isinstance(evidence, Mapping)
                or set(evidence) != {"run_id", "seq"}
                or evidence.get("run_id") != run_id
                or evidence.get("seq") != sequence
            ):
                raise HarnessAcceptanceError(f"{label}[{index}] locator is invalid")
            locator = (run_id, sequence)
            locators.append(locator)
            covered.add(run_id)
            if label == "entries":
                if locator in seen:
                    raise HarnessAcceptanceError(
                        "Navigation action locator is duplicated"
                    )
                seen.add(locator)
    if covered != set(corpus.execution_ids):
        raise HarnessAcceptanceError("Navigation index does not cover every Trajectory")
    evidence = [
        _fact("all Trajectory identities are navigable", str(len(covered))),
        _fact("action entries are unique", str(len(entries))),
        _fact("script events remain navigable", str(len(scripts))),
    ]
    return locators, evidence


def _isolation_source() -> str:
    return r'''# acceptance-probe:sandbox-isolation
import json, os
facts = {}
facts['evidence_readable'] = open('/evidence/corpus.json', 'rb').read(1) == b'{'
def denied(name, action):
    try:
        action()
    except OSError:
        facts[name] = True
    else:
        facts[name] = False
denied('evidence_write_denied', lambda: open('/evidence/corpus.json', 'ab').write(b'x'))
denied('root_write_denied', lambda: open('/harness-host-write-probe', 'wb').write(b'x'))
try:
    os.symlink('/evidence', '/work/evidence-link')
except FileExistsError:
    pass
denied(
    'symlink_escape_denied',
    lambda: open('/work/evidence-link/corpus.json', 'ab').write(b'x'))
os.unlink('/work/evidence-link')
# Docker Desktop kernels expose many down tunnel devices (tunl0, gre0,
# sit0, ...) in /sys/class/net even with --network none. Network isolation
# is measured by interfaces that actually carry an address: only loopback
# may be up and configured.
def active_interfaces():
    with open('/proc/net/route', 'rb') as route_stream:
        route = route_stream.read().decode('ascii', 'replace')
    active = set()
    for line in route.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[1] != '00000000':
            active.add(fields[0])
    if not active:
        active.add('lo')
    return active
facts['loopback_only'] = active_interfaces() == {'lo'}
sensitive = ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY', 'AUTHORIZATION')
facts['credential_environment_absent'] = not any(
    any(marker in key.upper() for marker in sensitive) for key in os.environ)
facts['secret_mount_absent'] = (
    not os.path.isdir('/run/secrets') or not os.listdir('/run/secrets'))
facts['non_root'] = os.geteuid() != 0
print(json.dumps(facts, sort_keys=True))
'''


def _memory_bytes(value: str) -> int:
    """Normalize the Docker memory syntax used by the fixed sandbox."""

    matched = re.fullmatch(r"([1-9][0-9]*)([bkmg])?", value.casefold())
    if matched is None:
        raise HarnessAcceptanceError(
            f"Unsupported Docker memory limit syntax: {value!r}"
        )
    scales = {None: 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    return int(matched.group(1)) * scales[matched.group(2)]


def _inspect_active_container(
    context: Mapping[str, Any],
    *,
    expected_image_id: str,
    expected_limits: Mapping[str, Any],
) -> JsonObject:
    """Read Docker's active configuration and enforce every lab boundary."""

    environment = validate_research_sandbox_context(context)
    command = [
        environment["SKILL_EVOLUTION_DOCKER_COMMAND"],
        "inspect",
        environment["SKILL_EVOLUTION_RESEARCH_CONTAINER"],
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HarnessAcceptanceError(
            f"Could not inspect the active research container: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise HarnessAcceptanceError(
            f"Docker could not inspect the active research container: {detail}"
        )
    if len(completed.stdout.encode("utf-8")) > 4 * 1024 * 1024:
        raise HarnessAcceptanceError("Docker inspection output is excessive")
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise HarnessAcceptanceError("Docker inspection output is invalid") from error
    if not isinstance(decoded, list) or len(decoded) != 1:
        raise HarnessAcceptanceError("Docker inspection did not identify one container")
    inspected = decoded[0]
    if not isinstance(inspected, Mapping):
        raise HarnessAcceptanceError("Docker inspection record is invalid")
    host = inspected.get("HostConfig")
    config = inspected.get("Config")
    network = inspected.get("NetworkSettings")
    if not all(isinstance(item, Mapping) for item in (host, config, network)):
        raise HarnessAcceptanceError("Docker inspection lacks active configuration")
    assert isinstance(host, Mapping)
    assert isinstance(config, Mapping)
    assert isinstance(network, Mapping)
    raw_networks = network.get("Networks")
    # Docker lists the implicit "none" pseudo-network for --network none
    # containers; only real network attachments count against isolation.
    network_count = (
        len(
            {
                name
                for name in raw_networks
                if name != "none"
            }
        )
        if isinstance(raw_networks, Mapping)
        else -1
    )
    ulimits = host.get("Ulimits")
    nofile = next(
        (
            item
            for item in ulimits
            if isinstance(item, Mapping) and item.get("Name") == "nofile"
        ),
        None,
    ) if isinstance(ulimits, list) else None
    tmpfs = host.get("Tmpfs")
    log_config = host.get("LogConfig")
    cap_drop = host.get("CapDrop")
    security_options = host.get("SecurityOpt")
    active: JsonObject = {
        "image_id": inspected.get("Image"),
        "network_mode": host.get("NetworkMode"),
        "network_count": network_count,
        "log_driver": (
            log_config.get("Type") if isinstance(log_config, Mapping) else None
        ),
        "readonly_rootfs": host.get("ReadonlyRootfs"),
        "user": config.get("User"),
        "pids_limit": host.get("PidsLimit"),
        "nano_cpus": host.get("NanoCpus"),
        "memory_bytes": host.get("Memory"),
        "nofile_soft": nofile.get("Soft") if isinstance(nofile, Mapping) else None,
        "nofile_hard": nofile.get("Hard") if isinstance(nofile, Mapping) else None,
        "cap_drop": sorted(str(item) for item in cap_drop)
        if isinstance(cap_drop, list)
        else [],
        "security_options": sorted(str(item) for item in security_options)
        if isinstance(security_options, list)
        else [],
        "tmpfs": dict(tmpfs) if isinstance(tmpfs, Mapping) else {},
        "tool_budgets": {
            "command_timeout_milliseconds": int(
                environment["SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS"]
            ),
            "max_output_bytes": int(
                environment["SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES"]
            ),
            "max_tool_calls": int(
                environment["SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS"]
            ),
            "max_concurrent_tool_calls": int(
                environment[
                    "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS"
                ]
            ),
            "max_total_output_bytes": int(
                environment[
                    "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES"
                ]
            ),
            "max_total_command_milliseconds": int(
                environment[
                    "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS"
                ]
            ),
        },
    }
    expected = {
        "image_id": expected_image_id,
        "network_mode": "none",
        "network_count": 0,
        "log_driver": "none",
        "readonly_rootfs": True,
        "user": "65534:65534",
        "pids_limit": expected_limits["pids"],
        "nano_cpus": round(float(expected_limits["cpus"]) * 1_000_000_000),
        "memory_bytes": _memory_bytes(str(expected_limits["memory"])),
        "nofile_soft": expected_limits["open_files"],
        "nofile_hard": expected_limits["open_files"],
    }
    for field, expected_value in expected.items():
        if active.get(field) != expected_value:
            raise HarnessAcceptanceError(
                f"Active Docker {field} differs from the sandbox contract"
            )
    if "ALL" not in active["cap_drop"]:
        raise HarnessAcceptanceError("Active Docker capabilities were not dropped")
    if not any(
        re.fullmatch(r"no-new-privileges(?::true)?", item)
        for item in active["security_options"]
    ):
        raise HarnessAcceptanceError(
            "Active Docker no-new-privileges boundary is missing"
        )
    expected_tmpfs = {
        "/work": int(expected_limits["work_bytes"]),
        "/tmp": int(expected_limits["temporary_bytes"]),
    }
    for mount, size in expected_tmpfs.items():
        options = active["tmpfs"].get(mount)
        if not isinstance(options, str) or f"size={size}" not in options.split(","):
            raise HarnessAcceptanceError(
                f"Active Docker {mount} quota differs from the sandbox contract"
            )
    expected_budgets = {
        "command_timeout_milliseconds": int(
            expected_limits["command_timeout_seconds"]
        )
        * 1000,
        "max_output_bytes": expected_limits["max_output_bytes"],
        "max_tool_calls": expected_limits["max_tool_calls"],
        "max_concurrent_tool_calls": expected_limits[
            "max_concurrent_tool_calls"
        ],
        "max_total_output_bytes": expected_limits[
            "max_total_output_bytes"
        ],
        "max_total_command_milliseconds": expected_limits[
            "max_total_command_milliseconds"
        ],
    }
    if active["tool_budgets"] != expected_budgets:
        raise HarnessAcceptanceError(
            "Active research-tool budgets differ from the sandbox contract"
        )
    return active


def _verify_corpus_rejections(corpus: ResearchCorpusVerification) -> list[JsonObject]:
    """Prove corrupt, missing, and incompatible corpora fail before research."""

    cases = ("corrupt_file", "missing_file", "unsupported_schema")
    rejected: list[str] = []
    before = research_evidence_tree_digest(corpus.directory)
    with tempfile.TemporaryDirectory(prefix="harness-corpus-negative-") as raw:
        root = Path(raw)
        for case in cases:
            candidate = root / case
            shutil.copytree(corpus.directory, candidate)
            if case == "corrupt_file":
                with (candidate / "baseline.json").open("ab") as stream:
                    stream.write(b" ")
            elif case == "missing_file":
                (candidate / "navigation-index.json").unlink()
            else:
                manifest = json.loads(
                    (candidate / "corpus.json").read_text(encoding="utf-8")
                )
                manifest["schema"] = "research.corpus.unsupported"
                atomic_write_json(candidate / "corpus.json", manifest)
            try:
                verify_research_corpus(candidate)
            except (ResearchCorpusError, OSError, ValueError):
                rejected.append(case)
            else:
                raise HarnessAcceptanceError(
                    f"Corpus preflight accepted the {case} negative case"
                )
    after = research_evidence_tree_digest(corpus.directory)
    if before != after:
        raise HarnessAcceptanceError("Corpus negative tests changed frozen evidence")
    return [
        _fact(
            f"{case} corpus fails before Agent start",
            "rejected",
            digest=hashlib.sha256(
                _canonical_bytes({"case": case, "outcome": "rejected"})
            ).hexdigest(),
        )
        for case in rejected
    ]


def _load_journal(path: Path) -> list[JsonObject]:
    records: list[JsonObject] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise HarnessAcceptanceError(
                        f"Harness journal line {line_number} is not an object"
                    )
                records.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessAcceptanceError(
            f"Harness journal is unreadable: {error}"
        ) from error
    if not records:
        raise HarnessAcceptanceError("Harness journal is empty")
    return records


def _tool_actions(records: Sequence[Mapping[str, Any]]) -> dict[str, JsonObject]:
    actions: dict[str, JsonObject] = {}
    for record in records:
        if record.get("type") != "tool_action":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        call_id = payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if call_id in actions:
            raise HarnessAcceptanceError(f"Duplicate tool action in audit: {call_id}")
        actions[call_id] = dict(payload)
    return actions


def _action_json(
    actions: Mapping[str, Mapping[str, Any]],
    call_id: str,
) -> tuple[JsonObject, Mapping[str, Any]]:
    """Decode one successful production tool's single JSON text result."""

    action = actions[call_id]
    result = action.get("result")
    if not isinstance(result, Mapping):
        raise HarnessAcceptanceError(f"{call_id} returned no structured result")
    content = result.get("content")
    details = result.get("details")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], Mapping)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
        or not isinstance(details, Mapping)
        or not _cleanup_details_are_complete(details)
    ):
        raise HarnessAcceptanceError(f"{call_id} result envelope is invalid")
    try:
        decoded = json.loads(content[0]["text"])
    except json.JSONDecodeError as error:
        raise HarnessAcceptanceError(
            f"{call_id} result is not semantic JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise HarnessAcceptanceError(f"{call_id} JSON result is not an object")
    return decoded, details


def _cleanup_details_are_complete(details: Mapping[str, Any]) -> bool:
    """Return whether a tool proved its PID namespace was fully reaped."""

    rounds = details.get("cleanupRounds")
    return (
        details.get("cleanupVerified") is True
        and details.get("cleanupSnapshot") == ["pid1", "cleanup"]
        and details.get("cleanupObservedProcessCount") == 2
        and details.get("cleanupResidualProcessCount") == 0
        and isinstance(rounds, int)
        and not isinstance(rounds, bool)
        and rounds >= 0
    )


def _raw_trajectory_records(
    corpus: ResearchCorpusVerification,
    run_id: str,
) -> tuple[list[str], list[JsonObject]]:
    candidates = [
        corpus.directory / "runs" / run_id / "trajectory.jsonl",
        corpus.directory / "runs" / run_id / "trace.jsonl",
    ]
    path = next(
        (candidate for candidate in candidates if candidate.is_file()),
        candidates[0],
    )
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    records: list[JsonObject] = []
    for line in raw_lines:
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise HarnessAcceptanceError("Trajectory record is not an object")
        records.append(decoded)
    return raw_lines, records


def _bounded_trajectory_record(record: Mapping[str, Any]) -> JsonObject:
    encoded = json.dumps(
        dict(record), ensure_ascii=False, separators=(",", ":")
    )
    if len(encoded) <= 50_000:
        return {"record": dict(record), "truncated": False}
    return {
        "seq": record.get("seq"),
        "record_preview": encoded[:50_000],
        "record_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "truncated": True,
    }


def _fact_for_action(
    assertion: str,
    observed: str,
    action: Mapping[str, Any],
) -> JsonObject:
    return _fact(
        assertion,
        observed,
        digest=hashlib.sha256(_canonical_bytes(action)).hexdigest(),
    )


def _fact_for_actions(
    assertion: str,
    observed: str,
    actions: Sequence[Mapping[str, Any]],
) -> JsonObject:
    return _fact(
        assertion,
        observed,
        digest=hashlib.sha256(
            _canonical_bytes(
                {"actions": [dict(action) for action in actions]}
            )
        ).hexdigest(),
    )


def _assert_positive_drive(
    drive: Any,
    records: Sequence[Mapping[str, Any]],
    corpus: ResearchCorpusVerification,
    extension_names: Sequence[str],
) -> tuple[JsonObject, list[JsonObject]]:
    if drive.status != "succeeded" or not isinstance(drive.result, Mapping):
        raise HarnessAcceptanceError(
            f"Production research loop did not succeed: {drive.status}"
        )
    actions = _tool_actions(records)
    process_records = [
        record.get("payload")
        for record in records
        if record.get("type") == "pi_process_starting"
    ]
    if len(process_records) != 1 or not isinstance(process_records[0], Mapping):
        raise HarnessAcceptanceError("Production Pi process audit is missing")
    extensions = process_records[0].get("extensions")
    if not isinstance(extensions, list) or not set(extension_names).issubset(
        set(extensions)
    ):
        raise HarnessAcceptanceError("Production research extensions were not loaded")
    expected = {
        "harness-search": "research_search",
        "harness-read": "research_read",
        "harness-filter": "research_query",
        "harness-scripts": "research_query",
        "harness-window": "research_trajectory_window",
        "harness-write": "research_work_write",
        "harness-exec": "research_exec",
        "harness-work-read": "research_work_read",
        "harness-submit": "submit_multi_trajectory_research",
    }
    for call_id, tool_name in expected.items():
        action = actions.get(call_id)
        if (
            action is None
            or action.get("tool_name") != tool_name
            or action.get("status") != "succeeded"
        ):
            raise HarnessAcceptanceError(
                f"Production research tool did not succeed: {call_id}"
            )
    if "forbidden-post-submit" in actions:
        raise HarnessAcceptanceError("Research continued after formal submission")

    execution_ids = list(corpus.execution_ids)
    first_run = execution_ids[0]
    raw_lines, trajectory_records = _raw_trajectory_records(corpus, first_run)

    search, search_details = _action_json(actions, "harness-search")
    trajectory_path_name = (
        "trace.jsonl"
        if (corpus.directory / "runs" / first_run / "trace.jsonl").is_file()
        else "trajectory.jsonl"
    )
    expected_trajectory_path = f"runs/{first_run}/{trajectory_path_name}"
    matches = search.get("matches")
    expected_match_count = min(2, len(trajectory_records))
    if (
        search.get("query") != first_run
        or search.get("path") != expected_trajectory_path
        or search.get("total_matches") != len(trajectory_records)
        or not isinstance(matches, list)
        or len(matches) != expected_match_count
        or [item.get("line") for item in matches if isinstance(item, Mapping)]
        != list(range(1, expected_match_count + 1))
        or any(
            not isinstance(item, Mapping)
            or item.get("path") != expected_trajectory_path
            for item in matches
        )
        or search_details.get("operation") != "search"
    ):
        raise HarnessAcceptanceError("Full-text search returned wrong Trajectory matches")

    read, read_details = _action_json(actions, "harness-read")
    read_lines = read.get("lines")
    if (
        not isinstance(read_lines, list)
        or len(read_lines) != 1
        or not isinstance(read_lines[0], Mapping)
        or read_lines[0].get("line") != 1
        or read_lines[0].get("text_truncated") is not False
        or not isinstance(read_lines[0].get("text"), str)
    ):
        raise HarnessAcceptanceError("Evidence read returned no complete Trajectory line")
    try:
        read_record = json.loads(read_lines[0]["text"])
    except json.JSONDecodeError as error:
        raise HarnessAcceptanceError("Evidence read Trajectory line is invalid") from error
    if (
        read.get("path") != expected_trajectory_path
        or read.get("offset") != 1
        or read_record != trajectory_records[0]
        or read.get("total_lines") != len(raw_lines)
        or read_details.get("operation") != "read_evidence"
    ):
        raise HarnessAcceptanceError("Evidence read did not return exact Trajectory lines")

    navigation_entries = corpus.navigation_index["entries"]
    selected_entries = [
        (index, item)
        for index, item in enumerate(navigation_entries, start=1)
        if item["run_id"] == first_run
    ]
    expected_filter_records = [
        {
            "index_position": index,
            "record": {
                field: item[field]
                for field in ("run_id", "seq", "flags")
                if field in item
            },
        }
        for index, item in selected_entries[:2]
    ]
    filtered, filter_details = _action_json(actions, "harness-filter")
    if (
        filtered.get("path") != "navigation-index.json"
        or filtered.get("collection") != "entries"
        or filtered.get("records") != expected_filter_records
        or filtered.get("total_matches") != len(selected_entries)
        or filter_details.get("operation") != "query"
    ):
        raise HarnessAcceptanceError("Structured query returned wrong action rows")

    navigation_scripts = corpus.navigation_index["scripts"]
    expected_script_records = [
        {
            "index_position": index,
            "record": {
                field: item[field]
                for field in ("run_id", "seq", "event", "path", "content_sha256")
                if field in item
            },
        }
        for index, item in enumerate(navigation_scripts[:2], start=1)
    ]
    scripts, script_details = _action_json(actions, "harness-scripts")
    if (
        scripts.get("path") != "navigation-index.json"
        or scripts.get("collection") != "scripts"
        or scripts.get("records") != expected_script_records
        or scripts.get("total_matches") != len(navigation_scripts)
        or script_details.get("operation") != "query"
    ):
        raise HarnessAcceptanceError("Script extraction returned wrong index rows")

    expected_window_records = [
        _bounded_trajectory_record(item)
        for item in trajectory_records
        if item.get("seq") == 1
    ]
    window, window_details = _action_json(actions, "harness-window")
    if (
        window
        != {
            "run_id": first_run,
            "target_seq": 1,
            "before": 0,
            "after": 0,
            "records": expected_window_records,
        }
        or window_details.get("operation") != "trajectory_window"
    ):
        raise HarnessAcceptanceError("Trajectory window did not return source records")

    write, write_details = _action_json(actions, "harness-write")
    write_arguments = actions["harness-write"].get("arguments")
    if not isinstance(write_arguments, Mapping):
        raise HarnessAcceptanceError("Work-write arguments are missing from audit")
    program = write_arguments.get("content")
    if (
        write_arguments.get("path") != "cross-trajectory.py"
        or not isinstance(program, str)
        or "cross-trajectory.json" not in program
        or write
        != {
            "path": "cross-trajectory.py",
            "bytes": len(program.encode("utf-8")),
            "sha256": hashlib.sha256(program.encode("utf-8")).hexdigest(),
        }
        or write_details.get("operation") != "write_work"
    ):
        raise HarnessAcceptanceError("Work write did not persist the exact program")

    execution, details = _action_json(actions, "harness-exec")
    if any(
        (
            details.get("exitCode") != 0,
            details.get("timedOut") is not False,
            details.get("aborted") is not False,
            details.get("outputLimitExceeded") is not False,
            details.get("cleanupVerified") is not True,
        )
    ):
        raise HarnessAcceptanceError("Research program execution was not clean")
    try:
        stdout_value = json.loads(str(execution.get("stdout", "")).strip())
    except json.JSONDecodeError as error:
        raise HarnessAcceptanceError(
            "Research program stdout is not valid JSON"
        ) from error
    if (
        stdout_value != {"runs": sorted(execution_ids)}
        or execution.get("stderr") != ""
        or execution.get("exit_code") != 0
        or execution.get("timed_out") is not False
        or execution.get("aborted") is not False
        or execution.get("output_limit_exceeded") is not False
    ):
        raise HarnessAcceptanceError(
            "Cross-Trajectory program did not produce the exact eligible Trajectory set"
        )

    work_read, work_read_details = _action_json(actions, "harness-work-read")
    work_lines = work_read.get("lines")
    if (
        work_read.get("path") != "cross-trajectory.json"
        or work_read.get("offset") != 1
        or not isinstance(work_lines, list)
        or len(work_lines) != 1
        or work_lines[0].get("line") != 1
        or work_lines[0].get("text_truncated") is not False
        or work_read_details.get("operation") != "read_work"
    ):
        raise HarnessAcceptanceError("Work read did not return the derived file")
    try:
        work_value = json.loads(work_lines[0]["text"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise HarnessAcceptanceError("Derived work file is not valid JSON") from error
    if work_value != {"runs": sorted(execution_ids)}:
        raise HarnessAcceptanceError("Derived work file has the wrong Trajectory set")

    attestations = [
        record.get("payload")
        for record in records
        if record.get("type") == "harness_driver_attestation"
    ]
    if len(attestations) != 1 or not isinstance(attestations[0], Mapping):
        raise HarnessAcceptanceError(
            "Harness driver termination attestation is missing"
        )
    attestation = attestations[0]
    if (
        attestation.get("mode") != "positive"
        or attestation.get("callCount") != 9
        or attestation.get("pendingResponses") != 2
    ):
        raise HarnessAcceptanceError("Harness driver termination invariant failed")
    assert isinstance(drive.result, Mapping)
    facts = [
        _fact(
            "all fixed production extensions were loaded by Pi",
            ",".join(extension_names),
            digest=hashlib.sha256(
                _canonical_bytes(dict(process_records[0]))
            ).hexdigest(),
        ),
        _fact_for_action(
            "full-text search returned every record in the selected Trajectory",
            f"matches={len(trajectory_records)}",
            actions["harness-search"],
        ),
        _fact_for_action(
            "evidence read returned the exact requested raw Trajectory lines",
            "lines=1",
            actions["harness-read"],
        ),
        _fact_for_action(
            "structured query returned the exact selected action rows",
            f"rows={len(selected_entries)}",
            actions["harness-filter"],
        ),
        _fact_for_action(
            "script extraction returned the exact script index rows",
            f"rows={len(navigation_scripts)}",
            actions["harness-scripts"],
        ),
        _fact_for_action(
            "Trajectory window returned the exact records around its locator",
            f"records={len(expected_window_records)}",
            actions["harness-window"],
        ),
        _fact_for_action(
            "work write persisted the exact cross-Trajectory program",
            f"bytes={write['bytes']}",
            actions["harness-write"],
        ),
        _fact_for_action(
            "program execution computed the exact eligible Trajectory set",
            f"runs={len(execution_ids)}",
            actions["harness-exec"],
        ),
        _fact_for_action(
            "work read returned the exact saved derivation",
            f"runs={len(execution_ids)}",
            actions["harness-work-read"],
        ),
        _fact_for_action(
            "terminating submission was accepted and stopped further actions",
            "one accepted submission; two queued tripwires unconsumed",
            actions["harness-submit"],
        ),
    ]
    return dict(drive.result), facts


def _assert_budget_drive(
    drive: Any,
    records: Sequence[Mapping[str, Any]],
) -> None:
    if drive.status != "succeeded":
        raise HarnessAcceptanceError("Tool-budget recovery drive did not succeed")
    actions = _tool_actions(records)
    failed = actions.get("budget-must-fail")
    if failed is None or failed.get("status") != "failed":
        raise HarnessAcceptanceError("Production tool-call budget was not enforced")
    if "Research tool-call budget exhausted" not in json.dumps(
        failed.get("result"), ensure_ascii=False, sort_keys=True
    ):
        raise HarnessAcceptanceError("Tool-budget failure was not diagnostic")
    submitted = actions.get("budget-submit")
    if submitted is None or submitted.get("status") != "succeeded":
        raise HarnessAcceptanceError("Budget failure could not recover to submission")


def _assert_cleanup_drive(
    drive: Any,
    records: Sequence[Mapping[str, Any]],
) -> list[JsonObject]:
    """Prove failed commands leave no process able to write after return."""

    if drive.status != "succeeded":
        raise HarnessAcceptanceError("Process-cleanup drive did not succeed")
    actions = _tool_actions(records)

    def details(call_id: str, expected_status: str) -> Mapping[str, Any]:
        action = actions.get(call_id)
        if action is None:
            raise HarnessAcceptanceError(
                f"Process-cleanup action is missing: {call_id}"
            )
        result = action.get("result")
        # Pi 0.81.1 records tool calls that returned isError=true with
        # payload status "succeeded"; the tool-level failure signal lives
        # inside the result object. Normalize that here so failed commands
        # are still asserted as failures.
        effective_status = (
            "failed"
            if (
                isinstance(result, Mapping)
                and result.get("isError") is True
            )
            else action.get("status")
        )
        if effective_status != expected_status:
            raise HarnessAcceptanceError(
                f"Process-cleanup action has wrong status: {call_id}"
            )
        observed = result.get("details") if isinstance(result, Mapping) else None
        observed = result.get("details") if isinstance(result, Mapping) else None
        if not isinstance(observed, Mapping):
            raise HarnessAcceptanceError(
                f"Process-cleanup action lacks details: {call_id}"
            )
        if not _cleanup_details_are_complete(observed):
            raise HarnessAcceptanceError(
                f"Container process cleanup was not verified: {call_id}"
            )
        return observed

    timeout_ids = [f"cleanup-timeout-{index}" for index in range(1, 4)]
    timeout_verify_ids = [
        f"cleanup-timeout-verify-{index}" for index in range(1, 4)
    ]
    timeouts = [details(call_id, "failed") for call_id in timeout_ids]
    timeout_verifications = [
        details(call_id, "succeeded") for call_id in timeout_verify_ids
    ]
    output = details("cleanup-output", "failed")
    output_verify = details("cleanup-output-verify", "succeeded")
    if any(item.get("timedOut") is not True for item in timeouts):
        raise HarnessAcceptanceError("Production timeout was not observed")
    if output.get("outputLimitExceeded") is not True:
        raise HarnessAcceptanceError("Production output limit was not observed")
    for label, observed in [
        *[("timeout", item) for item in timeout_verifications],
        ("output", output_verify),
    ]:
        if (
            observed.get("exitCode") != 0
            or observed.get("timedOut") is not False
            or observed.get("outputLimitExceeded") is not False
        ):
            raise HarnessAcceptanceError(
                f"{label} residual-write verification did not pass"
            )
    submitted = actions.get("cleanup-submit")
    if submitted is None or submitted.get("status") != "succeeded":
        raise HarnessAcceptanceError(
            "Process-cleanup verification could not submit"
        )
    return [
        _fact_for_actions(
            "multiple timed-out process trees were synchronously reaped",
            "rounds=3; proc_snapshot=pid1+cleanup; residual=0",
            [actions[call_id] for call_id in timeout_ids],
        ),
        _fact_for_actions(
            "no timed-out descendant could perform a delayed write",
            "rounds=3; all_delayed_files_absent=true",
            [actions[call_id] for call_id in timeout_verify_ids],
        ),
        _fact_for_action(
            "output-limited command process tree was synchronously cleaned",
            "cleanup_verified=true",
            actions["cleanup-output"],
        ),
        _fact_for_action(
            "output-limited background process could not perform a delayed write",
            "delayed_file_absent=true",
            actions["cleanup-output-verify"],
        ),
    ]


def _assert_invalid_drive(
    drive: Any,
    *,
    expected_text: str | Sequence[str],
) -> None:
    if drive.status != "invalid_output" or not isinstance(
        drive.parse_failure, Mapping
    ):
        raise HarnessAcceptanceError(
            f"Protocol negative case was not rejected: {drive.status}"
        )
    expected = (
        (expected_text,)
        if isinstance(expected_text, str)
        else tuple(expected_text)
    )
    message = str(drive.parse_failure.get("message", "")).casefold()
    if not any(item.casefold() in message for item in expected):
        raise HarnessAcceptanceError(
            "Protocol rejection did not identify the violated invariant"
        )


def _assert_structured_rejections(
    accepted: Mapping[str, Any],
    *,
    corpus: ResearchCorpusVerification,
) -> list[str]:
    """Exercise the fixed formal-result negative suite."""

    def clone() -> JsonObject:
        value = json.loads(json.dumps(accepted))
        assert isinstance(value, dict)
        return value

    def validate(value: Mapping[str, Any], *, expected_role: str) -> None:
        normalized = validate_research_result(
            value,
            expected_role=expected_role,
            expected_corpus_digest=corpus.content_sha256,
            expected_baseline_digest=corpus.baseline_sha256,
            allowed_trajectory_ids=corpus.execution_ids,
            known_derivation_ids=["harness-exec"],
        )
        validate_research_result_evidence(normalized, bundle_root=corpus.directory)

    cases: list[tuple[str, JsonObject, str]] = []
    forged = clone()
    forged["corpus_digest"] = "0" * 64
    cases.append(("forged_corpus", forged, "outcome_consistency_analyst"))

    false_reference = clone()
    false_reference["findings"][0]["evidence"][0]["seq"] = 999_999_999
    cases.append(
        ("false_reference", false_reference, "outcome_consistency_analyst")
    )

    repeated_single = clone()
    ids = list(corpus.execution_ids)
    repeated_single["role"] = "behavior_pattern_analyst"
    finding = repeated_single["findings"][0]
    finding["pattern_type"] = "implicit_behavior"
    finding["observed_trajectory_ids"] = ids[:1]
    finding["checked_absent_trajectory_ids"] = ids[1:]
    finding["evidence"] = finding["evidence"][:1]
    finding["derivation_ids"] = []
    cases.append(("single_trajectory_repeat", repeated_single, "behavior_pattern_analyst"))

    wrong_denominator = clone()
    denominator = ids[:2]
    wrong_denominator["research_scope"]["eligible_trajectory_ids"] = denominator
    wrong_denominator["research_scope"]["reviewed_trajectory_ids"] = denominator
    wrong_finding = wrong_denominator["findings"][0]
    wrong_finding["eligible_trajectory_ids"] = denominator
    wrong_finding["observed_trajectory_ids"] = denominator
    wrong_finding["evidence"] = wrong_finding["evidence"][:2]
    cases.append(
        ("wrong_denominator", wrong_denominator, "outcome_consistency_analyst")
    )

    no_counterexample = clone()
    no_counterexample["research_scope"]["counterexample_search"] = ""
    cases.append(
        ("missing_counterexample", no_counterexample, "outcome_consistency_analyst")
    )

    unknown_derivation = clone()
    unknown_derivation["findings"][0]["derivation_ids"].append(
        "unknown-derivation"
    )
    cases.append(
        ("unknown_derivation", unknown_derivation, "outcome_consistency_analyst")
    )

    rejected: list[str] = []
    for name, value, role in cases:
        try:
            validate(value, expected_role=role)
        except ResearchResultError:
            rejected.append(name)
        else:
            raise HarnessAcceptanceError(
                f"Structured result gate accepted negative case: {name}"
            )
    return rejected


def _implementation_identity(
    *,
    tools_path: Path,
    output_path: Path,
    driver_path: Path,
) -> JsonObject:
    runtime_path = Path(__file__).with_name("research_agent_runtime.py")
    paths = {
        "validator_sha256": Path(__file__),
        "runtime_sha256": runtime_path,
        "research_tools_sha256": tools_path,
        "research_output_sha256": output_path,
        "driver_sha256": driver_path,
    }
    for path in paths.values():
        if path.is_symlink() or not path.is_file():
            raise HarnessAcceptanceError(
                f"Harness implementation file is missing or unsafe: {path}"
            )
    return {
        **{name: _file_sha256(path) for name, path in paths.items()},
        "driver_protocol_version": HARNESS_DRIVER_PROTOCOL_VERSION,
    }


def _audit_inventory_at(
    directory_fd: int,
    *,
    prefix: str = "",
) -> tuple[list[JsonObject], dict[str, bytes]]:
    """Read one audit tree through pinned descriptors without reopening files."""

    try:
        names_before = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness audit directory is unreadable: {error}"
        ) from error
    files: list[JsonObject] = []
    contents: dict[str, bytes] = {}
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for name in names_before:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise HarnessAcceptanceError(
                f"Harness audit contains an unsafe path: {name}"
            )
        relative = f"{prefix}{name}"
        if not prefix and name == "manifest.json":
            continue
        child_fd: int | None = None
        try:
            child_fd = os.open(name, open_flags, dir_fd=directory_fd)
            metadata = os.fstat(child_fd)
            if stat.S_ISDIR(metadata.st_mode):
                nested_files, nested_contents = _audit_inventory_at(
                    child_fd,
                    prefix=f"{relative}/",
                )
                current = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise HarnessAcceptanceError(
                        "Harness audit directory changed while read: "
                        f"{relative}"
                    )
                files.extend(nested_files)
                contents.update(nested_contents)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise HarnessAcceptanceError(
                    f"Harness audit contains an unsafe path: {relative}"
                )
            content = _read_fd_bytes(child_fd)
            current = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise HarnessAcceptanceError(
                    f"Harness audit file changed while read: {relative}"
                )
            files.append(
                {
                    "path": relative,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            contents[relative] = content
        except OSError as error:
            raise HarnessAcceptanceError(
                f"Harness audit contains an unsafe path: {relative}: {error}"
            ) from error
        finally:
            if child_fd is not None:
                os.close(child_fd)
    try:
        names_after = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness audit directory changed while read: {error}"
        ) from error
    if names_after != names_before:
        raise HarnessAcceptanceError(
            "Harness audit directory changed while being inventoried"
        )
    files.sort(key=lambda item: str(item["path"]))
    return files, contents


def _open_pinned_output_parent(boundary: _OutputBoundary) -> int:
    try:
        relative = boundary.destination.parent.relative_to(
            boundary.trust_anchor
        )
    except ValueError as error:
        raise HarnessAcceptanceError(
            "Harness output parent escaped its trust root"
        ) from error
    parent_fd, _ = _open_directory_chain(
        boundary.trust_anchor,
        relative.parts,
        create=False,
        expected_anchor=(
            boundary.anchor_device,
            boundary.anchor_inode,
        ),
        label="Harness output parent",
    )
    metadata = os.fstat(parent_fd)
    if (metadata.st_dev, metadata.st_ino) != (
        boundary.parent_device,
        boundary.parent_inode,
    ):
        os.close(parent_fd)
        raise HarnessAcceptanceError("Harness output parent identity changed")
    return parent_fd


def _open_audit_root(boundary: _AuditBoundary) -> tuple[int, int]:
    parent_fd = _open_pinned_output_parent(boundary.output)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        audit_fd = os.open(boundary.working_name, flags, dir_fd=parent_fd)
    except OSError as error:
        os.close(parent_fd)
        raise HarnessAcceptanceError(
            f"Harness audit boundary is missing or unsafe: {error}"
        ) from error
    metadata = os.fstat(audit_fd)
    if (metadata.st_dev, metadata.st_ino) != (boundary.device, boundary.inode):
        os.close(audit_fd)
        os.close(parent_fd)
        raise HarnessAcceptanceError("Harness audit directory identity changed")
    try:
        os.stat(
            boundary.final_directory.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        os.close(audit_fd)
        os.close(parent_fd)
        raise HarnessAcceptanceError("Harness audit output appeared during run")
    return parent_fd, audit_fd


def _verify_audit_boundary(boundary: _AuditBoundary) -> None:
    parent_fd, audit_fd = _open_audit_root(boundary)
    os.close(audit_fd)
    os.close(parent_fd)


def _audit_create_directory(boundary: _AuditBoundary, name: str) -> Path:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise HarnessAcceptanceError("Harness audit directory name is unsafe")
    parent_fd, audit_fd = _open_audit_root(boundary)
    try:
        os.mkdir(name, mode=0o700, dir_fd=audit_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        child_fd = os.open(name, flags, dir_fd=audit_fd)
        os.close(child_fd)
        os.fsync(audit_fd)
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Could not create Harness audit directory {name}: {error}"
        ) from error
    finally:
        os.close(audit_fd)
        os.close(parent_fd)
    return boundary.working_directory / name


def _write_audit_file_at(directory_fd: int, name: str, content: bytes) -> str:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise HarnessAcceptanceError("Harness audit file name is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Could not create Harness audit file {name}: {error}"
        ) from error
    try:
        with os.fdopen(file_fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(file_fd)
    os.fsync(directory_fd)
    return hashlib.sha256(content).hexdigest()


def _audit_write_json(
    boundary: _AuditBoundary,
    name: str,
    value: Mapping[str, Any],
) -> str:
    encoded = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    parent_fd, audit_fd = _open_audit_root(boundary)
    try:
        return _write_audit_file_at(audit_fd, name, encoded)
    finally:
        os.close(audit_fd)
        os.close(parent_fd)


def _prepare_audit(
    output: _OutputBoundary,
    *,
    implementation_paths: Mapping[str, Path],
    implementation: Mapping[str, Any],
) -> _AuditBoundary:
    final = output.destination.with_name(f"{output.destination.stem}.audit")
    parent_fd = _open_pinned_output_parent(output)
    working_name = (
        f".{final.name}.{os.urandom(16).hex()}.staging"
    )
    try:
        for name in (final.name, working_name):
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise HarnessAcceptanceError(
                f"Harness audit path already exists: {name}"
            )
        os.mkdir(working_name, mode=0o700, dir_fd=parent_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        audit_fd = os.open(working_name, flags, dir_fd=parent_fd)
        metadata = os.fstat(audit_fd)
        os.close(audit_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Could not create Harness audit boundary: {error}"
        ) from error
    finally:
        os.close(parent_fd)
    boundary = _AuditBoundary(
        output=output,
        working_directory=output.destination.parent / working_name,
        final_directory=final,
        working_name=working_name,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    _audit_create_directory(boundary, "implementation")
    parent_fd, audit_fd = _open_audit_root(boundary)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        snapshots_fd = os.open("implementation", flags, dir_fd=audit_fd)
        try:
            for name, source in implementation_paths.items():
                _write_audit_file_at(snapshots_fd, name, source.read_bytes())
        finally:
            os.close(snapshots_fd)
    finally:
        os.close(audit_fd)
        os.close(parent_fd)
    _audit_write_json(
        boundary,
        "command-plan.json",
        {
            "schema": "research.harness_command_plan.v1",
            "driver_protocol_version": HARNESS_DRIVER_PROTOCOL_VERSION,
            "provider": RESEARCH_HARNESS_FAUX_PROVIDER,
            "model": RESEARCH_HARNESS_FAUX_MODEL,
            "modes": [
                "positive",
                "budget",
                "cleanup",
                "duplicate_submission",
                "post_submission",
            ],
            "implementation": dict(implementation),
        },
    )
    return boundary


def _seal_audit(boundary: _AuditBoundary) -> JsonObject:
    _verify_audit_boundary(boundary)
    parent_fd, audit_fd = _open_audit_root(boundary)
    try:
        files, _ = _audit_inventory_at(audit_fd)
    finally:
        os.close(audit_fd)
        os.close(parent_fd)
    tree_sha256 = hashlib.sha256(_canonical_bytes({"files": files})).hexdigest()
    manifest: JsonObject = {
        "schema": "research.harness_audit_manifest.v1",
        "driver_protocol_version": HARNESS_DRIVER_PROTOCOL_VERSION,
        "tree_sha256": tree_sha256,
        "files": files,
    }
    manifest_sha256 = _audit_write_json(boundary, "manifest.json", manifest)
    parent_fd, audit_fd = _open_audit_root(boundary)
    try:
        os.rename(
            boundary.working_name,
            boundary.final_directory.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        final_metadata = os.stat(
            boundary.final_directory.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (final_metadata.st_dev, final_metadata.st_ino) != (
            boundary.device,
            boundary.inode,
        ):
            raise HarnessAcceptanceError(
                "Published Harness audit identity differs"
            )
        os.fsync(parent_fd)
    finally:
        os.close(audit_fd)
        os.close(parent_fd)
    return {
        "directory": boundary.final_directory.name,
        "manifest_sha256": manifest_sha256,
        "tree_sha256": tree_sha256,
    }


def _report_identity(
    corpus: ResearchCorpusVerification | None,
    *,
    expected_corpus_digest: str | None,
    expected_baseline_digest: str | None,
) -> JsonObject:
    return {
        "corpus_id": str(corpus.manifest["corpus_id"]) if corpus else None,
        "content_sha256": (
            corpus.content_sha256 if corpus else expected_corpus_digest
        ),
        "baseline_sha256": (
            corpus.baseline_sha256 if corpus else expected_baseline_digest
        ),
        "execution_ids": list(corpus.execution_ids) if corpus else [],
    }


def run_harness_acceptance(
    *,
    corpus_directory: str | os.PathLike[str],
    sandbox: DockerResearchSandbox,
    report_path: str | os.PathLike[str],
    trusted_output_root: str | os.PathLike[str],
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
    research_tools_path: str | os.PathLike[str] | None = None,
    research_output_path: str | os.PathLike[str] | None = None,
    research_harness_context_path: str | os.PathLike[str] | None = None,
    driver_extension_path: str | os.PathLike[str] | None = None,
    pi_command: Sequence[str] | str | None = None,
    extra_pi_args: Sequence[str] = (),
) -> HarnessAcceptanceVerification:
    """Execute all fixed checks and atomically write one immutable report.

    No check outcomes are accepted as parameters. Sandbox-dependent checks run
    only through ``DockerResearchSandbox.isolated_run`` and Docker ``exec``.
    An unavailable or failing isolation backend therefore produces a failed
    report instead of executing any probe on the host.
    """

    if not isinstance(sandbox, DockerResearchSandbox):
        raise TypeError("sandbox must be a DockerResearchSandbox")
    output_boundary = _prepare_output_destination(
        report_path,
        trusted_directory=trusted_output_root,
    )
    destination = output_boundary.destination
    project_root = Path(__file__).resolve().parent.parent
    tools_path = Path(
        research_tools_path or project_root / "extensions/research-tools.ts"
    ).resolve()
    output_path = Path(
        research_output_path or project_root / "extensions/research-output.ts"
    ).resolve()
    context_path = Path(
        research_harness_context_path
        or project_root / "prompts/analysis/research-harness-context-v1.json"
    ).resolve()
    driver_path = Path(
        driver_extension_path
        or project_root / "extensions/research-harness-driver.ts"
    ).resolve()
    implementation = _implementation_identity(
        tools_path=tools_path,
        output_path=output_path,
        driver_path=driver_path,
    )
    implementation_paths = {
        "research-harness-acceptance.py": Path(__file__).resolve(),
        "research-agent-runtime.py": Path(__file__).with_name(
            "research_agent_runtime.py"
        ),
        "research-tools.ts": tools_path,
        "research-output.ts": output_path,
        "research-harness-driver.ts": driver_path,
    }
    harness_context = None
    harness_context_error: str | None = None
    try:
        harness_context = load_approved_research_harness_context(
            context_path,
            research_tools_path=tools_path,
            research_output_path=output_path,
        )
    except (OSError, ValueError, RuntimeError) as error:
        harness_context_error = str(error)
    if harness_context is not None:
        implementation_paths["research-harness-context.json"] = context_path
        implementation_paths[
            "research-harness-context-approval.json"
        ] = harness_context.approval.approval_path
    audit_boundary = _prepare_audit(
        output_boundary,
        implementation_paths=implementation_paths,
        implementation=implementation,
    )
    audit_directory = audit_boundary.working_directory
    started_at = _now()
    checks: dict[str, JsonObject] = {}
    corpus: ResearchCorpusVerification | None = None
    try:
        corpus = verify_research_corpus(
            corpus_directory,
            expected_content_sha256=expected_corpus_digest,
            expected_baseline_sha256=expected_baseline_digest,
        )
        negative_evidence = _verify_corpus_rejections(corpus)
        checks["corpus_preflight"] = _passed(
            "corpus_preflight",
            [
                _fact(
                    "frozen corpus and baseline passed complete byte verification",
                    f"files={len(corpus.manifest['files'])}; baseline=bound",
                    digest=hashlib.sha256(
                        _canonical_bytes(
                            {
                                "corpus": corpus.content_sha256,
                                "baseline": corpus.baseline_sha256,
                            }
                        )
                    ).hexdigest(),
                ),
                *negative_evidence,
            ],
        )
    except (ResearchCorpusError, OSError, ValueError) as error:
        checks["corpus_preflight"] = _failed("corpus_preflight", error)

    locators: list[tuple[str, int]] = []
    sequences: dict[str, set[int]] = {}
    if corpus is not None:
        try:
            locators, evidence = _validate_navigation(corpus)
            checks["navigation_index"] = _passed("navigation_index", evidence)
        except (HarnessAcceptanceError, ValueError) as error:
            checks["navigation_index"] = _failed("navigation_index", error)
        try:
            sequences = _trajectory_sequences(corpus)
            missing = [
                f"{run_id}:{sequence}"
                for run_id, sequence in locators
                if sequence not in sequences.get(run_id, set())
            ]
            indexed_entries = corpus.navigation_index.get("entries", [])
            if missing:
                raise HarnessAcceptanceError(
                    f"Navigation locators do not resolve: {missing[:10]}"
                )
            if len(indexed_entries) != sum(len(items) for items in sequences.values()):
                raise HarnessAcceptanceError(
                    "Navigation index silently omitted or added Trajectory records"
                )
            locator_digest = hashlib.sha256(
                _canonical_bytes(
                    {"locators": [list(item) for item in sorted(set(locators))]}
                )
            ).hexdigest()
            checks["evidence_roundtrip"] = _passed(
                "evidence_roundtrip",
                [
                    _fact(
                        "every indexed locator resolves to original Trajectory evidence",
                        str(len(locators)),
                        digest=locator_digest,
                    ),
                    _fact(
                        "every raw Trajectory record has a navigation entry",
                        str(sum(len(items) for items in sequences.values())),
                    ),
                ],
            )
        except (HarnessAcceptanceError, OSError, ValueError) as error:
            checks["evidence_roundtrip"] = _failed("evidence_roundtrip", error)
    else:
        checks["navigation_index"] = _failed(
            "navigation_index", "Corpus preflight did not pass"
        )
        checks["evidence_roundtrip"] = _failed(
            "evidence_roundtrip", "Corpus preflight did not pass"
        )

    preflight: ResearchSandboxPreflightResult
    try:
        preflight = sandbox.preflight()
    except BaseException as error:
        preflight = ResearchSandboxPreflightResult(
            available=False,
            backend=RESEARCH_SANDBOX_BACKEND,
            detail=f"Sandbox preflight raised: {error}",
            image=sandbox.image,
        )
    declared_limits = sandbox.limits.to_dict()
    limits_sha256 = hashlib.sha256(_canonical_bytes(declared_limits)).hexdigest()
    sandbox_record: JsonObject = {
        "backend": preflight.backend,
        "image": preflight.image,
        "image_id": preflight.image_id,
        "preflight_available": preflight.available,
        "limits": declared_limits,
        "limits_sha256": limits_sha256,
        "active_config": None,
        "active_config_sha256": None,
    }
    execution_identity: JsonObject | None = None
    execution_identity_sha256: str | None = None
    execution_identity_error: str | None = None
    if (
        preflight.available
        and preflight.image_id
        and preflight.control_plane_identity is not None
        and harness_context is not None
    ):
        try:
            pi_execution_identity = attest_pi_execution_identity(
                pi_command,
                extra_pi_args=extra_pi_args,
                working_directory=project_root,
            )
            execution_identity = build_research_execution_identity(
                repository_root=project_root,
                pi_execution_identity=pi_execution_identity,
                harness_context_sha256=harness_context.context_sha256,
                research_tools_sha256=implementation[
                    "research_tools_sha256"
                ],
                research_output_sha256=implementation[
                    "research_output_sha256"
                ],
                sandbox_backend=preflight.backend,
                sandbox_image=preflight.image,
                sandbox_image_id=preflight.image_id,
                sandbox_limits=declared_limits,
                sandbox_control_plane_identity=(
                    preflight.control_plane_identity
                ),
            )
            execution_identity_sha256 = research_execution_identity_digest(
                execution_identity,
                repository_root=project_root,
                verify_pi_executable=True,
            )
            _audit_write_json(
                audit_boundary,
                "execution-identity.json",
                execution_identity,
            )
        except (ResearchCapabilityError, OSError, ValueError) as error:
            execution_identity_error = str(error)
    accepted_result: JsonObject | None = None
    if (
        corpus is None
        or not preflight.available
        or not preflight.image_id
        or preflight.control_plane_identity is None
        or execution_identity is None
    ):
        reason = (
            "Corpus is unavailable"
            if corpus is None
            else (
                f"Sandbox unavailable: {preflight.detail}"
                if not preflight.available or not preflight.image_id
                else "Research execution identity unavailable: "
                f"{execution_identity_error or harness_context_error}"
            )
        )
        for name in (
            "fake_agent_research_loop",
            "sandbox_isolation",
            "resource_limits",
            "structured_submission",
        ):
            checks[name] = _failed(name, reason)
    else:
        context: JsonObject | None = None
        lifecycle_error: BaseException | None = None
        drives: dict[str, Any] = {}
        journals: dict[str, list[JsonObject]] = {}
        active_config: JsonObject | None = None
        try:
            _verify_audit_boundary(audit_boundary)
            with sandbox.isolated_run(
                evidence_directory=corpus.directory,
                work_archive_directory=audit_directory / "sandbox-work",
                expected_evidence_digest=research_evidence_tree_digest(
                    corpus.directory
                ),
                expected_control_plane_identity=execution_identity[
                    "sandbox"
                ]["control_plane"],
            ) as active_context:
                context = active_context
                validate_research_sandbox_context(context)
                if context.get("image_id") != preflight.image_id:
                    raise HarnessAcceptanceError(
                        "Sandbox image changed between preflight and run"
                    )
                sandbox_record["image_id"] = context.get("image_id")
                active_config = _inspect_active_container(
                    context,
                    expected_image_id=preflight.image_id,
                    expected_limits=declared_limits,
                )
                sandbox_record["active_config"] = active_config
                sandbox_record["active_config_sha256"] = hashlib.sha256(
                    _canonical_bytes(active_config)
                ).hexdigest()

                runtime = ResearchPiAgentRuntime(
                    agent_runs_root=audit_directory / "unused-agent-runs",
                    research_extension_path=tools_path,
                    research_output_extension_path=output_path,
                    research_harness_context_path=context_path,
                    sandbox=sandbox,
                    model=ModelConfiguration(
                        provider=RESEARCH_HARNESS_FAUX_PROVIDER,
                        model=RESEARCH_HARNESS_FAUX_MODEL,
                        thinking="off",
                    ),
                    pi_command=pi_command,
                    extra_pi_args=extra_pi_args,
                    repository_root=project_root,
                )
                first_run = list(corpus.execution_ids)[0]
                trajectory_filename = (
                    "trace.jsonl"
                    if (
                        corpus.directory
                        / "runs"
                        / first_run
                        / "trace.jsonl"
                    ).is_file()
                    else "trajectory.jsonl"
                )
                validation_context: JsonObject = {
                    "corpus_digest": corpus.content_sha256,
                    "baseline_digest": corpus.baseline_sha256,
                    "eligible_trajectory_ids": list(corpus.execution_ids),
                    "trajectory_filename": trajectory_filename,
                }
                for mode in (
                    "positive",
                    "budget",
                    "cleanup",
                    "duplicate_submission",
                    "post_submission",
                ):
                    mode_directory = _audit_create_directory(
                        audit_boundary, f"pi-{mode}"
                    )
                    journal_path = mode_directory / "trajectory.jsonl"
                    journal = TrajectoryJournal(
                        journal_path, f"deterministic-{mode}"
                    )
                    try:
                        drives[mode] = runtime.drive_deterministic_harness(
                            workspace=mode_directory / "workspace",
                            evidence=corpus.directory,
                            sandbox_context=context,
                            validation_context=validation_context,
                            journal=journal,
                            driver_extension_path=driver_path,
                            mode=mode,
                            research_execution_identity=execution_identity,
                        )
                    finally:
                        journal.close()
                    _verify_audit_boundary(audit_boundary)
                    journals[mode] = _load_journal(journal_path)

                try:
                    accepted_result, positive_evidence = _assert_positive_drive(
                        drives["positive"],
                        journals["positive"],
                        corpus,
                        (tools_path.name, output_path.name, driver_path.name),
                    )
                    checks["fake_agent_research_loop"] = _passed(
                        "fake_agent_research_loop",
                        positive_evidence,
                    )
                except HarnessAcceptanceError as error:
                    checks["fake_agent_research_loop"] = _failed(
                        "fake_agent_research_loop", error
                    )

                try:
                    isolation = _probe_python(context, _isolation_source())
                    if isolation.exit_code != 0 or isolation.timed_out:
                        raise HarnessAcceptanceError(
                            "Isolation probe did not complete"
                        )
                    facts = json.loads(isolation.stdout)
                    required = {
                        "evidence_readable",
                        "evidence_write_denied",
                        "root_write_denied",
                        "symlink_escape_denied",
                        "loopback_only",
                        "credential_environment_absent",
                        "secret_mount_absent",
                        "non_root",
                    }
                    if (
                        not isinstance(facts, Mapping)
                        or set(facts) != required
                        or not all(facts.values())
                    ):
                        raise HarnessAcceptanceError(
                            f"Isolation probe failed assertions: {facts}"
                        )
                    log_sink = _verify_disabled_container_logs(context)
                    checks["sandbox_isolation"] = _passed(
                        "sandbox_isolation",
                        [
                            _fact(
                                "Docker reports network none and no attached networks",
                                "network_mode=none; attached=0",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {
                                            "network_mode": active_config[
                                                "network_mode"
                                            ],
                                            "network_count": active_config[
                                                "network_count"
                                            ],
                                        }
                                    )
                                ).hexdigest(),
                            ),
                            _fact(
                                "container exposes only the loopback interface",
                                "interfaces=lo",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {"loopback_only": facts["loopback_only"]}
                                    )
                                ).hexdigest(),
                            ),
                            _fact(
                                "frozen evidence is readable but not writable",
                                "readable=true; write_denied=true",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {
                                            "readable": facts[
                                                "evidence_readable"
                                            ],
                                            "write_denied": facts[
                                                "evidence_write_denied"
                                            ],
                                        }
                                    )
                                ).hexdigest(),
                            ),
                            _fact(
                                "container cannot write through its read-only root",
                                "root_write_denied=true; non_root=true; "
                                "docker_log_sink=disabled",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {
                                            "root_write_denied": facts[
                                                "root_write_denied"
                                            ],
                                            "non_root": facts["non_root"],
                                            "log_sink": log_sink,
                                        }
                                    )
                                ).hexdigest(),
                            ),
                            _fact(
                                "work symlinks cannot escape into frozen evidence",
                                "symlink_escape_denied=true",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {
                                            "symlink_escape_denied": facts[
                                                "symlink_escape_denied"
                                            ]
                                        }
                                    )
                                ).hexdigest(),
                            ),
                            _fact(
                                "credentials and secret mounts are absent",
                                "environment_absent=true; mounts_absent=true",
                                digest=hashlib.sha256(
                                    _canonical_bytes(
                                        {
                                            "environment_absent": facts[
                                                "credential_environment_absent"
                                            ],
                                            "mounts_absent": facts[
                                                "secret_mount_absent"
                                            ],
                                        }
                                    )
                                ).hexdigest(),
                            ),
                        ],
                    )
                except (HarnessAcceptanceError, json.JSONDecodeError) as error:
                    checks["sandbox_isolation"] = _failed(
                        "sandbox_isolation", error
                    )

                try:
                    _assert_budget_drive(drives["budget"], journals["budget"])
                    cleanup_evidence = _assert_cleanup_drive(
                        drives["cleanup"], journals["cleanup"]
                    )
                    checks["resource_limits"] = _passed(
                        "resource_limits",
                        [
                            _fact(
                                "active Docker CPU, memory, process, file, and "
                                "tmpfs limits match the declared laboratory",
                                json.dumps(active_config, sort_keys=True),
                                digest=sandbox_record["active_config_sha256"],
                            ),
                            _fact(
                                "production tool-call budget rejects the next call",
                                "third research tool rejected; submission recovered",
                                digest=_file_sha256(
                                    audit_directory / "pi-budget/trajectory.jsonl"
                                ),
                            ),
                            *cleanup_evidence,
                        ],
                    )
                except HarnessAcceptanceError as error:
                    checks["resource_limits"] = _failed(
                        "resource_limits", error
                    )

                try:
                    if accepted_result is None:
                        raise HarnessAcceptanceError(
                            "Production Agent produced no accepted result"
                        )
                    normalized = validate_research_result(
                        accepted_result,
                        expected_role="outcome_consistency_analyst",
                        expected_corpus_digest=corpus.content_sha256,
                        expected_baseline_digest=corpus.baseline_sha256,
                        allowed_trajectory_ids=corpus.execution_ids,
                        known_derivation_ids=["harness-exec"],
                    )
                    validate_research_result_evidence(
                        normalized, bundle_root=corpus.directory
                    )
                    rejected = _assert_structured_rejections(
                        normalized, corpus=corpus
                    )
                    _assert_invalid_drive(
                        drives["duplicate_submission"],
                        expected_text="exactly one submission",
                    )
                    _assert_invalid_drive(
                        drives["post_submission"],
                        expected_text=(
                            "sole tool",
                            "continued after successful submission",
                        ),
                    )
                    accepted_digest = hashlib.sha256(
                        _canonical_bytes(normalized)
                    ).hexdigest()
                    checks["structured_submission"] = _passed(
                        "structured_submission",
                        [
                            _fact(
                                "one production submission passed strict result and "
                                "original-evidence validation",
                                "valid_production_submission=1",
                                digest=accepted_digest,
                            ),
                            *[
                                _fact(
                                    f"{case} malformed result was rejected",
                                    "rejected",
                                    digest=hashlib.sha256(
                                        _canonical_bytes(
                                            {
                                                "case": case,
                                                "outcome": "rejected",
                                            }
                                        )
                                    ).hexdigest(),
                                )
                                for case in (
                                    "false_reference",
                                    "single_trajectory_repeat",
                                    "wrong_denominator",
                                    "missing_counterexample",
                                    "unknown_derivation",
                                )
                                if case in rejected
                            ],
                            _fact(
                                "duplicate production submissions were rejected",
                                "rejected",
                                digest=_file_sha256(
                                    audit_directory
                                    / "pi-duplicate_submission/trajectory.jsonl"
                                ),
                            ),
                            _fact(
                                "actions sharing a successful submission batch were "
                                "rejected",
                                "rejected",
                                digest=_file_sha256(
                                    audit_directory
                                    / "pi-post_submission/trajectory.jsonl"
                                ),
                            ),
                        ],
                    )
                except (HarnessAcceptanceError, ResearchResultError) as error:
                    checks["structured_submission"] = _failed(
                        "structured_submission", error
                    )
            _verify_audit_boundary(audit_boundary)
        except BaseException as error:
            lifecycle_error = error
        if lifecycle_error is not None:
            for name in (
                "fake_agent_research_loop",
                "sandbox_isolation",
                "resource_limits",
                "structured_submission",
            ):
                if checks.get(name, {}).get("status") != "failed":
                    checks[name] = _failed(
                        name, f"Sandbox lifecycle failed: {lifecycle_error}"
                    )
        elif context is not None:
            before = context.get("evidence_digest_before")
            after = context.get("evidence_digest_after")
            if before != after or after is None:
                checks["sandbox_isolation"] = _failed(
                    "sandbox_isolation",
                    "Evidence digest was not preserved across the isolated run",
                )

    current_implementation = _implementation_identity(
        tools_path=tools_path,
        output_path=output_path,
        driver_path=driver_path,
    )
    if current_implementation != implementation:
        for name in HARNESS_CHECKS:
            if checks[name]["status"] == "passed":
                checks[name] = _failed(
                    name, "Harness implementation changed during acceptance"
                )
    ordered_checks = [checks[name] for name in HARNESS_CHECKS]
    status = (
        "passed"
        if all(item["status"] == "passed" for item in ordered_checks)
        else "failed"
    )
    audit = _seal_audit(audit_boundary)
    body: JsonObject = {
        "validator_version": HARNESS_VALIDATOR_VERSION,
        "status": status,
        "started_at": started_at,
        "ended_at": _now(),
        "corpus": _report_identity(
            corpus,
            expected_corpus_digest=expected_corpus_digest,
            expected_baseline_digest=expected_baseline_digest,
        ),
        "implementation": implementation,
        "execution_identity": execution_identity,
        "execution_identity_sha256": execution_identity_sha256,
        "sandbox": sandbox_record,
        "audit": audit,
        "checks": ordered_checks,
    }
    report: JsonObject = {
        "schema": HARNESS_ACCEPTANCE_SCHEMA,
        "content_sha256": hashlib.sha256(_canonical_bytes(body)).hexdigest(),
        **body,
    }
    validate_harness_acceptance_report(report)
    _reverify_output_boundary(
        output_boundary,
        destination_must_be_absent=True,
    )
    report_file_sha256 = _write_output_json(output_boundary, report)
    _reverify_output_boundary(
        output_boundary,
        destination_must_be_absent=False,
    )
    return load_harness_acceptance_report(
        destination,
        expected_file_sha256=report_file_sha256,
        expected_corpus_digest=expected_corpus_digest,
        expected_baseline_digest=expected_baseline_digest,
        expected_limits_sha256=limits_sha256,
        corpus_directory=corpus_directory,
        trusted_output_root=trusted_output_root,
    )


def _require_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise HarnessAcceptanceError(
            f"{label} fields differ from contract: "
            f"missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise HarnessAcceptanceError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HarnessAcceptanceError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise HarnessAcceptanceError(f"{label} must include a timezone")
    return parsed


def _validate_active_config(
    active: Mapping[str, Any],
    *,
    image_id: str,
    limits: Mapping[str, Any],
) -> None:
    expected_fields = {
        "image_id",
        "network_mode",
        "network_count",
        "log_driver",
        "readonly_rootfs",
        "user",
        "pids_limit",
        "nano_cpus",
        "memory_bytes",
        "nofile_soft",
        "nofile_hard",
        "cap_drop",
        "security_options",
        "tmpfs",
        "tool_budgets",
    }
    _require_fields(active, expected_fields, "Harness active Docker config")
    expected_scalars = {
        "image_id": image_id,
        "network_mode": "none",
        "network_count": 0,
        "log_driver": "none",
        "readonly_rootfs": True,
        "user": "65534:65534",
        "pids_limit": limits["pids"],
        "nano_cpus": round(float(limits["cpus"]) * 1_000_000_000),
        "memory_bytes": _memory_bytes(str(limits["memory"])),
        "nofile_soft": limits["open_files"],
        "nofile_hard": limits["open_files"],
    }
    for field, expected in expected_scalars.items():
        if active.get(field) != expected:
            raise HarnessAcceptanceError(
                f"Harness active Docker {field} differs from declared limits"
            )
    cap_drop = active.get("cap_drop")
    if not isinstance(cap_drop, list) or "ALL" not in cap_drop:
        raise HarnessAcceptanceError("Harness active Docker capabilities are unsafe")
    security = active.get("security_options")
    if not isinstance(security, list) or not any(
        isinstance(item, str)
        and re.fullmatch(r"no-new-privileges(?::true)?", item)
        for item in security
    ):
        raise HarnessAcceptanceError("Harness active Docker privileges are unsafe")
    tmpfs = active.get("tmpfs")
    if not isinstance(tmpfs, Mapping):
        raise HarnessAcceptanceError("Harness active Docker tmpfs is invalid")
    for mount, size in (
        ("/work", limits["work_bytes"]),
        ("/tmp", limits["temporary_bytes"]),
    ):
        options = tmpfs.get(mount)
        if not isinstance(options, str) or f"size={size}" not in options.split(","):
            raise HarnessAcceptanceError(
                f"Harness active Docker {mount} quota is invalid"
            )
    expected_budgets = {
        "command_timeout_milliseconds": limits["command_timeout_seconds"]
        * 1000,
        "max_output_bytes": limits["max_output_bytes"],
        "max_tool_calls": limits["max_tool_calls"],
        "max_concurrent_tool_calls": limits["max_concurrent_tool_calls"],
        "max_total_output_bytes": limits["max_total_output_bytes"],
        "max_total_command_milliseconds": limits[
            "max_total_command_milliseconds"
        ],
    }
    if active.get("tool_budgets") != expected_budgets:
        raise HarnessAcceptanceError("Harness active tool budgets are invalid")


def validate_harness_acceptance_report(value: Mapping[str, Any]) -> JsonObject:
    """Validate the exact report shape, identities and derived overall status."""

    _require_fields(
        value,
        {
            "schema",
            "content_sha256",
            "validator_version",
            "status",
            "started_at",
            "ended_at",
            "corpus",
            "implementation",
            "execution_identity",
            "execution_identity_sha256",
            "sandbox",
            "audit",
            "checks",
        },
        "Harness acceptance report",
    )
    if value.get("schema") != HARNESS_ACCEPTANCE_SCHEMA:
        raise HarnessAcceptanceError("Unsupported Harness acceptance schema")
    if value.get("validator_version") != HARNESS_VALIDATOR_VERSION:
        raise HarnessAcceptanceError("Unsupported Harness validator version")
    stored_digest = value.get("content_sha256")
    if not isinstance(stored_digest, str) or not _SHA256.fullmatch(stored_digest):
        raise HarnessAcceptanceError("Harness report content digest is invalid")
    body = {
        key: item
        for key, item in value.items()
        if key not in {"schema", "content_sha256"}
    }
    if hashlib.sha256(_canonical_bytes(body)).hexdigest() != stored_digest:
        raise HarnessAcceptanceError("Harness report body digest does not match")
    started = _parse_timestamp(value.get("started_at"), "started_at")
    ended = _parse_timestamp(value.get("ended_at"), "ended_at")
    if ended < started:
        raise HarnessAcceptanceError("Harness report ended before it started")

    corpus = value.get("corpus")
    if not isinstance(corpus, Mapping):
        raise HarnessAcceptanceError("Harness corpus identity must be an object")
    _require_fields(
        corpus,
        {"corpus_id", "content_sha256", "baseline_sha256", "execution_ids"},
        "Harness corpus identity",
    )
    for field in ("content_sha256", "baseline_sha256"):
        item = corpus.get(field)
        if item is not None and (
            not isinstance(item, str) or not _SHA256.fullmatch(item)
        ):
            raise HarnessAcceptanceError(f"Harness corpus {field} is invalid")
    corpus_id = corpus.get("corpus_id")
    if corpus_id is not None and (
        not isinstance(corpus_id, str) or not corpus_id
    ):
        raise HarnessAcceptanceError("Harness corpus id is invalid")
    content_digest = corpus.get("content_sha256")
    if (
        corpus_id is not None
        and isinstance(content_digest, str)
        and corpus_id != f"corpus-{content_digest[:20]}"
    ):
        raise HarnessAcceptanceError("Harness corpus id differs from its digest")
    execution_ids = corpus.get("execution_ids")
    if not isinstance(execution_ids, list) or not all(
        isinstance(item, str) and item for item in execution_ids
    ) or len(execution_ids) != len(set(execution_ids)):
        raise HarnessAcceptanceError("Harness execution identities are invalid")

    implementation = value.get("implementation")
    if not isinstance(implementation, Mapping):
        raise HarnessAcceptanceError("Harness implementation identity is invalid")
    _require_fields(
        implementation,
        {
            "validator_sha256",
            "runtime_sha256",
            "research_tools_sha256",
            "research_output_sha256",
            "driver_sha256",
            "driver_protocol_version",
        },
        "Harness implementation identity",
    )
    for field in (
        "validator_sha256",
        "runtime_sha256",
        "research_tools_sha256",
        "research_output_sha256",
        "driver_sha256",
    ):
        item = implementation.get(field)
        if not isinstance(item, str) or not _SHA256.fullmatch(item):
            raise HarnessAcceptanceError(
                f"Harness implementation {field} is invalid"
            )
    if (
        implementation.get("driver_protocol_version")
        != HARNESS_DRIVER_PROTOCOL_VERSION
    ):
        raise HarnessAcceptanceError("Unsupported Harness driver protocol")

    raw_execution_identity = value.get("execution_identity")
    execution_identity_digest = value.get("execution_identity_sha256")
    if raw_execution_identity is None:
        if execution_identity_digest is not None:
            raise HarnessAcceptanceError(
                "Harness execution digest exists without an identity"
            )
        execution_identity = None
    else:
        if not isinstance(raw_execution_identity, Mapping):
            raise HarnessAcceptanceError(
                "Harness execution identity must be an object"
            )
        try:
            execution_identity = validate_research_execution_identity(
                raw_execution_identity
            )
            expected_execution_digest = research_execution_identity_digest(
                execution_identity
            )
        except ResearchCapabilityError as error:
            raise HarnessAcceptanceError(
                f"Harness execution identity is invalid: {error}"
            ) from error
        if (
            not isinstance(execution_identity_digest, str)
            or not _SHA256.fullmatch(execution_identity_digest)
            or execution_identity_digest != expected_execution_digest
        ):
            raise HarnessAcceptanceError(
                "Harness execution identity digest is invalid"
            )
        toolchain = execution_identity["toolchain"]
        if (
            toolchain["research_tools_sha256"]
            != implementation["research_tools_sha256"]
            or toolchain["research_output_sha256"]
            != implementation["research_output_sha256"]
        ):
            raise HarnessAcceptanceError(
                "Harness execution toolchain differs from implementation"
            )
        implementation_files = {
            item["path"]: item["sha256"]
            for item in execution_identity["implementation"]["files"]
        }
        repeated_implementation_files = {
            "validator_sha256": (
                "skill_evolution/research_harness_acceptance.py"
            ),
            "runtime_sha256": (
                "skill_evolution/research_agent_runtime.py"
            ),
            "research_tools_sha256": "extensions/research-tools.ts",
            "research_output_sha256": "extensions/research-output.ts",
            "driver_sha256": "extensions/research-harness-driver.ts",
        }
        if any(
            implementation[field] != implementation_files[path]
            for field, path in repeated_implementation_files.items()
        ):
            raise HarnessAcceptanceError(
                "Harness implementation summary differs from execution identity"
            )

    sandbox = value.get("sandbox")
    if not isinstance(sandbox, Mapping):
        raise HarnessAcceptanceError("Harness sandbox identity must be an object")
    _require_fields(
        sandbox,
        {
            "backend",
            "image",
            "image_id",
            "preflight_available",
            "limits",
            "limits_sha256",
            "active_config",
            "active_config_sha256",
        },
        "Harness sandbox identity",
    )
    if sandbox.get("backend") != RESEARCH_SANDBOX_BACKEND:
        raise HarnessAcceptanceError("Harness used an unsupported sandbox backend")
    if not isinstance(sandbox.get("image"), str) or not sandbox["image"]:
        raise HarnessAcceptanceError("Harness sandbox image is invalid")
    image_id = sandbox.get("image_id")
    if image_id is not None and (
        not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id)
    ):
        raise HarnessAcceptanceError("Harness sandbox image id is invalid")
    if not isinstance(sandbox.get("preflight_available"), bool):
        raise HarnessAcceptanceError("Harness sandbox preflight fact is invalid")
    limits = sandbox.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != {
        "cpus",
        "memory",
        "pids",
        "open_files",
        "work_bytes",
        "temporary_bytes",
        "command_timeout_seconds",
        "max_output_bytes",
        "max_tool_calls",
        "max_concurrent_tool_calls",
        "max_total_output_bytes",
        "max_total_command_milliseconds",
    }:
        raise HarnessAcceptanceError("Harness sandbox limits are invalid")
    cpus = limits.get("cpus")
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, (int, float))
        or cpus <= 0
        or not isinstance(limits.get("memory"), str)
        or not limits["memory"]
    ):
        raise HarnessAcceptanceError("Harness sandbox limit values are invalid")
    for field in (
        "pids",
        "open_files",
        "work_bytes",
        "temporary_bytes",
        "command_timeout_seconds",
        "max_output_bytes",
        "max_tool_calls",
        "max_concurrent_tool_calls",
        "max_total_output_bytes",
        "max_total_command_milliseconds",
    ):
        item = limits.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise HarnessAcceptanceError(
                f"Harness sandbox limit {field} is invalid"
            )
    limits_digest = sandbox.get("limits_sha256")
    if (
        not isinstance(limits_digest, str)
        or not _SHA256.fullmatch(limits_digest)
        or hashlib.sha256(_canonical_bytes(limits)).hexdigest()
        != limits_digest
    ):
        raise HarnessAcceptanceError("Harness sandbox limits digest is invalid")
    active = sandbox.get("active_config")
    active_digest = sandbox.get("active_config_sha256")
    if active is None:
        if active_digest is not None:
            raise HarnessAcceptanceError(
                "Harness active Docker digest exists without configuration"
            )
    else:
        if not isinstance(active, Mapping) or image_id is None:
            raise HarnessAcceptanceError("Harness active Docker config is invalid")
        if (
            not isinstance(active_digest, str)
            or not _SHA256.fullmatch(active_digest)
            or hashlib.sha256(_canonical_bytes(active)).hexdigest()
            != active_digest
        ):
            raise HarnessAcceptanceError(
                "Harness active Docker config digest is invalid"
            )
        _validate_active_config(active, image_id=image_id, limits=limits)

    audit = value.get("audit")
    if not isinstance(audit, Mapping):
        raise HarnessAcceptanceError("Harness audit identity must be an object")
    _require_fields(
        audit,
        {"directory", "manifest_sha256", "tree_sha256"},
        "Harness audit identity",
    )
    audit_directory = audit.get("directory")
    if (
        not isinstance(audit_directory, str)
        or not audit_directory
        or Path(audit_directory).name != audit_directory
        or audit_directory in {".", ".."}
    ):
        raise HarnessAcceptanceError("Harness audit directory is unsafe")
    for field in ("manifest_sha256", "tree_sha256"):
        item = audit.get(field)
        if not isinstance(item, str) or not _SHA256.fullmatch(item):
            raise HarnessAcceptanceError(f"Harness audit {field} is invalid")

    raw_checks = value.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) != len(HARNESS_CHECKS):
        raise HarnessAcceptanceError("Harness check set is incomplete")
    statuses: list[str] = []
    for expected_name, check in zip(HARNESS_CHECKS, raw_checks, strict=True):
        if not isinstance(check, Mapping):
            raise HarnessAcceptanceError("Harness check must be an object")
        _require_fields(
            check,
            {"name", "status", "subchecks", "evidence", "error"},
            "Harness check",
        )
        if check.get("name") != expected_name:
            raise HarnessAcceptanceError("Harness checks are not in the fixed order")
        status = check.get("status")
        if status not in {"passed", "failed"}:
            raise HarnessAcceptanceError("Harness check status is invalid")
        statuses.append(str(status))
        subchecks = check.get("subchecks")
        expected_subchecks = HARNESS_SUBCHECKS[expected_name]
        if not isinstance(subchecks, list) or len(subchecks) != len(
            expected_subchecks
        ):
            raise HarnessAcceptanceError("Harness subcheck set is incomplete")
        subcheck_statuses: list[str] = []
        for expected_subcheck, subcheck in zip(
            expected_subchecks, subchecks, strict=True
        ):
            if not isinstance(subcheck, Mapping):
                raise HarnessAcceptanceError("Harness subcheck is invalid")
            _require_fields(
                subcheck,
                {"name", "status", "evidence_sha256", "error"},
                "Harness subcheck",
            )
            if subcheck.get("name") != expected_subcheck:
                raise HarnessAcceptanceError(
                    "Harness subchecks are not in the fixed order"
                )
            sub_status = subcheck.get("status")
            if sub_status not in {"passed", "failed", "not_run"}:
                raise HarnessAcceptanceError("Harness subcheck status is invalid")
            subcheck_statuses.append(str(sub_status))
            sub_digest = subcheck.get("evidence_sha256")
            if sub_status == "passed" and (
                not isinstance(sub_digest, str)
                or not _SHA256.fullmatch(sub_digest)
            ):
                raise HarnessAcceptanceError(
                    "Passed Harness subcheck lacks evidence identity"
                )
            if sub_status != "passed" and sub_digest is not None:
                raise HarnessAcceptanceError(
                    "Non-passed Harness subcheck cannot claim evidence"
                )
            sub_error = subcheck.get("error")
            if sub_status == "failed" and (
                not isinstance(sub_error, str) or not sub_error
            ):
                raise HarnessAcceptanceError(
                    "Failed Harness subcheck needs an error"
                )
            if sub_status != "failed" and sub_error is not None:
                raise HarnessAcceptanceError(
                    "Only failed Harness subchecks may carry errors"
                )
        if status == "passed" and not all(
            item == "passed" for item in subcheck_statuses
        ):
            raise HarnessAcceptanceError(
                "Passed Harness check has an incomplete subcheck"
            )
        if status == "failed" and not any(
            item == "failed" for item in subcheck_statuses
        ):
            raise HarnessAcceptanceError(
                "Failed Harness check lacks a failed subcheck"
            )
        evidence = check.get("evidence")
        if (
            not isinstance(evidence, list)
            or len(evidence) != len(subchecks)
        ):
            raise HarnessAcceptanceError(
                "Harness check needs one evidence fact per subcheck"
            )
        for fact in evidence:
            if not isinstance(fact, Mapping):
                raise HarnessAcceptanceError("Harness check evidence is invalid")
            _require_fields(
                fact, {"kind", "assertion", "observed", "sha256"}, "Harness evidence"
            )
            if fact.get("kind") != "observed_fact" or not all(
                isinstance(fact.get(field), str) and fact.get(field)
                for field in ("assertion", "observed")
            ):
                raise HarnessAcceptanceError("Harness evidence fact is invalid")
            digest = fact.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            ):
                raise HarnessAcceptanceError("Harness evidence digest is invalid")
        if status == "passed":
            for subcheck, fact in zip(subchecks, evidence, strict=True):
                evidence_sha256 = hashlib.sha256(
                    _canonical_bytes({"evidence": [fact]})
                ).hexdigest()
                if subcheck.get("evidence_sha256") != evidence_sha256:
                    raise HarnessAcceptanceError(
                        "Harness subcheck evidence identity differs from its fact"
                    )
        error = check.get("error")
        if status == "passed" and error is not None:
            raise HarnessAcceptanceError("A passed Harness check cannot have an error")
        if status == "failed" and (not isinstance(error, str) or not error):
            raise HarnessAcceptanceError("A failed Harness check needs an error")
    expected_status = (
        "passed" if all(item == "passed" for item in statuses) else "failed"
    )
    if value.get("status") != expected_status:
        raise HarnessAcceptanceError("Harness overall status differs from fixed checks")
    if expected_status == "passed":
        if (
            not corpus.get("corpus_id")
            or not isinstance(corpus.get("content_sha256"), str)
            or not isinstance(corpus.get("baseline_sha256"), str)
            or len(execution_ids) < 2
        ):
            raise HarnessAcceptanceError("Passed Harness report lacks corpus identity")
        if (
            not sandbox.get("preflight_available")
            or image_id is None
            or active is None
        ):
            raise HarnessAcceptanceError("Passed Harness report lacks sandbox identity")
        if execution_identity is None:
            raise HarnessAcceptanceError(
                "Passed Harness report lacks execution identity"
            )
        execution_sandbox = execution_identity["sandbox"]
        if execution_sandbox != {
            "backend": sandbox["backend"],
            "image": sandbox["image"],
            "image_id": image_id,
            "limits": dict(limits),
            "control_plane": execution_identity["sandbox"][
                "control_plane"
            ],
        }:
            raise HarnessAcceptanceError(
                "Harness execution identity differs from its sandbox"
            )
    return dict(value)


def _verify_audit_bundle(
    report_boundary: _OutputBoundary,
    report: Mapping[str, Any],
) -> None:
    """Rehash one pinned audit tree without path-based file reopen races."""

    audit_identity = report["audit"]
    assert isinstance(audit_identity, Mapping)
    audit_name = str(audit_identity["directory"])
    parent_fd = _open_pinned_output_parent(report_boundary)
    audit_fd: int | None = None
    try:
        try:
            audit_fd = os.open(
                audit_name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise HarnessAcceptanceError(
                f"Harness audit directory is missing or unsafe: {error}"
            ) from error
        audit_metadata = os.fstat(audit_fd)
        manifest_bytes, _ = _read_regular_file_at(
            audit_fd,
            "manifest.json",
            label="Harness audit manifest",
        )
        if (
            hashlib.sha256(manifest_bytes).hexdigest()
            != audit_identity["manifest_sha256"]
        ):
            raise HarnessAcceptanceError(
                "Harness audit manifest digest differs"
            )
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HarnessAcceptanceError(
                f"Harness audit manifest is unreadable: {error}"
            ) from error
        if not isinstance(manifest, Mapping):
            raise HarnessAcceptanceError("Harness audit manifest is invalid")
        _require_fields(
            manifest,
            {"schema", "driver_protocol_version", "tree_sha256", "files"},
            "Harness audit manifest",
        )
        if (
            manifest.get("schema")
            != "research.harness_audit_manifest.v1"
            or manifest.get("driver_protocol_version")
            != HARNESS_DRIVER_PROTOCOL_VERSION
            or manifest.get("tree_sha256")
            != audit_identity["tree_sha256"]
        ):
            raise HarnessAcceptanceError(
                "Harness audit manifest identity differs"
            )
        declared_files = manifest.get("files")
        if not isinstance(declared_files, list):
            raise HarnessAcceptanceError(
                "Harness audit file inventory is invalid"
            )
        seen: set[str] = set()
        for item in declared_files:
            if not isinstance(item, Mapping):
                raise HarnessAcceptanceError(
                    "Harness audit file entry is invalid"
                )
            _require_fields(
                item,
                {"path", "bytes", "sha256"},
                "Harness audit file",
            )
            relative = item.get("path")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or relative in seen
            ):
                raise HarnessAcceptanceError(
                    "Harness audit file path is unsafe"
                )
            seen.add(relative)
            size = item.get("bytes")
            digest = item.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
            ):
                raise HarnessAcceptanceError(
                    "Harness audit file identity is invalid"
                )
        observed_files, observed_contents = _audit_inventory_at(audit_fd)
        if observed_files != declared_files:
            raise HarnessAcceptanceError(
                "Harness audit file inventory differs"
            )
        tree_sha256 = hashlib.sha256(
            _canonical_bytes({"files": observed_files})
        ).hexdigest()
        if tree_sha256 != audit_identity["tree_sha256"]:
            raise HarnessAcceptanceError("Harness audit tree digest differs")
        implementation = report["implementation"]
        assert isinstance(implementation, Mapping)
        snapshots = {
            "validator_sha256": "research-harness-acceptance.py",
            "runtime_sha256": "research-agent-runtime.py",
            "research_tools_sha256": "research-tools.ts",
            "research_output_sha256": "research-output.ts",
            "driver_sha256": "research-harness-driver.ts",
        }
        for field, name in snapshots.items():
            content = observed_contents.get(f"implementation/{name}")
            if content is None:
                raise HarnessAcceptanceError(
                    f"Harness implementation snapshot is missing: {name}"
                )
            if hashlib.sha256(content).hexdigest() != implementation[field]:
                raise HarnessAcceptanceError(
                    f"Harness implementation snapshot differs: {name}"
                )
        execution_identity = report.get("execution_identity")
        if execution_identity is not None:
            identity_bytes = observed_contents.get("execution-identity.json")
            if identity_bytes is None:
                raise HarnessAcceptanceError(
                    "Harness execution identity snapshot is missing"
                )
            try:
                archived_identity = json.loads(identity_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HarnessAcceptanceError(
                    "Harness execution identity snapshot is unreadable: "
                    f"{error}"
                ) from error
            if archived_identity != execution_identity:
                raise HarnessAcceptanceError(
                    "Harness execution identity snapshot differs"
                )
        if report.get("status") == "passed":
            required = {
                "command-plan.json",
                "execution-identity.json",
                "pi-positive/trajectory.jsonl",
                "pi-budget/trajectory.jsonl",
                "pi-cleanup/trajectory.jsonl",
                "pi-duplicate_submission/trajectory.jsonl",
                "pi-post_submission/trajectory.jsonl",
            }
            if not required.issubset(seen):
                raise HarnessAcceptanceError(
                    "Passed Harness audit lacks a required replay record"
                )
        current = os.stat(
            audit_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (
            audit_metadata.st_dev,
            audit_metadata.st_ino,
        ):
            raise HarnessAcceptanceError(
                "Harness audit directory changed during verification"
            )
    except OSError as error:
        raise HarnessAcceptanceError(
            f"Harness audit directory changed during verification: {error}"
        ) from error
    finally:
        if audit_fd is not None:
            os.close(audit_fd)
        os.close(parent_fd)


def load_harness_acceptance_report(
    report_path: str | os.PathLike[str],
    *,
    expected_file_sha256: str | None = None,
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
    expected_image_id: str | None = None,
    expected_limits_sha256: str | None = None,
    corpus_directory: str | os.PathLike[str] | None = None,
    trusted_output_root: str | os.PathLike[str] | None = None,
    require_passed: bool = False,
) -> HarnessAcceptanceVerification:
    """Reload a sealed report and optionally reverify all bound identities."""

    raw_path = Path(report_path)
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    if trusted_output_root is None:
        try:
            implicit_root = raw_path.parent.resolve(strict=True)
        except OSError as error:
            raise HarnessAcceptanceError(
                f"Harness report parent is unavailable: {error}"
            ) from error
        raw_path = implicit_root / raw_path.name
        trusted_output_root = implicit_root
    boundary = _existing_output_destination(
        raw_path,
        trusted_directory=trusted_output_root,
    )
    parent_fd = _open_pinned_output_parent(boundary)
    try:
        report_bytes, report_metadata = _read_regular_file_at(
            parent_fd,
            boundary.destination.name,
            label="Harness acceptance report",
        )
    finally:
        os.close(parent_fd)
    file_digest = hashlib.sha256(report_bytes).hexdigest()
    if expected_file_sha256 is not None and file_digest != expected_file_sha256:
        raise HarnessAcceptanceError("Harness acceptance report file digest differs")
    try:
        value = json.loads(report_bytes)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        StorageError,
    ) as error:
        raise HarnessAcceptanceError(
            f"Harness acceptance report is unreadable: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise HarnessAcceptanceError("Harness acceptance report must be an object")
    report = validate_harness_acceptance_report(value)
    _verify_audit_bundle(boundary, report)
    execution_identity = report.get("execution_identity")
    if execution_identity is not None:
        try:
            current_identity = validate_research_execution_identity(
                execution_identity,
                repository_root=Path(__file__).resolve().parent.parent,
                verify_pi_executable=True,
            )
            current_digest = research_execution_identity_digest(
                current_identity,
                repository_root=Path(__file__).resolve().parent.parent,
                verify_pi_executable=True,
            )
        except ResearchCapabilityError as error:
            raise HarnessAcceptanceError(
                f"Harness execution identity is no longer current: {error}"
            ) from error
        if current_digest != report.get("execution_identity_sha256"):
            raise HarnessAcceptanceError(
                "Harness execution identity changed after acceptance"
            )
    identity = report["corpus"]
    sandbox = report["sandbox"]
    if (
        expected_corpus_digest is not None
        and identity["content_sha256"] != expected_corpus_digest
    ):
        raise HarnessAcceptanceError("Harness report belongs to a different corpus")
    if (
        expected_baseline_digest is not None
        and identity["baseline_sha256"] != expected_baseline_digest
    ):
        raise HarnessAcceptanceError("Harness report belongs to a different baseline")
    if expected_image_id is not None and sandbox["image_id"] != expected_image_id:
        raise HarnessAcceptanceError(
            "Harness report belongs to a different sandbox image"
        )
    if expected_limits_sha256 is not None:
        if not _SHA256.fullmatch(expected_limits_sha256):
            raise HarnessAcceptanceError(
                "Expected Harness sandbox limits digest is invalid"
            )
        if sandbox["limits_sha256"] != expected_limits_sha256:
            raise HarnessAcceptanceError(
                "Harness report belongs to different sandbox limits"
            )
    if corpus_directory is not None:
        verified = verify_research_corpus(
            corpus_directory,
            expected_content_sha256=identity["content_sha256"],
            expected_baseline_sha256=identity["baseline_sha256"],
        )
        if list(verified.execution_ids) != identity["execution_ids"]:
            raise HarnessAcceptanceError("Harness report Trajectory identities changed")
    if require_passed and report["status"] != "passed":
        raise HarnessAcceptanceError("Harness acceptance did not pass")
    _assert_output_file_identity(boundary, report_metadata)
    return HarnessAcceptanceVerification(
        boundary.destination,
        report,
        file_digest,
    )


def verify_harness_acceptance_report(
    report_path: str | os.PathLike[str],
    *,
    expected_file_sha256: str,
    trusted_output_root: str | os.PathLike[str],
    corpus_directory: str | os.PathLike[str],
    expected_corpus_digest: str,
    expected_baseline_digest: str,
    expected_image_id: str | None = None,
    expected_limits_sha256: str | None = None,
    require_passed: bool = True,
) -> HarnessAcceptanceVerification:
    """Reverify a workflow-bound report, corpus tree, and optional image id."""

    if not _SHA256.fullmatch(expected_file_sha256):
        raise HarnessAcceptanceError("Expected Harness report digest is invalid")
    return load_harness_acceptance_report(
        report_path,
        expected_file_sha256=expected_file_sha256,
        expected_corpus_digest=expected_corpus_digest,
        expected_baseline_digest=expected_baseline_digest,
        expected_image_id=expected_image_id,
        expected_limits_sha256=expected_limits_sha256,
        corpus_directory=corpus_directory,
        trusted_output_root=trusted_output_root,
        require_passed=require_passed,
    )
