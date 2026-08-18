"""Build deterministic, hierarchy-native corpora for multi-trajectory research."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import tempfile
from typing import Any

from skill_evolution.evaluation import (
    EvaluationSuiteError,
    EvaluationSuiteResolver,
    ResolvedEvaluationSuite,
    validate_evaluation_suite,
)
from skill_evolution.evidence import sanitize_for_evidence
from skill_evolution.hierarchy import (
    HierarchyError,
    SkillHierarchyRepository,
    package_digest,
)
from skill_evolution.storage import (
    JsonObject,
    StorageError,
    atomic_write_json,
    load_json_object,
)
from skill_evolution.trajectory_precheck import precheck_trajectory


RESEARCH_READINESS_SCHEMA = "research.readiness.v1"
RESEARCH_CORPUS_SCHEMA = "research.corpus.v1"
RESEARCH_CORPUS_MAP_SCHEMA = "research.corpus_map.v1"
RESEARCH_NAVIGATION_INDEX_SCHEMA = "research.navigation_index.v1"
RESEARCH_BASELINE_SCHEMA = "research.baseline.v1"
RESEARCH_TASK_CONDITION_MAP_SCHEMA = "research.task_condition_map.v1"
RESEARCH_REDACTION_POLICY_SCHEMA = "research.redaction_policy.v1"

RESULT_RELIABILITY = "result_reliability"
BEHAVIOR_PATTERNS = "behavior_patterns"
RECOVERY_SUCCESS = "recovery_success"
CONDITIONS_COVERAGE = "conditions_coverage"
CONSISTENCY = "consistency"
RESOURCE_EFFICIENCY = "resource_efficiency"
RESEARCH_OBJECTIVES = frozenset(
    {
        RESULT_RELIABILITY,
        BEHAVIOR_PATTERNS,
        RECOVERY_SUCCESS,
        CONDITIONS_COVERAGE,
        CONSISTENCY,
        RESOURCE_EFFICIENCY,
    }
)
_COMPARABILITY_OBJECTIVES = frozenset(
    {RESULT_RELIABILITY, CONSISTENCY, RESOURCE_EFFICIENCY}
)

_TERMINAL_EXECUTION_STATUSES = {
    "succeeded",
    "failed",
    "interrupted",
    "orchestration_failed",
    "indeterminate",
}
_SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".ts", ".sh", ".rb", ".pl"}
_VALIDATION_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:grep|wc|head|tail|tidy|xmllint|test)(?:\s|$)",
    re.IGNORECASE,
)
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:)?/[^\s\"']+")
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;'\"}]+"
    ),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"password|secret|credential)\s*[:=]\s*)[^\s,;'\"}]+"
    ),
    re.compile(
        r"(?im)^(\s*(?:export\s+)?[A-Z][A-Z0-9_]*"
        r"(?:TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL)[A-Z0-9_]*\s*=\s*).+$"
    ),
    re.compile(
        r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
        r"-----END [^-\r\n]*PRIVATE KEY-----"
    ),
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
)
_SENSITIVE_EXACT_KEYS = {
    "access_token",
    "bearer_token",
    "refresh_token",
    "token",
}
_HIDDEN_REASONING_KEYS = {
    "analysis_content",
    "reasoning",
    "reasoning_content",
    "thinking",
    "thinking_content",
}
_TEXT_ARTIFACT_SUFFIXES = {
    ".css",
    ".csv",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".pl",
    ".py",
    ".rb",
    ".sh",
    ".svg",
    ".ts",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SENSITIVE_ARTIFACT_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx"}
_RESERVED_ARTIFACT_NAMES = {
    "baseline.json",
    "corpus-map.json",
    "corpus.json",
    "execution.json",
    "messages.jsonl",
    "navigation-index.json",
    "readiness.json",
    "revision.json",
    "session.json",
    "session.jsonl",
    "task.json",
    "trajectory.jsonl",
    "trace.jsonl",
}
_TEXT_MEDIA_TYPES = {
    "application/javascript",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "image/svg+xml",
}
_REDACTION_POLICY: JsonObject = {
    "schema": RESEARCH_REDACTION_POLICY_SCHEMA,
    "policy_id": "observable-evidence-v1",
    "hidden_reasoning": "redacted",
    "credentials": "redacted",
    "environment": "redacted",
    "text_artifacts": "utf8-sanitized",
    "binary_artifacts": "excluded",
    "pi_session": "excluded",
}


class ResearchCorpusError(ValueError):
    """Raised when research evidence cannot be frozen without ambiguity."""


@dataclass(frozen=True)
class ResearchCorpusResult:
    """One completely built research corpus and its deterministic projections."""

    directory: Path
    manifest: JsonObject
    corpus_map: JsonObject
    navigation_index: JsonObject
    baseline: JsonObject
    readiness: JsonObject
    corpus_digest: str
    baseline_digest: str

    @property
    def corpus_id(self) -> str:
        """Return the content-addressed corpus identity."""

        return str(self.manifest["corpus_id"])

    @property
    def content_sha256(self) -> str:
        """Return the stable digest of the corpus manifest body."""

        return self.corpus_digest

    @property
    def baseline_sha256(self) -> str:
        """Return the stable digest of the serialized baseline."""

        return self.baseline_digest

    @property
    def manifest_path(self) -> Path:
        """Return the verified corpus manifest path."""

        return self.directory / "corpus.json"

    @property
    def map_path(self) -> Path:
        """Return the verified prompt-facing corpus map path."""

        return self.directory / "corpus-map.json"

    @property
    def index_path(self) -> Path:
        """Return the verified navigation index path."""

        return self.directory / "navigation-index.json"

    @property
    def baseline_path(self) -> Path:
        """Return the verified deterministic baseline path."""

        return self.directory / "baseline.json"


@dataclass(frozen=True)
class ResearchCorpusVerification:
    """A fail-closed reload of one immutable research corpus."""

    directory: Path
    manifest: JsonObject
    corpus_map: JsonObject
    navigation_index: JsonObject
    baseline: JsonObject
    content_sha256: str
    baseline_sha256: str

    @property
    def execution_ids(self) -> tuple[str, ...]:
        """Return the execution identities bound by the verified manifest."""

        return tuple(str(item) for item in self.manifest["execution_ids"])


@dataclass(frozen=True)
class _AcceptedReport:
    analysis_id: str
    kind: str
    schema: str
    source: Path
    source_sha256: str


@dataclass(frozen=True)
class _ArtifactSource:
    role: str
    artifact_id: str
    declared_path: str
    source: Path | None
    size: int | None
    sha256: str | None
    media_type: str | None


@dataclass(frozen=True)
class _PreparedExecution:
    execution_id: str
    execution: JsonObject
    directory: Path
    trajectory: Path
    source_trajectory_sha256: str
    records: tuple[JsonObject, ...]
    accepted_reports: tuple[_AcceptedReport, ...]
    artifacts: tuple[_ArtifactSource, ...]


def _comparison_runtime_facts(
    prepared: _PreparedExecution,
) -> tuple[JsonObject, tuple[str, ...]]:
    """Return stable runtime facts and the facts that could not be observed."""

    setup_runtime = _mapping(_mapping(prepared.execution.get("setup")).get("runtime"))
    observed: Mapping[str, Any] = {}
    for record in prepared.records:
        if record.get("type") != "runtime_observed":
            continue
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            observed = payload
            break

    observed_model = _mapping(observed.get("model"))
    setup_model = _mapping(setup_runtime.get("model"))
    provider = observed_model.get("provider")
    if not isinstance(provider, str) or not provider:
        provider = setup_model.get("provider") or setup_runtime.get("provider")
    model_id = observed_model.get("id")
    if not isinstance(model_id, str) or not model_id:
        model_id = setup_model.get("id") or setup_runtime.get("model_id")
    api = observed_model.get("api")
    if not isinstance(api, str) or not api:
        api = setup_model.get("api") or setup_runtime.get("api")

    thinking = observed.get("thinking_level")
    if not isinstance(thinking, str) or not thinking:
        thinking = setup_runtime.get("thinking_level")
    if not isinstance(thinking, str) or not thinking:
        pi_args = setup_runtime.get("pi_args")
        if isinstance(pi_args, list) and "--thinking" in pi_args:
            index = pi_args.index("--thinking") + 1
            if index < len(pi_args) and isinstance(pi_args[index], str):
                thinking = pi_args[index]

    raw_pi_args = setup_runtime.get("pi_args")
    pi_args: list[str] = []
    if isinstance(raw_pi_args, list) and all(
        isinstance(item, str) for item in raw_pi_args
    ):
        ignored_with_values = {"--name", "--session-dir"}
        index = 0
        while index < len(raw_pi_args):
            argument = raw_pi_args[index]
            if argument in ignored_with_values:
                index += 2
                continue
            if argument == "--skill":
                pi_args.extend((argument, "<frozen-skill-path>"))
                index += 2
                continue
            pi_args.append(argument)
            index += 1

    facts: JsonObject = {
        "platform": setup_runtime.get("platform"),
        "python": setup_runtime.get("python"),
        "model": {
            "provider": provider,
            "id": model_id,
            "api": api if isinstance(api, str) and api else None,
        },
        "thinking_level": thinking,
        "pi_args": pi_args,
    }
    missing: list[str] = []
    for field in ("platform", "python", "thinking_level"):
        if not isinstance(facts[field], str) or not facts[field]:
            missing.append(field)
    model = _mapping(facts["model"])
    for field in ("provider", "id"):
        if not isinstance(model.get(field), str) or not model[field]:
            missing.append(f"model.{field}")
    if not pi_args:
        missing.append("pi_args")
    return facts, tuple(missing)


def _comparison_basis(
    prepared: _PreparedExecution,
    *,
    suite_conditions: Mapping[str, Any] | None = None,
    require_suite_conditions: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Fingerprint facts that must match inside a declared comparison group."""

    task = _sanitize_research_value(prepared.execution.get("task", {}))
    if not isinstance(task, Mapping):
        task = {}
    semantic_task = dict(task)
    semantic_task.pop("task_case_id", None)
    inputs = [
        {
            "artifact_id": artifact.artifact_id,
            "declared_path": artifact.declared_path,
            "bytes": artifact.size,
            "sha256": artifact.sha256,
            "media_type": artifact.media_type,
        }
        for artifact in prepared.artifacts
        if artifact.role == "input"
    ]
    inputs.sort(key=lambda item: (str(item["artifact_id"]), str(item["declared_path"])))
    runtime, missing = _comparison_runtime_facts(prepared)
    missing_facts = list(missing)
    if require_suite_conditions and suite_conditions is None:
        missing_facts.append("evaluation_suite.conditions")
    basis: JsonObject = {
        "revision_id": prepared.execution["revision_id"],
        "task": semantic_task,
        "inputs": inputs,
        "runtime": runtime,
        "evaluation_suite_conditions": (
            dict(suite_conditions) if suite_conditions is not None else None
        ),
    }
    return (
        hashlib.sha256(_canonical_bytes(basis)).hexdigest(),
        tuple(missing_facts),
    )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sanitize_research_text(value: str) -> str:
    """Remove recognizable credentials from otherwise observable text."""

    sanitized = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.groups:
            sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
        else:
            sanitized = pattern.sub("[REDACTED_PRIVATE_KEY]", sanitized)
    return sanitized


