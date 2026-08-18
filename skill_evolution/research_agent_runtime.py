"""Run evidence-backed multi-Trajectory specialists inside the research Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator, Protocol
import uuid

from scripts.pi_rpc import (
    PiRequestTimeoutError,
    PiRpcClient,
    PiRpcError,
)
from scripts.prompt_approval import ApprovedPrompt, load_approved_prompt
from scripts.trajectory_spike import TrajectoryJournal
from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AgentRole,
    AgentRunRepository,
    AgentRunResult,
    AgentSpec,
    ModelConfiguration,
)
from skill_evolution.evidence import sanitize_for_evidence
from skill_evolution.research_capability import (
    RESEARCH_PI_TOOL_ALLOWLIST,
    ResearchCapabilityError,
    attest_pi_execution_identity,
    build_research_capability_identity,
    build_research_execution_identity,
    file_sha256,
    pi_execution_environment,
    read_selected_pi_api_key,
    research_execution_identity_digest,
    resolve_research_pi_agent_directory,
    validate_research_execution_identity,
    validate_selected_pi_credential,
    verify_pi_execution_identity_current,
)
from skill_evolution.research_corpus import (
    ResearchCorpusVerification,
    verify_research_corpus,
)
from skill_evolution.research_results import (
    validate_error_identification,
    validate_error_identification_evidence,
    validate_error_report,
    validate_error_report_evidence,
    validate_research_result,
    validate_research_result_evidence,
)
from skill_evolution.research_sandbox import (
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxError,
    TreeDigest,
    research_evidence_tree_digest,
    validate_research_sandbox_context,
)
from skill_evolution.storage import JsonObject, atomic_write_json


RESEARCH_LAB_PROFILE = "multi_trajectory_research"
RESEARCH_SUBMISSION_TOOL = "submit_multi_trajectory_research"
ERROR_IDENTIFICATION_SUBMISSION_TOOL = "submit_error_identification"
ERROR_REPORT_SUBMISSION_TOOL = "submit_error_report"
_RESEARCH_HARNESS_EXTENSION_PATHS = {
    "research_tools": Path("extensions/research-tools.ts"),
    "research_output": Path("extensions/research-output.ts"),
    "research_harness_driver": Path(
        "extensions/research-harness-driver.ts"
    ),
}
RESEARCH_EXEC_TOOL = "research_exec"
RESEARCH_PROMPT_DATA_SCHEMA = "research.prompt_data.v1"
RESEARCH_HARNESS_CONTEXT_SCHEMA = "prompt.research_harness_context.v1"
RESEARCH_HARNESS_FAUX_PROVIDER = "research-harness-faux"
RESEARCH_HARNESS_FAUX_MODEL = "research-harness-driver-v1"
BEHAVIOR_RESEARCH_PROMPT_ID = "analysis.behavior-pattern-research"
RESEARCH_HARNESS_CONTEXT_PROMPT_ID = "analysis.research-harness-context"
MAX_RESEARCH_PROMPT_DATA_BYTES = 256 * 1024
MAX_RESEARCH_SUBMISSION_BYTES = 2 * 1024 * 1024
MAX_RESEARCH_AUDIT_TEXT_BYTES = 64 * 1024
_PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)([^\r\n,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)")
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_AUDIT_HIDDEN_KEYS = {
    "analysis",
    "analysis_content",
    "reasoning",
    "reasoning_content",
    "thinking",
    "thinking_content",
}
_AUDIT_SECRET_KEYS = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "bearer_token",
    "credential",
    "credentials",
}
_RESEARCH_ENVIRONMENT_NAMES = frozenset(
    {
        "SKILL_EVOLUTION_DOCKER_COMMAND",
        "SKILL_EVOLUTION_HARNESS_BASELINE_DIGEST",
        "SKILL_EVOLUTION_HARNESS_CORPUS_DIGEST",
        "SKILL_EVOLUTION_HARNESS_DRIVER_MODE",
        "SKILL_EVOLUTION_HARNESS_EXECUTION_IDS",
        "SKILL_EVOLUTION_HARNESS_TRAJECTORY_FILENAME",
        "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS",
        "SKILL_EVOLUTION_RESEARCH_CONCURRENT_STATE",
        "SKILL_EVOLUTION_RESEARCH_CONTAINER",
        "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS",
        "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES",
    }
)


class ResearchAgentRuntimeError(RuntimeError):
    """Raised when a research Agent cannot be started safely."""


class ResearchSandboxBackend(Protocol):
    """Minimal isolated-lab boundary required by the research runtime."""

    name: str

    def preflight(self) -> Any:
        """Return an object with availability and immutable image facts."""

    def isolated_run(
        self,
        *,
        evidence_directory: str | os.PathLike[str],
        work_archive_directory: str | os.PathLike[str],
        expected_evidence_digest: TreeDigest,
        expected_control_plane_identity: Mapping[str, Any],
    ) -> Any:
        """Yield one sandbox context and seal it on context exit."""


@dataclass(frozen=True)
class ResearchPrompt:
    """Approved static protocol plus bounded, archived corpus navigation data."""

    dynamic_data: JsonObject
    dynamic_text: str
    rendered_text: str
    rendered_sha256: str


@dataclass(frozen=True)
class ApprovedResearchHarnessContext:
    """Owner-approved prompt-visible tool sources used by one AgentRun."""

    approval: ApprovedPrompt
    version: str
    tool_schema_version: str
    research_tools_bytes: bytes
    research_tools_sha256: str
    research_output_bytes: bytes
    research_output_sha256: str
    context_sha256: str


@dataclass
class _DriveResult:
    """Mutable terminal observations returned while the sandbox is still live."""

    status: str = "failed"
    result: JsonObject | None = None
    error: JsonObject | None = None
    parse_failure: JsonObject | None = None
    settled: bool = False
    timeout_uncertain: bool = False
    session_id: str | None = None


@dataclass(frozen=True)
class _IsolatedPiConfiguration:
    """Ephemeral non-secret Pi configuration plus an optional read-only auth FD."""

    agent_directory: Path
    home_directory: Path
    temporary_directory: Path
    pass_fds: tuple[int, ...]


@contextmanager
def _isolated_pi_configuration(
    *,
    source_agent_directory: Path,
    provider: str,
) -> Iterator[_IsolatedPiConfiguration]:
    """Expose only certified model facts and a read-only selected credential."""

    temporary = tempfile.TemporaryDirectory(prefix="skill-evolution-pi-")
    agent_directory = Path(temporary.name)
    home_directory = agent_directory / "home"
    temporary_directory = agent_directory / "tmp"
    auth_snapshot: Any | None = None
    try:
        agent_directory.chmod(0o700)
        home_directory.mkdir(mode=0o500)
        temporary_directory.mkdir(mode=0o700)
        if provider != RESEARCH_HARNESS_FAUX_PROVIDER:
            if not Path("/dev/fd").is_dir():
                raise ResearchAgentRuntimeError(
                    "This host cannot expose a read-only Pi credential bridge"
                )
            auth_source = source_agent_directory / "auth.json"
            flags = os.O_RDONLY
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            source_descriptor = os.open(auth_source, flags)
            try:
                key = read_selected_pi_api_key(
                    source_descriptor,
                    provider=provider,
                )
            finally:
                os.close(source_descriptor)
            auth_snapshot = tempfile.TemporaryFile(
                mode="w+b",
                dir=temporary_directory,
            )
            auth_snapshot.write(
                json.dumps(
                    {
                        provider: {
                            "type": "api_key",
                            "key": key,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            auth_snapshot.flush()
            os.fsync(auth_snapshot.fileno())
            os.fchmod(auth_snapshot.fileno(), 0o400)
            auth_snapshot.seek(0)
            auth_descriptor = auth_snapshot.fileno()
            os.symlink(
                f"/dev/fd/{auth_descriptor}",
                agent_directory / "auth.json",
            )
        yield _IsolatedPiConfiguration(
            agent_directory=agent_directory,
            home_directory=home_directory,
            temporary_directory=temporary_directory,
            pass_fds=(
                (auth_snapshot.fileno(),) if auth_snapshot is not None else ()
            ),
        )
    except ResearchCapabilityError as error:
        raise ResearchAgentRuntimeError(str(error)) from error
    finally:
        if auth_snapshot is not None:
            auth_snapshot.close()
        try:
            agent_directory.chmod(0o700)
            home_directory.chmod(0o700)
            temporary_directory.chmod(0o700)
        except OSError:
            pass
        temporary.cleanup()


def _research_pi_environment(
    pi_identity: Mapping[str, Any],
    runtime_environment: Mapping[str, str],
    isolated: _IsolatedPiConfiguration,
) -> dict[str, str]:
    """Create the complete allowlisted environment for a research Pi process."""

    selected = {
        name: value
        for name, value in runtime_environment.items()
        if name in _RESEARCH_ENVIRONMENT_NAMES
    }
    selected.update(
        {
            "HOME": str(isolated.home_directory),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PI_CODING_AGENT_DIR": str(isolated.agent_directory),
            "TMPDIR": str(isolated.temporary_directory),
        }
    )
    try:
        return pi_execution_environment(pi_identity, selected)
    except ResearchCapabilityError as error:
        raise ResearchAgentRuntimeError(str(error)) from error


def _deterministic_harness_environment(
    sandbox_context: Mapping[str, Any],
    sandbox_identity: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, str]:
    """Bind the no-model driver to one attested active sandbox context."""

    try:
        environment = validate_research_sandbox_context(sandbox_context)
    except ResearchSandboxError as error:
        raise ResearchAgentRuntimeError(str(error)) from error
    for field in ("backend", "image", "image_id", "limits"):
        if sandbox_context.get(field) != sandbox_identity.get(field):
            raise ResearchAgentRuntimeError(
                f"Deterministic Harness sandbox {field} differs from identity"
            )
    if (
        sandbox_context.get("control_plane_identity")
        != sandbox_identity.get("control_plane")
    ):
        raise ResearchAgentRuntimeError(
            "Deterministic Harness control plane differs from identity"
        )
    container_id = sandbox_context.get("container_id")
    if (
        not isinstance(container_id, str)
        or not _CONTAINER_ID_PATTERN.fullmatch(container_id)
    ):
        raise ResearchAgentRuntimeError(
            "Deterministic Harness container identity is invalid"
        )
    control_plane = sandbox_identity["control_plane"]
    resolved_command = control_plane["resolved_command"]
    docker_command = str(resolved_command[0])
    if docker_command != control_plane["executable"]["path"]:
        raise ResearchAgentRuntimeError(
            "Deterministic Harness Docker command identity is inconsistent"
        )
    limits = sandbox_identity["limits"]
    expected_environment = {
        "SKILL_EVOLUTION_RESEARCH_CONTAINER": container_id,
        "SKILL_EVOLUTION_DOCKER_COMMAND": docker_command,
        "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS": str(
            limits["command_timeout_seconds"] * 1000
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES": str(
            limits["max_output_bytes"]
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS": str(
            limits["max_tool_calls"]
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS": str(
            limits["max_concurrent_tool_calls"]
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES": str(
            limits["max_total_output_bytes"]
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS": str(
            limits["max_total_command_milliseconds"]
        ),
    }
    if environment != expected_environment:
        raise ResearchAgentRuntimeError(
            "Deterministic Harness tool environment differs from sandbox"
        )
    if mode == "budget":
        environment["SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS"] = "2"
    elif mode == "cleanup":
        environment["SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS"] = "1000"
        environment["SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES"] = "4096"
    return environment


def _validate_research_workspace_configuration(workspace: Path) -> None:
    """Reject project-local Pi settings or system-prompt overrides."""

    local_configuration = workspace / ".pi"
    if local_configuration.exists() or local_configuration.is_symlink():
        raise ResearchAgentRuntimeError(
            "Research workspace may not contain project-local Pi configuration"
        )


def _validate_fixed_harness_extensions(
    *,
    repository_root: Path,
    research_tools_path: Path,
    research_output_path: Path,
    driver_path: Path,
    expected_sha256: Mapping[str, str],
) -> None:
    """Require the three fixed, identity-bound deterministic extensions."""

    if set(expected_sha256) != set(_RESEARCH_HARNESS_EXTENSION_PATHS):
        raise ResearchAgentRuntimeError(
            "Deterministic Harness extension identity is incomplete"
        )
    observed_paths = {
        "research_tools": Path(research_tools_path),
        "research_output": Path(research_output_path),
        "research_harness_driver": Path(driver_path),
    }
    for name, relative in _RESEARCH_HARNESS_EXTENSION_PATHS.items():
        expected_path = repository_root / relative
        observed_path = observed_paths[name]
        if not observed_path.is_absolute() or observed_path != expected_path:
            raise ResearchAgentRuntimeError(
                "Deterministic Harness may load only fixed repository "
                f"extensions: {name}"
            )
        current = repository_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ResearchAgentRuntimeError(
                    "Deterministic Harness extension path may not contain "
                    f"a symlink: {name}"
                )
        expected_digest = expected_sha256[name]
        if (
            not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            raise ResearchAgentRuntimeError(
                "Deterministic Harness extension digest is invalid"
            )
        try:
            observed_digest = file_sha256(observed_path)
        except ResearchCapabilityError as error:
            raise ResearchAgentRuntimeError(str(error)) from error
        if observed_digest != expected_digest:
            raise ResearchAgentRuntimeError(
                "Deterministic Harness extension changed after identity "
                f"binding: {name}"
            )


def _runtime_attestation_entry(
    entry: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return attestation data from one persisted Pi custom entry."""

    if (
        entry.get("customType") != "research-runtime-attestation"
        or not isinstance(entry.get("data"), Mapping)
    ):
        return None
    return entry["data"]


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    """Atomically write one archived prompt without changing existing inputs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    """Atomically snapshot approved extension bytes for exact replay."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_approved_research_harness_context(
    manifest_path: str | os.PathLike[str],
    *,
    research_tools_path: Path,
    research_output_path: Path,
) -> ApprovedResearchHarnessContext:
    """Bind all prompt-visible extension text to one owner approval."""

    approved = load_approved_prompt(manifest_path)
    try:
        raw = json.loads(approved.text)
    except json.JSONDecodeError as error:
        raise ResearchAgentRuntimeError(
            "Research Harness context is not valid JSON"
        ) from error
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema",
        "title",
        "version",
        "tool_schema_version",
        "prompt_visible_extensions",
    }:
        raise ResearchAgentRuntimeError(
            "Research Harness context fields differ from schema"
        )
    if raw.get("schema") != RESEARCH_HARNESS_CONTEXT_SCHEMA:
        raise ResearchAgentRuntimeError(
            "Unsupported Research Harness context schema"
        )
    _text_value(raw.get("title"), label="context.title")
    extensions = raw.get("prompt_visible_extensions")
    if not isinstance(extensions, list) or len(extensions) != 2:
        raise ResearchAgentRuntimeError(
            "Research Harness context must bind both extensions"
        )
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in extensions:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "file",
            "sha256",
        }:
            raise ResearchAgentRuntimeError(
                "Research Harness extension binding is invalid"
            )
        name = item.get("name")
        if not isinstance(name, str) or name in by_name:
            raise ResearchAgentRuntimeError(
                "Research Harness extension names are invalid"
            )
        by_name[name] = item
    expected = {
        "research_tools": research_tools_path,
        "research_output": research_output_path,
    }
    if set(by_name) != set(expected):
        raise ResearchAgentRuntimeError(
            "Research Harness context extension set changed"
        )
    loaded: dict[str, tuple[bytes, str]] = {}
    for name, path in expected.items():
        binding = by_name[name]
        if binding.get("file") != path.name:
            raise ResearchAgentRuntimeError(
                f"Research Harness context file changed for {name}"
            )
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ResearchAgentRuntimeError(
                f"Research extension cannot be read: {path}"
            ) from error
        digest = _sha256_bytes(content)
        if binding.get("sha256") != digest:
            raise ResearchAgentRuntimeError(
                f"Research extension changed after context approval: {name}"
            )
        loaded[name] = (content, digest)
    return ApprovedResearchHarnessContext(
        approval=approved,
        version=_text_value(raw.get("version"), label="context.version"),
        tool_schema_version=_text_value(
            raw.get("tool_schema_version"),
            label="context.tool_schema_version",
        ),
        research_tools_bytes=loaded["research_tools"][0],
        research_tools_sha256=loaded["research_tools"][1],
        research_output_bytes=loaded["research_output"][0],
        research_output_sha256=loaded["research_output"][1],
        context_sha256=approved.content_sha256,
    )


