"""Gate internal multi-Trajectory research from corpus validation to specialists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
import uuid

from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AGENT_TERMINAL_STATUSES,
    AgentOrchestrationError,
    AgentRole,
    MultiPiOrchestrator,
    SpecialistRunOutcome,
)
from skill_evolution.research_board import SpecialistBoardRepository
from skill_evolution.research_capability import (
    RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA,
    ResearchCapabilityError,
    build_research_capability_certificate,
    research_capability_certificate_digest,
    research_capability_execution_identity_digest,
    research_capability_identity_digest,
    validate_research_capability_certificate,
    validate_research_capability_identity,
)
from skill_evolution.research_artifacts import (
    ResearchArtifactError,
    seal_research_result_reference,
    validate_research_result_reference,
    verify_research_result_reference,
)
from skill_evolution.research_corpus import (
    RESEARCH_OBJECTIVES,
    RESEARCH_READINESS_SCHEMA,
    ResearchCorpusError,
    ResearchCorpusResult,
    verify_research_corpus,
)
from skill_evolution.research_harness_acceptance import (
    HARNESS_CHECKS,
    HARNESS_VALIDATOR_VERSION,
    HarnessAcceptanceError,
    run_harness_acceptance,
    verify_harness_acceptance_report,
)
from skill_evolution.research_sandbox import DockerResearchSandbox
from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    StorageError,
    atomic_write_json,
    load_json_object,
    new_object_id,
    utc_now,
)


RESEARCH_BATCH_SCHEMA = "research.batch.v1"
VALIDATION_BENCHMARK_SCHEMA = "research.validation_benchmark.v1"
VALIDATION_BENCHMARK_BINDING_SCHEMA = (
    "research.validation_benchmark_binding.v1"
)
VALIDATION_BENCHMARK_SNAPSHOT = "validation/benchmark.json"
HARNESS_ACCEPTANCE_BINDING_SCHEMA = (
    "research.harness_acceptance_binding.v1"
)
HARNESS_ACCEPTANCE_SNAPSHOT = "harness/acceptance.json"
CAPABILITY_CERTIFICATE_BINDING_SCHEMA = (
    "research.capability_certificate_binding.v1"
)
CAPABILITY_CERTIFICATE_SNAPSHOT = "capability/certificate.json"
RESEARCH_BATCH_STATUSES = frozenset(
    {
        "prepared",
        "harness_validated",
        "single_agent_validation_running",
        "single_agent_validated",
        "specialists_running",
        "specialists_incomplete",
        "specialists_completed",
        "failed",
    }
)
SMOKE_ATTEMPTS_PER_CYCLE = 2
REVIEW_CHECKS = (
    "evidence",
    "protocol",
    "safety",
    "hidden_benchmark",
)
FAILURE_CATEGORIES = frozenset(
    {
        "evidence_unreachable",
        "protocol_unclear",
        "sample_insufficient",
        "result_gate",
        "agent_exploration",
    }
)
BENCHMARK_REQUIRED_FINDINGS = (
    "common_logical_stage",
    "common_purpose",
    "script_or_flow_differences",
    "before_after_effect",
    "non_adopting_trajectories_or_counterexample_scope",
    "raw_evidence_locations",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResearchWorkflowError(ValueError):
    """Raised when an internal research batch attempts an unsafe transition."""


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchWorkflowError(f"{label} must be non-empty text")
    return value.strip()


def _digest(value: object, *, label: str) -> str:
    normalized = _text(value, label=label)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ResearchWorkflowError(f"{label} must be a lowercase SHA-256")
    return normalized


def _file_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ResearchWorkflowError(f"File cannot be inspected: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ResearchWorkflowError(f"File is not a safe regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_directory_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _write_new_capability_snapshot(
    batch_root: Path,
    value: Mapping[str, Any],
) -> tuple[Path, str]:
    """Create the fixed certificate beneath pinned, no-follow directories."""

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        batch_fd = os.open(batch_root, directory_flags | no_follow)
    except OSError as error:
        raise ResearchWorkflowError(
            "Research batch directory cannot be opened safely"
        ) from error
    parent_fd: int | None = None
    temporary_name = f".certificate.json.{uuid.uuid4().hex}.tmp"
    try:
        batch_identity = os.fstat(batch_fd)
        try:
            os.mkdir("capability", mode=0o700, dir_fd=batch_fd)
        except FileExistsError:
            pass
        try:
            parent_fd = os.open(
                "capability",
                directory_flags | no_follow,
                dir_fd=batch_fd,
            )
        except OSError as error:
            raise ResearchWorkflowError(
                "Capability certificate parent is unsafe"
            ) from error
        parent_identity = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_identity.st_mode):
            raise ResearchWorkflowError(
                "Capability certificate parent must be a directory"
            )
        try:
            os.stat(
                "certificate.json",
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ResearchWorkflowError(
                "Capability certificate snapshot path is not empty"
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
        temporary_fd = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            "certificate.json",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        written = os.stat(
            "certificate.json",
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(written.st_mode):
            raise ResearchWorkflowError(
                "Capability certificate is not a regular file"
            )
        current_batch = os.stat(batch_root, follow_symlinks=False)
        current_parent = os.stat(
            batch_root / "capability",
            follow_symlinks=False,
        )
        if (
            not _same_directory_identity(batch_identity, current_batch)
            or not _same_directory_identity(parent_identity, current_parent)
        ):
            raise ResearchWorkflowError(
                "Capability certificate parent changed during write"
            )
    except OSError as error:
        raise ResearchWorkflowError(
            "Capability certificate could not be written safely"
        ) from error
    finally:
        if parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        os.close(batch_fd)
    return (
        batch_root / CAPABILITY_CERTIFICATE_SNAPSHOT,
        hashlib.sha256(encoded).hexdigest(),
    )


def _mapping(value: object, *, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchWorkflowError(f"{label} must be an object")
    return dict(value)


def _timestamp(value: object, *, label: str) -> str:
    normalized = _text(value, label=label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResearchWorkflowError(f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ResearchWorkflowError(f"{label} must include a timezone")
    return normalized


def validate_validation_benchmark(value: Mapping[str, Any]) -> JsonObject:
    """Validate one approved, human-authored hidden smoke benchmark."""

    expected = {
        "schema",
        "benchmark_id",
        "title",
        "status",
        "skill_id",
        "revision_id",
        "execution_ids",
        "required_discoveries",
        "owner",
        "approved_by",
        "approved_at",
    }
    if set(value) != expected:
        raise ResearchWorkflowError(
            "Validation benchmark fields differ from schema"
        )
    if value.get("schema") != VALIDATION_BENCHMARK_SCHEMA:
        raise ResearchWorkflowError("Unsupported validation benchmark schema")
    if value.get("status") != "approved":
        raise ResearchWorkflowError(
            "Validation benchmark is not approved by its owner"
        )
    execution_ids = value.get("execution_ids")
    if (
        not isinstance(execution_ids, list)
        or not execution_ids
        or len(execution_ids) != len(set(execution_ids))
        or not all(isinstance(item, str) and item for item in execution_ids)
    ):
        raise ResearchWorkflowError(
            "Validation benchmark Execution IDs are invalid"
        )
    raw_discoveries = value.get("required_discoveries")
    if not isinstance(raw_discoveries, list) or not raw_discoveries:
        raise ResearchWorkflowError(
            "Validation benchmark requires at least one discovery"
        )
    discoveries: list[JsonObject] = []
    discovery_ids: set[str] = set()
    for index, raw_discovery in enumerate(raw_discoveries):
        label = f"required_discoveries[{index}]"
        if not isinstance(raw_discovery, Mapping) or set(raw_discovery) != {
            "discovery_id",
            "description",
            "minimum_supporting_trajectory_count",
            "required_findings",
        }:
            raise ResearchWorkflowError(f"{label} fields differ from schema")
        discovery_id = _text(
            raw_discovery.get("discovery_id"),
            label=f"{label}.discovery_id",
        )
        if discovery_id in discovery_ids:
            raise ResearchWorkflowError(
                "Validation benchmark discovery IDs must be unique"
            )
        discovery_ids.add(discovery_id)
        minimum = raw_discovery.get("minimum_supporting_trajectory_count")
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 2
            or minimum > len(execution_ids)
        ):
            raise ResearchWorkflowError(
                f"{label} has an invalid supporting trajectory minimum"
            )
        findings = raw_discovery.get("required_findings")
        if findings != list(BENCHMARK_REQUIRED_FINDINGS):
            raise ResearchWorkflowError(
                f"{label} must declare every required finding"
            )
        discoveries.append(
            {
                "discovery_id": discovery_id,
                "description": _text(
                    raw_discovery.get("description"),
                    label=f"{label}.description",
                ),
                "minimum_supporting_trajectory_count": minimum,
                "required_findings": list(BENCHMARK_REQUIRED_FINDINGS),
            }
        )
    return {
        "schema": VALIDATION_BENCHMARK_SCHEMA,
        "benchmark_id": _text(
            value.get("benchmark_id"),
            label="benchmark_id",
        ),
        "title": _text(value.get("title"), label="title"),
        "status": "approved",
        "skill_id": _text(value.get("skill_id"), label="skill_id"),
        "revision_id": _text(
            value.get("revision_id"),
            label="revision_id",
        ),
        "execution_ids": list(execution_ids),
        "required_discoveries": discoveries,
        "owner": _text(value.get("owner"), label="owner"),
        "approved_by": _text(
            value.get("approved_by"),
            label="approved_by",
        ),
        "approved_at": _timestamp(
            value.get("approved_at"),
            label="approved_at",
        ),
    }


def _validate_benchmark_binding(value: object) -> JsonObject | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "benchmark_id",
        "snapshot_path",
        "source_sha256",
        "snapshot_sha256",
        "frozen_at",
    }:
        raise ResearchWorkflowError(
            "Validation benchmark binding fields differ from schema"
        )
    if value.get("schema") != VALIDATION_BENCHMARK_BINDING_SCHEMA:
        raise ResearchWorkflowError(
            "Unsupported validation benchmark binding schema"
        )
    if value.get("snapshot_path") != VALIDATION_BENCHMARK_SNAPSHOT:
        raise ResearchWorkflowError("Validation benchmark snapshot path changed")
    return {
        "schema": VALIDATION_BENCHMARK_BINDING_SCHEMA,
        "benchmark_id": _text(
            value.get("benchmark_id"),
            label="validation_benchmark.benchmark_id",
        ),
        "snapshot_path": VALIDATION_BENCHMARK_SNAPSHOT,
        "source_sha256": _digest(
            value.get("source_sha256"),
            label="validation_benchmark.source_sha256",
        ),
        "snapshot_sha256": _digest(
            value.get("snapshot_sha256"),
            label="validation_benchmark.snapshot_sha256",
        ),
        "frozen_at": _timestamp(
            value.get("frozen_at"),
            label="validation_benchmark.frozen_at",
        ),
    }


def _validate_harness_binding(value: object) -> JsonObject | None:
    """Validate the immutable reference to a workflow-executed acceptance."""

    if value is None:
        return None
    expected = {
        "schema",
        "status",
        "snapshot_path",
        "file_sha256",
        "content_sha256",
        "validator_version",
        "execution_identity_sha256",
        "image_id",
        "checks",
        "recorded_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ResearchWorkflowError(
            "Harness acceptance binding fields differ from schema"
        )
    if value.get("schema") != HARNESS_ACCEPTANCE_BINDING_SCHEMA:
        raise ResearchWorkflowError(
            "Unsupported Harness acceptance binding schema"
        )
    status = value.get("status")
    if status not in {"passed", "failed"}:
        raise ResearchWorkflowError("Harness acceptance status is invalid")
    if value.get("snapshot_path") != HARNESS_ACCEPTANCE_SNAPSHOT:
        raise ResearchWorkflowError("Harness acceptance snapshot path changed")
    checks = value.get("checks")
    if (
        not isinstance(checks, Mapping)
        or set(checks) != set(HARNESS_CHECKS)
        or not all(isinstance(item, bool) for item in checks.values())
    ):
        raise ResearchWorkflowError("Harness acceptance checks are invalid")
    expected_status = "passed" if all(checks.values()) else "failed"
    if status != expected_status:
        raise ResearchWorkflowError(
            "Harness acceptance status differs from its checks"
        )
    if value.get("validator_version") != HARNESS_VALIDATOR_VERSION:
        raise ResearchWorkflowError("Harness validator version changed")
    image_id = value.get("image_id")
    if image_id is not None and (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    ):
        raise ResearchWorkflowError("Harness sandbox image identity is invalid")
    if status == "passed" and image_id is None:
        raise ResearchWorkflowError(
            "Passed Harness acceptance lacks an immutable image identity"
        )
    return {
        "schema": HARNESS_ACCEPTANCE_BINDING_SCHEMA,
        "status": status,
        "snapshot_path": HARNESS_ACCEPTANCE_SNAPSHOT,
        "file_sha256": _digest(
            value.get("file_sha256"),
            label="harness.file_sha256",
        ),
        "content_sha256": _digest(
            value.get("content_sha256"),
            label="harness.content_sha256",
        ),
        "validator_version": HARNESS_VALIDATOR_VERSION,
        "execution_identity_sha256": _digest(
            value.get("execution_identity_sha256"),
            label="harness.execution_identity_sha256",
        ),
        "image_id": image_id,
        "checks": {name: bool(checks[name]) for name in HARNESS_CHECKS},
        "recorded_at": _timestamp(
            value.get("recorded_at"),
            label="harness.recorded_at",
        ),
    }


def _validate_capability_binding(value: object) -> JsonObject | None:
    """Validate the frozen certificate reference carried by one batch."""

    if value is None:
        return None
    expected = {
        "schema",
        "mode",
        "snapshot_path",
        "file_sha256",
        "certificate_sha256",
        "identity_sha256",
        "source_batch_id",
        "bound_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ResearchWorkflowError(
            "Capability certificate binding fields differ from schema"
        )
    if value.get("schema") != CAPABILITY_CERTIFICATE_BINDING_SCHEMA:
        raise ResearchWorkflowError(
            "Unsupported capability certificate binding schema"
        )
    mode = value.get("mode")
    if mode not in {"issued", "imported"}:
        raise ResearchWorkflowError("Capability certificate mode is invalid")
    if value.get("snapshot_path") != CAPABILITY_CERTIFICATE_SNAPSHOT:
        raise ResearchWorkflowError("Capability certificate path changed")
    return {
        "schema": CAPABILITY_CERTIFICATE_BINDING_SCHEMA,
        "mode": mode,
        "snapshot_path": CAPABILITY_CERTIFICATE_SNAPSHOT,
        "file_sha256": _digest(
            value.get("file_sha256"),
            label="capability.file_sha256",
        ),
        "certificate_sha256": _digest(
            value.get("certificate_sha256"),
            label="capability.certificate_sha256",
        ),
        "identity_sha256": _digest(
            value.get("identity_sha256"),
            label="capability.identity_sha256",
        ),
        "source_batch_id": _text(
            value.get("source_batch_id"),
            label="capability.source_batch_id",
        ),
        "bound_at": _timestamp(
            value.get("bound_at"),
            label="capability.bound_at",
        ),
    }


def _load_validation_benchmark_source(
    source: Path,
) -> tuple[bytes, JsonObject]:
    if source.is_symlink() or not source.is_file():
        raise ResearchWorkflowError(
            "Validation benchmark source is missing or unsafe"
        )
    try:
        raw = source.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResearchWorkflowError(
            "Validation benchmark source is not valid UTF-8 JSON"
        ) from error
    if not isinstance(decoded, Mapping):
        raise ResearchWorkflowError(
            "Validation benchmark source must be a JSON object"
        )
    return raw, validate_validation_benchmark(decoded)


def _validate_readiness(value: Mapping[str, Any]) -> JsonObject:
    if value.get("schema") != RESEARCH_READINESS_SCHEMA:
        raise ResearchWorkflowError("Unsupported research readiness schema")
    if value.get("status") != "ready":
        raise ResearchWorkflowError("Research readiness is not ready")
    issues = value.get("issues")
    if issues != []:
        raise ResearchWorkflowError("Ready research cannot retain issues")
    objectives = value.get("objectives")
    execution_ids = value.get("execution_ids")
    if not isinstance(objectives, list) or not objectives:
        raise ResearchWorkflowError("Research readiness has no objectives")
    if not set(objectives).issubset(RESEARCH_OBJECTIVES):
        raise ResearchWorkflowError("Research readiness has unknown objectives")
    if not isinstance(execution_ids, list) or not execution_ids:
        raise ResearchWorkflowError("Research readiness has no Executions")
    if len(execution_ids) != len(set(execution_ids)) or not all(
        isinstance(item, str) and item for item in execution_ids
    ):
        raise ResearchWorkflowError("Readiness Execution IDs must be unique")
    _text(value.get("skill_id"), label="readiness.skill_id")
    _text(value.get("revision_id"), label="readiness.revision_id")
    return dict(value)


def _validate_smoke_attempt(value: Mapping[str, Any], *, label: str) -> JsonObject:
    expected = {
        "attempt_id",
        "agent_run_id",
        "session_ref",
        "status",
        "result_ref",
        "error",
        "recorded_at",
    }
    if set(value) != expected:
        raise ResearchWorkflowError(f"{label} fields differ from schema")
    status = value.get("status")
    if status not in AGENT_TERMINAL_STATUSES:
        raise ResearchWorkflowError(f"{label} has a non-terminal status")
    agent_run_id = value.get("agent_run_id")
    session_ref = value.get("session_ref")
    result_ref = value.get("result_ref")
    error = value.get("error")
    if agent_run_id is not None:
        _text(agent_run_id, label=f"{label}.agent_run_id")
    if session_ref is not None:
        _text(session_ref, label=f"{label}.session_ref")
    if result_ref is not None and not isinstance(result_ref, Mapping):
        raise ResearchWorkflowError(f"{label}.result_ref must be an object")
    if error is not None and not isinstance(error, Mapping):
        raise ResearchWorkflowError(f"{label}.error must be an object")
    if status == "succeeded" and (
        agent_run_id is None or session_ref is None or not result_ref
    ):
        raise ResearchWorkflowError(
            f"{label} succeeded without run, session, or result evidence"
        )
    if status == "succeeded":
        try:
            result_ref = validate_research_result_reference(
                result_ref,
                expected_role=AgentRole.BEHAVIOR_PATTERN.value,
                expected_agent_run_id=str(agent_run_id),
            )
        except ResearchArtifactError as artifact_error:
            raise ResearchWorkflowError(str(artifact_error)) from artifact_error
    return dict(value)


def _validate_cycle(
    value: Mapping[str, Any],
    *,
    expected_number: int,
    benchmark_sha256: str | None,
) -> JsonObject:
    expected = {
        "cycle",
        "status",
        "repair_summary",
        "repair_categories",
        "capability_identity",
        "capability_identity_sha256",
        "attempts",
        "reviews",
        "failure_reasons",
        "started_at",
        "ended_at",
    }
    if set(value) != expected or value.get("cycle") != expected_number:
        raise ResearchWorkflowError("Validation cycle identity is invalid")
    repair_summary = value.get("repair_summary")
    if expected_number == 1 and repair_summary is not None:
        raise ResearchWorkflowError("First validation cycle cannot claim repair")
    if expected_number > 1:
        _text(repair_summary, label="cycle.repair_summary")
    repair_categories = value.get("repair_categories")
    if (
        not isinstance(repair_categories, list)
        or len(repair_categories) != len(set(repair_categories))
        or not set(repair_categories).issubset(FAILURE_CATEGORIES)
        or (expected_number == 1 and repair_categories)
        or (expected_number > 1 and not repair_categories)
    ):
        raise ResearchWorkflowError("Cycle repair categories are invalid")
    status = value.get("status")
    if status not in {"running", "awaiting_review", "passed", "failed"}:
        raise ResearchWorkflowError("Validation cycle status is invalid")
    raw_identity = value.get("capability_identity")
    if not isinstance(raw_identity, Mapping):
        raise ResearchWorkflowError("Validation cycle lacks capability identity")
    try:
        capability_identity = validate_research_capability_identity(raw_identity)
        capability_identity_sha256 = research_capability_identity_digest(
            capability_identity
        )
    except ResearchCapabilityError as error:
        raise ResearchWorkflowError(str(error)) from error
    if value.get("capability_identity_sha256") != capability_identity_sha256:
        raise ResearchWorkflowError(
            "Validation cycle capability identity digest differs"
        )
    attempts_value = value.get("attempts")
    reviews_value = value.get("reviews")
    if not isinstance(attempts_value, list) or len(attempts_value) > 2:
        raise ResearchWorkflowError("Validation cycle attempts are invalid")
    if not isinstance(reviews_value, list) or len(reviews_value) > 2:
        raise ResearchWorkflowError("Validation cycle reviews are invalid")
    attempts = [
        _validate_smoke_attempt(item, label=f"cycle.attempts[{index}]")
        for index, item in enumerate(attempts_value)
        if isinstance(item, Mapping)
    ]
    if len(attempts) != len(attempts_value):
        raise ResearchWorkflowError("Validation attempt must be an object")
    attempt_ids = [str(item["attempt_id"]) for item in attempts]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise ResearchWorkflowError("Validation attempt IDs must be unique")
    reviews: list[JsonObject] = []
    for review in reviews_value:
        if not isinstance(review, Mapping) or set(review) != {
            "review_id",
            "attempt_id",
            "reviewer",
            "checks",
            "benchmark_sha256",
            "reviewed_at",
        }:
            raise ResearchWorkflowError("Smoke review fields are invalid")
        checks = review.get("checks")
        if not isinstance(checks, Mapping) or set(checks) != set(REVIEW_CHECKS):
            raise ResearchWorkflowError("Smoke review checks are incomplete")
        if not all(isinstance(item, bool) for item in checks.values()):
            raise ResearchWorkflowError("Smoke review checks must be boolean")
        if review.get("attempt_id") not in attempt_ids:
            raise ResearchWorkflowError("Smoke review references another attempt")
        _text(review.get("review_id"), label="review_id")
        _text(review.get("reviewer"), label="reviewer")
        review_digest = _digest(
            review.get("benchmark_sha256"),
            label="review.benchmark_sha256",
        )
        if benchmark_sha256 is None or review_digest != benchmark_sha256:
            raise ResearchWorkflowError(
                "Smoke review references another validation benchmark"
            )
        _text(review.get("reviewed_at"), label="reviewed_at")
        reviews.append(dict(review))
    reviewed_ids = [str(item["attempt_id"]) for item in reviews]
    if len(reviewed_ids) != len(set(reviewed_ids)):
        raise ResearchWorkflowError("Smoke attempt cannot be reviewed twice")
    failure_reasons = value.get("failure_reasons")
    if not isinstance(failure_reasons, list) or not all(
        isinstance(item, str) and item for item in failure_reasons
    ):
        raise ResearchWorkflowError("Cycle failure reasons are invalid")
    if (
        len(failure_reasons) != len(set(failure_reasons))
        or not set(failure_reasons).issubset(FAILURE_CATEGORIES)
    ):
        raise ResearchWorkflowError("Cycle failure categories are invalid")
    if status == "running" and (len(attempts) >= 2 or reviews):
        raise ResearchWorkflowError("Running cycle has too much terminal state")
    if status == "awaiting_review" and (
        len(attempts) != 2
        or any(item["status"] != "succeeded" for item in attempts)
        or failure_reasons
    ):
        raise ResearchWorkflowError("Reviewing cycle has invalid attempts")
    if status == "passed" and (
        len(attempts) != 2
        or len(reviews) != 2
        or any(item["status"] != "succeeded" for item in attempts)
        or any(not all(item["checks"].values()) for item in reviews)
        or len({item["agent_run_id"] for item in attempts}) != 2
        or len({item["session_ref"] for item in attempts}) != 2
        or failure_reasons
    ):
        raise ResearchWorkflowError("Passed cycle does not satisfy every gate")
    if status == "failed" and not failure_reasons:
        raise ResearchWorkflowError("Failed cycle requires explicit reasons")
    ended_at = value.get("ended_at")
    if status in {"passed", "failed"}:
        _text(ended_at, label="cycle.ended_at")
    elif ended_at is not None:
        raise ResearchWorkflowError("Open cycle cannot have ended_at")
    _text(value.get("started_at"), label="cycle.started_at")
    return {
        **dict(value),
        "capability_identity": capability_identity,
        "capability_identity_sha256": capability_identity_sha256,
        "attempts": attempts,
        "reviews": reviews,
    }


def _validate_corpus(
    corpus: ResearchCorpusResult,
) -> tuple[str, str, Path, JsonObject]:
    readiness = _validate_readiness(corpus.readiness)
    try:
        verified = verify_research_corpus(
            corpus.directory,
            expected_content_sha256=corpus.corpus_digest,
            expected_baseline_sha256=corpus.baseline_digest,
        )
    except ResearchCorpusError as error:
        raise ResearchWorkflowError(str(error)) from error
    stored_manifest = verified.manifest
    if stored_manifest != corpus.manifest:
        raise ResearchWorkflowError("Research corpus result differs from disk")
    if verified.corpus_map != corpus.corpus_map:
        raise ResearchWorkflowError("Research corpus map differs from disk")
    if verified.navigation_index != corpus.navigation_index:
        raise ResearchWorkflowError("Research navigation index differs from disk")
    if verified.baseline != corpus.baseline:
        raise ResearchWorkflowError("Research baseline differs from disk")
    if stored_manifest.get("execution_ids") != readiness["execution_ids"]:
        raise ResearchWorkflowError("Corpus and readiness Executions differ")
    if stored_manifest.get("objectives") != readiness["objectives"]:
        raise ResearchWorkflowError("Corpus and readiness objectives differ")
    if stored_manifest.get("skill_id") != readiness["skill_id"]:
        raise ResearchWorkflowError("Corpus and readiness Skill differ")
    if stored_manifest.get("revision_id") != readiness["revision_id"]:
        raise ResearchWorkflowError("Corpus and readiness Revision differ")
    return (
        verified.content_sha256,
        verified.baseline_sha256,
        verified.directory,
        readiness,
    )


def validate_research_batch(value: Mapping[str, Any]) -> JsonObject:
    """Validate the durable batch envelope and its immutable bindings."""

    expected = {
        "schema",
        "id",
        "status",
        "corpus_id",
        "corpus_digest",
        "baseline_digest",
        "evidence_path",
        "readiness",
        "harness_validation",
        "validation_benchmark",
        "validation_cycles",
        "capability_certification",
        "specialist_board_id",
        "failure",
        "created_at",
        "updated_at",
    }
    if set(value) != expected:
        raise ResearchWorkflowError("Research batch fields differ from schema")
    if value.get("schema") != RESEARCH_BATCH_SCHEMA:
        raise ResearchWorkflowError("Unsupported research batch schema")
    status = value.get("status")
    if status not in RESEARCH_BATCH_STATUSES:
        raise ResearchWorkflowError("Unsupported research batch status")
    benchmark = _validate_benchmark_binding(
        value.get("validation_benchmark")
    )
    capability = _validate_capability_binding(
        value.get("capability_certification")
    )
    cycles_value = value.get("validation_cycles")
    if not isinstance(cycles_value, list):
        raise ResearchWorkflowError("validation_cycles must be a list")
    cycles = [
        _validate_cycle(
            item,
            expected_number=index,
            benchmark_sha256=(
                str(benchmark["snapshot_sha256"])
                if benchmark is not None
                else None
            ),
        )
        for index, item in enumerate(cycles_value, start=1)
        if isinstance(item, Mapping)
    ]
    if len(cycles) != len(cycles_value):
        raise ResearchWorkflowError("Validation cycle must be an object")
    for cycle in cycles:
        for attempt in cycle["attempts"]:
            if attempt["status"] != "succeeded":
                continue
            try:
                validate_research_result_reference(
                    attempt["result_ref"],
                    expected_role=AgentRole.BEHAVIOR_PATTERN.value,
                    expected_agent_run_id=str(attempt["agent_run_id"]),
                    expected_corpus_digest=str(value.get("corpus_digest")),
                    expected_baseline_digest=str(value.get("baseline_digest")),
                )
            except ResearchArtifactError as artifact_error:
                raise ResearchWorkflowError(
                    str(artifact_error)
                ) from artifact_error
    harness = _validate_harness_binding(value.get("harness_validation"))
    board_id = value.get("specialist_board_id")
    if board_id is not None:
        _text(board_id, label="specialist_board_id")
    failure = value.get("failure")
    if failure is not None and not isinstance(failure, Mapping):
        raise ResearchWorkflowError("failure must be an object or null")
    if status == "prepared" and (
        harness is not None
        or benchmark is not None
        or cycles
        or capability is not None
        or board_id
    ):
        raise ResearchWorkflowError("Prepared batch already contains later state")
    if status != "prepared" and (
        not isinstance(harness, Mapping) or harness.get("status") != "passed"
    ):
        if not (
            status == "failed"
            and isinstance(harness, Mapping)
            and harness.get("status") == "failed"
        ):
            raise ResearchWorkflowError("Batch advanced without Harness approval")
    if benchmark is not None and (
        not isinstance(harness, Mapping) or harness.get("status") != "passed"
    ):
        raise ResearchWorkflowError(
            "Validation benchmark was frozen without a passed Harness"
        )
    latest_cycle = cycles[-1] if cycles else None
    if cycles and benchmark is None:
        raise ResearchWorkflowError(
            "Validation cycle has no frozen hidden benchmark"
        )
    if cycles and any(cycle["status"] != "failed" for cycle in cycles[:-1]):
        raise ResearchWorkflowError(
            "Only a failed validation cycle can be followed by a repair"
        )
    if status == "harness_validated" and (
        cycles or capability is not None or board_id is not None
    ):
        raise ResearchWorkflowError(
            "Harness-validated batch already contains later state"
        )
    if status == "single_agent_validation_running" and (
        latest_cycle is None
        or latest_cycle["status"] not in {"running", "awaiting_review"}
        or board_id is not None
    ):
        raise ResearchWorkflowError("Batch validation state differs from cycle")
    if status in {
        "single_agent_validated",
        "specialists_running",
        "specialists_incomplete",
        "specialists_completed",
    } and (
        (latest_cycle is None or latest_cycle["status"] != "passed")
        and capability is None
    ):
        raise ResearchWorkflowError(
            "Batch advanced without a passed smoke cycle or certificate"
        )
    if status.startswith("specialists_") and capability is None:
        raise ResearchWorkflowError(
            "Specialist state requires a capability certificate"
        )
    if status == "single_agent_validated" and board_id is not None:
        raise ResearchWorkflowError("Validated batch already contains a board")
    if status.startswith("specialists_") and board_id is None:
        raise ResearchWorkflowError("Specialist state requires a result board")
    if status == "failed" and failure is None:
        raise ResearchWorkflowError("Failed batch requires a failure record")
    if status != "failed" and failure is not None:
        raise ResearchWorkflowError("Non-failed batch cannot retain a failure")
    return {
        **dict(value),
        "id": _text(value.get("id"), label="batch.id"),
        "corpus_id": _text(value.get("corpus_id"), label="batch.corpus_id"),
        "corpus_digest": _digest(
            value.get("corpus_digest"),
            label="batch.corpus_digest",
        ),
        "baseline_digest": _digest(
            value.get("baseline_digest"),
            label="batch.baseline_digest",
        ),
        "evidence_path": _text(
            value.get("evidence_path"),
            label="batch.evidence_path",
        ),
        "readiness": _validate_readiness(
            _mapping(value.get("readiness"), label="batch.readiness")
        ),
        "harness_validation": harness,
        "validation_benchmark": benchmark,
        "validation_cycles": cycles,
        "capability_certification": capability,
        "created_at": _text(value.get("created_at"), label="created_at"),
        "updated_at": _text(value.get("updated_at"), label="updated_at"),
    }


class ResearchWorkflow:
    """Persist and enforce every gate of one internal research batch."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        orchestrator: MultiPiOrchestrator | None = None,
        repository_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.repository = ManifestRepository(root, manifest_name="batch.json")
        self.orchestrator = orchestrator
        self.repository_root = Path(
            repository_root or Path(__file__).resolve().parents[1]
        ).resolve()
        self._lock = threading.RLock()

    def prepare(
        self,
        corpus: ResearchCorpusResult,
        *,
        batch_id: str | None = None,
    ) -> JsonObject:
        """Create a batch only after the frozen corpus is completely ready."""

        corpus_digest, baseline_digest, evidence, readiness = (
            _validate_corpus(corpus)
        )
        identifier = batch_id or new_object_id("research-batch")
        now = utc_now()
        manifest: JsonObject = {
            "schema": RESEARCH_BATCH_SCHEMA,
            "id": identifier,
            "status": "prepared",
            "corpus_id": _text(
                corpus.manifest.get("corpus_id"),
                label="corpus.corpus_id",
            ),
            "corpus_digest": corpus_digest,
            "baseline_digest": baseline_digest,
            "evidence_path": str(evidence),
            "readiness": readiness,
            "harness_validation": None,
            "validation_benchmark": None,
            "validation_cycles": [],
            "capability_certification": None,
            "specialist_board_id": None,
            "failure": None,
            "created_at": now,
            "updated_at": now,
        }
        validate_research_batch(manifest)
        self.repository.create(identifier, manifest)
        return self.load(identifier)

    def load(self, batch_id: str) -> JsonObject:
        """Load one validated internal research batch."""

        batch = validate_research_batch(self.repository.load(batch_id))
        self._verify_harness_acceptance(batch_id, batch)
        self._verify_capability_certification(batch_id, batch)
        self._verify_smoke_result_references(batch)
        return batch

    def run_harness_validation(
        self,
        batch_id: str,
        *,
        sandbox: DockerResearchSandbox,
        pi_command: Sequence[str] | str | None = None,
        extra_pi_args: Sequence[str] = (),
        research_harness_context_path: (
            str | os.PathLike[str] | None
        ) = None,
    ) -> JsonObject:
        """Execute the fixed Harness suite and freeze its report in the batch."""

        if not isinstance(sandbox, DockerResearchSandbox):
            raise ResearchWorkflowError(
                "Harness validation requires the production Docker sandbox"
            )
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "prepared":
                raise ResearchWorkflowError("Harness gate is already closed")
            self._verify_bound_evidence(batch)
            batch_root = self.repository.object_directory(batch_id)
            if batch_root.is_symlink() or not batch_root.is_dir():
                raise ResearchWorkflowError("Research batch directory is unsafe")
            report_path = batch_root / HARNESS_ACCEPTANCE_SNAPSHOT
            if report_path.exists() or report_path.is_symlink():
                raise ResearchWorkflowError(
                    "Harness acceptance snapshot path is not empty"
                )
            try:
                verification = run_harness_acceptance(
                    corpus_directory=str(batch["evidence_path"]),
                    sandbox=sandbox,
                    report_path=report_path,
                    trusted_output_root=batch_root,
                    expected_corpus_digest=str(batch["corpus_digest"]),
                    expected_baseline_digest=str(batch["baseline_digest"]),
                    pi_command=pi_command,
                    extra_pi_args=extra_pi_args,
                    research_harness_context_path=(
                        research_harness_context_path
                    ),
                )
            except (HarnessAcceptanceError, OSError) as error:
                raise ResearchWorkflowError(str(error)) from error
            report = verification.report
            checks = {
                str(item["name"]): item["status"] == "passed"
                for item in report["checks"]
            }
            passed = verification.passed
            batch["harness_validation"] = {
                "schema": HARNESS_ACCEPTANCE_BINDING_SCHEMA,
                "status": "passed" if passed else "failed",
                "checks": {name: checks[name] for name in HARNESS_CHECKS},
                "snapshot_path": HARNESS_ACCEPTANCE_SNAPSHOT,
                "file_sha256": verification.file_sha256,
                "content_sha256": report["content_sha256"],
                "validator_version": report["validator_version"],
                "execution_identity_sha256": report[
                    "execution_identity_sha256"
                ],
                "image_id": report["sandbox"]["image_id"],
                "recorded_at": utc_now(),
            }
            batch["status"] = "harness_validated" if passed else "failed"
            batch["failure"] = (
                None
                if passed
                else {
                    "stage": "harness_validation",
                    "reasons": [
                        name for name, value in checks.items() if not value
                    ],
                }
            )
            return self._replace(batch_id, batch)

    def record_harness_validation(
        self,
        batch_id: str,
        *,
        checks: Mapping[str, bool],
        report_ref: Mapping[str, Any],
    ) -> JsonObject:
        """Reject the retired caller-authored Harness result path."""

        del batch_id, checks, report_ref
        raise ResearchWorkflowError(
            "Caller-authored Harness checks are retired; execute the fixed "
            "Harness acceptance suite"
        )

    def freeze_validation_benchmark(
        self,
        batch_id: str,
        *,
        benchmark_file: str | os.PathLike[str],
    ) -> JsonObject:
        """Freeze one approved human benchmark outside Agent-visible evidence."""

        requested = Path(benchmark_file)
        if requested.is_symlink():
            raise ResearchWorkflowError(
                "Validation benchmark source is missing or unsafe"
            )
        source = requested.resolve()
        raw, benchmark = _load_validation_benchmark_source(source)
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "harness_validated":
                raise ResearchWorkflowError(
                    "Validation benchmark requires a passed Harness"
                )
            if batch["validation_benchmark"] is not None:
                raise ResearchWorkflowError(
                    "Validation benchmark is already frozen"
                )
            self._verify_bound_evidence(batch)
            evidence = Path(str(batch["evidence_path"])).resolve()
            batch_root = self.repository.object_directory(batch_id).resolve()
            if source.is_relative_to(evidence) or source.is_relative_to(
                batch_root
            ):
                raise ResearchWorkflowError(
                    "Validation benchmark source must remain outside evidence "
                    "and batch storage"
                )
            self._require_benchmark_identity(batch, benchmark)
            snapshot = batch_root / VALIDATION_BENCHMARK_SNAPSHOT
            if snapshot.exists() or snapshot.parent.is_symlink():
                raise ResearchWorkflowError(
                    "Validation benchmark snapshot path is not empty"
                )
            atomic_write_json(snapshot, benchmark)
            snapshot_digest = _file_sha256(snapshot)
            if validate_validation_benchmark(
                load_json_object(snapshot)
            ) != benchmark:
                raise ResearchWorkflowError(
                    "Validation benchmark snapshot changed while freezing"
                )
            batch["validation_benchmark"] = {
                "schema": VALIDATION_BENCHMARK_BINDING_SCHEMA,
                "benchmark_id": benchmark["benchmark_id"],
                "snapshot_path": VALIDATION_BENCHMARK_SNAPSHOT,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "snapshot_sha256": snapshot_digest,
                "frozen_at": utc_now(),
            }
            return self._replace(batch_id, batch)

    def run_single_agent_validation_cycle(
        self,
        batch_id: str,
        *,
        repair_summary: str | None = None,
        repair_categories: list[str] | None = None,
    ) -> JsonObject:
        """Run both fresh behavior smokes and retain the whole cycle."""

        with self._lock:
            batch = self.load(batch_id)
            self._require_validation_cycle_start(
                batch,
                repair_summary,
                repair_categories,
            )
            self._verify_bound_evidence(batch)
            self._verify_validation_benchmark(batch_id, batch)
        orchestrator = self._orchestrator()
        orchestrator.preflight_specialists([AgentRole.BEHAVIOR_PATTERN])
        capability_identity = self._current_capability_identity(orchestrator)
        capability_identity_sha256 = research_capability_identity_digest(
            capability_identity,
            repository_root=self.repository_root,
        )
        with self._lock:
            batch = self.load(batch_id)
            self._require_validation_cycle_start(
                batch,
                repair_summary,
                repair_categories,
            )
            self._verify_bound_evidence(batch)
            self._verify_validation_benchmark(batch_id, batch)
            self._require_capability_matches_harness(
                batch,
                capability_identity,
            )
            cycle_number = len(batch["validation_cycles"]) + 1
            cycle: JsonObject = {
                "cycle": cycle_number,
                "status": "running",
                "repair_summary": (
                    _text(repair_summary, label="repair_summary")
                    if repair_summary is not None
                    else None
                ),
                "repair_categories": list(repair_categories or []),
                "capability_identity": capability_identity,
                "capability_identity_sha256": capability_identity_sha256,
                "attempts": [],
                "reviews": [],
                "failure_reasons": [],
                "started_at": utc_now(),
                "ended_at": None,
            }
            batch["validation_cycles"] = [
                *batch["validation_cycles"],
                cycle,
            ]
            batch["status"] = "single_agent_validation_running"
            batch["failure"] = None
            self._replace(batch_id, batch)

        for attempt_number in range(1, SMOKE_ATTEMPTS_PER_CYCLE + 1):
            try:
                current_identity = self._current_capability_identity(orchestrator)
                current_digest = research_capability_identity_digest(
                    current_identity,
                    repository_root=self.repository_root,
                )
                if current_digest != capability_identity_sha256:
                    raise ResearchWorkflowError(
                        "Research capability identity changed between smokes"
                    )
                self._require_capability_matches_harness(
                    batch,
                    current_identity,
                )
                outcomes = orchestrator.run_specialists_only(
                    campaign_id=batch_id,
                    round_number=cycle_number,
                    evidence_bundle=Path(batch["evidence_path"]),
                    context={
                        "validation_smoke": True,
                        "validation_cycle": cycle_number,
                        "validation_attempt": attempt_number,
                        "corpus_id": batch["corpus_id"],
                        "corpus_digest": batch["corpus_digest"],
                        "baseline_digest": batch["baseline_digest"],
                        "research_execution_identity_sha256": (
                            batch["harness_validation"][
                                "execution_identity_sha256"
                            ]
                        ),
                        "eligible_trajectory_ids": list(
                            batch["readiness"]["execution_ids"]
                        ),
                    },
                    roles=[AgentRole.BEHAVIOR_PATTERN],
                )
            except Exception as error:
                outcomes = (
                    SpecialistRunOutcome(
                        role=AgentRole.BEHAVIOR_PATTERN,
                        run=None,
                        exception=self._exception_record(error),
                    ),
                )
            self._append_smoke_outcome(batch_id, outcomes[0])
        return self.load(batch_id)

    def review_single_agent_attempt(
        self,
        batch_id: str,
        *,
        attempt_id: str,
        reviewer: str,
        checks: Mapping[str, bool],
        failure_categories: list[str] | None = None,
    ) -> JsonObject:
        """Append one explicit four-part human review for a smoke attempt."""

        if set(checks) != set(REVIEW_CHECKS) or not all(
            isinstance(value, bool) for value in checks.values()
        ):
            raise ResearchWorkflowError(
                "Review must explicitly set evidence, protocol, safety, "
                "and hidden_benchmark"
            )
        reviewer_name = _text(reviewer, label="reviewer")
        if failure_categories is not None and (
            not failure_categories
            or len(failure_categories) != len(set(failure_categories))
            or not set(failure_categories).issubset(FAILURE_CATEGORIES)
        ):
            raise ResearchWorkflowError(
                "Review failure categories are invalid"
            )
        if all(checks.values()) and failure_categories:
            raise ResearchWorkflowError(
                "A passed review cannot declare failure categories"
            )
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "single_agent_validation_running":
                raise ResearchWorkflowError("Validation is not awaiting review")
            self._verify_bound_evidence(batch)
            self._verify_validation_benchmark(batch_id, batch)
            cycle = dict(batch["validation_cycles"][-1])
            if cycle["status"] != "awaiting_review":
                raise ResearchWorkflowError("Validation cycle cannot be reviewed")
            attempts = {
                item["attempt_id"]: item for item in cycle["attempts"]
            }
            if attempt_id not in attempts:
                raise ResearchWorkflowError("Review references another attempt")
            if any(
                item["attempt_id"] == attempt_id
                for item in cycle["reviews"]
            ):
                raise ResearchWorkflowError("Attempt review is append-only")
            cycle["reviews"] = [
                *cycle["reviews"],
                {
                    "review_id": new_object_id("smoke-review"),
                    "attempt_id": attempt_id,
                    "reviewer": reviewer_name,
                    "checks": {name: checks[name] for name in REVIEW_CHECKS},
                    "benchmark_sha256": batch["validation_benchmark"][
                        "snapshot_sha256"
                    ],
                    "reviewed_at": utc_now(),
                },
            ]
            if not all(checks.values()):
                categories = list(failure_categories or [])
                if not categories:
                    category_by_check = {
                        "evidence": "evidence_unreachable",
                        "protocol": "protocol_unclear",
                        "safety": "result_gate",
                        "hidden_benchmark": "agent_exploration",
                    }
                    for name in REVIEW_CHECKS:
                        category = category_by_check[name]
                        if not checks[name] and category not in categories:
                            categories.append(category)
                self._fail_cycle(
                    batch,
                    cycle,
                    categories,
                )
            elif len(cycle["reviews"]) == SMOKE_ATTEMPTS_PER_CYCLE:
                cycle["status"] = "passed"
                cycle["ended_at"] = utc_now()
                batch["validation_cycles"][-1] = cycle
                batch["status"] = "single_agent_validated"
                batch["failure"] = None
            else:
                batch["validation_cycles"][-1] = cycle
            return self._replace(batch_id, batch)

    def issue_capability_certification(self, batch_id: str) -> JsonObject:
        """Seal a portable capability certificate from the passed smoke cycle."""

        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "single_agent_validated":
                raise ResearchWorkflowError(
                    "Capability certification requires a passed smoke cycle"
                )
            if batch["capability_certification"] is not None:
                raise ResearchWorkflowError(
                    "Capability certification is already frozen"
                )
            if not batch["validation_cycles"]:
                raise ResearchWorkflowError(
                    "Imported capability cannot be issued as a new certificate"
                )
            cycle = batch["validation_cycles"][-1]
            if cycle["status"] != "passed":
                raise ResearchWorkflowError(
                    "Capability certification requires a passed smoke cycle"
                )
            benchmark = self._verify_validation_benchmark(batch_id, batch)
            self._verify_bound_evidence(batch)
            current_identity = self._current_capability_identity()
            current_digest = research_capability_identity_digest(
                current_identity,
                repository_root=self.repository_root,
            )
            if current_digest != cycle["capability_identity_sha256"]:
                raise ResearchWorkflowError(
                    "Research capability changed after the smoke runs"
                )
            self._require_capability_matches_harness(batch, current_identity)
            reviews = {
                str(item["attempt_id"]): item for item in cycle["reviews"]
            }
            smoke_runs: list[JsonObject] = []
            for attempt in cycle["attempts"]:
                attempt_id = str(attempt["attempt_id"])
                review = reviews.get(attempt_id)
                if review is None:
                    raise ResearchWorkflowError(
                        "Capability smoke is missing its passed review"
                    )
                smoke_runs.append(
                    {
                        "run_id": attempt["agent_run_id"],
                        "session_id": attempt["session_ref"],
                        "result_sha256": attempt["result_ref"]["sha256"],
                        "review": {
                            "review_id": review["review_id"],
                            "status": "passed",
                            "reviewer": review["reviewer"],
                            "checks": dict(review["checks"]),
                            "benchmark_sha256": review["benchmark_sha256"],
                            "reviewed_at": review["reviewed_at"],
                        },
                    }
                )
            try:
                certificate = build_research_capability_certificate(
                    source_batch_id=batch_id,
                    source_corpus_sha256=str(batch["corpus_digest"]),
                    source_baseline_sha256=str(batch["baseline_digest"]),
                    identity=current_identity,
                    hidden_benchmark_sha256=str(
                        batch["validation_benchmark"]["snapshot_sha256"]
                    ),
                    smoke_runs=smoke_runs,
                    issued_at=utc_now(),
                    repository_root=self.repository_root,
                )
            except ResearchCapabilityError as error:
                raise ResearchWorkflowError(str(error)) from error
            if benchmark["benchmark_id"] != batch["validation_benchmark"][
                "benchmark_id"
            ]:
                raise ResearchWorkflowError(
                    "Capability benchmark identity changed during issuance"
                )
            return self._freeze_capability_certificate(
                batch_id,
                batch,
                certificate,
                mode="issued",
            )

    def import_capability_certification(
        self,
        batch_id: str,
        *,
        source_batch_id: str,
    ) -> JsonObject:
        """Import a trusted certificate from another batch in this workflow."""

        if source_batch_id == batch_id:
            raise ResearchWorkflowError(
                "Capability source and target batches must differ"
            )
        with self._lock:
            source = self.load(source_batch_id)
            source_binding = _validate_capability_binding(
                source.get("capability_certification")
            )
            if source_binding is None or source_binding["mode"] != "issued":
                raise ResearchWorkflowError(
                    "Capability source lacks an originally issued certificate"
                )
            certificate = self._verify_capability_certification(
                source_batch_id,
                source,
            )
            if certificate is None:
                raise ResearchWorkflowError(
                    "Capability source certificate is unavailable"
                )
            target = self.load(batch_id)
            if target["status"] != "harness_validated":
                raise ResearchWorkflowError(
                    "Capability import requires a Harness-validated target"
                )
            if target["capability_certification"] is not None:
                raise ResearchWorkflowError(
                    "Target capability certification is already frozen"
                )
            self._verify_bound_evidence(target)
            current_identity = self._current_capability_identity()
            current_digest = research_capability_identity_digest(
                current_identity,
                repository_root=self.repository_root,
            )
            if current_digest != certificate["identity_sha256"]:
                raise ResearchWorkflowError(
                    "Current research capability differs from the certificate"
                )
            self._require_capability_matches_harness(target, current_identity)
            return self._freeze_capability_certificate(
                batch_id,
                target,
                certificate,
                mode="imported",
            )

    def run_specialists(self, batch_id: str) -> JsonObject:
        """Run all four specialists after both prior gates have passed."""

        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "single_agent_validated":
                raise ResearchWorkflowError(
                    "Specialists require validated single-agent research"
                )
            self._require_full_readiness(batch["readiness"])
            self._verify_bound_evidence(batch)
            self._require_current_capability_certificate(batch_id, batch)
        orchestrator = self._orchestrator()
        orchestrator.preflight_specialists()
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "single_agent_validated":
                raise ResearchWorkflowError(
                    "Specialists require validated single-agent research"
                )
            self._require_full_readiness(batch["readiness"])
            self._verify_bound_evidence(batch)
            self._require_current_capability_certificate(batch_id, batch)
            boards = self._boards(batch_id)
            board = boards.create(
                corpus_digest=batch["corpus_digest"],
                baseline_digest=batch["baseline_digest"],
            )
            batch["specialist_board_id"] = board["id"]
            batch["status"] = "specialists_running"
            self._replace(batch_id, batch)

        try:
            outcomes = orchestrator.run_specialists_only(
                campaign_id=batch_id,
                round_number=1,
                evidence_bundle=Path(batch["evidence_path"]),
                context=self._specialist_context(batch),
            )
        except Exception as error:
            outcomes = tuple(
                SpecialistRunOutcome(
                    role=role,
                    run=None,
                    exception=self._exception_record(error),
                )
                for role in ACTIVE_SPECIALIST_ROLES
            )
        board = self._record_specialist_outcomes(batch_id, batch, outcomes)
        return self._finish_specialist_state(batch_id, board)

    def retry_specialist(
        self,
        batch_id: str,
        *,
        role: AgentRole,
    ) -> JsonObject:
        """Retry exactly one failed role without exposing successful reports."""

        if role not in ACTIVE_SPECIALIST_ROLES:
            raise ResearchWorkflowError("Retry role is not an active specialist")
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "specialists_incomplete":
                raise ResearchWorkflowError("Batch has no retryable specialists")
            board = self._load_board(batch_id, batch)
            role_index = ACTIVE_SPECIALIST_ROLES.index(role)
            if board["roles"][role_index]["status"] == "succeeded":
                raise ResearchWorkflowError("Successful role cannot be retried")
            self._verify_bound_evidence(batch)
            self._require_current_capability_certificate(batch_id, batch)
        orchestrator = self._orchestrator()
        orchestrator.preflight_specialists([role])
        with self._lock:
            batch = self.load(batch_id)
            if batch["status"] != "specialists_incomplete":
                raise ResearchWorkflowError("Batch has no retryable specialists")
            board = self._load_board(batch_id, batch)
            role_index = ACTIVE_SPECIALIST_ROLES.index(role)
            if board["roles"][role_index]["status"] == "succeeded":
                raise ResearchWorkflowError("Successful role cannot be retried")
            self._verify_bound_evidence(batch)
            self._require_current_capability_certificate(batch_id, batch)
            batch["status"] = "specialists_running"
            self._replace(batch_id, batch)

        try:
            outcomes = orchestrator.run_specialists_only(
                campaign_id=batch_id,
                round_number=(
                    len(board["roles"][role_index]["attempts"]) + 1
                ),
                evidence_bundle=Path(batch["evidence_path"]),
                context=self._specialist_context(batch),
                roles=[role],
            )
        except Exception as error:
            outcomes = (
                SpecialistRunOutcome(
                    role=role,
                    run=None,
                    exception=self._exception_record(error),
                ),
            )
        board = self._record_specialist_outcomes(batch_id, batch, outcomes)
        return self._finish_specialist_state(batch_id, board)

    def specialist_board(self, batch_id: str) -> JsonObject:
        """Return the current internal board without creating a user report."""

        batch = self.load(batch_id)
        return self._load_board(batch_id, batch)

    def _append_smoke_outcome(
        self,
        batch_id: str,
        outcome: SpecialistRunOutcome,
    ) -> None:
        with self._lock:
            batch = self.load(batch_id)
            cycle = dict(batch["validation_cycles"][-1])
            if cycle["status"] != "running":
                raise ResearchWorkflowError("Smoke cycle is not running")
            attempt = self._smoke_attempt(
                outcome,
                corpus_digest=str(batch["corpus_digest"]),
                baseline_digest=str(batch["baseline_digest"]),
            )
            cycle["attempts"] = [*cycle["attempts"], attempt]
            if len(cycle["attempts"]) > SMOKE_ATTEMPTS_PER_CYCLE:
                raise ResearchWorkflowError("Smoke cycle already has two attempts")
            if len(cycle["attempts"]) == SMOKE_ATTEMPTS_PER_CYCLE:
                reasons = self._smoke_failure_reasons(cycle["attempts"])
                if reasons:
                    self._fail_cycle(batch, cycle, reasons)
                else:
                    cycle["status"] = "awaiting_review"
                    batch["validation_cycles"][-1] = cycle
            else:
                batch["validation_cycles"][-1] = cycle
            self._replace(batch_id, batch)

    def _smoke_attempt(
        self,
        outcome: SpecialistRunOutcome,
        *,
        corpus_digest: str,
        baseline_digest: str,
    ) -> JsonObject:
        if outcome.role is not AgentRole.BEHAVIOR_PATTERN:
            raise ResearchWorkflowError("Smoke attempt used the wrong role")
        run = outcome.run
        status = outcome.status
        error = outcome.exception or (run.error if run is not None else None)
        result_ref: JsonObject | None = None
        session_ref: str | None = None
        agent_run_id: str | None = None
        if run is not None:
            agent_run_id = run.agent_run_id
            try:
                session_ref = self._research_session_id(run.run_directory)
            except ResearchWorkflowError as identity_error:
                if status == "succeeded":
                    status = "invalid_output"
                    error = {
                        "type": "InvalidResearchSessionIdentity",
                        "message": str(identity_error),
                    }
            if status == "succeeded" and run.result is not None:
                try:
                    result_ref = seal_research_result_reference(
                        result_file=run.run_directory / "result.json",
                        run_directory=run.run_directory,
                        result=run.result,
                        role=AgentRole.BEHAVIOR_PATTERN.value,
                        agent_run_id=run.agent_run_id,
                        corpus_digest=corpus_digest,
                        baseline_digest=baseline_digest,
                    )
                except ResearchArtifactError as artifact_error:
                    status = "invalid_output"
                    error = {
                        "type": "InvalidResearchResultArtifact",
                        "message": str(artifact_error),
                    }
            elif status == "succeeded":
                status = "invalid_output"
                error = {
                    "type": "MissingResearchResult",
                    "message": "Succeeded smoke has no validated result",
                }
        return {
            "attempt_id": new_object_id("smoke-attempt"),
            "agent_run_id": agent_run_id,
            "session_ref": session_ref,
            "status": status,
            "result_ref": result_ref,
            "error": dict(error) if isinstance(error, Mapping) else None,
            "recorded_at": utc_now(),
        }

    @staticmethod
    def _research_session_id(run_directory: Path) -> str:
        if run_directory.is_symlink():
            raise ResearchWorkflowError("Research AgentRun directory is unsafe")
        root = run_directory.resolve()
        declared = root / "research/session-identity.json"
        path = declared.resolve(strict=False)
        if (
            not path.is_relative_to(root)
            or declared.parent.is_symlink()
            or declared.is_symlink()
            or not path.is_file()
        ):
            raise ResearchWorkflowError(
                "Research AgentRun has no safe session identity"
            )
        try:
            identity = load_json_object(path)
        except StorageError as error:
            raise ResearchWorkflowError(str(error)) from error
        if set(identity) != {
            "schema",
            "session_id",
            "process_isolated",
            "session_retained",
        }:
            raise ResearchWorkflowError(
                "Research session identity fields differ from schema"
            )
        if identity.get("schema") != "analysis.research_session_identity.v1":
            raise ResearchWorkflowError(
                "Research session identity schema is unsupported"
            )
        if identity.get("process_isolated") is not True:
            raise ResearchWorkflowError(
                "Research Agent did not use a fresh isolated process"
            )
        if identity.get("session_retained") is not False:
            raise ResearchWorkflowError(
                "Research Agent retained a forbidden Pi session"
            )
        return _text(identity.get("session_id"), label="session_id")

    @staticmethod
    def _smoke_failure_reasons(
        attempts: list[JsonObject],
    ) -> list[str]:
        reasons: list[str] = []
        for item in attempts:
            status = item["status"]
            if status == "succeeded":
                continue
            category = (
                "result_gate"
                if status == "invalid_output"
                else "agent_exploration"
            )
            error = item.get("error")
            if isinstance(error, Mapping):
                message = str(error.get("message", "")).casefold()
                if "sample" in message or "insufficient trajectory" in message:
                    category = "sample_insufficient"
                elif "protocol" in message or "prompt" in message:
                    category = "protocol_unclear"
                elif "evidence" in message or "corpus" in message:
                    category = "evidence_unreachable"
            if category not in reasons:
                reasons.append(category)
        run_ids = [item["agent_run_id"] for item in attempts]
        sessions = [item["session_ref"] for item in attempts]
        if None in run_ids or len(set(run_ids)) != len(run_ids):
            if "result_gate" not in reasons:
                reasons.append("result_gate")
        if None in sessions or len(set(sessions)) != len(sessions):
            if "result_gate" not in reasons:
                reasons.append("result_gate")
        return reasons

    @staticmethod
    def _fail_cycle(
        batch: JsonObject,
        cycle: JsonObject,
        reasons: list[str],
    ) -> None:
        cycle["status"] = "failed"
        cycle["failure_reasons"] = list(reasons)
        cycle["ended_at"] = utc_now()
        batch["validation_cycles"][-1] = cycle
        batch["status"] = "failed"
        batch["failure"] = {
            "stage": "single_agent_validation",
            "cycle": cycle["cycle"],
            "reasons": list(reasons),
        }

    @staticmethod
    def _require_validation_cycle_start(
        batch: Mapping[str, Any],
        repair_summary: str | None,
        repair_categories: list[str] | None,
    ) -> None:
        if batch["status"] == "harness_validated":
            if repair_summary is not None or repair_categories:
                raise ResearchWorkflowError(
                    "First validation cycle cannot claim a repair"
                )
            return
        failure = batch.get("failure")
        if (
            batch["status"] == "failed"
            and isinstance(failure, Mapping)
            and failure.get("stage") == "single_agent_validation"
        ):
            _text(repair_summary, label="repair_summary")
            if (
                not isinstance(repair_categories, list)
                or len(repair_categories) != len(set(repair_categories))
                or set(repair_categories) != set(failure.get("reasons", []))
                or not set(repair_categories).issubset(FAILURE_CATEGORIES)
            ):
                raise ResearchWorkflowError(
                    "repair_categories must name every prior failure category"
                )
            return
        raise ResearchWorkflowError("Batch cannot start a validation cycle")

    @staticmethod
    def _require_full_readiness(readiness: Mapping[str, Any]) -> None:
        normalized = _validate_readiness(readiness)
        missing = RESEARCH_OBJECTIVES - set(normalized["objectives"])
        if missing:
            raise ResearchWorkflowError(
                f"Specialist readiness is incomplete: {sorted(missing)}"
            )
        if normalized.get("coverage") is None:
            raise ResearchWorkflowError("Specialist readiness lacks coverage")

    def _record_specialist_outcomes(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
        outcomes: tuple[SpecialistRunOutcome, ...],
    ) -> JsonObject:
        boards = self._boards(batch_id)
        board_id = _text(
            batch.get("specialist_board_id"),
            label="specialist_board_id",
        )
        board: JsonObject | None = None
        for outcome in outcomes:
            result_ref = None
            if outcome.run is not None and outcome.status == "succeeded":
                if outcome.run.result is None:
                    board = boards.append_attempt(
                        board_id,
                        corpus_digest=str(batch["corpus_digest"]),
                        baseline_digest=str(batch["baseline_digest"]),
                        role=outcome.role,
                        status="invalid_output",
                        agent_run_id=outcome.run.agent_run_id,
                        error={
                            "type": "MissingResearchResult",
                            "message": "Specialist returned no validated result",
                        },
                    )
                    continue
                try:
                    result_ref = seal_research_result_reference(
                        result_file=(
                            outcome.run.run_directory / "result.json"
                        ),
                        run_directory=outcome.run.run_directory,
                        result=outcome.run.result,
                        role=outcome.role.value,
                        agent_run_id=outcome.run.agent_run_id,
                        corpus_digest=str(batch["corpus_digest"]),
                        baseline_digest=str(batch["baseline_digest"]),
                    )
                except ResearchArtifactError as artifact_error:
                    board = boards.append_attempt(
                        board_id,
                        corpus_digest=str(batch["corpus_digest"]),
                        baseline_digest=str(batch["baseline_digest"]),
                        role=outcome.role,
                        status="invalid_output",
                        agent_run_id=outcome.run.agent_run_id,
                        error={
                            "type": "InvalidResearchResultArtifact",
                            "message": str(artifact_error),
                        },
                    )
                    continue
            board = boards.record_outcome(
                board_id,
                corpus_digest=str(batch["corpus_digest"]),
                baseline_digest=str(batch["baseline_digest"]),
                outcome=outcome,
                result_ref=result_ref,
            )
        if board is None:
            raise ResearchWorkflowError("No specialist outcomes were recorded")
        return board

    def _finish_specialist_state(
        self,
        batch_id: str,
        board: Mapping[str, Any],
    ) -> JsonObject:
        with self._lock:
            batch = self.load(batch_id)
            batch["status"] = (
                "specialists_completed"
                if board["status"] == "complete"
                else "specialists_incomplete"
            )
            batch["failure"] = None
            return self._replace(batch_id, batch)

    @staticmethod
    def _specialist_context(batch: Mapping[str, Any]) -> JsonObject:
        return {
            "corpus_id": batch["corpus_id"],
            "corpus_digest": batch["corpus_digest"],
            "baseline_digest": batch["baseline_digest"],
            "research_execution_identity_sha256": batch[
                "harness_validation"
            ]["execution_identity_sha256"],
            "eligible_trajectory_ids": list(
                batch["readiness"]["execution_ids"]
            ),
            "instruction": (
                "Inspect the frozen corpus independently; do not read other "
                "specialist results."
            ),
        }

    def _boards(self, batch_id: str) -> SpecialistBoardRepository:
        directory = self.repository.object_directory(batch_id)
        return SpecialistBoardRepository(directory / "specialist-boards")

    def _load_board(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
    ) -> JsonObject:
        board_id = batch.get("specialist_board_id")
        if not isinstance(board_id, str):
            raise ResearchWorkflowError("Batch has no specialist board")
        return self._boards(batch_id).load(board_id)

    @staticmethod
    def _require_benchmark_identity(
        batch: Mapping[str, Any],
        benchmark: Mapping[str, Any],
    ) -> None:
        readiness = _mapping(
            batch.get("readiness"),
            label="batch.readiness",
        )
        for field in ("skill_id", "revision_id", "execution_ids"):
            if benchmark.get(field) != readiness.get(field):
                raise ResearchWorkflowError(
                    f"Validation benchmark {field} differs from the batch"
                )

    def _verify_validation_benchmark(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
    ) -> JsonObject:
        binding = _validate_benchmark_binding(
            batch.get("validation_benchmark")
        )
        if binding is None:
            raise ResearchWorkflowError(
                "Single-Agent validation requires a frozen hidden benchmark"
            )
        batch_root = self.repository.object_directory(batch_id)
        if batch_root.is_symlink() or not batch_root.is_dir():
            raise ResearchWorkflowError("Research batch directory is unsafe")
        root = batch_root.resolve()
        declared = root / str(binding["snapshot_path"])
        snapshot = declared.resolve(strict=False)
        if (
            not snapshot.is_relative_to(root)
            or declared.parent.is_symlink()
            or declared.is_symlink()
            or not snapshot.is_file()
        ):
            raise ResearchWorkflowError(
                "Validation benchmark snapshot is missing or unsafe"
            )
        if _file_sha256(snapshot) != binding["snapshot_sha256"]:
            raise ResearchWorkflowError(
                "Validation benchmark snapshot digest changed"
            )
        try:
            benchmark = validate_validation_benchmark(
                load_json_object(snapshot)
            )
        except StorageError as error:
            raise ResearchWorkflowError(str(error)) from error
        if benchmark["benchmark_id"] != binding["benchmark_id"]:
            raise ResearchWorkflowError(
                "Validation benchmark identity differs from its binding"
            )
        self._require_benchmark_identity(batch, benchmark)
        return benchmark

    def _verify_harness_acceptance(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
    ) -> None:
        binding = _validate_harness_binding(batch.get("harness_validation"))
        if binding is None:
            return
        batch_root = self.repository.object_directory(batch_id)
        if batch_root.is_symlink() or not batch_root.is_dir():
            raise ResearchWorkflowError("Research batch directory is unsafe")
        root = batch_root.resolve()
        declared = root / str(binding["snapshot_path"])
        snapshot = declared.resolve(strict=False)
        if (
            not snapshot.is_relative_to(root)
            or declared.parent.is_symlink()
            or declared.is_symlink()
            or not snapshot.is_file()
        ):
            raise ResearchWorkflowError(
                "Harness acceptance snapshot is missing or unsafe"
            )
        try:
            verification = verify_harness_acceptance_report(
                snapshot,
                expected_file_sha256=str(binding["file_sha256"]),
                trusted_output_root=batch_root,
                corpus_directory=str(batch["evidence_path"]),
                expected_corpus_digest=str(batch["corpus_digest"]),
                expected_baseline_digest=str(batch["baseline_digest"]),
                expected_image_id=(
                    str(binding["image_id"])
                    if binding["image_id"] is not None
                    else None
                ),
                require_passed=binding["status"] == "passed",
            )
        except (HarnessAcceptanceError, ResearchCorpusError) as error:
            raise ResearchWorkflowError(str(error)) from error
        report = verification.report
        observed_checks = {
            str(item["name"]): item["status"] == "passed"
            for item in report["checks"]
        }
        if report["content_sha256"] != binding["content_sha256"]:
            raise ResearchWorkflowError(
                "Harness acceptance content identity changed"
            )
        if report["validator_version"] != binding["validator_version"]:
            raise ResearchWorkflowError("Harness validator identity changed")
        if (
            report["execution_identity_sha256"]
            != binding["execution_identity_sha256"]
        ):
            raise ResearchWorkflowError(
                "Harness execution identity binding changed"
            )
        if report["status"] != binding["status"]:
            raise ResearchWorkflowError("Harness acceptance status changed")
        if observed_checks != binding["checks"]:
            raise ResearchWorkflowError("Harness acceptance checks changed")

    def _freeze_capability_certificate(
        self,
        batch_id: str,
        batch: JsonObject,
        certificate: Mapping[str, Any],
        *,
        mode: str,
    ) -> JsonObject:
        try:
            normalized = validate_research_capability_certificate(
                certificate,
                repository_root=self.repository_root,
            )
            certificate_digest = research_capability_certificate_digest(
                normalized,
                repository_root=self.repository_root,
            )
        except ResearchCapabilityError as error:
            raise ResearchWorkflowError(str(error)) from error
        if normalized["schema"] != RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA:
            raise ResearchWorkflowError(
                "Unsupported capability certificate schema"
            )
        batch_root = self.repository.object_directory(batch_id)
        if batch_root.is_symlink() or not batch_root.is_dir():
            raise ResearchWorkflowError("Research batch directory is unsafe")
        destination, destination_sha256 = _write_new_capability_snapshot(
            batch_root,
            normalized,
        )
        batch["capability_certification"] = {
            "schema": CAPABILITY_CERTIFICATE_BINDING_SCHEMA,
            "mode": mode,
            "snapshot_path": CAPABILITY_CERTIFICATE_SNAPSHOT,
            "file_sha256": destination_sha256,
            "certificate_sha256": certificate_digest,
            "identity_sha256": normalized["identity_sha256"],
            "source_batch_id": normalized["source"]["batch_id"],
            "bound_at": utc_now(),
        }
        batch["status"] = "single_agent_validated"
        batch["failure"] = None
        return self._replace(batch_id, batch)

    def _verify_capability_certification(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
    ) -> JsonObject | None:
        binding = _validate_capability_binding(
            batch.get("capability_certification")
        )
        if binding is None:
            return None
        batch_root = self.repository.object_directory(batch_id)
        if batch_root.is_symlink() or not batch_root.is_dir():
            raise ResearchWorkflowError("Research batch directory is unsafe")
        root = batch_root.resolve()
        declared = root / str(binding["snapshot_path"])
        snapshot = declared.resolve(strict=False)
        if (
            not snapshot.is_relative_to(root)
            or declared.parent.is_symlink()
            or declared.is_symlink()
            or not snapshot.is_file()
        ):
            raise ResearchWorkflowError(
                "Capability certificate snapshot is missing or unsafe"
            )
        if _file_sha256(snapshot) != binding["file_sha256"]:
            raise ResearchWorkflowError(
                "Capability certificate file digest changed"
            )
        try:
            decoded = load_json_object(snapshot)
            certificate = validate_research_capability_certificate(
                decoded,
                repository_root=self.repository_root,
            )
            certificate_digest = research_capability_certificate_digest(
                certificate,
                repository_root=self.repository_root,
            )
        except (ResearchCapabilityError, StorageError) as error:
            raise ResearchWorkflowError(str(error)) from error
        if certificate_digest != binding["certificate_sha256"]:
            raise ResearchWorkflowError(
                "Capability certificate content digest changed"
            )
        if certificate["identity_sha256"] != binding["identity_sha256"]:
            raise ResearchWorkflowError(
                "Capability certificate identity binding changed"
            )
        if certificate["source"]["batch_id"] != binding["source_batch_id"]:
            raise ResearchWorkflowError(
                "Capability certificate source identity changed"
            )
        if binding["mode"] == "issued":
            if certificate["source"] != {
                "batch_id": batch_id,
                "corpus_sha256": batch["corpus_digest"],
                "baseline_sha256": batch["baseline_digest"],
            }:
                raise ResearchWorkflowError(
                    "Issued capability certificate source changed"
                )
        return certificate

    def _require_current_capability_certificate(
        self,
        batch_id: str,
        batch: Mapping[str, Any],
    ) -> JsonObject:
        certificate = self._verify_capability_certification(batch_id, batch)
        if certificate is None:
            raise ResearchWorkflowError(
                "Specialists require a capability certificate"
            )
        current_identity = self._current_capability_identity()
        current_digest = research_capability_identity_digest(
            current_identity,
            repository_root=self.repository_root,
        )
        if current_digest != certificate["identity_sha256"]:
            raise ResearchWorkflowError(
                "Current research capability differs from its certificate"
            )
        self._require_capability_matches_harness(batch, current_identity)
        return certificate

    def _require_capability_matches_harness(
        self,
        batch: Mapping[str, Any],
        capability_identity: Mapping[str, Any],
    ) -> None:
        binding = _validate_harness_binding(batch.get("harness_validation"))
        if binding is None or binding["status"] != "passed":
            raise ResearchWorkflowError(
                "Research capability requires a passed Harness acceptance"
            )
        try:
            current_execution_sha256 = (
                research_capability_execution_identity_digest(
                    capability_identity,
                    repository_root=self.repository_root,
                )
            )
        except ResearchCapabilityError as error:
            raise ResearchWorkflowError(str(error)) from error
        if (
            current_execution_sha256
            != binding["execution_identity_sha256"]
        ):
            raise ResearchWorkflowError(
                "Current research execution identity differs from the "
                "batch's passed Harness"
            )

    @staticmethod
    def _verify_bound_evidence(batch: Mapping[str, Any]) -> None:
        try:
            verified = verify_research_corpus(
                str(batch["evidence_path"]),
                expected_content_sha256=str(batch["corpus_digest"]),
                expected_baseline_sha256=str(batch["baseline_digest"]),
            )
        except ResearchCorpusError as error:
            raise ResearchWorkflowError(str(error)) from error
        readiness = _mapping(
            batch.get("readiness"),
            label="batch.readiness",
        )
        if verified.manifest.get("corpus_id") != batch.get("corpus_id"):
            raise ResearchWorkflowError("Bound corpus identity changed")
        for field in (
            "skill_id",
            "revision_id",
            "objectives",
            "execution_ids",
        ):
            if verified.manifest.get(field) != readiness.get(field):
                raise ResearchWorkflowError(
                    f"Bound corpus {field} differs from readiness"
                )

    @staticmethod
    def _verify_smoke_result_references(batch: Mapping[str, Any]) -> None:
        for cycle in batch.get("validation_cycles", []):
            if not isinstance(cycle, Mapping):
                continue
            for attempt in cycle.get("attempts", []):
                if (
                    not isinstance(attempt, Mapping)
                    or attempt.get("status") != "succeeded"
                ):
                    continue
                try:
                    verify_research_result_reference(
                        attempt["result_ref"],
                        expected_role=AgentRole.BEHAVIOR_PATTERN.value,
                        expected_agent_run_id=str(attempt["agent_run_id"]),
                        expected_corpus_digest=str(batch["corpus_digest"]),
                        expected_baseline_digest=str(batch["baseline_digest"]),
                    )
                except (KeyError, ResearchArtifactError) as artifact_error:
                    raise ResearchWorkflowError(
                        str(artifact_error)
                    ) from artifact_error

    @staticmethod
    def _exception_record(error: Exception) -> JsonObject:
        return {
            "type": type(error).__name__,
            "message": str(error),
        }

    def _orchestrator(self) -> MultiPiOrchestrator:
        if self.orchestrator is None:
            raise ResearchWorkflowError("This workflow has no Agent runtime")
        return self.orchestrator

    def _current_capability_identity(
        self,
        orchestrator: MultiPiOrchestrator | None = None,
    ) -> JsonObject:
        active = orchestrator or self._orchestrator()
        capability = getattr(active, "research_capability_identity", None)
        if not callable(capability):
            raise ResearchWorkflowError(
                "Agent runtime cannot attest its research capability identity"
            )
        try:
            identity = capability()
            return validate_research_capability_identity(
                identity,
                repository_root=self.repository_root,
            )
        except (
            AgentOrchestrationError,
            ResearchCapabilityError,
            RuntimeError,
            ValueError,
        ) as error:
            raise ResearchWorkflowError(str(error)) from error

    def _replace(self, batch_id: str, batch: Mapping[str, Any]) -> JsonObject:
        validated = validate_research_batch(batch)
        self.repository.replace(batch_id, validated)
        return self.load(batch_id)