def _sanitize_research_value(value: Any) -> Any:
    """Apply the corpus policy to structured evidence, including string fields."""

    baseline = sanitize_for_evidence(value)

    def harden(item: Any) -> Any:
        if isinstance(item, list):
            return [harden(child) for child in item]
        if isinstance(item, str):
            return _sanitize_research_text(item)
        if not isinstance(item, Mapping):
            return item
        sanitized: JsonObject = {}
        for raw_key, child in item.items():
            key = str(raw_key)
            lowered = key.lower().replace("-", "_")
            if lowered in _HIDDEN_REASONING_KEYS and isinstance(
                child, (str, list, Mapping)
            ):
                sanitized[key] = "[HIDDEN_MODEL_REASONING]"
            elif (
                lowered in _SENSITIVE_EXACT_KEYS
                or any(part in lowered for part in _SENSITIVE_KEY_PARTS)
            ):
                sanitized[key] = "[REDACTED]"
            elif lowered in {"env", "environment"}:
                sanitized[key] = "[REDACTED_ENVIRONMENT]"
            else:
                sanitized[key] = harden(child)
        return sanitized

    return harden(baseline)


def _assert_sanitized_value(value: Any, *, label: str) -> None:
    if _sanitize_research_value(value) != value:
        raise ResearchCorpusError(f"{label} violates the corpus redaction policy")


def _artifact_metadata_is_text(
    declared_path: str,
    media_type: str | None,
) -> bool:
    suffix = Path(declared_path).suffix.lower()
    if suffix in _SENSITIVE_ARTIFACT_SUFFIXES:
        return False
    if suffix in _TEXT_ARTIFACT_SUFFIXES:
        return True
    normalized_type = (media_type or "").split(";", 1)[0].strip().lower()
    return (
        normalized_type.startswith("text/")
        or normalized_type in _TEXT_MEDIA_TYPES
    )


def _artifact_is_text(artifact: _ArtifactSource) -> bool:
    return _artifact_metadata_is_text(
        artifact.declared_path,
        artifact.media_type,
    )