def _text_value(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchAgentRuntimeError(f"{label} must be non-empty text")
    return value.strip()


def _redact_stderr(value: str) -> str:
    """Keep a bounded diagnostic preview without persisting obvious secrets."""

    redacted = _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", value)
    redacted = _BEARER_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", redacted)
    return redacted[:2000]


def _redact_audit_text(value: str) -> str:
    """Retain bounded tool JSON needed for deterministic semantic replay."""

    redacted = _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", value)
    redacted = _BEARER_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", redacted)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= MAX_RESEARCH_AUDIT_TEXT_BYTES:
        return redacted
    return encoded[:MAX_RESEARCH_AUDIT_TEXT_BYTES].decode(
        "utf-8", errors="ignore"
    )


def _sanitize_research_audit(value: Any) -> Any:
    """Remove hidden model content and inline credentials from audit data."""

    if isinstance(value, Mapping):
        sanitized: JsonObject = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if normalized in _AUDIT_HIDDEN_KEYS:
                sanitized[key] = "[REDACTED_HIDDEN_MODEL_CONTENT]"
            elif normalized in _AUDIT_SECRET_KEYS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_research_audit(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_research_audit(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_research_audit(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, (Mapping, list)):
            value = json.dumps(
                _sanitize_research_audit(decoded),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return _redact_audit_text(value)
    return value


def _safe_rpc_observer(journal: TrajectoryJournal):
    """Return an observer that removes hidden reasoning and credential fields."""

    def observe(
        direction: str,
        raw: str,
        parsed: JsonObject | None,
        parse_error: str | None,
    ) -> None:
        if parsed is None:
            journal.record_rpc(
                direction,
                "[UNPARSEABLE_RPC_RECORD_REDACTED]",
                None,
                parse_error,
            )
            return
        sanitized = _sanitize_research_audit(sanitize_for_evidence(parsed))
        if not isinstance(sanitized, Mapping):
            return
        journal.record_rpc(
            direction,
            "[NORMALIZED_RPC_RECORD]",
            dict(sanitized),
            parse_error,
        )

    return observe


def _safe_stderr_observer(journal: TrajectoryJournal):
    """Return a bounded stderr observer for the research audit trail."""

    def observe(line: str) -> None:
        journal.append(
            source="pi_process",
            record_type="process_stderr",
            payload={
                "line_sha256": _sha256_text(line),
                "preview": _redact_stderr(line),
                "truncated": len(line) > 2000,
            },
        )

    return observe


def render_research_prompt(
    approved: ApprovedPrompt,
    verification: ResearchCorpusVerification,
    *,
    max_dynamic_bytes: int = MAX_RESEARCH_PROMPT_DATA_BYTES,
    extra_data: Mapping[str, Any] | None = None,
) -> ResearchPrompt:
    """Append only a bounded navigation map and immutable identities."""

    if max_dynamic_bytes <= 0:
        raise ValueError("max_dynamic_bytes must be positive")
    dynamic_data: JsonObject = {
        "schema": RESEARCH_PROMPT_DATA_SCHEMA,
        "corpus_digest": verification.content_sha256,
        "baseline_digest": verification.baseline_sha256,
        "eligible_trajectory_ids": list(verification.execution_ids),
        "corpus_map": verification.corpus_map,
    }
    if extra_data is not None:
        if not isinstance(extra_data, Mapping):
            raise ValueError("extra_data must be a mapping")
        dynamic_data = {**dynamic_data, **dict(extra_data)}
    encoded = _json_bytes(dynamic_data)
    if len(encoded) > max_dynamic_bytes:
        raise ResearchAgentRuntimeError(
            "The corpus map exceeds the approved prompt-data limit"
        )
    dynamic_text = json.dumps(
        dynamic_data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    # Preserve valid JSON while preventing untrusted strings from terminating
    # the explicit data boundary in the rendered prompt.
    dynamic_text = (
        dynamic_text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    rendered = (
        approved.text.rstrip()
        + "\n\n"
        + "<untrusted-research-corpus-map>\n"
        + dynamic_text
        + "\n</untrusted-research-corpus-map>\n"
    )
    return ResearchPrompt(
        dynamic_data=dynamic_data,
        dynamic_text=dynamic_text,
        rendered_text=rendered,
        rendered_sha256=_sha256_text(rendered),
    )


def _validate_context(
    context: Mapping[str, Any],
    verification: ResearchCorpusVerification,
) -> None:
    expected_values = {
        "corpus_digest": verification.content_sha256,
        "baseline_digest": verification.baseline_sha256,
    }
    for field, expected in expected_values.items():
        if context.get(field) != expected:
            raise ResearchAgentRuntimeError(
                f"Research context {field} differs from frozen evidence"
            )
    eligible = context.get("eligible_trajectory_ids")
    if (
        not isinstance(eligible, list)
        or not all(isinstance(item, str) and item for item in eligible)
        or len(eligible) != len(set(eligible))
        or set(eligible) != set(verification.execution_ids)
    ):
        raise ResearchAgentRuntimeError(
            "Research context must declare the complete corpus denominator"
        )


_RESEARCH_SUBMISSION_TOOLS = {
    AgentRole.BEHAVIOR_PATTERN: RESEARCH_SUBMISSION_TOOL,
    AgentRole.CONDITIONS_COVERAGE: RESEARCH_SUBMISSION_TOOL,
    AgentRole.OUTCOME_CONSISTENCY: RESEARCH_SUBMISSION_TOOL,
    AgentRole.RESOURCE_EFFICIENCY: RESEARCH_SUBMISSION_TOOL,
    AgentRole.ERROR_IDENTIFIER: ERROR_IDENTIFICATION_SUBMISSION_TOOL,
    AgentRole.ERROR_ANALYST: ERROR_REPORT_SUBMISSION_TOOL,
}


def _validate_spec(spec: AgentSpec) -> None:
    expected_tool = _RESEARCH_SUBMISSION_TOOLS.get(spec.role)
    if expected_tool is None:
        raise ResearchAgentRuntimeError(
            "Research runtime accepts only research specialist and "
            "error-analysis roles"
        )
    if spec.tool_mode != "read_only":
        raise ResearchAgentRuntimeError("Research specialists are read-only")
    if spec.research_lab_profile != RESEARCH_LAB_PROFILE:
        raise ResearchAgentRuntimeError(
            "AgentSpec does not select the multi-Trajectory research lab"
        )
    if spec.submission_tool != expected_tool:
        raise ResearchAgentRuntimeError(
            "AgentSpec does not require the expected research submission tool"
        )


class ResearchPiAgentRuntime:
    """Run one specialist with a verified corpus and no host execution fallback."""

    def __init__(
        self,
        *,
        agent_runs_root: str | os.PathLike[str],
        research_extension_path: str | os.PathLike[str],
        research_output_extension_path: str | os.PathLike[str],
        research_harness_context_path: str | os.PathLike[str],
        sandbox: ResearchSandboxBackend,
        model: ModelConfiguration | None = None,
        pi_command: Sequence[str] | str | None = None,
        extra_pi_args: Sequence[str] = (),
        abort_wait_seconds: float = 3.0,
        max_prompt_data_bytes: int = MAX_RESEARCH_PROMPT_DATA_BYTES,
        max_submission_bytes: int = MAX_RESEARCH_SUBMISSION_BYTES,
        repository_root: str | os.PathLike[str] | None = None,
        pi_agent_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        if abort_wait_seconds <= 0:
            raise ValueError("abort_wait_seconds must be positive")
        if max_prompt_data_bytes <= 0 or max_submission_bytes <= 0:
            raise ValueError("Research output limits must be positive")
        self.runs = AgentRunRepository(agent_runs_root)
        self.research_extension_path = Path(
            research_extension_path
        ).resolve()
        self.research_output_extension_path = Path(
            research_output_extension_path
        ).resolve()
        self.research_harness_context_path = Path(
            research_harness_context_path
        ).resolve()
        self.sandbox = sandbox
        self.model = model or ModelConfiguration.from_project_configuration()
        self.pi_command = pi_command
        self.extra_pi_args = tuple(extra_pi_args)
        self.abort_wait_seconds = abort_wait_seconds
        self.max_prompt_data_bytes = max_prompt_data_bytes
        self.max_submission_bytes = max_submission_bytes
        self.repository_root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else Path(__file__).resolve().parent.parent
        )
        self.pi_agent_directory = resolve_research_pi_agent_directory(
            pi_agent_directory
        )

    def preflight(self, specs: Sequence[AgentSpec]) -> None:
        """Check every approval, extension, and mandatory sandbox before a run."""

        self._preflight_details(specs)

    def _preflight_details(
        self,
        specs: Sequence[AgentSpec],
    ) -> tuple[
        dict[AgentRole, ApprovedPrompt],
        ApprovedResearchHarnessContext,
        Any,
    ]:
        """Return the exact approved and isolated boundaries after preflight."""

        if not specs:
            raise ResearchAgentRuntimeError(
                "Research preflight requires at least one AgentSpec"
            )
        if not self.research_extension_path.is_file():
            raise ResearchAgentRuntimeError(
                "Research tool extension does not exist: "
                f"{self.research_extension_path}"
            )
        if not self.research_output_extension_path.is_file():
            raise ResearchAgentRuntimeError(
                "Research output extension does not exist: "
                f"{self.research_output_extension_path}"
            )
        harness_context = load_approved_research_harness_context(
            self.research_harness_context_path,
            research_tools_path=self.research_extension_path,
            research_output_path=self.research_output_extension_path,
        )
        if self.model.provider != RESEARCH_HARNESS_FAUX_PROVIDER:
            try:
                validate_selected_pi_credential(
                    self.pi_agent_directory,
                    provider=self.model.provider,
                )
            except ResearchCapabilityError as error:
                raise ResearchAgentRuntimeError(str(error)) from error
        approved_prompts: dict[AgentRole, ApprovedPrompt] = {}
        for spec in specs:
            _validate_spec(spec)
            approved = load_approved_prompt(spec.prompt_path)
            if _PLACEHOLDER_PATTERN.search(approved.text):
                raise ResearchAgentRuntimeError(
                    f"Research prompt has an unresolved placeholder: "
                    f"{spec.prompt_path}"
                )
            approved_prompts[spec.role] = approved
        result = self.sandbox.preflight()
        if (
            not getattr(result, "available", False)
            or getattr(result, "backend", None) != RESEARCH_SANDBOX_BACKEND
            or not getattr(result, "image_id", None)
            or not isinstance(
                getattr(result, "control_plane_identity", None),
                Mapping,
            )
        ):
            detail = getattr(result, "detail", "Research sandbox unavailable")
            raise ResearchAgentRuntimeError(str(detail))
        return approved_prompts, harness_context, result

    def research_capability_identity(self, spec: AgentSpec) -> JsonObject:
        """Return an identity only for a fully runnable behavior smoke."""

        if spec.role is not AgentRole.BEHAVIOR_PATTERN:
            raise ResearchAgentRuntimeError(
                "Research capability can only be certified by the behavior "
                "specialist"
            )
        approved_prompts, harness_context, sandbox = (
            self._preflight_details([spec])
        )
        approved = approved_prompts[AgentRole.BEHAVIOR_PATTERN]
        if approved.prompt_id != BEHAVIOR_RESEARCH_PROMPT_ID:
            raise ResearchAgentRuntimeError(
                "Behavior capability requires the approved behavior research "
                "prompt"
            )
        if (
            harness_context.approval.prompt_id
            != RESEARCH_HARNESS_CONTEXT_PROMPT_ID
        ):
            raise ResearchAgentRuntimeError(
                "Behavior capability requires the approved research Harness "
                "context"
            )
        try:
            pi_execution_identity = attest_pi_execution_identity(
                self.pi_command,
                extra_pi_args=self.extra_pi_args,
                working_directory=self.repository_root,
            )
            return build_research_capability_identity(
                repository_root=self.repository_root,
                prompt_id=approved.prompt_id,
                prompt_version=approved.version,
                prompt_sha256=approved.content_sha256,
                harness_context_sha256=harness_context.context_sha256,
                harness_version=harness_context.version,
                tool_schema_version=harness_context.tool_schema_version,
                research_tools_sha256=(
                    harness_context.research_tools_sha256
                ),
                research_output_sha256=(
                    harness_context.research_output_sha256
                ),
                pi_execution_identity=pi_execution_identity,
                model=self.model.to_dict(),
                sandbox_backend=str(sandbox.backend),
                sandbox_image=str(sandbox.image),
                sandbox_image_id=str(sandbox.image_id),
                sandbox_limits=self._sandbox_limits(),
                sandbox_control_plane_identity=(
                    sandbox.control_plane_identity
                ),
            )
        except ResearchCapabilityError as error:
            raise ResearchAgentRuntimeError(str(error)) from error

    def current_execution_identity_sha256(self, spec: AgentSpec) -> str:
        """Return the exact execution-identity digest for the next run."""

        _, harness_context, sandbox = self._preflight_details([spec])
        identity = self._execution_identity(
            harness_context=harness_context,
            sandbox=sandbox,
        )
        return research_execution_identity_digest(
            identity,
            repository_root=self.repository_root,
            verify_pi_executable=True,
        )

    def _sandbox_limits(self) -> JsonObject:
        raw_limits = getattr(self.sandbox, "limits", None)
        to_dict = getattr(raw_limits, "to_dict", None)
        if callable(to_dict):
            return dict(to_dict())
        if isinstance(raw_limits, Mapping):
            return dict(raw_limits)
        raise ResearchAgentRuntimeError(
            "Research sandbox does not expose its enforced limits"
        )

    def _execution_identity(
        self,
        *,
        harness_context: ApprovedResearchHarnessContext,
        sandbox: Any,
    ) -> JsonObject:
        """Attest the exact boundary that the next Pi process will use."""

        try:
            pi_execution = attest_pi_execution_identity(
                self.pi_command,
                extra_pi_args=self.extra_pi_args,
                working_directory=self.repository_root,
            )
            return build_research_execution_identity(
                repository_root=self.repository_root,
                pi_execution_identity=pi_execution,
                harness_context_sha256=harness_context.context_sha256,
                research_tools_sha256=(
                    harness_context.research_tools_sha256
                ),
                research_output_sha256=(
                    harness_context.research_output_sha256
                ),
                sandbox_backend=str(sandbox.backend),
                sandbox_image=str(sandbox.image),
                sandbox_image_id=str(sandbox.image_id),
                sandbox_limits=self._sandbox_limits(),
                sandbox_control_plane_identity=(
                    sandbox.control_plane_identity
                ),
            )
        except ResearchCapabilityError as error:
            raise ResearchAgentRuntimeError(str(error)) from error

    def run(
        self,
        *,
        spec: AgentSpec,
        campaign_id: str,
        round_number: int,
        context: Mapping[str, Any],
        evidence_bundle: Path,
        candidate_workspace: Path | None = None,
    ) -> AgentRunResult:
        """Execute one approved research protocol and validate its sole submission."""

        if candidate_workspace is not None:
            raise ResearchAgentRuntimeError(
                "Research specialists cannot receive a host write workspace"
            )
        approved_prompts, harness_context, sandbox = (
            self._preflight_details([spec])
        )
        execution_identity = self._execution_identity(
            harness_context=harness_context,
            sandbox=sandbox,
        )
        execution_identity_sha256 = research_execution_identity_digest(
            execution_identity,
            repository_root=self.repository_root,
            verify_pi_executable=True,
        )
        expected_execution_sha256 = context.get(
            "research_execution_identity_sha256"
        )
        if (
            not isinstance(expected_execution_sha256, str)
            or not _SHA256_PATTERN.fullmatch(expected_execution_sha256)
        ):
            raise ResearchAgentRuntimeError(
                "Research run requires a passed Harness execution identity"
            )
        if expected_execution_sha256 != execution_identity_sha256:
            raise ResearchAgentRuntimeError(
                "Research execution differs from the batch's passed Harness"
            )
        verification = verify_research_corpus(
            evidence_bundle,
            expected_content_sha256=str(context.get("corpus_digest", "")),
            expected_baseline_sha256=str(context.get("baseline_digest", "")),
        )
        _validate_context(context, verification)
        approved = approved_prompts[spec.role]
        extra_data: JsonObject | None = None
        if "error_description" in context:
            error_description = context.get("error_description")
            if not isinstance(error_description, Mapping):
                raise ResearchAgentRuntimeError(
                    "context.error_description must be an object"
                )
            extra_data = {"error_description": dict(error_description)}
        prompt = render_research_prompt(
            approved,
            verification,
            max_dynamic_bytes=self.max_prompt_data_bytes,
            extra_data=extra_data,
        )

        agent_run_id, run_directory = self.runs.prepare(
            spec=spec,
            campaign_id=campaign_id,
            round_number=round_number,
            model=self.model,
            context=context,
            evidence_bundle=evidence_bundle,
        )
        workspace = run_directory / "workspace"
        evidence = workspace / "evidence"
        try:
            copied_verification = verify_research_corpus(
                evidence,
                expected_content_sha256=verification.content_sha256,
                expected_baseline_sha256=verification.baseline_sha256,
            )
            _validate_context(context, copied_verification)
            expected_evidence_digest = research_evidence_tree_digest(evidence)
            atomic_write_json(
                run_directory / "prompt/dynamic-data.json",
                prompt.dynamic_data,
            )
            _atomic_write_text(
                run_directory / "prompt/rendered.md",
                prompt.rendered_text,
            )
            context_snapshot = run_directory / "prompt/harness-context.json"
            context_approval_snapshot = (
                run_directory / "prompt/harness-context-approval.json"
            )
            tools_snapshot = run_directory / "prompt/research-tools.ts"
            output_snapshot = run_directory / "prompt/research-output.ts"
            _atomic_write_text(
                context_snapshot,
                harness_context.approval.text,
            )
            _atomic_write_bytes(
                context_approval_snapshot,
                harness_context.approval.approval_path.read_bytes(),
            )
            _atomic_write_bytes(
                tools_snapshot,
                harness_context.research_tools_bytes,
            )
            _atomic_write_bytes(
                output_snapshot,
                harness_context.research_output_bytes,
            )
            if (
                _sha256_bytes(tools_snapshot.read_bytes())
                != harness_context.research_tools_sha256
                or _sha256_bytes(output_snapshot.read_bytes())
                != harness_context.research_output_sha256
            ):
                raise ResearchAgentRuntimeError(
                    "Research extension snapshot changed while archiving"
                )
            model_context_sha256 = _sha256_bytes(
                _json_bytes(
                    {
                        "rendered_prompt_sha256": prompt.rendered_sha256,
                        "harness_context_sha256": (
                            harness_context.context_sha256
                        ),
                    }
                )
            )
            self.runs.repository.update(
                agent_run_id,
                {
                    "prompt": {
                        "template_snapshot": "prompt/template.md",
                        "approval_snapshot": "prompt/approval.json",
                        "dynamic_data_snapshot": "prompt/dynamic-data.json",
                        "rendered_snapshot": "prompt/rendered.md",
                        "prompt_id": approved.prompt_id,
                        "version": approved.version,
                        "approved_by": approved.approved_by,
                        "approved_at": approved.approved_at,
                        "content_sha256": approved.content_sha256,
                        "rendered_sha256": prompt.rendered_sha256,
                        "harness_context_snapshot": (
                            "prompt/harness-context.json"
                        ),
                        "harness_context_approval_snapshot": (
                            "prompt/harness-context-approval.json"
                        ),
                        "harness_context_sha256": (
                            harness_context.context_sha256
                        ),
                        "research_tools_snapshot": "prompt/research-tools.ts",
                        "research_tools_sha256": (
                            harness_context.research_tools_sha256
                        ),
                        "research_output_snapshot": (
                            "prompt/research-output.ts"
                        ),
                        "research_output_sha256": (
                            harness_context.research_output_sha256
                        ),
                        "tool_schema_version": (
                            harness_context.tool_schema_version
                        ),
                        "model_context_sha256": model_context_sha256,
                    },
                    "output_contract": {
                        "mode": "validated_tool_submission",
                        "tool": spec.submission_tool,
                        "maximum_bytes": self.max_submission_bytes,
                    },
                    "research": {
                        "corpus_digest": verification.content_sha256,
                        "baseline_digest": verification.baseline_sha256,
                        "eligible_trajectory_ids": list(
                            verification.execution_ids
                        ),
                        "sandbox_attestation": "research/sandbox.json",
                        "work_archive": "research/work",
                        "session_identity": "research/session-identity.json",
                        "execution_identity": (
                            "research/execution-identity.json"
                        ),
                        "execution_identity_sha256": (
                            execution_identity_sha256
                        ),
                        "pi_session_retained": False,
                    },
                },
                expected_status="prepared",
            )
        except Exception as error:
            error_record = {
                "type": type(error).__name__,
                "message": str(error),
            }
            self.runs.finish(
                agent_run_id,
                status="failed",
                error=error_record,
                session_status="not_started",
            )
            return AgentRunResult(
                agent_run_id=agent_run_id,
                role=spec.role,
                status="failed",
                result=None,
                error=error_record,
                run_directory=run_directory,
            )

        research_directory = run_directory / "research"
        research_directory.mkdir()
        atomic_write_json(
            research_directory / "execution-identity.json",
            execution_identity,
        )
        journal = TrajectoryJournal(run_directory / "trajectory.jsonl", agent_run_id)
        journal.append(
            source="framework",
            record_type="trajectory_started",
            payload={
                "manifest": {
                    "schema": "analysis.research_agent_run.trajectory.v1",
                    "agent_run_id": agent_run_id,
                    "role": spec.role.value,
                    "campaign_id": campaign_id,
                    "round": round_number,
                    "model": self.model.to_dict(),
                    "research_lab_profile": RESEARCH_LAB_PROFILE,
                    "corpus_digest": verification.content_sha256,
                    "baseline_digest": verification.baseline_sha256,
                    "prompt": {
                        "prompt_id": approved.prompt_id,
                        "version": approved.version,
                        "static_sha256": approved.content_sha256,
                        "rendered_sha256": prompt.rendered_sha256,
                    },
                }
            },
        )

        drive = _DriveResult()
        sandbox_context: JsonObject | None = None
        try:
            with self.sandbox.isolated_run(
                evidence_directory=evidence,
                work_archive_directory=research_directory / "work",
                expected_evidence_digest=expected_evidence_digest,
                expected_control_plane_identity=execution_identity[
                    "sandbox"
                ]["control_plane"],
            ) as raw_sandbox_context:
                if not isinstance(raw_sandbox_context, dict):
                    raise ResearchAgentRuntimeError(
                        "Research sandbox returned an invalid context"
                    )
                # The sandbox fills post-run digests into this same object while
                # leaving the context manager, so retain its identity.
                sandbox_context = raw_sandbox_context
                tool_environment = validate_research_sandbox_context(
                    sandbox_context
                )
                mounted_verification = verify_research_corpus(
                    evidence,
                    expected_content_sha256=verification.content_sha256,
                    expected_baseline_sha256=verification.baseline_sha256,
                )
                if (
                    research_evidence_tree_digest(evidence)
                    != expected_evidence_digest
                ):
                    raise ResearchAgentRuntimeError(
                        "Research evidence changed before Agent start"
                    )
                _validate_context(context, mounted_verification)
                journal.append(
                    source="framework",
                    record_type="research_sandbox_started",
                    payload={
                        "backend": sandbox_context.get("backend"),
                        "image": sandbox_context.get("image"),
                        "image_id": sandbox_context.get("image_id"),
                        "network": sandbox_context.get("network"),
                        "root_filesystem": sandbox_context.get(
                            "root_filesystem"
                        ),
                        "limits": sandbox_context.get("limits"),
                    },
                )
                drive = self._drive_pi(
                    spec=spec,
                    agent_run_id=agent_run_id,
                    workspace=workspace,
                    prompt_text=prompt.rendered_text,
                    runtime_environment=tool_environment,
                    validation_context=context,
                    evidence=evidence,
                    journal=journal,
                    research_extension_path=tools_snapshot,
                    research_output_extension_path=output_snapshot,
                    pi_execution_identity=execution_identity["pi"],
                )
            assert sandbox_context is not None
            atomic_write_json(
                research_directory / "sandbox.json",
                sandbox_context,
            )
            journal.append(
                source="framework",
                record_type="research_sandbox_sealed",
                payload={
                    "evidence_digest_before": sandbox_context.get(
                        "evidence_digest_before"
                    ),
                    "evidence_digest_after": sandbox_context.get(
                        "evidence_digest_after"
                    ),
                    "work_digest": sandbox_context.get("work_digest"),
                },
            )
        except Exception as error:
            drive = _DriveResult(
                status="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
        finally:
            atomic_write_json(
                research_directory / "session-identity.json",
                {
                    "schema": "analysis.research_session_identity.v1",
                    "session_id": drive.session_id,
                    "process_isolated": True,
                    "session_retained": False,
                },
            )
            session_path = run_directory / "pi-session.jsonl"
            session_path.touch(exist_ok=True)
            journal.append(
                source="framework",
                record_type="session_captured",
                payload={
                    "path": "pi-session.jsonl",
                    "status": "not_captured_by_policy",
                },
            )
            journal.capture_incomplete_state(
                "agent_settled" if drive.settled else "agent_run_ended"
            )
            journal.append(
                source="framework",
                record_type="trajectory_finished",
                payload={
                    "status": drive.status,
                    "result_path": (
                        "result.json" if drive.result is not None else None
                    ),
                    "error": drive.error,
                    "parse_failure": drive.parse_failure,
                },
            )
            journal.close()

        self.runs.finish(
            agent_run_id,
            status=drive.status,
            result=drive.result,
            error=drive.error,
            parse_failure=drive.parse_failure,
            session_status="not_captured_by_policy",
        )
        return AgentRunResult(
            agent_run_id=agent_run_id,
            role=spec.role,
            status=drive.status,
            result=drive.result,
            error=drive.error or drive.parse_failure,
            run_directory=run_directory,
        )

    def drive_deterministic_harness(
        self,
        *,
        workspace: Path,
        evidence: Path,
        sandbox_context: Mapping[str, Any],
        validation_context: Mapping[str, Any],
        journal: TrajectoryJournal,
        driver_extension_path: Path,
        mode: str,
        research_execution_identity: Mapping[str, Any],
    ) -> _DriveResult:
        """Drive production tools through Pi with a keyless faux provider.

        This is an executable acceptance path, not an AgentRun. It deliberately
        bypasses prompt approval because no model is contacted, while reusing
        the same Pi extensions, RPC events, submission validator, and protocol
        state machine as a real specialist.
        """

        if (
            self.model.provider != RESEARCH_HARNESS_FAUX_PROVIDER
            or self.model.model != RESEARCH_HARNESS_FAUX_MODEL
            or self.model.thinking != "off"
        ):
            raise ResearchAgentRuntimeError(
                "Deterministic Harness drive requires the fixed faux model"
            )
        if mode not in {
            "positive",
            "budget",
            "cleanup",
            "duplicate_submission",
            "post_submission",
        }:
            raise ResearchAgentRuntimeError(
                f"Unsupported deterministic Harness mode: {mode}"
            )
        try:
            bound_execution = validate_research_execution_identity(
                research_execution_identity,
                repository_root=self.repository_root,
                verify_pi_executable=True,
            )
        except ResearchCapabilityError as error:
            raise ResearchAgentRuntimeError(str(error)) from error
        implementation_files = {
            str(item["path"]): str(item["sha256"])
            for item in bound_execution["implementation"]["files"]
        }
        driver_relative = str(
            _RESEARCH_HARNESS_EXTENSION_PATHS[
                "research_harness_driver"
            ]
        )
        driver_sha256 = implementation_files.get(driver_relative)
        if driver_sha256 is None:
            raise ResearchAgentRuntimeError(
                "Research execution identity does not bind the Harness driver"
            )
        extension_sha256 = {
            "research_tools": bound_execution["toolchain"][
                "research_tools_sha256"
            ],
            "research_output": bound_execution["toolchain"][
                "research_output_sha256"
            ],
            "research_harness_driver": driver_sha256,
        }
        _validate_fixed_harness_extensions(
            repository_root=self.repository_root,
            research_tools_path=self.research_extension_path,
            research_output_path=self.research_output_extension_path,
            driver_path=driver_extension_path,
            expected_sha256=extension_sha256,
        )
        environment = _deterministic_harness_environment(
            sandbox_context,
            bound_execution["sandbox"],
            mode=mode,
        )
        eligible = validation_context.get("eligible_trajectory_ids")
        if not isinstance(eligible, list) or len(eligible) < 2:
            raise ResearchAgentRuntimeError(
                "Deterministic Harness requires at least two eligible Trajectories"
            )
        environment.update(
            {
                "SKILL_EVOLUTION_HARNESS_DRIVER_MODE": mode,
                "SKILL_EVOLUTION_HARNESS_CORPUS_DIGEST": str(
                    validation_context["corpus_digest"]
                ),
                "SKILL_EVOLUTION_HARNESS_BASELINE_DIGEST": str(
                    validation_context["baseline_digest"]
                ),
                "SKILL_EVOLUTION_HARNESS_EXECUTION_IDS": json.dumps(
                    eligible, ensure_ascii=False, separators=(",", ":")
                ),
                "SKILL_EVOLUTION_HARNESS_TRAJECTORY_FILENAME": str(
                    validation_context.get(
                        "trajectory_filename", "trajectory.jsonl"
                    )
                ),
            }
        )
        workspace.mkdir(parents=True, exist_ok=False)
        spec = AgentSpec(
            role=AgentRole.OUTCOME_CONSISTENCY,
            prompt_path=driver_extension_path,
            tool_mode="read_only",
            timeout_seconds=120,
            research_lab_profile=RESEARCH_LAB_PROFILE,
            submission_tool=RESEARCH_SUBMISSION_TOOL,
        )
        return self._drive_pi(
            spec=spec,
            agent_run_id=f"deterministic-{mode}",
            workspace=workspace,
            prompt_text=(
                "Run the deterministic research Harness acceptance sequence."
            ),
            runtime_environment=environment,
            validation_context=validation_context,
            evidence=evidence,
            journal=journal,
            research_extension_path=self.research_extension_path,
            research_output_extension_path=(
                self.research_output_extension_path
            ),
            additional_extension_paths=(driver_extension_path,),
            additional_pi_args=(),
            pi_execution_identity=bound_execution["pi"],
            deterministic_extension_sha256=extension_sha256,
            record_process_start=False,
        )

    def _drive_pi(
        self,
        *,
        spec: AgentSpec,
        agent_run_id: str,
        workspace: Path,
        prompt_text: str,
        runtime_environment: Mapping[str, str],
        validation_context: Mapping[str, Any],
        evidence: Path,
        journal: TrajectoryJournal,
        research_extension_path: Path,
        research_output_extension_path: Path,
        additional_extension_paths: Sequence[Path] = (),
        additional_pi_args: Sequence[str] = (),
        pi_execution_identity: Mapping[str, Any] | None = None,
        deterministic_extension_sha256: (
            Mapping[str, str] | None
        ) = None,
        record_process_start: bool = True,
    ) -> _DriveResult:
        """Drive Pi to settlement and close it before the sandbox is sealed."""

        if additional_extension_paths:
            if (
                len(additional_extension_paths) != 1
                or self.model.provider != RESEARCH_HARNESS_FAUX_PROVIDER
                or self.model.model != RESEARCH_HARNESS_FAUX_MODEL
                or self.model.thinking != "off"
                or additional_pi_args
                or deterministic_extension_sha256 is None
            ):
                raise ResearchAgentRuntimeError(
                    "Only the fixed deterministic Harness driver may be "
                    "loaded as an additional research extension"
                )
            _validate_fixed_harness_extensions(
                repository_root=self.repository_root,
                research_tools_path=research_extension_path,
                research_output_path=research_output_extension_path,
                driver_path=additional_extension_paths[0],
                expected_sha256=deterministic_extension_sha256,
            )
        elif deterministic_extension_sha256 is not None:
            raise ResearchAgentRuntimeError(
                "Deterministic extension identity requires the fixed driver"
            )

        pi_args = [
            "--name",
            f"{spec.role.value}-{agent_run_id}",
            "--no-builtin-tools",
            "--tools",
            ",".join(RESEARCH_PI_TOOL_ALLOWLIST),
            "--no-extensions",
            "--extension",
            str(research_extension_path),
            "--extension",
            str(research_output_extension_path),
            *(
                argument
                for extension in additional_extension_paths
                for argument in ("--extension", str(extension))
            ),
            "--no-prompt-templates",
            "--no-skills",
            "--no-context-files",
            "--no-themes",
            "--offline",
            "--provider",
            self.model.provider,
            "--model",
            self.model.model,
            "--thinking",
            self.model.thinking,
            *additional_pi_args,
            *self.extra_pi_args,
        ]
        if pi_execution_identity is None:
            pi_execution_identity = attest_pi_execution_identity(
                self.pi_command,
                extra_pi_args=self.extra_pi_args,
                working_directory=self.repository_root,
            )
        client: PiRpcClient | None = None
        process_resources = ExitStack()
        drive = _DriveResult()
        last_assistant: Mapping[str, Any] | None = None
        pending_submissions: dict[str, JsonObject] = {}
        successful_submissions: list[JsonObject] = []
        submission_batch_by_call: dict[str, int] = {}
        submission_derivations_by_call: dict[str, set[str]] = {}
        successful_submission_call_id: str | None = None
        successful_submission_derivations: set[str] = set()
        submission_attempt_count = 0
        successful_submission_seen = False
        post_submission_actions: list[str] = []
        pending_derivations: set[str] = set()
        successful_derivations: set[str] = set()
        current_tool_batch = 0
        tool_batches: dict[int, list[tuple[str, str]]] = {}
        extension_errors: list[str] = []
        poisoned_session_reasons: list[str] = []
        try:
            journal.append(
                source="framework",
                record_type="pi_process_starting",
                payload={
                    "role": spec.role.value,
                    "built_in_tools": False,
                    "extensions": [
                        research_extension_path.name,
                        research_output_extension_path.name,
                        *(path.name for path in additional_extension_paths),
                    ],
                    "output_contract": "validated_tool_submission",
                },
            )
            verified_pi = verify_pi_execution_identity_current(
                pi_execution_identity
            )
            _validate_research_workspace_configuration(workspace)
            isolated_configuration = process_resources.enter_context(
                _isolated_pi_configuration(
                    source_agent_directory=self.pi_agent_directory,
                    provider=self.model.provider,
                )
            )
            process_environment = _research_pi_environment(
                verified_pi,
                runtime_environment,
                isolated_configuration,
            )
            client = PiRpcClient(
                cwd=workspace,
                pi_command=verified_pi["resolved_command"],
                pi_args=pi_args,
                no_session=True,
                approve_project=False,
                env=process_environment,
                replace_environment=True,
                pass_fds=isolated_configuration.pass_fds,
                rpc_record_observer=_safe_rpc_observer(journal),
                stderr_observer=_safe_stderr_observer(journal),
            )
            client.start()
            if record_process_start:
                self.runs.mark_running(
                    agent_run_id,
                    process_id=client.process.pid,
                )
            journal.append(
                source="framework",
                record_type="pi_process_started",
                payload={"pid": client.process.pid},
            )
            state = client.request({"type": "get_state"}, timeout=30)
            state_data = state.get("data")
            if not state.get("success") or not isinstance(state_data, Mapping):
                raise ResearchAgentRuntimeError(
                    "Pi did not return a valid research runtime state"
                )
            observed_model = state_data.get("model")
            if (
                not isinstance(observed_model, Mapping)
                or observed_model.get("provider") != self.model.provider
                or observed_model.get("id") != self.model.model
                or state_data.get("thinkingLevel") != self.model.thinking
                or state_data.get("sessionFile") is not None
            ):
                raise ResearchAgentRuntimeError(
                    "Pi runtime model or thinking differs from research policy"
                )
            observed_session_id = state_data.get("sessionId")
            if (
                isinstance(observed_session_id, str)
                and observed_session_id
            ):
                drive.session_id = observed_session_id
            journal.append(
                source="framework",
                record_type="runtime_observed",
                payload={
                    "provider": observed_model.get("provider"),
                    "model_id": observed_model.get("id"),
                    "thinking_level": state_data.get("thinkingLevel"),
                    "session_id": state_data.get("sessionId"),
                },
            )
            models_response = client.request(
                {"type": "get_available_models"}, timeout=10
            )
            models_data = models_response.get("data")
            available_models = (
                models_data.get("models")
                if isinstance(models_data, Mapping)
                else None
            )
            matching_models = (
                [
                    item
                    for item in available_models
                    if isinstance(item, Mapping)
                    and item.get("provider") == self.model.provider
                    and item.get("id") == self.model.model
                ]
                if isinstance(available_models, list)
                else []
            )
            if (
                not models_response.get("success")
                or len(matching_models) != 1
            ):
                raise ResearchAgentRuntimeError(
                    "Isolated Pi credential or selected model is unavailable"
                )
            journal.append(
                source="framework",
                record_type="runtime_model_available",
                payload={"selected_model_available": True},
            )
            entries_response = client.request(
                {"type": "get_entries"}, timeout=10
            )
            entries_data = entries_response.get("data")
            if (
                not entries_response.get("success")
                or not isinstance(entries_data, Mapping)
                or not isinstance(entries_data.get("entries"), list)
            ):
                raise ResearchAgentRuntimeError(
                    "Pi did not attest its active research tools"
                )
            attestation_entries = [
                entry
                for entry in entries_data["entries"]
                if isinstance(entry, Mapping)
                and _runtime_attestation_entry(entry) is not None
            ]
            if len(attestation_entries) != 1:
                raise ResearchAgentRuntimeError(
                    "Pi did not provide one unique active-tools attestation"
                )
            attestation = _runtime_attestation_entry(
                attestation_entries[0]
            )
            assert attestation is not None
            expected_tools = sorted(RESEARCH_PI_TOOL_ALLOWLIST)
            if (
                attestation.get("schema")
                != "research.runtime_attestation.v1"
                or attestation.get("active_tools") != expected_tools
            ):
                raise ResearchAgentRuntimeError(
                    "Pi active tools differ from the research allowlist"
                )
            journal.append(
                source="framework",
                record_type="runtime_tools_attested",
                payload={"active_tools": expected_tools},
            )
            response = client.request(
                {"type": "prompt", "message": prompt_text}, timeout=30
            )
            if not response.get("success"):
                raise ResearchAgentRuntimeError(
                    f"Pi rejected prompt: {response.get('error')}"
                )
            for event in client.events_until(
                lambda item: item.get("type") == "agent_settled",
                timeout=spec.timeout_seconds,
            ):
                event_type = event.get("type")
                tool_name = event.get("toolName")
                tool_call_id = event.get("toolCallId")
                if event_type in {
                    "message_start",
                    "message_update",
                    "message_end",
                }:
                    message = event.get("message")
                    if (
                        isinstance(message, Mapping)
                        and message.get("role") == "assistant"
                    ):
                        if successful_submission_seen:
                            post_submission_actions.append(
                                f"assistant_{event_type}"
                            )
                        if event_type == "message_end":
                            last_assistant = message
                            current_tool_batch += 1
                            tool_batches[current_tool_batch] = []
                elif event_type == "tool_execution_start":
                    if successful_submission_seen:
                        post_submission_actions.append(
                            f"tool:{tool_name}"
                        )
                    if (
                        isinstance(tool_call_id, str)
                        and tool_call_id
                        and isinstance(tool_name, str)
                        and tool_name
                    ):
                        tool_batches.setdefault(
                            current_tool_batch,
                            [],
                        ).append((tool_call_id, tool_name))
                    if tool_name == spec.submission_tool:
                        submission_attempt_count += 1
                        arguments = event.get("args")
                        if (
                            isinstance(tool_call_id, str)
                            and tool_call_id
                            and isinstance(arguments, Mapping)
                        ):
                            pending_submissions[tool_call_id] = dict(
                                arguments
                            )
                            submission_batch_by_call[
                                tool_call_id
                            ] = current_tool_batch
                            submission_derivations_by_call[
                                tool_call_id
                            ] = set(successful_derivations)
                    elif (
                        tool_name == RESEARCH_EXEC_TOOL
                        and isinstance(tool_call_id, str)
                        and tool_call_id
                    ):
                        pending_derivations.add(tool_call_id)
                elif event_type == "tool_execution_end":
                    if (
                        successful_submission_seen
                        and tool_call_id != successful_submission_call_id
                    ):
                        post_submission_actions.append(
                            f"tool_end:{tool_name}"
                        )
                    if tool_name == spec.submission_tool:
                        arguments = (
                            pending_submissions.pop(tool_call_id, None)
                            if isinstance(tool_call_id, str)
                            else None
                        )
                        if not event.get("isError") and arguments is not None:
                            successful_submissions.append(arguments)
                            assert isinstance(tool_call_id, str)
                            successful_submission_call_id = tool_call_id
                            successful_submission_derivations = set(
                                submission_derivations_by_call.get(
                                    tool_call_id,
                                    set(),
                                )
                            )
                            successful_submission_seen = True
                    elif (
                        tool_name == RESEARCH_EXEC_TOOL
                        and isinstance(tool_call_id, str)
                        and tool_call_id in pending_derivations
                    ):
                        pending_derivations.discard(tool_call_id)
                        if not event.get("isError"):
                            successful_derivations.add(tool_call_id)
                elif (
                    event_type == "tool_execution_update"
                    and successful_submission_seen
                    and tool_call_id != successful_submission_call_id
                ):
                    post_submission_actions.append(
                        f"tool_update:{tool_name}"
                    )
                elif event_type == "entry_appended":
                    entry = event.get("entry")
                    if (
                        isinstance(entry, Mapping)
                        and entry.get("customType")
                        == "research-session-poisoned"
                        and isinstance(entry.get("data"), Mapping)
                    ):
                        poison_data = dict(entry["data"])
                        reason = str(
                            poison_data.get(
                                "reason", "container cleanup unverified"
                            )
                        )
                        poisoned_session_reasons.append(reason)
                        journal.append(
                            source="research_tools",
                            record_type="research_session_poisoned",
                            payload=poison_data,
                        )
                    elif (
                        isinstance(entry, Mapping)
                        and entry.get("customType")
                        == "research-harness-driver-attestation"
                        and isinstance(entry.get("data"), Mapping)
                    ):
                        journal.append(
                            source="harness_driver",
                            record_type="harness_driver_attestation",
                            payload=dict(entry["data"]),
                        )
                elif event_type == "extension_error":
                    extension_errors.append(
                        _redact_stderr(str(event.get("error", event)))
                    )
            drive.settled = True
            if extension_errors:
                raise ResearchAgentRuntimeError(
                    "Pi extension failed during research: "
                    + "; ".join(extension_errors[:3])
                )
            if drive.session_id is None:
                raise ResearchAgentRuntimeError(
                    "Pi did not expose a fresh research session identity"
                )
            self._validate_submission(
                drive=drive,
                spec=spec,
                context=validation_context,
                evidence=evidence,
                submissions=successful_submissions,
                submission_attempt_count=submission_attempt_count,
                submission_batch_tools=(
                    tool_batches.get(
                        submission_batch_by_call.get(
                            successful_submission_call_id or "",
                            -1,
                        ),
                        [],
                    )
                ),
                derivation_ids=successful_submission_derivations,
                post_submission_actions=post_submission_actions,
                poisoned_session_reasons=poisoned_session_reasons,
                last_assistant=last_assistant,
                workspace=workspace,
            )
        except PiRequestTimeoutError as error:
            drive.error = {
                "type": type(error).__name__,
                "message": str(error),
            }
            drive.status, drive.timeout_uncertain = self._abort_after_timeout(
                client, journal
            )
        except Exception as error:
            drive.error = {
                "type": type(error).__name__,
                "message": str(error),
            }
            drive.status = "failed"
        finally:
            if client is not None:
                client.close()
                observer_errors = client.observer_errors
                if observer_errors:
                    drive.status = "failed"
                    drive.result = None
                    drive.parse_failure = None
                    drive.error = {
                        "type": "ResearchAuditObserverError",
                        "message": (
                            "Research audit capture failed; the result cannot "
                            "be accepted"
                        ),
                        "error_count": len(observer_errors),
                    }
                    journal.append(
                        source="framework",
                        record_type="research_audit_failed",
                        payload={"error_count": len(observer_errors)},
                    )
                try:
                    exit_code = client.process.returncode
                except PiRpcError:
                    exit_code = None
                journal.append(
                    source="framework",
                    record_type="pi_process_exited",
                    payload={
                        "exit_code": exit_code,
                        "timeout_uncertain": drive.timeout_uncertain,
                    },
                )
            try:
                process_resources.close()
            except Exception:
                drive.status = "failed"
                drive.result = None
                drive.parse_failure = None
                drive.error = {
                    "type": "ResearchPiConfigurationCleanupError",
                    "message": (
                        "Ephemeral Pi configuration could not be removed"
                    ),
                }
        return drive

    def _validate_submission(
        self,
        *,
        drive: _DriveResult,
        spec: AgentSpec,
        context: Mapping[str, Any],
        evidence: Path,
        submissions: Sequence[Mapping[str, Any]],
        submission_attempt_count: int,
        submission_batch_tools: Sequence[tuple[str, str]],
        derivation_ids: set[str],
        post_submission_actions: Sequence[str],
        poisoned_session_reasons: Sequence[str],
        last_assistant: Mapping[str, Any] | None,
        workspace: Path,
    ) -> None:
        """Apply structural, denominator, evidence, and protocol gates."""

        raw_text = _message_text(last_assistant or {})
        try:
            if poisoned_session_reasons:
                raise ValueError(
                    "Research session process cleanup was not verified: "
                    f"{list(poisoned_session_reasons)}"
                )
            if submission_attempt_count != 1 or len(submissions) != 1:
                raise ValueError(
                    "Research must make exactly one submission attempt and "
                    "that attempt must succeed"
                )
            if post_submission_actions:
                raise ValueError(
                    "Research continued after successful submission: "
                    f"{list(post_submission_actions)}"
                )
            if (
                len(submission_batch_tools) != 1
                or submission_batch_tools[0][1] != spec.submission_tool
            ):
                raise ValueError(
                    "The research submission must be the sole tool in its "
                    "assistant tool batch"
                )
            submission = dict(submissions[0])
            if len(_json_bytes(submission)) > self.max_submission_bytes:
                raise ValueError("Research submission exceeds its output limit")
            eligible = context.get("eligible_trajectory_ids")
            assert isinstance(eligible, list)
            if spec.submission_tool == RESEARCH_SUBMISSION_TOOL:
                validated = validate_research_result(
                    submission,
                    expected_role=spec.role.value,
                    expected_corpus_digest=str(context["corpus_digest"]),
                    expected_baseline_digest=str(context["baseline_digest"]),
                    allowed_trajectory_ids=eligible,
                    known_derivation_ids=sorted(derivation_ids),
                )
                validate_research_result_evidence(
                    validated,
                    bundle_root=evidence,
                )
            elif spec.submission_tool == ERROR_IDENTIFICATION_SUBMISSION_TOOL:
                validated = validate_error_identification(
                    submission,
                    expected_corpus_digest=str(context["corpus_digest"]),
                    expected_baseline_digest=str(context["baseline_digest"]),
                    allowed_trajectory_ids=eligible,
                )
                validate_error_identification_evidence(
                    validated,
                    bundle_root=evidence,
                )
            elif spec.submission_tool == ERROR_REPORT_SUBMISSION_TOOL:
                validated = validate_error_report(
                    submission,
                    expected_corpus_digest=str(context["corpus_digest"]),
                    expected_baseline_digest=str(context["baseline_digest"]),
                    allowed_trajectory_ids=eligible,
                    known_derivation_ids=sorted(derivation_ids),
                )
                validate_error_report_evidence(
                    validated,
                    bundle_root=evidence,
                )
            else:
                raise ValueError(
                    f"Unsupported research submission tool: "
                    f"{spec.submission_tool}"
                )
        except (AssertionError, KeyError, TypeError, ValueError) as error:
            invalid_path = workspace.parent / "result.invalid.json"
            atomic_write_json(
                invalid_path,
                {
                    "successful_submissions": [dict(item) for item in submissions],
                    "submission_attempt_count": submission_attempt_count,
                    "post_submission_actions": list(post_submission_actions),
                    "final_message": raw_text,
                },
            )
            drive.parse_failure = {
                "type": type(error).__name__,
                "message": str(error),
                "raw_output": invalid_path.name,
            }
            drive.status = "invalid_output"
            return
        drive.result = validated
        drive.status = "succeeded"

    def _abort_after_timeout(
        self,
        client: PiRpcClient | None,
        journal: TrajectoryJournal,
    ) -> tuple[str, bool]:
        """Abort before process teardown and expose uncertain settlement."""

        if client is None:
            return "timed_out", False
        acknowledged = False
        settled = False
        try:
            response = client.request({"type": "abort"}, timeout=5)
            acknowledged = bool(response.get("success"))
        except PiRpcError:
            acknowledged = False
        journal.append(
            source="framework",
            record_type="agent_abort_requested",
            payload={"acknowledged": acknowledged},
        )
        deadline = time.monotonic() + self.abort_wait_seconds
        while time.monotonic() < deadline:
            try:
                event = client.next_event(
                    timeout=max(0.05, deadline - time.monotonic())
                )
            except PiRequestTimeoutError:
                break
            if event.get("type") == "agent_settled":
                settled = True
                break
        if acknowledged and settled:
            return "timed_out", False
        return "indeterminate", True


def _message_text(message: Mapping[str, Any]) -> str:
    """Join only visible assistant text blocks for protocol checks."""

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block["text"])
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
