"""Skill-first domain contracts and file-backed hierarchy repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any

from skill_evolution.analysis import (
    AnalysisContractError,
    validate_skill_contract_document,
)
from skill_evolution.storage import (
    JsonObject,
    StorageError,
    atomic_write_json,
    load_json_object,
    new_object_id,
    utc_now,
)


SKILL_REVISION_SCHEMA = "skill.revision.v1"
SKILL_EXECUTION_SCHEMA = "skill.execution.v1"
EXECUTION_SET_SCHEMA = "execution.set.v1"
ANALYSIS_RECORD_SCHEMA = "analysis.record.v1"
MULTI_TRAJECTORY_VIEW_SCHEMA = "analysis.multi_trajectory_view.v1"
MULTI_TRAJECTORY_ERRORS_SCHEMA = "analysis.multi_trajectory_errors.v1"
SKILL_INDEX_SCHEMA = "skill.index.v1"
SKILL_CATALOG_SCHEMA = "skill.catalog.v1"
CUTOVER_SCHEMA = "skill.hierarchy_cutover.v1"

REVISION_LIFECYCLES = frozenset(
    {"active", "historical", "candidate", "retired"}
)
EXECUTION_ORIGINS = frozenset({"direct", "replay", "comparison"})
EXECUTION_STATUSES = frozenset(
    {
        "running",
        "succeeded",
        "failed",
        "interrupted",
        "orchestration_failed",
        "indeterminate",
    }
)
EXECUTION_SET_PURPOSES = frozenset(
    {"replay", "evaluation", "diagnostic"}
)
EXECUTION_SET_STATUSES = frozenset(
    {"planned", "running", "completed", "completed_with_failures", "failed"}
)
ANALYSIS_SCOPES = frozenset({"single_execution", "execution_set"})
ANALYSIS_KINDS = frozenset(
    {
        "precheck",
        "trajectory_error",
        "trajectory_profile",
        "artifact_comparison",
        "harness",
        "multi_role",
        "multi_trajectory",
        # 0021 时代的冻结记录仍使用旧 kind，只读兼容
        "trace_error",
        "trace_profile",
        "multi_trace",
    }
)
ANALYSIS_PRODUCERS = frozenset({"deterministic", "agent", "composite"})
ANALYSIS_STATUSES = frozenset(
    {
        "planned",
        "running",
        "accepted",
        "unavailable",
        "failed",
        "invalid_output",
        "timed_out",
        "indeterminate",
        "inconclusive",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TRAJECTORY_SCHEMAS = {"trajectory.actions.v1", "trace.actions.v1"}
_TRAJECTORY_FINISHED = {"trajectory_finished", "trace_finished"}
_TRAJECTORY_SEALED = {"trajectory_sealed", "trace_sealed"}


class HierarchyError(ValueError):
    """Raised when a hierarchy object or relationship is invalid."""


def _exact_fields(
    value: Mapping[str, Any],
    fields: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unexpected = sorted(actual - fields)
        raise HierarchyError(
            f"{label} fields differ: missing={missing}, "
            f"unexpected={unexpected}"
        )


def _text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise HierarchyError(f"{label}.{field} must be non-empty text")
    return item


def _optional_text(
    value: Mapping[str, Any],
    field: str,
    *,
    label: str,
) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise HierarchyError(f"{label}.{field} must be text or null")
    return item


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise HierarchyError(f"{label} must be a safe identifier")
    return value


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HierarchyError(f"{label} must be a relative path")
    if "\\" in value:
        raise HierarchyError(f"{label} must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise HierarchyError(f"{label} must stay inside its object directory")
    return path.as_posix()


def _json_list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HierarchyError(f"{label} must be a list")
    return value


def _json_mapping(value: object, *, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise HierarchyError(f"{label} must be an object")
    return dict(value)


def _optional_number(value: object, *, label: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HierarchyError(f"{label} must be a number or null")
    if value < 0:
        raise HierarchyError(f"{label} must not be negative")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise HierarchyError(f"Skill package must be a regular directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise HierarchyError(f"Skill package contains a symlink: {path}")
        if path.is_file() and path.name != ".DS_Store":
            files.append(path)
    if not any(path.relative_to(root).as_posix() == "SKILL.md" for path in files):
        raise HierarchyError("Skill package must contain SKILL.md")
    return files


def package_digest(root: str | os.PathLike[str]) -> tuple[str, list[JsonObject]]:
    """Return a deterministic package digest and inventory."""

    package = Path(root).resolve()
    digest = hashlib.sha256()
    inventory: list[JsonObject] = []
    for path in _regular_files(package):
        relative = path.relative_to(package).as_posix()
        content_digest = _sha256(path)
        size = path.stat().st_size
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(content_digest))
        inventory.append(
            {"path": relative, "bytes": size, "sha256": content_digest}
        )
    return digest.hexdigest(), inventory


def _validate_inventory(value: object, *, label: str) -> list[JsonObject]:
    records: list[JsonObject] = []
    seen: set[str] = set()
    for index, item in enumerate(_json_list(value, label=label)):
        record = _json_mapping(item, label=f"{label}[{index}]")
        _exact_fields(
            record,
            {"path", "bytes", "sha256"},
            label=f"{label}[{index}]",
        )
        path = _safe_relative(record.get("path"), label=f"{label}[{index}].path")
        if path in seen:
            raise HierarchyError(f"{label} contains duplicate path {path!r}")
        seen.add(path)
        size = record.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise HierarchyError(f"{label}[{index}].bytes must be non-negative")
        sha256 = record.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise HierarchyError(f"{label}[{index}].sha256 must be SHA-256")
        records.append({"path": path, "bytes": size, "sha256": sha256})
    return records


def validate_skill_revision(value: Mapping[str, Any]) -> JsonObject:
    """Validate one immutable Skill revision manifest."""

    fields = {
        "schema",
        "skill_id",
        "revision_id",
        "package_sha256",
        "entrypoint_sha256",
        "contract",
        "lifecycle",
        "package_path",
        "inventory",
        "captured_at",
        "legacy_identity",
    }
    _exact_fields(value, fields, label="SkillRevision")
    if value.get("schema") != SKILL_REVISION_SCHEMA:
        raise HierarchyError("Unsupported SkillRevision schema")
    skill_id = _identifier(value.get("skill_id"), label="skill_id")
    revision_id = _identifier(value.get("revision_id"), label="revision_id")
    package_sha256 = _text(value, "package_sha256", label="SkillRevision")
    entrypoint_sha256 = _text(value, "entrypoint_sha256", label="SkillRevision")
    for label, digest in (
        ("package_sha256", package_sha256),
        ("entrypoint_sha256", entrypoint_sha256),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HierarchyError(f"{label} must be SHA-256")
    lifecycle = value.get("lifecycle")
    if lifecycle not in REVISION_LIFECYCLES:
        raise HierarchyError("Unsupported SkillRevision lifecycle")
    package_path = _safe_relative(
        value.get("package_path"), label="SkillRevision.package_path"
    )
    contract = _json_mapping(value.get("contract"), label="SkillRevision.contract")
    _exact_fields(
        contract,
        {"path", "schema", "version", "status", "sha256"},
        label="SkillRevision.contract",
    )
    contract_status = contract.get("status")
    if contract_status not in {"approved", "proposed", "missing_at_execution"}:
        raise HierarchyError("Unsupported SkillRevision contract status")
    if contract_status == "missing_at_execution":
        metadata_fields = ("path", "schema", "version", "sha256")
        if any(contract.get(field) is not None for field in metadata_fields):
            raise HierarchyError("Missing historical contract cannot claim metadata")
    else:
        _safe_relative(contract.get("path"), label="SkillRevision.contract.path")
        for field in ("schema", "version"):
            _text(contract, field, label="SkillRevision.contract")
        digest = contract.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HierarchyError("SkillRevision.contract.sha256 must be SHA-256")
    legacy_identity = value.get("legacy_identity")
    if legacy_identity is not None and not isinstance(legacy_identity, Mapping):
        raise HierarchyError("legacy_identity must be an object or null")
    inventory = _validate_inventory(value.get("inventory"), label="inventory")
    captured_at = _text(value, "captured_at", label="SkillRevision")
    return {
        "schema": SKILL_REVISION_SCHEMA,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "package_sha256": package_sha256,
        "entrypoint_sha256": entrypoint_sha256,
        "contract": contract,
        "lifecycle": lifecycle,
        "package_path": package_path,
        "inventory": inventory,
        "captured_at": captured_at,
        "legacy_identity": dict(legacy_identity) if legacy_identity else None,
    }


def _validate_artifacts(value: object, *, label: str) -> list[JsonObject]:
    records: list[JsonObject] = []
    identifiers: set[str] = set()
    for index, item in enumerate(_json_list(value, label=label)):
        record = _json_mapping(item, label=f"{label}[{index}]")
        _exact_fields(
            record,
            {"artifact_id", "path", "bytes", "sha256", "media_type"},
            label=f"{label}[{index}]",
        )
        artifact_id = _identifier(
            record.get("artifact_id"), label=f"{label}[{index}].artifact_id"
        )
        if artifact_id in identifiers:
            raise HierarchyError(f"{label} contains duplicate artifact_id")
        identifiers.add(artifact_id)
        path = _safe_relative(record.get("path"), label=f"{label}[{index}].path")
        size = record.get("bytes")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, int) or size < 0
        ):
            raise HierarchyError(f"{label}[{index}].bytes is invalid")
        digest = record.get("sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise HierarchyError(f"{label}[{index}].sha256 is invalid")
        media_type = record.get("media_type")
        if media_type is not None and not isinstance(media_type, str):
            raise HierarchyError(f"{label}[{index}].media_type is invalid")
        records.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "bytes": size,
                "sha256": digest,
                "media_type": media_type,
            }
        )
    return records


def validate_skill_execution(value: Mapping[str, Any]) -> JsonObject:
    """Validate one Skill execution manifest."""

    fields = {
        "schema",
        "execution_id",
        "skill_id",
        "revision_id",
        "status",
        "origin",
        "execution_set_id",
        "comparison_id",
        "started_at",
        "ended_at",
        "duration_ms",
        "task",
        "inputs",
        "outputs",
        "supporting_artifacts",
        "trajectory",
        "session",
        "setup",
        "legacy",
    }
    if "trace" in value and "trajectory" not in value:
        # 0021 时代的冻结 execution.json 顶层字段名为 trace，只读投影为 trajectory
        value = {
            key: item
            for key, item in value.items()
            if key != "trace"
        } | {"trajectory": value["trace"]}
    _exact_fields(value, fields, label="SkillExecution")
    if value.get("schema") != SKILL_EXECUTION_SCHEMA:
        raise HierarchyError("Unsupported SkillExecution schema")
    execution_id = _identifier(value.get("execution_id"), label="execution_id")
    skill_id = _identifier(value.get("skill_id"), label="skill_id")
    revision_id = _identifier(value.get("revision_id"), label="revision_id")
    status = value.get("status")
    if status not in EXECUTION_STATUSES:
        raise HierarchyError("Unsupported SkillExecution status")
    origin = value.get("origin")
    if origin not in EXECUTION_ORIGINS:
        raise HierarchyError("Unsupported SkillExecution origin")
    execution_set_id = value.get("execution_set_id")
    if execution_set_id is not None:
        execution_set_id = _identifier(
            execution_set_id, label="execution_set_id"
        )
    comparison_id = value.get("comparison_id")
    if comparison_id is not None:
        comparison_id = _identifier(comparison_id, label="comparison_id")
    if origin == "replay" and execution_set_id is None:
        raise HierarchyError("Replay execution requires execution_set_id")
    if origin == "comparison" and comparison_id is None:
        raise HierarchyError("Comparison execution requires comparison_id")
    trajectory = _json_mapping(value.get("trajectory"), label="SkillExecution.trajectory")
    _exact_fields(
        trajectory,
        {"path", "schema", "source_format", "sealed"},
        label="SkillExecution.trajectory",
    )
    trajectory_path = trajectory.get("path")
    if trajectory_path is not None:
        trajectory_path = _safe_relative(trajectory_path, label="SkillExecution.trajectory.path")
    trajectory_schema = trajectory.get("schema")
    if trajectory_schema is not None and trajectory_schema not in _TRAJECTORY_SCHEMAS:
        raise HierarchyError("Unsupported trajectory schema")
    if not isinstance(trajectory.get("sealed"), bool):
        raise HierarchyError("SkillExecution.trajectory.sealed must be boolean")
    session = _json_mapping(value.get("session"), label="SkillExecution.session")
    _exact_fields(session, {"path", "status"}, label="SkillExecution.session")
    session_path = session.get("path")
    if session_path is not None:
        session_path = _safe_relative(
            session_path, label="SkillExecution.session.path"
        )
    setup = _json_mapping(value.get("setup"), label="SkillExecution.setup")
    legacy = value.get("legacy")
    if legacy is not None and not isinstance(legacy, Mapping):
        raise HierarchyError("SkillExecution.legacy must be an object or null")
    return {
        "schema": SKILL_EXECUTION_SCHEMA,
        "execution_id": execution_id,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "status": status,
        "origin": origin,
        "execution_set_id": execution_set_id,
        "comparison_id": comparison_id,
        "started_at": _optional_text(value, "started_at", label="SkillExecution"),
        "ended_at": _optional_text(value, "ended_at", label="SkillExecution"),
        "duration_ms": _optional_number(
            value.get("duration_ms"), label="SkillExecution.duration_ms"
        ),
        "task": _json_mapping(value.get("task"), label="SkillExecution.task"),
        "inputs": _validate_artifacts(value.get("inputs"), label="inputs"),
        "outputs": _validate_artifacts(value.get("outputs"), label="outputs"),
        "supporting_artifacts": _validate_artifacts(
            value.get("supporting_artifacts"), label="supporting_artifacts"
        ),
        "trajectory": {
            "path": trajectory_path,
            "schema": trajectory_schema,
            "source_format": _text(
                trajectory, "source_format", label="SkillExecution.trajectory"
            ),
            "sealed": trajectory["sealed"],
        },
        "session": {
            "path": session_path,
            "status": _optional_text(
                session, "status", label="SkillExecution.session"
            ),
        },
        "setup": setup,
        "legacy": dict(legacy) if legacy else None,
    }


def validate_execution_set(value: Mapping[str, Any]) -> JsonObject:
    """Validate a same-revision collection of Skill executions."""

    fields = {
        "schema",
        "set_id",
        "skill_id",
        "revision_id",
        "purpose",
        "status",
        "execution_ids",
        "task",
        "runtime",
        "provenance",
        "created_at",
        "ended_at",
    }
    _exact_fields(value, fields, label="ExecutionSet")
    if value.get("schema") != EXECUTION_SET_SCHEMA:
        raise HierarchyError("Unsupported ExecutionSet schema")
    purpose = value.get("purpose")
    if purpose not in EXECUTION_SET_PURPOSES:
        raise HierarchyError("Unsupported ExecutionSet purpose")
    status = value.get("status")
    if status not in EXECUTION_SET_STATUSES:
        raise HierarchyError("Unsupported ExecutionSet status")
    execution_ids = [
        _identifier(item, label="execution_ids item")
        for item in _json_list(value.get("execution_ids"), label="execution_ids")
    ]
    if len(set(execution_ids)) != len(execution_ids):
        raise HierarchyError("execution_ids must not contain duplicates")
    return {
        "schema": EXECUTION_SET_SCHEMA,
        "set_id": _identifier(value.get("set_id"), label="set_id"),
        "skill_id": _identifier(value.get("skill_id"), label="skill_id"),
        "revision_id": _identifier(
            value.get("revision_id"), label="revision_id"
        ),
        "purpose": purpose,
        "status": status,
        "execution_ids": execution_ids,
        "task": _json_mapping(value.get("task"), label="ExecutionSet.task"),
        "runtime": _json_mapping(
            value.get("runtime"), label="ExecutionSet.runtime"
        ),
        "provenance": _json_mapping(
            value.get("provenance"), label="ExecutionSet.provenance"
        ),
        "created_at": _text(value, "created_at", label="ExecutionSet"),
        "ended_at": _optional_text(value, "ended_at", label="ExecutionSet"),
    }


def validate_analysis_record(value: Mapping[str, Any]) -> JsonObject:
    """Validate an envelope that attaches existing analysis results."""

    fields = {
        "schema",
        "analysis_id",
        "skill_id",
        "revision_id",
        "scope",
        "execution_id",
        "execution_set_id",
        "kind",
        "producer",
        "status",
        "input_refs",
        "result_refs",
        "attempts",
        "created_at",
        "ended_at",
        "provenance",
    }
    _exact_fields(value, fields, label="AnalysisRecord")
    if value.get("schema") != ANALYSIS_RECORD_SCHEMA:
        raise HierarchyError("Unsupported AnalysisRecord schema")
    scope = value.get("scope")
    if scope not in ANALYSIS_SCOPES:
        raise HierarchyError("Unsupported AnalysisRecord scope")
    execution_id = value.get("execution_id")
    execution_set_id = value.get("execution_set_id")
    if execution_id is not None:
        execution_id = _identifier(execution_id, label="execution_id")
    if execution_set_id is not None:
        execution_set_id = _identifier(
            execution_set_id, label="execution_set_id"
        )
    if scope == "single_execution" and (
        execution_id is None or execution_set_id is not None
    ):
        raise HierarchyError("Single analysis requires only execution_id")
    if scope == "execution_set" and (
        execution_set_id is None or execution_id is not None
    ):
        raise HierarchyError("Multi analysis requires only execution_set_id")
    kind = value.get("kind")
    if kind not in ANALYSIS_KINDS:
        raise HierarchyError("Unsupported AnalysisRecord kind")
    if kind == "multi_trajectory" and scope != "execution_set":
        raise HierarchyError("Multi-trajectory analysis requires execution_set scope")
    producer = value.get("producer")
    if producer not in ANALYSIS_PRODUCERS:
        raise HierarchyError("Unsupported AnalysisRecord producer")
    status = value.get("status")
    if status not in ANALYSIS_STATUSES:
        raise HierarchyError("Unsupported AnalysisRecord status")
    for label in ("input_refs", "result_refs", "attempts"):
        _json_list(value.get(label), label=label)
    provenance = value.get("provenance")
    if provenance is not None and not isinstance(provenance, Mapping):
        raise HierarchyError("AnalysisRecord.provenance must be object or null")
    return {
        "schema": ANALYSIS_RECORD_SCHEMA,
        "analysis_id": _identifier(
            value.get("analysis_id"), label="analysis_id"
        ),
        "skill_id": _identifier(value.get("skill_id"), label="skill_id"),
        "revision_id": _identifier(
            value.get("revision_id"), label="revision_id"
        ),
        "scope": scope,
        "execution_id": execution_id,
        "execution_set_id": execution_set_id,
        "kind": kind,
        "producer": producer,
        "status": status,
        "input_refs": [dict(item) for item in value["input_refs"]],
        "result_refs": [dict(item) for item in value["result_refs"]],
        "attempts": [dict(item) for item in value["attempts"]],
        "created_at": _text(value, "created_at", label="AnalysisRecord"),
        "ended_at": _optional_text(value, "ended_at", label="AnalysisRecord"),
        "provenance": dict(provenance) if provenance else None,
    }


def validate_multi_trajectory_view(value: Mapping[str, Any]) -> JsonObject:
    """Validate the owner-facing multi-trajectory report projection."""

    fields = {
        "schema",
        "analysis_id",
        "skill_id",
        "revision_id",
        "generated_at",
        "analysis",
        "overview",
        "execution_set",
        "patterns",
        "findings",
        "evidence",
        "recommendation",
        "provenance",
    }
    _exact_fields(value, fields, label="MultiTrajectoryView")
    if value.get("schema") != MULTI_TRAJECTORY_VIEW_SCHEMA:
        raise HierarchyError("Unsupported MultiTrajectoryView schema")
    for field in (
        "analysis",
        "overview",
        "execution_set",
        "recommendation",
        "provenance",
    ):
        _json_mapping(value.get(field), label=f"MultiTrajectoryView.{field}")
    for field in ("patterns", "findings", "evidence"):
        _json_list(value.get(field), label=f"MultiTrajectoryView.{field}")
    analysis = dict(value["analysis"])
    if analysis.get("status") != "accepted" and value["findings"]:
        raise HierarchyError(
            "Unavailable multi-trajectory analysis cannot contain findings"
        )
    return dict(value)


def validate_multi_trajectory_errors_view(
    value: Mapping[str, Any],
) -> JsonObject:
    """Validate the owner-facing error-centric multi-trajectory projection."""

    fields = {
        "schema",
        "analysis_id",
        "skill_id",
        "revision_id",
        "generated_at",
        "scope",
        "errors",
        "reports",
        "limitations",
    }
    _exact_fields(value, fields, label="MultiTrajectoryErrorsView")
    if value.get("schema") != MULTI_TRAJECTORY_ERRORS_SCHEMA:
        raise HierarchyError("Unsupported MultiTrajectoryErrorsView schema")
    for field in ("analysis_id", "skill_id", "revision_id", "generated_at"):
        _text(value, field, label="MultiTrajectoryErrorsView")
    scope = value.get("scope")
    if not isinstance(scope, Mapping):
        raise HierarchyError("MultiTrajectoryErrorsView.scope must be an object")
    errors = value.get("errors")
    reports = value.get("reports")
    if not isinstance(errors, list) or not isinstance(reports, list):
        raise HierarchyError(
            "MultiTrajectoryErrorsView.errors and reports must be lists"
        )
    _json_list(
        value.get("limitations"),
        label="MultiTrajectoryErrorsView.limitations",
    )
    for index, error in enumerate(errors):
        _validate_errors_entry(
            error, label=f"MultiTrajectoryErrorsView.errors[{index}]"
        )
    for index, report in enumerate(reports):
        _validate_error_report_entry(
            report, label=f"MultiTrajectoryErrorsView.reports[{index}]"
        )
    return dict(value)


def _validate_errors_entry(error: object, *, label: str) -> None:
    """Validate one identified error inside the product projection."""

    if not isinstance(error, Mapping):
        raise HierarchyError(f"{label} must be an object")
    _exact_fields(
        error,
        {
            "error_id",
            "title",
            "summary",
            "anchor_evidence",
            "observed_trajectory_ids",
            "checked_absent_trajectory_ids",
            "suggested_dimensions",
            "notes",
        },
        label=label,
    )
    _text(error, "error_id", label=label)
    _text(error, "title", label=label)
    _text(error, "summary", label=label)
    for field in (
        "anchor_evidence",
        "observed_trajectory_ids",
        "checked_absent_trajectory_ids",
        "suggested_dimensions",
    ):
        _json_list(error.get(field), label=f"{label}.{field}")
    notes = error.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise HierarchyError(f"{label}.notes must be text or null")


def _validate_error_report_entry(report: object, *, label: str) -> None:
    """Validate one per-error report with its dimension findings."""

    if not isinstance(report, Mapping):
        raise HierarchyError(f"{label} must be an object")
    unexpected = set(report) - {
        "error_id",
        "dimensions",
        "limitations",
        "validation_warnings",
    }
    if unexpected:
        raise HierarchyError(
            f"{label} has unexpected fields: {sorted(unexpected)}"
        )
    for field in ("error_id", "dimensions", "limitations"):
        if field not in report:
            raise HierarchyError(f"{label} is missing {field}")
    _text(report, "error_id", label=label)
    dimensions = report.get("dimensions")
    if not isinstance(dimensions, list):
        raise HierarchyError(f"{label}.dimensions must be a list")
    _json_list(report.get("limitations"), label=f"{label}.limitations")
    warnings = report.get("validation_warnings")
    if warnings is not None:
        _json_list(warnings, label=f"{label}.validation_warnings")
        for index, warning in enumerate(warnings):
            if not isinstance(warning, str):
                raise HierarchyError(
                    f"{label}.validation_warnings[{index}] must be text"
                )
    for index, item in enumerate(dimensions):
        _validate_error_dimension(
            item, label=f"{label}.dimensions[{index}]"
        )


def _validate_error_dimension(item: object, *, label: str) -> None:
    """Validate one problem dimension finding inside a per-error report."""

    if not isinstance(item, Mapping):
        raise HierarchyError(f"{label} must be an object")
    _exact_fields(
        item,
        {
            "dimension",
            "claim",
            "observed_trajectory_ids",
            "checked_absent_trajectory_ids",
            "evidence",
            "counterevidence",
            "confidence",
            "derivation_ids",
            "limitations",
        },
        label=label,
    )
    if item.get("dimension") not in {
        "behavior",
        "conditions",
        "consistency",
        "resource",
    }:
        raise HierarchyError(f"{label}.dimension is unsupported")
    _text(item, "claim", label=label)
    for field in (
        "observed_trajectory_ids",
        "checked_absent_trajectory_ids",
        "evidence",
        "counterevidence",
        "derivation_ids",
        "limitations",
    ):
        _json_list(item.get(field), label=f"{label}.{field}")
    confidence = item.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise HierarchyError(f"{label}.confidence must be within 0..1")


@dataclass(frozen=True)
class RevisionRecord:
    """One registered immutable Skill package revision."""

    directory: Path
    manifest: JsonObject


@dataclass(frozen=True)
class ExecutionRecord:
    """One Skill execution and its payload directory."""

    directory: Path
    payload_directory: Path
    manifest: JsonObject


class SkillHierarchyRepository:
    """Store and query authoritative Skill-first runtime objects."""

    def __init__(self, runtime_root: str | os.PathLike[str]) -> None:
        self.root = Path(runtime_root).resolve()
        self.skills_root = self.root / "skills"
        self.migrations_root = self.root / "migrations"
        self.cutover_path = self.root / "hierarchy-cutover.json"

    def ensure(self) -> None:
        """Create only the hierarchy roots."""

        self.skills_root.mkdir(parents=True, exist_ok=True)
        self.migrations_root.mkdir(parents=True, exist_ok=True)

    def skill_directory(self, skill_id: str) -> Path:
        """Return one safe Skill aggregate directory."""

        return self.skills_root / _identifier(skill_id, label="skill_id")

    def revision_directory(self, skill_id: str, revision_id: str) -> Path:
        """Return one immutable revision directory."""

        revision = _identifier(revision_id, label="revision_id")
        return self.skill_directory(skill_id) / "revisions" / revision

    def execution_directory(self, skill_id: str, execution_id: str) -> Path:
        """Return one execution directory."""

        execution = _identifier(execution_id, label="execution_id")
        return self.skill_directory(skill_id) / "executions" / execution

    def execution_set_directory(self, skill_id: str, set_id: str) -> Path:
        """Return one execution-set directory."""

        set_name = _identifier(set_id, label="set_id")
        return self.skill_directory(skill_id) / "execution-sets" / set_name

    def multi_trajectory_analyses_directory(self, skill_id: str) -> Path:
        """Return the dedicated store for actual multi-trajectory analyses."""

        return self.skill_directory(skill_id) / "multi-trajectory-analyses"

    def execution_set_analyses_directory(
        self,
        skill_id: str,
        set_id: str,
    ) -> Path:
        """Return the store for Harness and other execution-set checks."""

        return self.execution_set_directory(skill_id, set_id) / "analyses"

    def register_revision(
        self,
        package: str | os.PathLike[str],
        *,
        lifecycle: str = "active",
        legacy_skill_id: str | None = None,
        legacy_identity: Mapping[str, Any] | None = None,
    ) -> RevisionRecord:
        """Copy one package into an immutable content-addressed revision."""

        if lifecycle not in REVISION_LIFECYCLES:
            raise HierarchyError("Unsupported revision lifecycle")
        source = Path(package).resolve()
        package_sha256, inventory = package_digest(source)
        entrypoint = source / "SKILL.md"
        contract_path = source / "skill_contract.json"
        if contract_path.is_file():
            try:
                contract = validate_skill_contract_document(
                    load_json_object(contract_path)
                )
            except AnalysisContractError as error:
                raise HierarchyError(str(error)) from error
            skill_id = str(contract["skill_id"])
            contract_record: JsonObject = {
                "path": "package/skill_contract.json",
                "schema": contract["schema"],
                "version": contract["version"],
                "status": contract["status"],
                "sha256": _sha256(contract_path),
            }
        else:
            if legacy_skill_id is None:
                raise HierarchyError(
                    "Historical package without a contract requires legacy_skill_id"
                )
            skill_id = _identifier(legacy_skill_id, label="legacy_skill_id")
            contract_record = {
                "path": None,
                "schema": None,
                "version": None,
                "status": "missing_at_execution",
                "sha256": None,
            }
        revision_id = f"rev-{package_sha256[:16]}"
        directory = self.revision_directory(skill_id, revision_id)
        snapshot = directory / "package"
        manifest: JsonObject = {
            "schema": SKILL_REVISION_SCHEMA,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "package_sha256": package_sha256,
            "entrypoint_sha256": _sha256(entrypoint),
            "contract": contract_record,
            "lifecycle": lifecycle,
            "package_path": "package",
            "inventory": inventory,
            "captured_at": utc_now(),
            "legacy_identity": (
                dict(legacy_identity) if legacy_identity is not None else None
            ),
        }
        validated = validate_skill_revision(manifest)
        if directory.exists():
            existing = validate_skill_revision(
                load_json_object(directory / "revision.json")
            )
            if existing["package_sha256"] != package_sha256:
                raise HierarchyError("Revision ID collision")
            return RevisionRecord(directory, existing)
        directory.mkdir(parents=True)
        try:
            shutil.copytree(source, snapshot)
            copied_digest, _ = package_digest(snapshot)
            if copied_digest != package_sha256:
                raise HierarchyError("Copied Skill revision failed hash verification")
            atomic_write_json(directory / "revision.json", validated)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self.rebuild_indexes()
        return RevisionRecord(directory, validated)

    def prepare_execution(
        self,
        *,
        skill_id: str,
        revision_id: str,
        origin: str,
        execution_set_id: str | None = None,
        comparison_id: str | None = None,
        execution_id: str | None = None,
    ) -> ExecutionRecord:
        """Create an empty payload and running execution manifest."""

        self.load_revision(skill_id, revision_id)
        identifier = execution_id or new_object_id("execution")
        directory = self.execution_directory(skill_id, identifier)
        if directory.exists():
            raise HierarchyError(f"Execution already exists: {identifier}")
        payload = directory / "payload"
        payload.mkdir(parents=True)
        manifest: JsonObject = {
            "schema": SKILL_EXECUTION_SCHEMA,
            "execution_id": identifier,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "status": "running",
            "origin": origin,
            "execution_set_id": execution_set_id,
            "comparison_id": comparison_id,
            "started_at": utc_now(),
            "ended_at": None,
            "duration_ms": None,
            "task": {},
            "inputs": [],
            "outputs": [],
            "supporting_artifacts": [],
            "trajectory": {
                "path": None,
                "schema": None,
                "source_format": "current",
                "sealed": False,
            },
            "session": {"path": None, "status": None},
            "setup": {},
            "legacy": None,
        }
        validated = validate_skill_execution(manifest)
        atomic_write_json(directory / "execution.json", validated)
        self.rebuild_indexes()
        return ExecutionRecord(directory, payload, validated)

    def finalize_execution(
        self,
        skill_id: str,
        execution_id: str,
        manifest: Mapping[str, Any],
    ) -> ExecutionRecord:
        """Seal one execution manifest after its payload is complete."""

        directory = self.execution_directory(skill_id, execution_id)
        if not (directory / "execution.json").is_file():
            raise HierarchyError(f"Execution does not exist: {execution_id}")
        validated = validate_skill_execution(manifest)
        if validated["skill_id"] != skill_id:
            raise HierarchyError("Execution skill_id does not match its directory")
        if validated["execution_id"] != execution_id:
            raise HierarchyError("Execution ID does not match its directory")
        self._validate_execution_relationships(validated)
        atomic_write_json(directory / "execution.json", validated)
        self.rebuild_indexes()
        return ExecutionRecord(directory, directory / "payload", validated)

    def create_execution_set(
        self,
        *,
        skill_id: str,
        revision_id: str,
        purpose: str,
        task: Mapping[str, Any],
        runtime: Mapping[str, Any],
        provenance: Mapping[str, Any],
        set_id: str | None = None,
        status: str = "planned",
    ) -> JsonObject:
        """Create a same-revision execution collection."""

        self.load_revision(skill_id, revision_id)
        identifier = set_id or new_object_id("set")
        directory = self.execution_set_directory(skill_id, identifier)
        if directory.exists():
            raise HierarchyError(f"ExecutionSet already exists: {identifier}")
        manifest: JsonObject = {
            "schema": EXECUTION_SET_SCHEMA,
            "set_id": identifier,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "purpose": purpose,
            "status": status,
            "execution_ids": [],
            "task": dict(task),
            "runtime": dict(runtime),
            "provenance": dict(provenance),
            "created_at": utc_now(),
            "ended_at": None,
        }
        validated = validate_execution_set(manifest)
        directory.mkdir(parents=True)
        atomic_write_json(directory / "set.json", validated)
        self.rebuild_indexes()
        return validated

    def replace_execution_set(
        self,
        skill_id: str,
        set_id: str,
        manifest: Mapping[str, Any],
    ) -> JsonObject:
        """Replace an execution set after validating every member revision."""

        validated = validate_execution_set(manifest)
        if validated["skill_id"] != skill_id or validated["set_id"] != set_id:
            raise HierarchyError("ExecutionSet identity does not match its path")
        for execution_id in validated["execution_ids"]:
            execution = self.load_execution(skill_id, execution_id)
            if execution["revision_id"] != validated["revision_id"]:
                raise HierarchyError(
                    "ExecutionSet cannot mix Skill revisions"
                )
        path = self.execution_set_directory(skill_id, set_id) / "set.json"
        if not path.is_file():
            raise HierarchyError(f"ExecutionSet does not exist: {set_id}")
        atomic_write_json(path, validated)
        self.rebuild_indexes()
        return validated

    def create_analysis(
        self,
        manifest: Mapping[str, Any],
    ) -> tuple[Path, JsonObject]:
        """Attach one analysis envelope to an Execution or Skill."""

        validated = validate_analysis_record(manifest)
        skill_id = str(validated["skill_id"])
        revision_id = str(validated["revision_id"])
        self.load_revision(skill_id, revision_id)
        if validated["scope"] == "single_execution":
            execution_id = str(validated["execution_id"])
            execution = self.load_execution(skill_id, execution_id)
            if execution["revision_id"] != revision_id:
                raise HierarchyError("Analysis revision differs from execution")
            directory = (
                self.execution_directory(skill_id, execution_id)
                / "analyses"
                / "single"
                / str(validated["analysis_id"])
            )
        else:
            set_id = str(validated["execution_set_id"])
            execution_set = self.load_execution_set(skill_id, set_id)
            if execution_set["revision_id"] != revision_id:
                raise HierarchyError("Analysis revision differs from execution set")
            root = (
                self.multi_trajectory_analyses_directory(skill_id)
                if validated["kind"] == "multi_trajectory"
                else self.execution_set_analyses_directory(skill_id, set_id)
            )
            directory = root / str(validated["analysis_id"])
        if directory.exists():
            raise HierarchyError(
                f"Analysis already exists: {validated['analysis_id']}"
            )
        directory.mkdir(parents=True)
        atomic_write_json(directory / "analysis.json", validated)
        self.rebuild_indexes()
        return directory, validated

    def replace_analysis(
        self,
        manifest: Mapping[str, Any],
    ) -> tuple[Path, JsonObject]:
        """Replace an existing analysis envelope without moving its attempts."""

        validated = validate_analysis_record(manifest)
        directory = self.analysis_directory(validated)
        path = directory / "analysis.json"
        if not path.is_file():
            raise HierarchyError(
                f"Analysis does not exist: {validated['analysis_id']}"
            )
        existing = validate_analysis_record(load_json_object(path))
        identity_fields = (
            "analysis_id",
            "skill_id",
            "revision_id",
            "scope",
            "execution_id",
            "execution_set_id",
            "kind",
        )
        if any(existing[field] != validated[field] for field in identity_fields):
            raise HierarchyError("Analysis identity fields cannot change")
        atomic_write_json(path, validated)
        self.rebuild_indexes()
        return directory, validated

    def analysis_directory(self, manifest: Mapping[str, Any]) -> Path:
        """Resolve the canonical directory for a validated analysis identity."""

        value = validate_analysis_record(manifest)
        if value["scope"] == "single_execution":
            return (
                self.execution_directory(
                    str(value["skill_id"]), str(value["execution_id"])
                )
                / "analyses"
                / "single"
                / str(value["analysis_id"])
            )
        skill_id = str(value["skill_id"])
        root = (
            self.multi_trajectory_analyses_directory(skill_id)
            if value["kind"] == "multi_trajectory"
            else self.execution_set_analyses_directory(
                skill_id,
                str(value["execution_set_id"]),
            )
        )
        return root / str(value["analysis_id"])

    def load_revision(self, skill_id: str, revision_id: str) -> JsonObject:
        """Load one validated revision."""

        directory = self._trusted_directory(
            self.revision_directory(skill_id, revision_id),
            self.skills_root,
            label="Skill revision",
        )
        path = directory / "revision.json"
        return validate_skill_revision(load_json_object(path))

    def load_execution(self, skill_id: str, execution_id: str) -> JsonObject:
        """Load one validated execution."""

        directory = self._trusted_directory(
            self.execution_directory(skill_id, execution_id),
            self.skills_root,
            label="Execution",
        )
        path = directory / "execution.json"
        return validate_skill_execution(load_json_object(path))

    def load_execution_set(self, skill_id: str, set_id: str) -> JsonObject:
        """Load one validated execution set."""

        directory = self._trusted_directory(
            self.execution_set_directory(skill_id, set_id),
            self.skills_root,
            label="Execution set",
        )
        path = directory / "set.json"
        return validate_execution_set(load_json_object(path))

    def load_skill_index(self, skill_id: str) -> JsonObject:
        """Load one writer-maintained navigation index without changing state."""

        value = load_json_object(self.skill_directory(skill_id) / "index.json")
        if value.get("schema") != SKILL_INDEX_SCHEMA:
            raise HierarchyError("Unsupported Skill index schema")
        if value.get("skill_id") != skill_id:
            raise HierarchyError("Skill index identity does not match its path")
        return value

    def load_catalog(self) -> JsonObject:
        """Load the writer-maintained catalog without changing runtime state."""

        value = load_json_object(self.root / "catalog.json")
        if value.get("schema") != SKILL_CATALOG_SCHEMA:
            raise HierarchyError("Unsupported Skill catalog schema")
        if not isinstance(value.get("skills"), list):
            raise HierarchyError("Skill catalog entries must be a list")
        return value

    def mark_cutover_complete(
        self,
        *,
        migration_id: str,
        disposition: Mapping[str, Any],
    ) -> JsonObject:
        """Publish the explicit boundary that makes hierarchy data authoritative."""

        marker: JsonObject = {
            "schema": CUTOVER_SCHEMA,
            "status": "completed",
            "migration_id": _identifier(migration_id, label="migration_id"),
            "completed_at": utc_now(),
            "disposition": dict(disposition),
        }
        atomic_write_json(self.cutover_path, marker)
        return marker

    def is_cutover_complete(self) -> bool:
        """Return whether the explicit hierarchy cutover marker is valid."""

        if not self.cutover_path.is_file() or self.cutover_path.is_symlink():
            return False
        try:
            value = load_json_object(self.cutover_path)
        except (OSError, StorageError):
            return False
        return (
            set(value)
            == {
                "schema",
                "status",
                "migration_id",
                "completed_at",
                "disposition",
            }
            and value.get("schema") == CUTOVER_SCHEMA
            and value.get("status") == "completed"
            and isinstance(value.get("migration_id"), str)
            and isinstance(value.get("completed_at"), str)
            and isinstance(value.get("disposition"), Mapping)
        )

    def list_revisions(self, skill_id: str) -> list[JsonObject]:
        """List every validated revision for one Skill."""

        return self._load_manifests(
            self.skill_directory(skill_id) / "revisions",
            "revision.json",
            validate_skill_revision,
        )

    def list_executions(self, skill_id: str) -> list[JsonObject]:
        """List every validated execution directly beneath one Skill."""

        return self._load_manifests(
            self.skill_directory(skill_id) / "executions",
            "execution.json",
            validate_skill_execution,
        )

    def list_execution_sets(self, skill_id: str) -> list[JsonObject]:
        """List every validated execution set for one Skill."""

        return self._load_manifests(
            self.skill_directory(skill_id) / "execution-sets",
            "set.json",
            validate_execution_set,
        )

    def load_analysis(
        self,
        skill_id: str,
        analysis_id: str,
        *,
        execution_id: str | None = None,
    ) -> JsonObject:
        """Load one validated analysis from its subject-owned directory."""

        _identifier(analysis_id, label="analysis_id")
        if execution_id is not None:
            directory = (
                self.execution_directory(skill_id, execution_id)
                / "analyses"
                / "single"
                / analysis_id
            )
        else:
            candidates = [
                self.multi_trajectory_analyses_directory(skill_id) / analysis_id
            ]
            candidates.extend(
                self.execution_set_analyses_directory(
                    skill_id,
                    str(execution_set["set_id"]),
                )
                / analysis_id
                for execution_set in self.list_execution_sets(skill_id)
            )
            matches = [
                candidate
                for candidate in candidates
                if candidate.is_dir()
                and not candidate.is_symlink()
                and (candidate / "analysis.json").is_file()
                and not (candidate / "analysis.json").is_symlink()
            ]
            if len(matches) != 1:
                raise HierarchyError(
                    f"Analysis was not found uniquely: {analysis_id}"
                )
            directory = matches[0]
        trusted = self._trusted_directory(
            directory,
            self.skills_root,
            label="Analysis",
        )
        return validate_analysis_record(
            load_json_object(trusted / "analysis.json")
        )

    def resolve_object_file(
        self,
        object_directory: str | os.PathLike[str],
        relative_path: str,
    ) -> Path:
        """Resolve an allow-listed relative file without following escapes."""

        safe = _safe_relative(relative_path, label="object file path")
        directory = self._trusted_directory(
            Path(object_directory),
            self.skills_root,
            label="Hierarchy object",
        )
        candidate = (directory / safe).resolve()
        if not candidate.is_relative_to(directory) or not candidate.is_file():
            raise HierarchyError("Hierarchy object file is missing or unsafe")
        if candidate.is_symlink():
            raise HierarchyError("Hierarchy object file cannot be a symlink")
        return candidate

    def list_analyses(
        self,
        skill_id: str,
        *,
        execution_id: str | None = None,
    ) -> list[JsonObject]:
        """List validated single or execution-set analysis envelopes."""

        if execution_id is not None:
            root = (
                self.execution_directory(skill_id, execution_id)
                / "analyses"
                / "single"
            )
            return self._load_analysis_root(root)
        records = self.list_multi_trajectory_analyses(skill_id)
        records.extend(self.list_execution_set_analyses(skill_id))
        return sorted(records, key=lambda item: str(item["analysis_id"]))

    def list_multi_trajectory_analyses(self, skill_id: str) -> list[JsonObject]:
        """List only real multi-trajectory analyses from their dedicated store."""

        root = self.multi_trajectory_analyses_directory(skill_id)
        records = self._load_analysis_root(root)
        if any(record["kind"] != "multi_trajectory" for record in records):
            raise HierarchyError(
                "Dedicated multi-trajectory store contains a different analysis kind"
            )
        return records

    def list_execution_set_analyses(
        self,
        skill_id: str,
        *,
        set_id: str | None = None,
    ) -> list[JsonObject]:
        """List Harness and workflow checks without presenting them as multi-trajectory."""

        execution_sets = (
            [self.load_execution_set(skill_id, set_id)]
            if set_id is not None
            else self.list_execution_sets(skill_id)
        )
        records: list[JsonObject] = []
        for execution_set in execution_sets:
            root = self.execution_set_analyses_directory(
                skill_id,
                str(execution_set["set_id"]),
            )
            records.extend(self._load_analysis_root(root))
        return sorted(records, key=lambda item: str(item["analysis_id"]))

    def _load_analysis_root(self, root: Path) -> list[JsonObject]:
        """Load validated analysis records from one trusted collection root."""

        if not root.is_dir() or root.is_symlink():
            return []
        records: list[JsonObject] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            path = directory / "analysis.json"
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and path.is_file()
                and not path.is_symlink()
            ):
                records.append(validate_analysis_record(load_json_object(path)))
        return records

    def rebuild_indexes(self) -> JsonObject:
        """Rebuild non-authoritative per-Skill and global navigation indexes."""

        self.ensure()
        skill_summaries: list[JsonObject] = []
        for skill_directory in sorted(self.skills_root.iterdir()):
            if not skill_directory.is_dir() or skill_directory.is_symlink():
                continue
            skill_id = _identifier(skill_directory.name, label="skill_id")
            revisions = self._load_manifests(
                skill_directory / "revisions",
                "revision.json",
                validate_skill_revision,
            )
            executions = self._load_manifests(
                skill_directory / "executions",
                "execution.json",
                validate_skill_execution,
            )
            execution_sets = self._load_manifests(
                skill_directory / "execution-sets",
                "set.json",
                validate_execution_set,
            )
            multi_analyses = self.list_multi_trajectory_analyses(skill_id)
            single_count = sum(
                len(self.list_analyses(skill_id, execution_id=item["execution_id"]))
                for item in executions
            )
            active = [
                item["revision_id"]
                for item in revisions
                if item["lifecycle"] == "active"
            ]
            index: JsonObject = {
                "schema": SKILL_INDEX_SCHEMA,
                "skill_id": skill_id,
                "generated_at": utc_now(),
                "active_revision_ids": active,
                "revisions": [
                    {
                        "revision_id": item["revision_id"],
                        "lifecycle": item["lifecycle"],
                        "contract": item["contract"],
                    }
                    for item in revisions
                ],
                "executions": [
                    {
                        "execution_id": item["execution_id"],
                        "revision_id": item["revision_id"],
                        "status": item["status"],
                        "origin": item["origin"],
                        "started_at": item["started_at"],
                        "execution_set_id": item["execution_set_id"],
                    }
                    for item in executions
                ],
                "execution_sets": [
                    {
                        "set_id": item["set_id"],
                        "revision_id": item["revision_id"],
                        "purpose": item["purpose"],
                        "status": item["status"],
                    }
                    for item in execution_sets
                ],
                "analysis_counts": {
                    "single": single_count,
                    "multi": len(multi_analyses),
                },
            }
            atomic_write_json(skill_directory / "index.json", index)
            display_name = skill_id
            for revision in revisions:
                package = (
                    skill_directory
                    / "revisions"
                    / revision["revision_id"]
                    / "package"
                    / "SKILL.md"
                )
                candidate = _skill_display_name(package)
                if candidate:
                    display_name = candidate
                    if revision["lifecycle"] == "active":
                        break
            skill_summaries.append(
                {
                    "skill_id": skill_id,
                    "display_name": display_name,
                    "active_revision_ids": active,
                    "revision_count": len(revisions),
                    "execution_count": len(executions),
                    "single_analysis_count": single_count,
                    "multi_analysis_count": len(multi_analyses),
                    "path": f"skills/{skill_id}",
                }
            )
        catalog: JsonObject = {
            "schema": SKILL_CATALOG_SCHEMA,
            "generated_at": utc_now(),
            "skills": skill_summaries,
        }
        atomic_write_json(self.root / "catalog.json", catalog)
        return catalog

    def _load_manifests(
        self,
        root: Path,
        name: str,
        validator: Any,
    ) -> list[JsonObject]:
        if not root.is_dir():
            return []
        result: list[JsonObject] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            path = directory / name
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and path.is_file()
                and not path.is_symlink()
            ):
                result.append(validator(load_json_object(path)))
        return result

    def _trusted_directory(
        self,
        directory: Path,
        boundary: Path,
        *,
        label: str,
    ) -> Path:
        if directory.is_symlink() or not directory.is_dir():
            raise HierarchyError(f"{label} does not exist or is unsafe")
        resolved = directory.resolve()
        root = boundary.resolve()
        if not resolved.is_relative_to(root):
            raise HierarchyError(f"{label} leaves the hierarchy root")
        return resolved

    def _validate_execution_relationships(self, execution: Mapping[str, Any]) -> None:
        self.load_revision(str(execution["skill_id"]), str(execution["revision_id"]))
        set_id = execution.get("execution_set_id")
        if set_id is not None:
            execution_set = self.load_execution_set(
                str(execution["skill_id"]), str(set_id)
            )
            if execution_set["revision_id"] != execution["revision_id"]:
                raise HierarchyError(
                    "Execution and ExecutionSet must use the same revision"
                )


def _skill_display_name(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'") or None
    return None


def artifact_record(
    path: Path,
    *,
    object_directory: Path,
    artifact_id: str,
) -> JsonObject:
    """Build one immutable artifact reference inside a hierarchy object."""

    resolved = path.resolve()
    root = object_directory.resolve()
    if not resolved.is_relative_to(root):
        raise HierarchyError("Artifact leaves its hierarchy object")
    relative = resolved.relative_to(root).as_posix()
    exists = resolved.is_file() and not resolved.is_symlink()
    media_type = mimetypes.guess_type(resolved.name)[0]
    return {
        "artifact_id": _identifier(artifact_id, label="artifact_id"),
        "path": relative,
        "bytes": resolved.stat().st_size if exists else None,
        "sha256": _sha256(resolved) if exists else None,
        "media_type": media_type,
    }


def execution_manifest_from_payload(
    *,
    execution_directory: str | os.PathLike[str],
    skill_id: str,
    revision_id: str,
    execution_id: str,
    origin: str,
    execution_set_id: str | None = None,
    comparison_id: str | None = None,
    legacy: Mapping[str, Any] | None = None,
) -> JsonObject:
    """Project one preserved run payload into ``skill.execution.v1``."""

    directory = Path(execution_directory).resolve()
    payload = directory / "payload"
    trajectory_path = _find_trajectory(payload)
    records = _read_trajectory_records(trajectory_path) if trajectory_path is not None else []
    first_manifest = _first_trajectory_manifest(records)
    outcome = _trajectory_outcome(records)
    registered_inputs: list[Path] = []
    registered_outputs: list[Path] = []
    for record in records:
        if record.get("type") != "artifact_registered":
            continue
        record_payload = record.get("payload")
        if not isinstance(record_payload, Mapping):
            continue
        artifact = record_payload.get("artifact")
        if not isinstance(artifact, Mapping):
            continue
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        target = payload / raw_path
        if record_payload.get("artifact_role") == "input":
            registered_inputs.append(target)
        elif record_payload.get("artifact_role") == "output":
            registered_outputs.append(target)

    input_records = [
        artifact_record(
            path,
            object_directory=directory,
            artifact_id=f"input-{index}",
        )
        for index, path in enumerate(registered_inputs, start=1)
    ]
    output_records = [
        artifact_record(
            path,
            object_directory=directory,
            artifact_id=f"output-{index}",
        )
        for index, path in enumerate(registered_outputs, start=1)
    ]
    declared = {
        path.resolve() for path in [*registered_inputs, *registered_outputs]
    }
    supporting: list[JsonObject] = []
    artifacts_root = payload / "artifacts"
    if artifacts_root.is_dir():
        for path in sorted(artifacts_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.resolve() in declared:
                continue
            relative_to_artifacts = path.relative_to(artifacts_root)
            if relative_to_artifacts.parts[:1] == ("skill",):
                continue
            supporting.append(
                artifact_record(
                    path,
                    object_directory=directory,
                    artifact_id=f"supporting-{len(supporting) + 1}",
                )
            )

    trajectory_schema = records[0].get("schema") if records else None
    source_format = (
        "legacy"
        if trajectory_schema == "trace.actions.v1"
        or (trajectory_path is not None and trajectory_path.name == "trace.jsonl")
        else "current"
    )
    sealed = any(record.get("type") in _TRAJECTORY_SEALED for record in records)
    session_path = payload / "pi-session.jsonl"
    session = outcome.get("session")
    session_status = (
        session.get("status") if isinstance(session, Mapping) else None
    )
    if session_status is None and session_path.is_file():
        session_status = "preserved"
    status = outcome.get("status")
    if status not in EXECUTION_STATUSES:
        status = "orchestration_failed" if trajectory_path is None else "indeterminate"
    task = first_manifest.get("task_case")
    if not isinstance(task, Mapping):
        task = first_manifest.get("task")
    setup: JsonObject = {}
    for field in ("skill", "runtime", "source"):
        if field in first_manifest:
            setup[field] = first_manifest[field]
    value: JsonObject = {
        "schema": SKILL_EXECUTION_SCHEMA,
        "execution_id": execution_id,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "status": status,
        "origin": origin,
        "execution_set_id": execution_set_id,
        "comparison_id": comparison_id,
        "started_at": outcome.get("started_at") or first_manifest.get("started_at"),
        "ended_at": outcome.get("ended_at"),
        "duration_ms": outcome.get("duration_ms"),
        "task": dict(task) if isinstance(task, Mapping) else {},
        "inputs": input_records,
        "outputs": output_records,
        "supporting_artifacts": supporting,
        "trajectory": {
            "path": (
                trajectory_path.relative_to(directory).as_posix()
                if trajectory_path is not None
                else None
            ),
            "schema": trajectory_schema,
            "source_format": source_format,
            "sealed": sealed,
        },
        "session": {
            "path": (
                session_path.relative_to(directory).as_posix()
                if session_path.is_file()
                else None
            ),
            "status": session_status,
        },
        "setup": setup,
        "legacy": dict(legacy) if legacy is not None else None,
    }
    return validate_skill_execution(value)


def _find_trajectory(payload: Path) -> Path | None:
    for name in ("trajectory.jsonl", "trace.jsonl"):
        path = payload / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _read_trajectory_records(path: Path) -> list[JsonObject]:
    records: list[JsonObject] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise HierarchyError(
                    f"Trajectory line {line_number} is invalid JSON"
                ) from error
            if not isinstance(value, dict):
                raise HierarchyError(
                    f"Trajectory line {line_number} must be a JSON object"
                )
            records.append(value)
    return records


def _first_trajectory_manifest(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    for record in records:
        record_type = record.get("type")
        if record_type not in {"trajectory_started", "trace_started"}:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return {}
        manifest = payload.get("manifest")
        return dict(manifest) if isinstance(manifest, Mapping) else {}
    return {}


def _trajectory_outcome(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    for record in reversed(records):
        if record.get("type") not in _TRAJECTORY_FINISHED:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return {}
        outcome = payload.get("outcome")
        return dict(outcome) if isinstance(outcome, Mapping) else {}
    return {}