def _sanitize_artifact_text(value: str, *, suffix: str) -> str:
    """Sanitize UTF-8 artifact text while preserving useful script structure."""

    if "\x00" in value:
        raise ResearchCorpusError("Text artifact contains a NUL byte")
    if suffix == ".json":
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ResearchCorpusError("JSON artifact is not valid JSON") from error
        return json.dumps(
            _sanitize_research_value(decoded),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + ("\n" if value.endswith("\n") else "")
    if suffix == ".jsonl":
        lines: list[str] = []
        for line_number, raw_line in enumerate(value.splitlines(), start=1):
            if not raw_line.strip():
                raise ResearchCorpusError(
                    f"JSONL artifact contains a blank line at {line_number}"
                )
            try:
                decoded = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise ResearchCorpusError(
                    f"JSONL artifact is invalid at line {line_number}"
                ) from error
            lines.append(
                json.dumps(
                    _sanitize_research_value(decoded),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return "\n".join(lines) + ("\n" if value.endswith("\n") else "")
    return _sanitize_research_text(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_regular(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ResearchCorpusError(f"Research source is missing or unsafe: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _record_type(record: Mapping[str, Any]) -> str:
    value = str(record.get("type", ""))
    return {
        "trace_started": "trajectory_started",
        "trace_finished": "trajectory_finished",
        "trace_sealed": "trajectory_sealed",
    }.get(value, value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _display_path(value: str, run_id: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("payload/artifacts/"):
        return "artifacts/" + normalized.removeprefix("payload/artifacts/")
    marker = "/artifacts/"
    if marker in normalized:
        return "artifacts/" + normalized.rsplit(marker, 1)[1]
    run_marker = f"/runs/{run_id}/"
    if run_marker in normalized:
        return normalized.rsplit(run_marker, 1)[1]
    if Path(normalized).is_absolute():
        return Path(normalized).name
    return normalized


def _scrub_absolute_paths(text: str, run_id: str) -> str:
    return _PATH_PATTERN.sub(
        lambda match: _display_path(match.group(0), run_id),
        text,
    )


def _collect_paths(value: Any, run_id: str) -> list[str]:
    paths: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if key.endswith("path") and isinstance(item, str) and item:
                paths.add(_display_path(item, run_id))
            else:
                paths.update(_collect_paths(item, run_id))
    elif isinstance(value, list):
        for item in value:
            paths.update(_collect_paths(item, run_id))
    return sorted(paths)


def _strict_trajectory_records(path: Path, execution_id: str) -> tuple[JsonObject, ...]:
    records: list[JsonObject] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    raise ResearchCorpusError(
                        f"Trajectory {execution_id} contains a blank line at {line_number}"
                    )
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise ResearchCorpusError(
                        f"Trajectory {execution_id} has invalid JSON at line {line_number}"
                    ) from error
                if not isinstance(value, dict):
                    raise ResearchCorpusError(
                        f"Trajectory {execution_id} line {line_number} is not an object"
                    )
                sequence = value.get("seq")
                if (
                    isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or sequence != line_number
                ):
                    raise ResearchCorpusError(
                        f"Trajectory {execution_id} seq must be continuous from one; "
                        f"line={line_number}, seq={sequence!r}"
                    )
                if value.get("run_id") != execution_id:
                    raise ResearchCorpusError(
                        f"Trajectory run identity differs from Execution {execution_id}"
                    )
                records.append(value)
    except (OSError, UnicodeError) as error:
        raise ResearchCorpusError(
            f"Trajectory {execution_id} cannot be read as UTF-8"
        ) from error
    if not records:
        raise ResearchCorpusError(f"Trajectory {execution_id} is empty")
    if _record_type(records[0]) != "trajectory_started":
        raise ResearchCorpusError(f"Trajectory {execution_id} does not start correctly")
    if _record_type(records[-1]) != "trajectory_sealed":
        raise ResearchCorpusError(f"Trajectory {execution_id} is not sealed at EOF")
    return tuple(records)


def _trajectory_outcome(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for record in reversed(records):
        if _record_type(record) == "trajectory_finished":
            return _mapping(_mapping(record.get("payload")).get("outcome"))
    return {}


def _analysis_reports(
    repository: SkillHierarchyRepository,
    *,
    skill_id: str,
    execution_id: str,
) -> tuple[_AcceptedReport, ...]:
    reports: list[_AcceptedReport] = []
    accepted = [
        record
        for record in repository.list_analyses(
            skill_id, execution_id=execution_id
        )
        if record["status"] == "accepted"
        and record["kind"] in {
            "precheck",
            "trajectory_error",
            "trace_error",
        }
    ]
    kinds = {
        "trajectory_error" if str(record["kind"]) == "trace_error" else str(record["kind"])
        for record in accepted
    }
    missing = {"precheck", "trajectory_error"} - kinds
    if missing:
        raise ResearchCorpusError(
            f"Execution {execution_id} lacks accepted single reports: "
            f"{sorted(missing)}"
        )
    for record in sorted(accepted, key=lambda item: str(item["analysis_id"])):
        directory = repository.analysis_directory(record)
        result_refs = record.get("result_refs")
        if not isinstance(result_refs, list) or not result_refs:
            raise ResearchCorpusError(
                f"Accepted analysis {record['analysis_id']} has no result"
            )
        for reference in result_refs:
            if not isinstance(reference, Mapping):
                raise ResearchCorpusError("Accepted analysis result ref is invalid")
            path = reference.get("path")
            schema = reference.get("schema")
            if not isinstance(path, str) or not isinstance(schema, str):
                raise ResearchCorpusError("Accepted analysis result ref is incomplete")
            try:
                source = repository.resolve_object_file(directory, path)
            except (HierarchyError, StorageError) as error:
                raise ResearchCorpusError(str(error)) from error
            reports.append(
                _AcceptedReport(
                    analysis_id=str(record["analysis_id"]),
                    kind=str(record["kind"]),
                    schema=schema,
                    source=source,
                    source_sha256=_sha256(source),
                )
            )
    prechecks = [
        item
        for item in reports
        if item.schema in {"trajectory.precheck.v1", "trace.precheck.v1"}
    ]
    if not prechecks:
        raise ResearchCorpusError(
            f"Execution {execution_id} lacks an accepted precheck result"
        )
    precheck = load_json_object(prechecks[-1].source)
    if (
        precheck.get("run_id") != execution_id
        or _mapping(precheck.get("integrity")).get("status") != "valid"
    ):
        raise ResearchCorpusError(
            f"Execution {execution_id} accepted precheck is not integrity-valid"
        )
    return tuple(reports)


def _artifact_sources(
    *,
    execution: Mapping[str, Any],
    directory: Path,
) -> tuple[_ArtifactSource, ...]:
    result: list[_ArtifactSource] = []
    status = str(execution["status"])
    reserved_paths = {
        str(path)
        for path in (
            _mapping(execution.get("trajectory")).get("path"),
            _mapping(execution.get("session")).get("path"),
        )
        if isinstance(path, str)
    }
    for role, field in (
        ("input", "inputs"),
        ("output", "outputs"),
        ("supporting", "supporting_artifacts"),
    ):
        raw_records = execution.get(field)
        if not isinstance(raw_records, list):
            raise ResearchCorpusError(f"Execution {field} is invalid")
        for record in raw_records:
            if not isinstance(record, Mapping):
                raise ResearchCorpusError(f"Execution {field} entry is invalid")
            relative = str(record["path"])
            raw_path = Path(relative)
            if raw_path.is_absolute() or ".." in raw_path.parts:
                raise ResearchCorpusError("Artifact path is unsafe")
            if raw_path.name.lower() in _RESERVED_ARTIFACT_NAMES:
                raise ResearchCorpusError(
                    f"Artifact uses a reserved corpus file name: {relative}"
                )
            if PurePosixPath(relative).as_posix() in reserved_paths:
                raise ResearchCorpusError(
                    f"Artifact aliases private execution state: {relative}"
                )
            unresolved = directory / raw_path
            current = directory
            for part in raw_path.parts:
                current = current / part
                if current.is_symlink():
                    raise ResearchCorpusError("Artifact path is unsafe")
            candidate = unresolved.resolve(strict=False)
            if not candidate.is_relative_to(directory):
                raise ResearchCorpusError("Artifact path is unsafe")
            declared_hash = record.get("sha256")
            declared_size = record.get("bytes")
            source: Path | None = None
            if declared_hash is None:
                if candidate.exists():
                    raise ResearchCorpusError(
                        f"Artifact exists without a sealed hash: {relative}"
                    )
                if role == "input" or (role == "output" and status == "succeeded"):
                    raise ResearchCorpusError(
                        f"Required artifact is missing: {relative}"
                    )
            else:
                if not candidate.is_file():
                    raise ResearchCorpusError(f"Artifact is missing: {relative}")
                observed_size = candidate.stat().st_size
                observed_hash = _sha256(candidate)
                if observed_size != declared_size or observed_hash != declared_hash:
                    raise ResearchCorpusError(
                        f"Artifact differs from its Execution manifest: {relative}"
                    )
                source = candidate
            result.append(
                _ArtifactSource(
                    role=role,
                    artifact_id=str(record["artifact_id"]),
                    declared_path=relative,
                    source=source,
                    size=declared_size if isinstance(declared_size, int) else None,
                    sha256=declared_hash if isinstance(declared_hash, str) else None,
                    media_type=(
                        str(record["media_type"])
                        if isinstance(record.get("media_type"), str)
                        else None
                    ),
                )
            )
    return tuple(result)


def _prepare_execution(
    repository: SkillHierarchyRepository,
    *,
    skill_id: str,
    execution_id: str,
) -> _PreparedExecution:
    execution = repository.load_execution(skill_id, execution_id)
    if execution["status"] not in _TERMINAL_EXECUTION_STATUSES:
        raise ResearchCorpusError(f"Execution {execution_id} is not terminal")
    trajectory_info = _mapping(execution.get("trajectory"))
    if trajectory_info.get("sealed") is not True:
        raise ResearchCorpusError(f"Execution {execution_id} trajectory is not sealed")
    trajectory_relative = trajectory_info.get("path")
    if not isinstance(trajectory_relative, str):
        raise ResearchCorpusError(f"Execution {execution_id} has no trajectory path")
    directory = repository.execution_directory(skill_id, execution_id)
    try:
        trajectory = repository.resolve_object_file(directory, trajectory_relative)
    except (HierarchyError, StorageError) as error:
        raise ResearchCorpusError(str(error)) from error
    records = _strict_trajectory_records(trajectory, execution_id)
    fresh_precheck = precheck_trajectory(trajectory)
    if _mapping(fresh_precheck.get("integrity")).get("status") != "valid":
        raise ResearchCorpusError(
            f"Execution {execution_id} fails a fresh trajectory integrity check"
        )
    outcome = _trajectory_outcome(records)
    if outcome.get("status") != execution["status"]:
        raise ResearchCorpusError(
            f"Execution {execution_id} outcome differs from its manifest"
        )
    return _PreparedExecution(
        execution_id=execution_id,
        execution=execution,
        directory=directory,
        trajectory=trajectory,
        source_trajectory_sha256=_sha256(trajectory),
        records=records,
        accepted_reports=_analysis_reports(
            repository,
            skill_id=skill_id,
            execution_id=execution_id,
        ),
        artifacts=_artifact_sources(execution=execution, directory=directory),
    )


def _usage_facts(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    tokens = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
    }
    model_calls = 0
    reported_cost_total = 0.0
    complete = True
    for record in records:
        if _record_type(record) != "message_action":
            continue
        message = _mapping(_mapping(record.get("payload")).get("message"))
        usage = _mapping(message.get("usage"))
        if not usage:
            continue
        model_calls += 1
        for destination, source in (
            ("input", "input"),
            ("output", "output"),
            ("cache_read", "cacheRead"),
            ("cache_write", "cacheWrite"),
            ("total", "totalTokens"),
        ):
            value = _number(usage.get(source))
            if value is None or value < 0:
                complete = False
                continue
            tokens[destination] += value
        cost = _number(_mapping(usage.get("cost")).get("total"))
        if cost is None or cost < 0:
            complete = False
        else:
            reported_cost_total += float(cost)
    available = model_calls > 0
    return {
        "available": available,
        "complete": available and complete,
        "model_calls": model_calls,
        "tokens": tokens,
        "reported_cost_total": round(reported_cost_total, 12),
    }


def _script_language(path: str) -> str | None:
    return {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".ts": "typescript",
        ".sh": "shell",
        ".rb": "ruby",
        ".pl": "perl",
    }.get(Path(path).suffix.lower())


def _navigation_for_execution(
    prepared: _PreparedExecution,
) -> tuple[list[JsonObject], list[JsonObject]]:
    run_id = prepared.execution_id
    entries: list[JsonObject] = []
    scripts: list[JsonObject] = []
    output_paths = {
        _display_path(item.declared_path, run_id)
        for item in prepared.artifacts
        if item.role == "output"
    }
    known_scripts: dict[str, set[str]] = {}
    unmatched_failures: list[JsonObject] = []
    for record in prepared.records:
        sanitized_record = _sanitize_research_value(record)
        if not isinstance(sanitized_record, Mapping):
            raise ResearchCorpusError("Sanitized navigation record is invalid")
        payload = _mapping(sanitized_record.get("payload"))
        record_type = _record_type(sanitized_record)
        sequence = int(record["seq"])
        tool_name: str | None = None
        status: str | None = None
        paths = set(_collect_paths(payload, run_id))
        flags: set[str] = set()
        recovered_failure_seqs: list[int] = []
        arguments: Mapping[str, Any] = {}
        if record_type == "tool_action":
            tool_name = str(payload.get("tool_name", "unknown"))
            status = str(payload.get("status", "unknown"))
            arguments = _mapping(payload.get("arguments"))
            argument_path = arguments.get("path")
            content = arguments.get("content")
            if isinstance(argument_path, str):
                display = _display_path(argument_path, run_id)
                paths.add(display)
                language = _script_language(display)
                if tool_name in {"write", "edit"} and language is not None:
                    script_content = content if isinstance(content, str) else ""
                    targets = {
                        output
                        for output in output_paths
                        if Path(output).name in script_content
                    }
                    known_scripts[Path(display).name] = targets
                    scripts.append(
                        {
                            "run_id": run_id,
                            "seq": sequence,
                            "event": "created" if tool_name == "write" else "modified",
                            "path": display,
                            "language": language,
                            "content_sha256": hashlib.sha256(
                                script_content.encode("utf-8")
                            ).hexdigest(),
                            "content": script_content,
                            "targets": sorted(targets),
                            "evidence": {"run_id": run_id, "seq": sequence},
                        }
                    )
                    flags.add("script")
                    paths.update(targets)
            command_value = arguments.get("command")
            command = command_value if isinstance(command_value, str) else ""
            for script_name, targets in known_scripts.items():
                if script_name in command:
                    script_path = next(
                        (
                            item["path"]
                            for item in reversed(scripts)
                            if Path(str(item["path"])).name == script_name
                        ),
                        script_name,
                    )
                    scripts.append(
                        {
                            "run_id": run_id,
                            "seq": sequence,
                            "event": "executed",
                            "path": script_path,
                            "language": _script_language(str(script_path)),
                            "content_sha256": None,
                            "content": None,
                            "targets": sorted(targets),
                            "evidence": {"run_id": run_id, "seq": sequence},
                        }
                    )
                    flags.add("script")
                    paths.update(targets)
            if status in {"failed", "interrupted"}:
                flags.add("failure")
                unmatched_failures.append(
                    {"seq": sequence, "tool_name": tool_name, "paths": set(paths)}
                )
            elif status == "succeeded":
                for failure in reversed(unmatched_failures):
                    same_path = bool(paths & failure["paths"])
                    same_pathless_tool = (
                        not paths
                        and not failure["paths"]
                        and tool_name == failure["tool_name"]
                    )
                    if not same_path and not same_pathless_tool:
                        continue
                    flags.add("recovery")
                    recovered_failure_seqs.append(int(failure["seq"]))
                    unmatched_failures.remove(failure)
                    break
            if _VALIDATION_PATTERN.search(command) or (
                tool_name == "read" and bool(paths & output_paths)
            ):
                flags.add("validation")
        usage = _usage_facts([record])
        if usage["available"]:
            flags.add("resource")
        raw_search = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        entry: JsonObject = {
            "run_id": run_id,
            "seq": sequence,
            "elapsed_ms": record.get("elapsed_ms"),
            "record_type": record_type,
            "tool_name": tool_name,
            "status": status,
            "paths": sorted(paths),
            "flags": sorted(flags),
            "recovered_failure_seqs": recovered_failure_seqs,
            "resource": usage if usage["available"] else None,
            "search_text": _scrub_absolute_paths(raw_search, run_id),
            "evidence": {"run_id": run_id, "seq": sequence},
        }
        entries.append(entry)
    return entries, scripts


def _build_navigation_index(
    prepared: Sequence[_PreparedExecution],
) -> JsonObject:
    entries: list[JsonObject] = []
    scripts: list[JsonObject] = []
    for execution in prepared:
        run_entries, run_scripts = _navigation_for_execution(execution)
        entries.extend(run_entries)
        scripts.extend(run_scripts)
    return {
        "schema": RESEARCH_NAVIGATION_INDEX_SCHEMA,
        "entries": entries,
        "scripts": scripts,
    }


def _aggregate(
    rows: Sequence[Mapping[str, Any]], field: str
) -> JsonObject:
    samples: list[tuple[str, float]] = []
    missing: list[str] = []
    for row in rows:
        value = _number(row.get(field))
        if value is None:
            missing.append(str(row["run_id"]))
        else:
            samples.append((str(row["run_id"]), float(value)))
    if not samples:
        return {
            "sample_count": 0,
            "missing_run_ids": missing,
            "min": None,
            "median": None,
            "max": None,
            "mean": None,
            "coefficient_of_variation": None,
        }
    values = [value for _, value in samples]
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)
    return {
        "sample_count": len(values),
        "missing_run_ids": missing,
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "mean": round(mean, 12),
        "coefficient_of_variation": (
            round(deviation / mean, 12) if mean != 0 else None
        ),
    }


def _build_baseline(
    prepared: Sequence[_PreparedExecution],
    navigation_index: Mapping[str, Any],
) -> JsonObject:
    entries_by_run: dict[str, list[Mapping[str, Any]]] = {}
    for entry in navigation_index["entries"]:
        entries_by_run.setdefault(str(entry["run_id"]), []).append(entry)
    rows: list[JsonObject] = []
    statuses: Counter[str] = Counter()
    failure_types: Counter[str] = Counter()
    for execution in prepared:
        run_id = execution.execution_id
        status = str(execution.execution["status"])
        statuses[status] += 1
        outcome = _trajectory_outcome(execution.records)
        error_type = _mapping(outcome.get("error")).get("type")
        if isinstance(error_type, str) and error_type:
            failure_types[error_type] += 1
        usage = _usage_facts(execution.records)
        tool_records = [
            record
            for record in execution.records
            if _record_type(record) == "tool_action"
        ]
        tool_duration_ms = sum(
            float(value)
            for record in tool_records
            if (
                value := _number(_mapping(record.get("payload")).get("duration_ms"))
            )
            is not None
        )
        run_entries = entries_by_run.get(run_id, [])
        row: JsonObject = {
            "run_id": run_id,
            "status": status,
            "duration_ms": execution.execution.get("duration_ms"),
            "model_calls": usage["model_calls"] if usage["complete"] else None,
            "input_tokens": (
                usage["tokens"]["input"] if usage["complete"] else None
            ),
            "output_tokens": (
                usage["tokens"]["output"] if usage["complete"] else None
            ),
            "cache_read_tokens": (
                usage["tokens"]["cache_read"] if usage["complete"] else None
            ),
            "cache_write_tokens": (
                usage["tokens"]["cache_write"] if usage["complete"] else None
            ),
            "total_tokens": (
                usage["tokens"]["total"] if usage["complete"] else None
            ),
            "reported_cost_total": (
                usage["reported_cost_total"] if usage["complete"] else None
            ),
            "tool_actions": len(tool_records),
            "failed_tool_actions": sum(
                _mapping(record.get("payload")).get("status")
                in {"failed", "interrupted"}
                for record in tool_records
            ),
            "tool_duration_ms": tool_duration_ms,
            "recovery_count": sum(
                "recovery" in entry["flags"] for entry in run_entries
            ),
            "validation_count": sum(
                "validation" in entry["flags"] for entry in run_entries
            ),
            "script_create_or_modify_count": sum(
                item["run_id"] == run_id
                and item["event"] in {"created", "modified"}
                for item in navigation_index["scripts"]
            ),
            "script_execute_count": sum(
                item["run_id"] == run_id and item["event"] == "executed"
                for item in navigation_index["scripts"]
            ),
            "resource_complete": (
                (duration := _number(execution.execution.get("duration_ms")))
                is not None
                and duration >= 0
                and usage["complete"]
            ),
        }
        rows.append(row)
    metrics = (
        "duration_ms",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "reported_cost_total",
        "tool_actions",
        "failed_tool_actions",
        "tool_duration_ms",
        "recovery_count",
        "validation_count",
        "script_create_or_modify_count",
        "script_execute_count",
    )
    succeeded = statuses.get("succeeded", 0)
    denominator = len(rows)
    return {
        "schema": RESEARCH_BASELINE_SCHEMA,
        "results": {
            "eligible": denominator,
            "included": denominator,
            "excluded": 0,
            "missing": 0,
            "succeeded": succeeded,
            "failed": denominator - succeeded,
            "success_rate": succeeded / denominator if denominator else None,
            "status_counts": dict(sorted(statuses.items())),
            "failure_type_counts": dict(sorted(failure_types.items())),
        },
        "runs": rows,
        "aggregate": {metric: _aggregate(rows, metric) for metric in metrics},
    }


def _issue(code: str, message: str, **fields: Any) -> JsonObject:
    return {"code": code, "message": message, **fields}


def _manifest_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchCorpusError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or value != path.as_posix()
    ):
        raise ResearchCorpusError(f"{label} is not a canonical relative path")
    return path.as_posix()


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return isinstance(value, str) and bool(
        re.match(r"^(?:/|[A-Za-z]:[\\/])", value)
    )


def _load_corpus_json(path: Path, *, label: str) -> JsonObject:
    if path.is_symlink() or not path.is_file():
        raise ResearchCorpusError(f"{label} is missing or unsafe")
    try:
        return load_json_object(path)
    except StorageError as error:
        raise ResearchCorpusError(str(error)) from error


def _verify_json_is_sanitized(path: Path, *, label: str) -> JsonObject:
    value = _load_corpus_json(path, label=label)
    _assert_sanitized_value(value, label=label)
    return value


def _verify_trajectory_is_sanitized(path: Path, *, run_id: str) -> int:
    record_count = 0
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    raise ResearchCorpusError(
                        f"Frozen trajectory {run_id} has a blank line at {line_number}"
                    )
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise ResearchCorpusError(
                        f"Frozen trajectory {run_id} is invalid at line {line_number}"
                    ) from error
                if not isinstance(record, Mapping):
                    raise ResearchCorpusError(
                        f"Frozen trajectory {run_id} line {line_number} is invalid"
                    )
                if record.get("run_id") != run_id or record.get("seq") != line_number:
                    raise ResearchCorpusError(
                        f"Frozen trajectory {run_id} identity or sequence is invalid"
                    )
                _assert_sanitized_value(
                    record,
                    label=f"Frozen trajectory {run_id} line {line_number}",
                )
                record_count += 1
    except (OSError, UnicodeError) as error:
        raise ResearchCorpusError(f"Frozen trajectory {run_id} is not safe UTF-8") from error
    return record_count


def _declared_corpus_file(
    root: Path,
    declared: Mapping[str, Mapping[str, Any]],
    raw_path: Any,
    *,
    label: str,
) -> tuple[str, Path]:
    relative = _manifest_relative_path(raw_path, label=label)
    if relative not in declared:
        raise ResearchCorpusError(f"{label} is absent from the file inventory")
    return relative, root / PurePosixPath(relative)


def verify_research_corpus(
    directory: str | os.PathLike[str],
    *,
    expected_content_sha256: str | None = None,
    expected_baseline_sha256: str | None = None,
) -> ResearchCorpusVerification:
    """Reload and verify every byte in a frozen research corpus.

    Callers should persist the returned digests with their research-session
    identity and pass them back as ``expected_*`` values before each use.
    """

    requested = Path(directory)
    if requested.is_symlink() or not requested.is_dir():
        raise ResearchCorpusError("Research corpus root is missing or unsafe")
    root = requested.resolve()
    manifest = _load_corpus_json(root / "corpus.json", label="Corpus manifest")
    expected_fields = {
        "schema",
        "corpus_id",
        "content_sha256",
        "purpose",
        "skill_id",
        "revision_id",
        "objectives",
        "execution_ids",
        "revision_manifest",
        "corpus_map",
        "navigation_index",
        "baseline",
        "readiness",
        "evaluation_suite",
        "task_condition_map",
        "runs",
        "files",
        "redaction",
    }
    if set(manifest) != expected_fields:
        raise ResearchCorpusError("Corpus manifest fields differ from its contract")
    if manifest.get("schema") != RESEARCH_CORPUS_SCHEMA:
        raise ResearchCorpusError("Unsupported research corpus schema")
    if manifest.get("purpose") not in {
        "multi_trajectory_research",
        "multi_trace_research",
    }:
        raise ResearchCorpusError("Research corpus purpose is invalid")
    if manifest.get("redaction") != _REDACTION_POLICY:
        raise ResearchCorpusError("Corpus redaction policy is invalid")
    stored_digest = manifest.get("content_sha256")
    if not isinstance(stored_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", stored_digest
    ):
        raise ResearchCorpusError("Corpus content digest is invalid")
    body = {
        key: value
        for key, value in manifest.items()
        if key not in {"schema", "corpus_id", "content_sha256"}
    }
    observed_digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    if observed_digest != stored_digest:
        raise ResearchCorpusError("Corpus manifest body digest does not match")
    if manifest.get("corpus_id") != f"corpus-{stored_digest[:20]}":
        raise ResearchCorpusError("Corpus identity does not match its digest")
    if expected_content_sha256 is not None and stored_digest != expected_content_sha256:
        raise ResearchCorpusError("Corpus digest differs from the research session")
    if _contains_absolute_path(manifest):
        raise ResearchCorpusError("Corpus manifest contains an absolute path")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ResearchCorpusError("Corpus manifest has no file inventory")
    declared: dict[str, Mapping[str, Any]] = {}
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping) or set(raw_file) != {
            "path",
            "bytes",
            "sha256",
        }:
            raise ResearchCorpusError(f"Corpus file entry {index} is invalid")
        relative = _manifest_relative_path(
            raw_file.get("path"), label=f"files[{index}].path"
        )
        if relative == "corpus.json" or relative in declared:
            raise ResearchCorpusError(
                "Corpus file inventory is duplicated or recursive"
            )
        size = raw_file.get("bytes")
        digest = raw_file.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ResearchCorpusError(f"Corpus file entry {relative} is invalid")
        declared[relative] = raw_file
    if list(declared) != sorted(declared):
        raise ResearchCorpusError("Corpus file inventory is not deterministic")

    observed_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ResearchCorpusError("Research corpus contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ResearchCorpusError("Research corpus contains a special file")
        relative = path.relative_to(root).as_posix()
        if relative != "corpus.json":
            observed_files.add(relative)
    if observed_files != set(declared):
        missing = sorted(set(declared) - observed_files)
        extra = sorted(observed_files - set(declared))
        raise ResearchCorpusError(
            f"Corpus tree differs from inventory: missing={missing}, extra={extra}"
        )
    for relative, record in declared.items():
        path = root / PurePosixPath(relative)
        if path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]:
            raise ResearchCorpusError(f"Corpus file failed verification: {relative}")

    document_paths = {
        "revision_manifest": manifest["revision_manifest"],
        "corpus_map": manifest["corpus_map"],
        "navigation_index": manifest["navigation_index"],
        "baseline": manifest["baseline"],
        "readiness": manifest["readiness"],
    }
    for label, raw_path in document_paths.items():
        relative = _manifest_relative_path(raw_path, label=label)
        if relative not in declared:
            raise ResearchCorpusError(f"{label} is absent from the file inventory")
    corpus_map = _load_corpus_json(
        root / str(manifest["corpus_map"]), label="Corpus map"
    )
    navigation = _load_corpus_json(
        root / str(manifest["navigation_index"]), label="Navigation index"
    )
    baseline = _load_corpus_json(
        root / str(manifest["baseline"]), label="Research baseline"
    )
    readiness = _verify_json_is_sanitized(
        root / str(manifest["readiness"]), label="Research readiness"
    )
    if corpus_map.get("schema") != RESEARCH_CORPUS_MAP_SCHEMA:
        raise ResearchCorpusError("Corpus map schema is invalid")
    if navigation.get("schema") != RESEARCH_NAVIGATION_INDEX_SCHEMA:
        raise ResearchCorpusError("Navigation index schema is invalid")
    if baseline.get("schema") != RESEARCH_BASELINE_SCHEMA:
        raise ResearchCorpusError("Research baseline schema is invalid")
    _assert_sanitized_value(corpus_map, label="Corpus map")
    _assert_sanitized_value(navigation, label="Navigation index")
    _assert_sanitized_value(baseline, label="Research baseline")
    if set(readiness) != {
        "schema",
        "status",
        "skill_id",
        "revision_id",
        "objectives",
        "execution_ids",
        "condition_groups",
        "coverage",
        "issues",
    } or readiness.get("schema") != RESEARCH_READINESS_SCHEMA:
        raise ResearchCorpusError("Research readiness schema is invalid")
    if readiness.get("status") != "ready" or readiness.get("issues") != []:
        raise ResearchCorpusError("Frozen research readiness is not ready")

    raw_execution_ids = manifest.get("execution_ids")
    if (
        not isinstance(raw_execution_ids, list)
        or not raw_execution_ids
        or not all(isinstance(item, str) and item for item in raw_execution_ids)
        or len(raw_execution_ids) != len(set(raw_execution_ids))
    ):
        raise ResearchCorpusError("Corpus execution identities are invalid")
    execution_ids = set(raw_execution_ids)
    if (
        readiness.get("skill_id") != manifest["skill_id"]
        or readiness.get("revision_id") != manifest["revision_id"]
        or readiness.get("objectives") != manifest["objectives"]
        or readiness.get("execution_ids") != raw_execution_ids
    ):
        raise ResearchCorpusError("Research readiness differs from the manifest")

    coverage_requested = CONDITIONS_COVERAGE in manifest["objectives"]
    suite_raw_path = manifest.get("evaluation_suite")
    mapping_raw_path = manifest.get("task_condition_map")
    suite_evidence_present = (
        suite_raw_path is not None or mapping_raw_path is not None
    )
    if coverage_requested and not suite_evidence_present:
        raise ResearchCorpusError(
            "Coverage corpus lacks frozen EvaluationSuite evidence"
        )
    if suite_evidence_present:
        if suite_raw_path is None or mapping_raw_path is None:
            raise ResearchCorpusError(
                "EvaluationSuite evidence requires both suite and mapping"
            )
        _, suite_path = _declared_corpus_file(
            root,
            declared,
            suite_raw_path,
            label="evaluation_suite",
        )
        _, mapping_path = _declared_corpus_file(
            root,
            declared,
            mapping_raw_path,
            label="task_condition_map",
        )
        suite_document = _verify_json_is_sanitized(
            suite_path, label="Frozen EvaluationSuite"
        )
        try:
            suite_document = validate_evaluation_suite(suite_document)
        except EvaluationSuiteError as error:
            raise ResearchCorpusError(str(error)) from error
        if (
            suite_document["status"] != "approved"
            or suite_document["skill_id"] != manifest["skill_id"]
        ):
            raise ResearchCorpusError(
                "Frozen EvaluationSuite is not approved for Skill"
            )
        task_condition_map = _verify_json_is_sanitized(
            mapping_path, label="Frozen task and condition map"
        )
        if set(task_condition_map) != {
            "schema",
            "suite_id",
            "skill_id",
            "task_cases",
            "execution_mapping",
            "coverage",
        } or task_condition_map.get("schema") != RESEARCH_TASK_CONDITION_MAP_SCHEMA:
            raise ResearchCorpusError("Task and condition map schema is invalid")
        if (
            task_condition_map.get("suite_id") != suite_document["suite_id"]
            or task_condition_map.get("skill_id") != manifest["skill_id"]
            or task_condition_map.get("coverage") != readiness["coverage"]
        ):
            raise ResearchCorpusError("Task and condition map identity is invalid")
        references = {
            str(item["task_case_id"]): item
            for item in suite_document["task_cases"]
        }
        raw_cases = task_condition_map.get("task_cases")
        mapped_case_ids = [
            item.get("task_case_id")
            for item in raw_cases
            if isinstance(item, Mapping)
        ] if isinstance(raw_cases, list) else []
        if (
            not isinstance(raw_cases, list)
            or len(mapped_case_ids) != len(raw_cases)
            or len(mapped_case_ids) != len(set(mapped_case_ids))
            or set(mapped_case_ids) != set(references)
        ):
            raise ResearchCorpusError(
                "Task and condition map does not cover suite cases"
            )
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping) or set(raw_case) != {
                "task_case_id",
                "conditions",
                "task",
            }:
                raise ResearchCorpusError("Normalized TaskCase record is invalid")
            task_case_id = str(raw_case["task_case_id"])
            task = raw_case.get("task")
            if (
                raw_case["conditions"] != references[task_case_id]["conditions"]
                or not isinstance(task, Mapping)
                or task.get("schema") != "task.case.v1"
                or task.get("task_case_id") != task_case_id
                or task.get("delivery") not in {"file", "inline_text"}
            ):
                raise ResearchCorpusError("Normalized TaskCase conditions differ")
        raw_mapping = task_condition_map.get("execution_mapping")
        mapped_run_ids = [
            item.get("run_id")
            for item in raw_mapping
            if isinstance(item, Mapping)
        ] if isinstance(raw_mapping, list) else []
        if (
            not isinstance(raw_mapping, list)
            or len(mapped_run_ids) != len(raw_mapping)
            or len(mapped_run_ids) != len(set(mapped_run_ids))
            or set(mapped_run_ids) != execution_ids
        ):
            raise ResearchCorpusError("Condition map does not cover manifest trajectories")
        for item in raw_mapping:
            if not isinstance(item, Mapping) or set(item) != {
                "run_id",
                "task_case_id",
                "conditions",
                "declared_comparable_group",
            }:
                raise ResearchCorpusError("Execution condition mapping is invalid")
            task_case_id = item["task_case_id"]
            if (
                task_case_id not in references
                or item["conditions"] != references[task_case_id]["conditions"]
                or item["declared_comparable_group"]
                != readiness["condition_groups"].get(item["run_id"])
            ):
                raise ResearchCorpusError("Execution condition mapping differs")

    map_trajectories = corpus_map.get(
        "trajectories", corpus_map.get("traces")
    )
    baseline_runs = baseline.get("runs")
    if not isinstance(map_trajectories, list) or {
        item.get("run_id") for item in map_trajectories if isinstance(item, Mapping)
    } != execution_ids:
        raise ResearchCorpusError("Corpus map does not cover the manifest trajectories")
    if not isinstance(baseline_runs, list) or {
        item.get("run_id") for item in baseline_runs if isinstance(item, Mapping)
    } != execution_ids:
        raise ResearchCorpusError("Baseline does not cover the manifest trajectories")
    if (
        corpus_map.get("skill_id") != manifest["skill_id"]
        or corpus_map.get("revision_id") != manifest["revision_id"]
        or corpus_map.get("objectives") != manifest["objectives"]
    ):
        raise ResearchCorpusError("Corpus map identity differs from the manifest")

    raw_runs = manifest.get("runs")
    if not isinstance(raw_runs, list) or len(raw_runs) != len(execution_ids):
        raise ResearchCorpusError("Corpus run manifests are invalid")
    run_ids = [
        item.get("execution_id") for item in raw_runs if isinstance(item, Mapping)
    ]
    if len(run_ids) != len(raw_runs) or set(run_ids) != execution_ids:
        raise ResearchCorpusError("Corpus run manifests do not cover Executions")
    referenced_run_files: set[str] = set()
    artifact_targets: set[str] = set()
    for raw_run in raw_runs:
        if not isinstance(raw_run, Mapping) or set(raw_run) not in {
            frozenset({"execution_id", "status", "task", "trajectory", "artifacts", "single_reports"}),
            frozenset({"execution_id", "status", "task", "trace", "artifacts", "single_reports"}),
        }:
            raise ResearchCorpusError("Corpus run manifest fields are invalid")
        run_id = str(raw_run["execution_id"])
        expected_root = f"runs/{run_id}"
        task_relative, task_path = _declared_corpus_file(
            root, declared, raw_run["task"], label=f"Run {run_id} task"
        )
        if task_relative != f"{expected_root}/task.json":
            raise ResearchCorpusError("Run task path is outside its fixed namespace")
        _verify_json_is_sanitized(task_path, label=f"Run {run_id} task")
        referenced_run_files.add(task_relative)

        trajectory = raw_run.get("trajectory", raw_run.get("trace"))
        if not isinstance(trajectory, Mapping) or set(trajectory) != {
            "path",
            "records",
            "schema",
            "source_sha256",
            "stored_sha256",
        }:
            raise ResearchCorpusError("Run trajectory reference is invalid")
        trajectory_relative, trajectory_path = _declared_corpus_file(
            root, declared, trajectory["path"], label=f"Run {run_id} trajectory"
        )
        if trajectory_relative not in {
            f"{expected_root}/trajectory.jsonl",
            f"{expected_root}/trace.jsonl",
        }:
            raise ResearchCorpusError("Run trajectory path is outside its fixed namespace")
        if (
            not isinstance(trajectory.get("records"), int)
            or trajectory["records"] <= 0
            or not isinstance(trajectory.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", trajectory["source_sha256"])
            or trajectory.get("stored_sha256") != declared[trajectory_relative]["sha256"]
        ):
            raise ResearchCorpusError("Run trajectory metadata is invalid")
        if _verify_trajectory_is_sanitized(trajectory_path, run_id=run_id) != trajectory["records"]:
            raise ResearchCorpusError("Run trajectory record count is invalid")
        referenced_run_files.add(trajectory_relative)

        raw_artifacts = raw_run.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise ResearchCorpusError("Run artifacts must be an array")
        artifact_keys: set[tuple[str, str]] = set()
        for artifact in raw_artifacts:
            if not isinstance(artifact, Mapping) or set(artifact) != {
                "role",
                "artifact_id",
                "declared_path",
                "source_available",
                "available",
                "path",
                "bytes",
                "sha256",
                "stored_bytes",
                "stored_sha256",
                "media_type",
                "redaction",
                "exclusion_reason",
            }:
                raise ResearchCorpusError("Run artifact reference is invalid")
            role = artifact.get("role")
            artifact_id = artifact.get("artifact_id")
            if (
                role not in {"input", "output", "supporting"}
                or not isinstance(artifact_id, str)
                or not artifact_id
            ):
                raise ResearchCorpusError("Run artifact identity is invalid")
            key = (str(role), artifact_id)
            if key in artifact_keys:
                raise ResearchCorpusError("Run artifact identity is duplicated")
            artifact_keys.add(key)
            declared_path = _manifest_relative_path(
                artifact.get("declared_path"), label="artifact.declared_path"
            )
            source_name = PurePosixPath(declared_path).name
            media_type = artifact.get("media_type")
            if media_type is not None and not isinstance(media_type, str):
                raise ResearchCorpusError("Artifact media type is invalid")
            if source_name.lower() in _RESERVED_ARTIFACT_NAMES:
                raise ResearchCorpusError("Artifact uses a reserved corpus file name")
            available = artifact.get("available")
            source_available = artifact.get("source_available")
            if not isinstance(available, bool) or not isinstance(
                source_available, bool
            ):
                raise ResearchCorpusError("Run artifact availability is invalid")
            if available:
                if source_available is not True:
                    raise ResearchCorpusError(
                        "Included artifact cannot claim a missing source"
                    )
                if not _artifact_metadata_is_text(declared_path, media_type):
                    raise ResearchCorpusError(
                        "Binary or sensitive artifact cannot enter the corpus"
                    )
                relative, path = _declared_corpus_file(
                    root,
                    declared,
                    artifact.get("path"),
                    label="artifact.path",
                )
                expected = (
                    f"{expected_root}/artifacts/{role}/{artifact_id}/{source_name}"
                )
                if relative != expected or relative in artifact_targets:
                    raise ResearchCorpusError(
                        "Artifact target is duplicated or outside its fixed namespace"
                    )
                artifact_targets.add(relative)
                referenced_run_files.add(relative)
                if (
                    artifact.get("redaction") not in {"none", "sanitized"}
                    or artifact.get("exclusion_reason") is not None
                    or artifact.get("stored_bytes") != declared[relative]["bytes"]
                    or artifact.get("stored_sha256") != declared[relative]["sha256"]
                ):
                    raise ResearchCorpusError("Stored artifact metadata is invalid")
                try:
                    text = path.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeError) as error:
                    raise ResearchCorpusError(
                        "Included artifact is not safe UTF-8 text"
                    ) from error
                if _sanitize_artifact_text(
                    text, suffix=PurePosixPath(source_name).suffix.lower()
                ) != text:
                    raise ResearchCorpusError(
                        "Included artifact violates the redaction policy"
                    )
            elif (
                artifact.get("path") is not None
                or artifact.get("stored_bytes") is not None
                or artifact.get("stored_sha256") is not None
                or artifact.get("redaction") is not None
                or not isinstance(artifact.get("exclusion_reason"), str)
                or not artifact["exclusion_reason"]
            ):
                raise ResearchCorpusError("Excluded artifact metadata is invalid")

        raw_reports = raw_run.get("single_reports")
        if not isinstance(raw_reports, list) or not raw_reports:
            raise ResearchCorpusError("Run single-report references are invalid")
        for report in raw_reports:
            if not isinstance(report, Mapping) or set(report) != {
                "analysis_id",
                "kind",
                "schema",
                "path",
                "source_sha256",
                "stored_sha256",
            }:
                raise ResearchCorpusError("Single-report reference is invalid")
            relative, report_path = _declared_corpus_file(
                root,
                declared,
                report.get("path"),
                label="single_report.path",
            )
            if not relative.startswith(f"{expected_root}/single-reports/"):
                raise ResearchCorpusError("Single report is outside its run namespace")
            if (
                relative in referenced_run_files
                or report.get("stored_sha256") != declared[relative]["sha256"]
                or not isinstance(report.get("source_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", report["source_sha256"])
            ):
                raise ResearchCorpusError("Single-report target is duplicated")
            _verify_json_is_sanitized(
                report_path, label=f"Run {run_id} single report"
            )
            referenced_run_files.add(relative)

    observed_run_files = {
        relative for relative in declared if relative.startswith("runs/")
    }
    if observed_run_files != referenced_run_files:
        raise ResearchCorpusError("Run file inventory contains unreferenced evidence")

    baseline_path = root / str(manifest["baseline"])
    baseline_digest = _sha256(baseline_path)
    if (
        expected_baseline_sha256 is not None
        and baseline_digest != expected_baseline_sha256
    ):
        raise ResearchCorpusError("Baseline digest differs from the research session")
    return ResearchCorpusVerification(
        root,
        manifest,
        corpus_map,
        navigation,
        baseline,
        stored_digest,
        baseline_digest,
    )


class ResearchCorpusBuilder:
    """Check readiness and freeze raw observable evidence without product writes."""

    def __init__(
        self,
        runtime_root: str | os.PathLike[str],
        *,
        evaluation_suites_root: str | os.PathLike[str] | None = None,
        project_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.repository = SkillHierarchyRepository(runtime_root)
        self.suite_resolver = (
            EvaluationSuiteResolver(
                evaluation_suites_root,
                project_root=project_root,
            )
            if evaluation_suites_root is not None
            else None
        )

    @staticmethod
    def _objectives(values: Sequence[str]) -> tuple[str, ...]:
        objectives = tuple(sorted(set(values)))
        if not objectives:
            raise ResearchCorpusError("At least one research objective is required")
        unknown = set(objectives) - RESEARCH_OBJECTIVES
        if unknown:
            raise ResearchCorpusError(
                f"Unsupported research objectives: {sorted(unknown)}"
            )
        return objectives

    def _prepare_selection(
        self,
        *,
        skill_id: str,
        execution_ids: Sequence[str],
    ) -> tuple[list[_PreparedExecution], list[JsonObject]]:
        issues: list[JsonObject] = []
        if not execution_ids:
            return [], [_issue("no_executions", "No Executions were selected")]
        if len(execution_ids) != len(set(execution_ids)):
            issues.append(
                _issue("duplicate_execution", "Execution selection contains duplicates")
            )
        prepared: list[_PreparedExecution] = []
        for execution_id in sorted(set(execution_ids)):
            try:
                prepared.append(
                    _prepare_execution(
                        self.repository,
                        skill_id=skill_id,
                        execution_id=execution_id,
                    )
                )
            except (HierarchyError, StorageError, ResearchCorpusError) as error:
                issues.append(
                    _issue(
                        "execution_not_research_ready",
                        str(error),
                        execution_id=execution_id,
                    )
                )
        revision_ids = {
            str(item.execution["revision_id"]) for item in prepared
        }
        if len(revision_ids) > 1:
            issues.append(
                _issue(
                    "mixed_revisions",
                    "Research selection must use exactly one Skill Revision",
                )
            )
        return prepared, issues

    def _resolve_suite(
        self,
        *,
        skill_id: str,
        suite_id: str | None,
        issues: list[JsonObject],
    ) -> ResolvedEvaluationSuite | None:
        if suite_id is None:
            issues.append(
                _issue(
                    "evaluation_suite_missing",
                    "Conditions and coverage require an EvaluationSuite",
                    objective=CONDITIONS_COVERAGE,
                )
            )
            return None
        if self.suite_resolver is None:
            issues.append(
                _issue(
                    "evaluation_suite_resolver_missing",
                    "No EvaluationSuite resolver is configured",
                    objective=CONDITIONS_COVERAGE,
                )
            )
            return None
        try:
            resolved = self.suite_resolver.resolve(
                suite_id, require_approved=False
            )
        except EvaluationSuiteError as error:
            issues.append(
                _issue(
                    "evaluation_suite_invalid",
                    str(error),
                    objective=CONDITIONS_COVERAGE,
                )
            )
            return None
        if resolved.document["status"] != "approved":
            issues.append(
                _issue(
                    "evaluation_suite_not_approved",
                    "EvaluationSuite is proposed and cannot gate coverage",
                    objective=CONDITIONS_COVERAGE,
                )
            )
            return None
        if resolved.document["skill_id"] != skill_id:
            issues.append(
                _issue(
                    "evaluation_suite_skill_mismatch",
                    "EvaluationSuite belongs to a different Skill",
                    objective=CONDITIONS_COVERAGE,
                )
            )
            return None
        return resolved

    def _readiness_from_prepared(
        self,
        *,
        skill_id: str,
        prepared: Sequence[_PreparedExecution],
        objectives: tuple[str, ...],
        issues: list[JsonObject],
        evaluation_suite_id: str | None,
        condition_groups: Mapping[str, str] | None,
    ) -> tuple[JsonObject, ResolvedEvaluationSuite | None]:
        count = len(prepared)
        if BEHAVIOR_PATTERNS in objectives and count < 3:
            issues.append(
                _issue(
                    "insufficient_behavior_samples",
                    "Behavior pattern research requires at least three Executions",
                    objective=BEHAVIOR_PATTERNS,
                )
            )

        navigation = _build_navigation_index(prepared) if prepared else {
            "entries": [],
            "scripts": [],
        }
        if RECOVERY_SUCCESS in objectives:
            recovered_runs = {
                str(entry["run_id"])
                for entry in navigation["entries"]
                if "recovery" in entry["flags"]
            }
            if len(recovered_runs) < 2:
                issues.append(
                    _issue(
                        "insufficient_recovery_samples",
                        "Recovery research requires two trajectories with observed recovery",
                        objective=RECOVERY_SUCCESS,
                    )
                )

        suite: ResolvedEvaluationSuite | None = None
        suite_conditions: dict[str, Mapping[str, Any]] = {}
        suite_supports_comparison = (
            evaluation_suite_id is not None
            and bool(_COMPARABILITY_OBJECTIVES.intersection(objectives))
        )
        if CONDITIONS_COVERAGE in objectives or suite_supports_comparison:
            suite = self._resolve_suite(
                skill_id=skill_id,
                suite_id=evaluation_suite_id,
                issues=issues,
            )
            if suite is not None:
                references = {
                    str(item["task_case_id"]): item
                    for item in suite.document["task_cases"]
                }
                for item in prepared:
                    task_case_id = item.execution["task"].get("task_case_id")
                    if isinstance(task_case_id, str) and task_case_id in references:
                        conditions = references[task_case_id]["conditions"]
                        if isinstance(conditions, Mapping):
                            suite_conditions[item.execution_id] = conditions
                if (
                    CONDITIONS_COVERAGE not in objectives
                    and suite_supports_comparison
                ):
                    unmapped = sorted(
                        item.execution_id
                        for item in prepared
                        if item.execution_id not in suite_conditions
                    )
                    if unmapped:
                        issues.append(
                            _issue(
                                "comparison_task_case_mapping_missing",
                                "Every selected comparison trajectory must map to "
                                "the explicit EvaluationSuite",
                                execution_ids=unmapped,
                            )
                        )

        declared_groups: dict[str, str] = {}
        if condition_groups is not None:
            for execution_id, raw_group in condition_groups.items():
                if execution_id not in {item.execution_id for item in prepared}:
                    issues.append(
                        _issue(
                            "unknown_condition_execution",
                            "Condition mapping references an unselected Execution",
                            execution_id=execution_id,
                        )
                    )
                elif not isinstance(raw_group, str) or not raw_group.strip():
                    issues.append(
                        _issue(
                            "invalid_condition_group",
                            "Condition group labels must be non-empty",
                            execution_id=execution_id,
                        )
                    )
                else:
                    declared_groups[execution_id] = raw_group.strip()

        prepared_by_id = {item.execution_id: item for item in prepared}
        members_by_group: dict[str, list[str]] = {}
        for execution_id, group in declared_groups.items():
            members_by_group.setdefault(group, []).append(execution_id)
        normalized_groups: dict[str, str] = {}
        for group, execution_ids in sorted(members_by_group.items()):
            bases: set[str] = set()
            missing_by_execution: dict[str, list[str]] = {}
            for execution_id in sorted(execution_ids):
                basis, missing = _comparison_basis(
                    prepared_by_id[execution_id],
                    suite_conditions=suite_conditions.get(execution_id),
                    require_suite_conditions=suite is not None,
                )
                bases.add(basis)
                if missing:
                    missing_by_execution[execution_id] = list(missing)
            if len(bases) != 1 or missing_by_execution:
                issues.append(
                    _issue(
                        "condition_group_not_comparable",
                        "A declared comparison group has different or incomplete "
                        "task, input, model, or runtime conditions",
                        condition_group=group,
                        execution_ids=sorted(execution_ids),
                        missing_runtime_facts=missing_by_execution,
                    )
                )
                continue
            normalized_groups.update(
                {execution_id: group for execution_id in execution_ids}
            )
        comparable_counts = Counter(normalized_groups.values())
        if RESULT_RELIABILITY in objectives and not any(
            value >= 2 for value in comparable_counts.values()
        ):
            issues.append(
                _issue(
                    "insufficient_result_samples",
                    "Result reliability requires two Executions in the same "
                    "declared comparable group",
                    objective=RESULT_RELIABILITY,
                )
            )
        if CONSISTENCY in objectives:
            if not any(value >= 2 for value in comparable_counts.values()):
                issues.append(
                    _issue(
                        "comparable_group_missing",
                        "Consistency requires a declared condition group with "
                        "two trajectories",
                        objective=CONSISTENCY,
                    )
                )

        coverage: JsonObject | None = None
        if CONDITIONS_COVERAGE in objectives:
            if suite is not None:
                case_references = {
                    str(item["task_case_id"]): item
                    for item in suite.document["task_cases"]
                }
                counts: Counter[str] = Counter()
                unmapped: list[str] = []
                for item in prepared:
                    task_case_id = item.execution["task"].get("task_case_id")
                    if (
                        not isinstance(task_case_id, str)
                        or task_case_id not in case_references
                    ):
                        unmapped.append(item.execution_id)
                        continue
                    conditions = case_references[task_case_id]["conditions"]
                    group = json.dumps(
                        conditions,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    counts[group] += 1
                if unmapped:
                    issues.append(
                        _issue(
                            "task_case_mapping_missing",
                            "Every coverage trajectory must map to an "
                            "EvaluationSuite TaskCase",
                            objective=CONDITIONS_COVERAGE,
                            execution_ids=unmapped,
                        )
                    )
                minimum_groups = suite.document["readiness"][
                    "minimum_distinct_condition_groups"
                ]
                minimum_samples = suite.document["readiness"][
                    "minimum_samples_per_condition_group"
                ]
                if len(counts) < minimum_groups:
                    issues.append(
                        _issue(
                            "insufficient_condition_groups",
                            "Too few distinct condition groups for occurrence analysis",
                            objective=CONDITIONS_COVERAGE,
                        )
                    )
                undersampled = sorted(
                    group for group, value in counts.items() if value < minimum_samples
                )
                if undersampled:
                    issues.append(
                        _issue(
                            "undersampled_condition_groups",
                            "Condition groups do not meet the suite sample threshold",
                            objective=CONDITIONS_COVERAGE,
                            condition_groups=undersampled,
                        )
                    )
                represented_case_ids = {
                    str(item.execution["task"].get("task_case_id"))
                    for item in prepared
                    if isinstance(item.execution["task"].get("task_case_id"), str)
                }
                coverage = {
                    "suite_id": suite.document["suite_id"],
                    "represented_task_case_ids": sorted(represented_case_ids),
                    "zero_sample_task_case_ids": sorted(
                        set(case_references) - represented_case_ids
                    ),
                }

        if RESOURCE_EFFICIENCY in objectives:
            complete_by_group: Counter[str] = Counter()
            for item in prepared:
                duration = _number(item.execution.get("duration_ms"))
                usage = _usage_facts(item.records)
                group = normalized_groups.get(item.execution_id)
                if (
                    group is not None
                    and duration is not None
                    and duration >= 0
                    and usage["complete"]
                ):
                    complete_by_group[group] += 1
            if not any(value >= 2 for value in complete_by_group.values()):
                issues.append(
                    _issue(
                        "insufficient_resource_samples",
                        "Resource efficiency requires two complete resource records "
                        "in the same declared comparable group",
                        objective=RESOURCE_EFFICIENCY,
                    )
                )

        revision_ids = sorted(
            {str(item.execution["revision_id"]) for item in prepared}
        )
        readiness: JsonObject = {
            "schema": RESEARCH_READINESS_SCHEMA,
            "status": "ready" if not issues else "not_ready",
            "skill_id": skill_id,
            "revision_id": revision_ids[0] if len(revision_ids) == 1 else None,
            "objectives": list(objectives),
            "execution_ids": [item.execution_id for item in prepared],
            "condition_groups": dict(sorted(normalized_groups.items())),
            "coverage": coverage,
            "issues": issues,
        }
        return readiness, suite

    def assess_readiness(
        self,
        *,
        skill_id: str,
        execution_ids: Sequence[str],
        objectives: Sequence[str],
        evaluation_suite_id: str | None = None,
        condition_groups: Mapping[str, str] | None = None,
    ) -> JsonObject:
        """Return a deterministic ready/not-ready result without writing state."""

        normalized_objectives = self._objectives(objectives)
        prepared, issues = self._prepare_selection(
            skill_id=skill_id,
            execution_ids=execution_ids,
        )
        readiness, _ = self._readiness_from_prepared(
            skill_id=skill_id,
            prepared=prepared,
            objectives=normalized_objectives,
            issues=issues,
            evaluation_suite_id=evaluation_suite_id,
            condition_groups=condition_groups,
        )
        return readiness

    def build(
        self,
        *,
        skill_id: str,
        execution_ids: Sequence[str],
        objectives: Sequence[str],
        destination: str | os.PathLike[str],
        evaluation_suite_id: str | None = None,
        condition_groups: Mapping[str, str] | None = None,
    ) -> ResearchCorpusResult:
        """Atomically build a deterministic corpus after all gates pass."""

        normalized_objectives = self._objectives(objectives)
        prepared, issues = self._prepare_selection(
            skill_id=skill_id,
            execution_ids=execution_ids,
        )
        readiness, suite = self._readiness_from_prepared(
            skill_id=skill_id,
            prepared=prepared,
            objectives=normalized_objectives,
            issues=issues,
            evaluation_suite_id=evaluation_suite_id,
            condition_groups=condition_groups,
        )
        if readiness["status"] != "ready":
            codes = [item["code"] for item in readiness["issues"]]
            raise ResearchCorpusError(f"Research is not ready: {codes}")
        destination_path = Path(destination).resolve()
        if destination_path.exists():
            raise ResearchCorpusError(
                f"Research corpus destination already exists: {destination_path}"
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".research-corpus-",
                dir=destination_path.parent,
            )
        )
        try:
            manifest, corpus_map, navigation, baseline = self._materialize(
                root=temporary,
                skill_id=skill_id,
                prepared=prepared,
                objectives=normalized_objectives,
                readiness=readiness,
                evaluation_suite=suite,
            )
            os.replace(temporary, destination_path)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        verification = verify_research_corpus(destination_path)
        return ResearchCorpusResult(
            destination_path,
            verification.manifest,
            verification.corpus_map,
            verification.navigation_index,
            verification.baseline,
            readiness,
            verification.content_sha256,
            verification.baseline_sha256,
        )

    def _materialize(
        self,
        *,
        root: Path,
        skill_id: str,
        prepared: Sequence[_PreparedExecution],
        objectives: tuple[str, ...],
        readiness: Mapping[str, Any],
        evaluation_suite: ResolvedEvaluationSuite | None,
    ) -> tuple[JsonObject, JsonObject, JsonObject, JsonObject]:
        revision_id = str(readiness["revision_id"])
        revision = self.repository.load_revision(skill_id, revision_id)
        revision_directory = self.repository.revision_directory(
            skill_id, revision_id
        )
        package = revision_directory / "package"
        observed_package_hash, inventory = package_digest(package)
        if observed_package_hash != revision["package_sha256"]:
            raise ResearchCorpusError("Skill Revision package failed hash verification")
        _copy_regular(
            revision_directory / "revision.json",
            root / "revision/revision.json",
        )
        for item in inventory:
            source = package / str(item["path"])
            _copy_regular(source, root / "revision/package" / str(item["path"]))

        run_manifests: list[JsonObject] = []
        frozen_artifact_counts: dict[str, int] = {}
        for item in prepared:
            run_root = root / "runs" / item.execution_id
            trajectory_destination = run_root / "trajectory.jsonl"
            trajectory_destination.parent.mkdir(parents=True, exist_ok=True)
            with trajectory_destination.open("w", encoding="utf-8") as stream:
                for record in item.records:
                    sanitized = _sanitize_research_value(record)
                    if not isinstance(sanitized, Mapping):
                        raise ResearchCorpusError("Sanitized trajectory record is invalid")
                    stream.write(
                        json.dumps(
                            sanitized,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            if _sha256(item.trajectory) != item.source_trajectory_sha256:
                raise ResearchCorpusError(
                    f"Trajectory changed while freezing: {item.execution_id}"
                )
            sanitized_task = _sanitize_research_value(item.execution["task"])
            if not isinstance(sanitized_task, Mapping):
                raise ResearchCorpusError("Sanitized task snapshot is invalid")
            atomic_write_json(run_root / "task.json", sanitized_task)

            artifact_records: list[JsonObject] = []
            artifact_targets: set[str] = set()
            for artifact in item.artifacts:
                frozen_path: str | None = None
                stored_size: int | None = None
                stored_sha256: str | None = None
                redaction = None
                exclusion_reason = None
                if artifact.source is not None:
                    source_name = Path(artifact.declared_path).name
                    destination = (
                        run_root
                        / "artifacts"
                        / artifact.role
                        / artifact.artifact_id
                        / source_name
                    )
                    destination_relative = destination.relative_to(root).as_posix()
                    if destination_relative in artifact_targets:
                        raise ResearchCorpusError(
                            f"Duplicate frozen artifact target: {destination_relative}"
                        )
                    artifact_targets.add(destination_relative)
                    before = _sha256(artifact.source)
                    suffix = Path(artifact.declared_path).suffix.lower()
                    if suffix in _SENSITIVE_ARTIFACT_SUFFIXES:
                        exclusion_reason = "sensitive_file_type"
                    elif not _artifact_is_text(artifact):
                        exclusion_reason = "binary_or_unsupported"
                    else:
                        try:
                            source_text = artifact.source.read_text(
                                encoding="utf-8", errors="strict"
                            )
                            stored_text = _sanitize_artifact_text(
                                source_text,
                                suffix=suffix,
                            )
                        except (OSError, UnicodeError, ResearchCorpusError):
                            exclusion_reason = "unsafe_or_invalid_text"
                        else:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_text(stored_text, encoding="utf-8")
                            frozen_path = destination_relative
                            stored_size = destination.stat().st_size
                            stored_sha256 = _sha256(destination)
                            redaction = (
                                "sanitized" if stored_text != source_text else "none"
                            )
                    after = _sha256(artifact.source)
                    if before != after or before != artifact.sha256:
                        raise ResearchCorpusError(
                            f"Artifact changed while freezing: {artifact.declared_path}"
                        )
                else:
                    exclusion_reason = "source_missing"
                artifact_records.append(
                    {
                        "role": artifact.role,
                        "artifact_id": artifact.artifact_id,
                        "declared_path": artifact.declared_path,
                        "source_available": artifact.source is not None,
                        "available": frozen_path is not None,
                        "path": frozen_path,
                        "bytes": artifact.size,
                        "sha256": artifact.sha256,
                        "stored_bytes": stored_size,
                        "stored_sha256": stored_sha256,
                        "media_type": artifact.media_type,
                        "redaction": redaction,
                        "exclusion_reason": exclusion_reason,
                    }
                )
            frozen_artifact_counts[item.execution_id] = sum(
                record["available"] for record in artifact_records
            )

            report_records: list[JsonObject] = []
            for index, report in enumerate(item.accepted_reports, start=1):
                destination = (
                    run_root
                    / "single-reports"
                    / report.analysis_id
                    / f"{index:02d}-{report.source.name}"
                )
                try:
                    source_report = load_json_object(report.source)
                except StorageError as error:
                    raise ResearchCorpusError(str(error)) from error
                sanitized_report = _sanitize_research_value(source_report)
                if not isinstance(sanitized_report, Mapping):
                    raise ResearchCorpusError("Sanitized single report is invalid")
                atomic_write_json(destination, sanitized_report)
                if _sha256(report.source) != report.source_sha256:
                    raise ResearchCorpusError(
                        f"Single report changed while freezing: {report.analysis_id}"
                    )
                report_records.append(
                    {
                        "analysis_id": report.analysis_id,
                        "kind": report.kind,
                        "schema": report.schema,
                        "path": destination.relative_to(root).as_posix(),
                        "source_sha256": report.source_sha256,
                        "stored_sha256": _sha256(destination),
                    }
                )
            run_manifests.append(
                {
                    "execution_id": item.execution_id,
                    "status": item.execution["status"],
                    "task": (run_root / "task.json").relative_to(root).as_posix(),
                    "trajectory": {
                        "path": trajectory_destination.relative_to(root).as_posix(),
                        "records": len(item.records),
                        "schema": item.execution["trajectory"]["schema"],
                        "source_sha256": item.source_trajectory_sha256,
                        "stored_sha256": _sha256(trajectory_destination),
                    },
                    "artifacts": artifact_records,
                    "single_reports": report_records,
                }
            )

        navigation = _build_navigation_index(prepared)
        baseline = _build_baseline(prepared, navigation)
        _assert_sanitized_value(readiness, label="Readiness snapshot")
        atomic_write_json(root / "readiness.json", readiness)

        evaluation_suite_path: str | None = None
        task_condition_map_path: str | None = None
        if CONDITIONS_COVERAGE in objectives and evaluation_suite is None:
            raise ResearchCorpusError(
                "Ready coverage research lacks an approved EvaluationSuite"
            )
        if evaluation_suite is not None:
            suite_document = validate_evaluation_suite(evaluation_suite.document)
            if suite_document["status"] != "approved":
                raise ResearchCorpusError(
                    "Only an approved EvaluationSuite may enter research evidence"
                )
            _assert_sanitized_value(suite_document, label="EvaluationSuite snapshot")
            evaluation_suite_path = "evaluation/suite.json"
            atomic_write_json(root / evaluation_suite_path, suite_document)

            references = {
                str(reference["task_case_id"]): reference
                for reference in suite_document["task_cases"]
            }
            task_cases: list[JsonObject] = []
            for task_case_id in sorted(evaluation_suite.task_cases):
                task_case = evaluation_suite.task_cases[task_case_id]
                task_payload = _sanitize_research_value(task_case.record_payload())
                if not isinstance(task_payload, Mapping):
                    raise ResearchCorpusError("Normalized TaskCase is invalid")
                task_cases.append(
                    {
                        "task_case_id": task_case_id,
                        "conditions": references[task_case_id]["conditions"],
                        "task": dict(task_payload),
                    }
                )
            execution_mapping: list[JsonObject] = []
            for item in prepared:
                task_case_id = str(item.execution["task"]["task_case_id"])
                execution_mapping.append(
                    {
                        "run_id": item.execution_id,
                        "task_case_id": task_case_id,
                        "conditions": references[task_case_id]["conditions"],
                        "declared_comparable_group": readiness[
                            "condition_groups"
                        ].get(item.execution_id),
                    }
                )
            task_condition_map: JsonObject = {
                "schema": RESEARCH_TASK_CONDITION_MAP_SCHEMA,
                "suite_id": suite_document["suite_id"],
                "skill_id": skill_id,
                "task_cases": task_cases,
                "execution_mapping": execution_mapping,
                "coverage": readiness["coverage"],
            }
            _assert_sanitized_value(
                task_condition_map, label="Task and condition map"
            )
            task_condition_map_path = "evaluation/task-condition-map.json"
            atomic_write_json(root / task_condition_map_path, task_condition_map)

        corpus_map: JsonObject = {
            "schema": RESEARCH_CORPUS_MAP_SCHEMA,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "objectives": list(objectives),
            "trajectories": [
                {
                    "run_id": item.execution_id,
                    "status": item.execution["status"],
                    "task_case_id": item.execution["task"].get("task_case_id"),
                    "condition_group": readiness["condition_groups"].get(
                        item.execution_id
                    ),
                    "trajectory_records": len(item.records),
                    "accepted_single_report_count": len(item.accepted_reports),
                    "artifact_count": frozen_artifact_counts[item.execution_id],
                }
                for item in prepared
            ],
            "available_queries": [
                "search observable trajectory text",
                "filter actions by run, sequence, tool, status, path, or flag",
                "read an action window around run_id and seq",
                "inspect extracted script writes and executions",
                "inspect deterministic result and resource baselines",
                "inspect frozen readiness and approved coverage definitions",
            ],
        }
        atomic_write_json(root / "corpus-map.json", corpus_map)
        atomic_write_json(root / "navigation-index.json", navigation)
        atomic_write_json(root / "baseline.json", baseline)

        files: list[JsonObject] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ResearchCorpusError("Research corpus contains a symlink")
            if not path.is_file() or path.name == "corpus.json":
                continue
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        manifest_body: JsonObject = {
            "purpose": "multi_trajectory_research",
            "skill_id": skill_id,
            "revision_id": revision_id,
            "objectives": list(objectives),
            "execution_ids": [item.execution_id for item in prepared],
            "revision_manifest": "revision/revision.json",
            "corpus_map": "corpus-map.json",
            "navigation_index": "navigation-index.json",
            "baseline": "baseline.json",
            "readiness": "readiness.json",
            "evaluation_suite": evaluation_suite_path,
            "task_condition_map": task_condition_map_path,
            "runs": run_manifests,
            "files": files,
            "redaction": dict(_REDACTION_POLICY),
        }
        content_sha256 = hashlib.sha256(_canonical_bytes(manifest_body)).hexdigest()
        manifest: JsonObject = {
            "schema": RESEARCH_CORPUS_SCHEMA,
            "corpus_id": f"corpus-{content_sha256[:20]}",
            "content_sha256": content_sha256,
            **manifest_body,
        }
        atomic_write_json(root / "corpus.json", manifest)
        return manifest, corpus_map, navigation, baseline
