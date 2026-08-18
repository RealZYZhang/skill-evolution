"""Strict contracts for evidence-backed multi-Trajectory specialist research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from skill_evolution.evidence import EvidenceError, EvidenceRef
from skill_evolution.storage import JsonObject


RESEARCH_RESULT_SCHEMA = "analysis.multi_trajectory_research.v1"
RESEARCH_SPECIALIST_ROLES = {
    "behavior_pattern_analyst",
    "conditions_coverage_analyst",
    "outcome_consistency_analyst",
    "resource_efficiency_analyst",
}
RESEARCH_PATTERN_TYPES = {
    "recurring_problem",
    "recovery_success",
    "implicit_behavior",
    "condition_association",
    "coverage_gap",
    "insufficient_condition_evidence",
    "inconsistency",
    "consistent_behavior",
    "resource_inefficiency",
    "efficient_pattern",
}
REPEATED_PATTERN_TYPES = {
    "recurring_problem",
    "recovery_success",
    "implicit_behavior",
}
_ROLE_PATTERN_TYPES = {
    "behavior_pattern_analyst": REPEATED_PATTERN_TYPES,
    "conditions_coverage_analyst": {
        "condition_association",
        "coverage_gap",
        "insufficient_condition_evidence",
    },
    "outcome_consistency_analyst": {
        "inconsistency",
        "consistent_behavior",
    },
    "resource_efficiency_analyst": {
        "resource_inefficiency",
        "efficient_pattern",
    },
}
_EVIDENCE_FIELDS = {
    "schema",
    "campaign_id",
    "run_id",
    "seq",
    "report_path",
    "json_pointer",
    "artifact_path",
    "line",
    "selector",
}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResearchResultError(ValueError):
    """Raised when a specialist submission is incomplete or ungrounded."""


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise ResearchResultError(
            f"{label} fields do not match the schema; "
            f"missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise ResearchResultError(f"{label}.{field} must be non-empty text")
    return item.strip()


def _digest(value: Mapping[str, Any], field: str, *, label: str) -> str:
    """Require an immutable lowercase SHA-256 identity."""

    item = _text(value, field, label=label)
    if not _SHA256_PATTERN.fullmatch(item):
        raise ResearchResultError(
            f"{label}.{field} must be a lowercase SHA-256 digest"
        )
    return item


def _string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    label: str,
    allow_empty: bool,
) -> list[str]:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ResearchResultError(f"{label}.{field} must be a string list")
    normalized = [item.strip() for item in raw]
    if not normalized and not allow_empty:
        raise ResearchResultError(f"{label}.{field} must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ResearchResultError(f"{label}.{field} must be unique")
    return normalized


def _evidence_list(
    raw: Any,
    *,
    label: str,
    allow_empty: bool,
) -> list[JsonObject]:
    if not isinstance(raw, list):
        raise ResearchResultError(f"{label} must be a list")
    if not raw and not allow_empty:
        raise ResearchResultError(f"{label} must not be empty")
    normalized: list[JsonObject] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ResearchResultError(f"{label}[{index}] must be an object")
        unexpected = set(item) - _EVIDENCE_FIELDS
        if unexpected:
            raise ResearchResultError(
                f"{label}[{index}] has unexpected fields: {sorted(unexpected)}"
            )
        try:
            normalized.append(EvidenceRef.from_dict(item).to_dict())
        except EvidenceError as error:
            raise ResearchResultError(f"{label}[{index}]: {error}") from error
    return normalized


def validate_research_result(
    value: Mapping[str, Any],
    *,
    expected_role: str | None = None,
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
    allowed_trajectory_ids: Sequence[str] | None = None,
    known_derivation_ids: Sequence[str] = (),
) -> JsonObject:
    """Validate one role result and all cross-Trajectory set relationships."""

    _exact_fields(
        value,
        {
            "schema",
            "role",
            "corpus_digest",
            "baseline_digest",
            "research_scope",
            "findings",
            "limitations",
        },
        label="research result",
    )
    if value.get("schema") != RESEARCH_RESULT_SCHEMA:
        raise ResearchResultError("Unsupported research result schema")
    role = _text(value, "role", label="research result")
    if role not in RESEARCH_SPECIALIST_ROLES:
        raise ResearchResultError(f"Unsupported research role: {role}")
    if expected_role is not None and role != expected_role:
        raise ResearchResultError("Research result role differs from its AgentSpec")
    corpus_digest = _digest(
        value, "corpus_digest", label="research result"
    )
    baseline_digest = _digest(
        value, "baseline_digest", label="research result"
    )
    if (
        expected_corpus_digest is not None
        and corpus_digest != expected_corpus_digest
    ):
        raise ResearchResultError("Research result uses a different corpus")
    if (
        expected_baseline_digest is not None
        and baseline_digest != expected_baseline_digest
    ):
        raise ResearchResultError("Research result uses a different baseline")

    raw_scope = value.get("research_scope")
    if not isinstance(raw_scope, Mapping):
        raise ResearchResultError("research_scope must be an object")
    _exact_fields(
        raw_scope,
        {
            "eligible_trajectory_ids",
            "reviewed_trajectory_ids",
            "counterexample_search",
        },
        label="research_scope",
    )
    eligible = _string_list(
        raw_scope,
        "eligible_trajectory_ids",
        label="research_scope",
        allow_empty=False,
    )
    reviewed = _string_list(
        raw_scope,
        "reviewed_trajectory_ids",
        label="research_scope",
        allow_empty=False,
    )
    if set(reviewed) != set(eligible):
        raise ResearchResultError(
            "research_scope.reviewed_trajectory_ids must cover every eligible Trajectory"
        )
    if allowed_trajectory_ids is not None and set(eligible) != set(
        allowed_trajectory_ids
    ):
        raise ResearchResultError(
            "Research scope must use the complete eligible Trajectory denominator"
        )
    scope = {
        "eligible_trajectory_ids": eligible,
        "reviewed_trajectory_ids": reviewed,
        "counterexample_search": _text(
            raw_scope,
            "counterexample_search",
            label="research_scope",
        ),
    }

    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list):
        raise ResearchResultError("findings must be a list")
    finding_ids: set[str] = set()
    normalized_findings: list[JsonObject] = []
    known_derivations = set(known_derivation_ids)
    for index, raw_finding in enumerate(raw_findings):
        label = f"findings[{index}]"
        if not isinstance(raw_finding, Mapping):
            raise ResearchResultError(f"{label} must be an object")
        _exact_fields(
            raw_finding,
            {
                "id",
                "subject",
                "pattern_type",
                "claim",
                "eligible_trajectory_ids",
                "observed_trajectory_ids",
                "checked_absent_trajectory_ids",
                "logical_phase",
                "shared_purpose",
                "observable_effect",
                "confidence",
                "evidence",
                "counterevidence",
                "derivation_ids",
                "limitations",
            },
            label=label,
        )
        finding_id = _text(raw_finding, "id", label=label)
        if finding_id in finding_ids:
            raise ResearchResultError(f"Duplicate finding id: {finding_id}")
        finding_ids.add(finding_id)
        pattern_type = _text(raw_finding, "pattern_type", label=label)
        if pattern_type not in RESEARCH_PATTERN_TYPES:
            raise ResearchResultError(
                f"{label}.pattern_type is unsupported: {pattern_type}"
            )
        if pattern_type not in _ROLE_PATTERN_TYPES[role]:
            raise ResearchResultError(
                f"{pattern_type} is outside the {role} responsibility"
            )
        finding_eligible = _string_list(
            raw_finding,
            "eligible_trajectory_ids",
            label=label,
            allow_empty=False,
        )
        observed = _string_list(
            raw_finding,
            "observed_trajectory_ids",
            label=label,
            allow_empty=pattern_type
            in {"coverage_gap", "insufficient_condition_evidence"},
        )
        checked_absent = _string_list(
            raw_finding,
            "checked_absent_trajectory_ids",
            label=label,
            allow_empty=True,
        )
        eligible_set = set(finding_eligible)
        observed_set = set(observed)
        absent_set = set(checked_absent)
        if not eligible_set.issubset(set(eligible)):
            raise ResearchResultError(
                f"{label}.eligible_trajectory_ids exceed the declared research scope"
            )
        if role == "behavior_pattern_analyst" and eligible_set != set(eligible):
            raise ResearchResultError(
                f"{label} behavior finding must use the complete research "
                "denominator"
            )
        if observed_set & absent_set:
            raise ResearchResultError(
                f"{label} cannot mark one Trajectory observed and absent"
            )
        if observed_set | absent_set != eligible_set:
            raise ResearchResultError(
                f"{label} must classify every eligible Trajectory as observed "
                "or checked absent"
            )
        if pattern_type in REPEATED_PATTERN_TYPES and len(observed) < 2:
            raise ResearchResultError(
                f"{label} repeated pattern requires two observed Trajectories"
            )
        if pattern_type in {"inconsistency", "consistent_behavior"} and len(
            observed
        ) < 2:
            raise ResearchResultError(
                f"{label} consistency finding requires two observed Trajectories"
            )
        confidence = raw_finding.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ResearchResultError(f"{label}.confidence must be within 0..1")
        evidence = _evidence_list(
            raw_finding.get("evidence"),
            label=f"{label}.evidence",
            allow_empty=False,
        )
        counterevidence = _evidence_list(
            raw_finding.get("counterevidence"),
            label=f"{label}.counterevidence",
            allow_empty=True,
        )
        trajectory_evidence = {
            str(item["run_id"])
            for item in evidence
            if isinstance(item.get("run_id"), str)
            and isinstance(item.get("seq"), int)
        }
        missing_trajectory_evidence = observed_set - trajectory_evidence
        if missing_trajectory_evidence:
            raise ResearchResultError(
                f"{label} lacks original Trajectory evidence for "
                f"{sorted(missing_trajectory_evidence)}"
            )
        evidence_trajectory_ids = trajectory_evidence | {
            str(item["run_id"])
            for item in counterevidence
            if isinstance(item.get("run_id"), str)
        }
        if not evidence_trajectory_ids.issubset(set(eligible)):
            raise ResearchResultError(
                f"{label} cites a Trajectory outside the research scope"
            )
        derivation_ids = _string_list(
            raw_finding,
            "derivation_ids",
            label=label,
            allow_empty=True,
        )
        unknown_derivations = set(derivation_ids) - known_derivations
        if unknown_derivations:
            raise ResearchResultError(
                f"{label} cites unknown derivations: {sorted(unknown_derivations)}"
            )
        normalized_findings.append(
            {
                "id": finding_id,
                "subject": _text(raw_finding, "subject", label=label),
                "pattern_type": pattern_type,
                "claim": _text(raw_finding, "claim", label=label),
                "eligible_trajectory_ids": finding_eligible,
                "observed_trajectory_ids": observed,
                "checked_absent_trajectory_ids": checked_absent,
                "logical_phase": _text(
                    raw_finding, "logical_phase", label=label
                ),
                "shared_purpose": _text(
                    raw_finding, "shared_purpose", label=label
                ),
                "observable_effect": _text(
                    raw_finding, "observable_effect", label=label
                ),
                "confidence": float(confidence),
                "evidence": evidence,
                "counterevidence": counterevidence,
                "derivation_ids": derivation_ids,
                "limitations": _string_list(
                    raw_finding,
                    "limitations",
                    label=label,
                    allow_empty=True,
                ),
            }
        )

    limitations = _string_list(
        value,
        "limitations",
        label="research result",
        allow_empty=bool(normalized_findings),
    )
    return {
        "schema": RESEARCH_RESULT_SCHEMA,
        "role": role,
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
        "research_scope": scope,
        "findings": normalized_findings,
        "limitations": limitations,
    }


def validate_research_result_evidence(
    result: Mapping[str, Any],
    *,
    bundle_root: str | Path,
) -> None:
    """Resolve every source EvidenceRef against one frozen research corpus."""

    for finding in result.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        for field in ("evidence", "counterevidence"):
            for raw_reference in finding.get(field, []):
                if not isinstance(raw_reference, Mapping):
                    raise ResearchResultError(
                        f"{field} entries must be evidence objects"
                    )
                try:
                    EvidenceRef.from_dict(raw_reference).validate(bundle_root)
                except EvidenceError as error:
                    raise ResearchResultError(str(error)) from error
ERROR_IDENTIFICATION_SCHEMA = "analysis.error_identification.v1"
ERROR_REPORT_SCHEMA = "analysis.error_report.v1"
ERROR_DIMENSIONS = frozenset(
    {"behavior", "conditions", "consistency", "resource"}
)


def _allowed_fields(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str],
    *,
    label: str,
) -> None:
    """Require every required field and forbid any field outside the union."""

    actual = set(value)
    unexpected = actual - required - optional
    missing = required - actual
    if unexpected or missing:
        raise ResearchResultError(
            f"{label} fields do not match the schema; "
            f"missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )


def _validate_scope(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_trajectory_ids: Sequence[str] | None,
) -> tuple[list[str], JsonObject]:
    """Validate and normalize the shared scope object for error analyses."""

    raw_scope = value.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise ResearchResultError(f"{label}.scope must be an object")
    _exact_fields(
        raw_scope,
        {
            "eligible_trajectory_ids",
            "reviewed_trajectory_ids",
            "counterexample_search",
        },
        label=f"{label}.scope",
    )
    eligible = _string_list(
        raw_scope,
        "eligible_trajectory_ids",
        label=f"{label}.scope",
        allow_empty=False,
    )
    reviewed = _string_list(
        raw_scope,
        "reviewed_trajectory_ids",
        label=f"{label}.scope",
        allow_empty=False,
    )
    if set(reviewed) != set(eligible):
        raise ResearchResultError(
            f"{label}.scope.reviewed_trajectory_ids must cover every "
            "eligible Trajectory"
        )
    if allowed_trajectory_ids is not None and set(eligible) != set(
        allowed_trajectory_ids
    ):
        raise ResearchResultError(
            f"{label}.scope must use the complete eligible Trajectory "
            "denominator"
        )
    scope = {
        "eligible_trajectory_ids": eligible,
        "reviewed_trajectory_ids": reviewed,
        "counterexample_search": _text(
            raw_scope,
            "counterexample_search",
            label=f"{label}.scope",
        ),
    }
    return eligible, scope


def _validate_denominator(
    *,
    observed: list[str],
    checked_absent: list[str],
    eligible: Sequence[str],
    label: str,
) -> None:
    """Require a complete, non-overlapping observed/checked-absent split."""

    observed_set = set(observed)
    absent_set = set(checked_absent)
    if observed_set & absent_set:
        raise ResearchResultError(
            f"{label} cannot mark one Trajectory observed and absent"
        )
    if observed_set | absent_set != set(eligible):
        raise ResearchResultError(
            f"{label} must classify every eligible Trajectory as observed "
            "or checked absent"
        )


def validate_error_identification(
    value: Mapping[str, Any],
    *,
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
    allowed_trajectory_ids: Sequence[str] | None = None,
) -> JsonObject:
    """Validate one error-identification list and its set relationships."""

    _exact_fields(
        value,
        {
            "schema",
            "role",
            "corpus_digest",
            "baseline_digest",
            "scope",
            "errors",
            "limitations",
        },
        label="error identification",
    )
    if value.get("schema") != ERROR_IDENTIFICATION_SCHEMA:
        raise ResearchResultError(
            "Unsupported error identification schema"
        )
    role = _text(value, "role", label="error identification")
    if role != "error_identifier":
        raise ResearchResultError(
            f"Unsupported error identification role: {role}"
        )
    corpus_digest = _digest(
        value, "corpus_digest", label="error identification"
    )
    baseline_digest = _digest(
        value, "baseline_digest", label="error identification"
    )
    if (
        expected_corpus_digest is not None
        and corpus_digest != expected_corpus_digest
    ):
        raise ResearchResultError(
            "Error identification uses a different corpus"
        )
    if (
        expected_baseline_digest is not None
        and baseline_digest != expected_baseline_digest
    ):
        raise ResearchResultError(
            "Error identification uses a different baseline"
        )
    eligible, scope = _validate_scope(
        value,
        label="error identification",
        allowed_trajectory_ids=allowed_trajectory_ids,
    )

    raw_errors = value.get("errors")
    if not isinstance(raw_errors, list):
        raise ResearchResultError("errors must be a list")
    error_ids: set[str] = set()
    normalized_errors: list[JsonObject] = []
    for index, raw_error in enumerate(raw_errors):
        label = f"errors[{index}]"
        if not isinstance(raw_error, Mapping):
            raise ResearchResultError(f"{label} must be an object")
        _allowed_fields(
            raw_error,
            required={
                "error_id",
                "title",
                "summary",
                "anchor_evidence",
                "observed_trajectory_ids",
                "checked_absent_trajectory_ids",
            },
            optional={"suggested_dimensions", "notes"},
            label=label,
        )
        error_id = _text(raw_error, "error_id", label=label)
        if error_id in error_ids:
            raise ResearchResultError(f"Duplicate error id: {error_id}")
        error_ids.add(error_id)
        title = _text(raw_error, "title", label=label)
        summary = _text(raw_error, "summary", label=label)
        anchor_evidence = _evidence_list(
            raw_error.get("anchor_evidence"),
            label=f"{label}.anchor_evidence",
            allow_empty=False,
        )
        observed = _string_list(
            raw_error,
            "observed_trajectory_ids",
            label=label,
            allow_empty=False,
        )
        checked_absent = _string_list(
            raw_error,
            "checked_absent_trajectory_ids",
            label=label,
            allow_empty=True,
        )
        _validate_denominator(
            observed=observed,
            checked_absent=checked_absent,
            eligible=eligible,
            label=label,
        )
        suggested_dimensions: list[str] | None = None
        if "suggested_dimensions" in raw_error:
            suggested_dimensions = _string_list(
                raw_error,
                "suggested_dimensions",
                label=label,
                allow_empty=False,
            )
            unknown = set(suggested_dimensions) - ERROR_DIMENSIONS
            if unknown:
                raise ResearchResultError(
                    f"{label}.suggested_dimensions has unknown dimensions: "
                    f"{sorted(unknown)}"
                )
        notes: str | None = None
        if "notes" in raw_error:
            notes = _text(raw_error, "notes", label=label)
        normalized_errors.append(
            {
                "error_id": error_id,
                "title": title,
                "summary": summary,
                "anchor_evidence": anchor_evidence,
                "observed_trajectory_ids": observed,
                "checked_absent_trajectory_ids": checked_absent,
                "suggested_dimensions": (
                    list(suggested_dimensions)
                    if suggested_dimensions is not None
                    else None
                ),
                "notes": notes,
            }
        )

    limitations = _string_list(
        value,
        "limitations",
        label="error identification",
        allow_empty=bool(normalized_errors),
    )
    return {
        "schema": ERROR_IDENTIFICATION_SCHEMA,
        "role": role,
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
        "scope": scope,
        "errors": normalized_errors,
        "limitations": limitations,
    }


def validate_error_report(
    value: Mapping[str, Any],
    *,
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
    allowed_trajectory_ids: Sequence[str] | None = None,
    known_derivation_ids: Sequence[str] = (),
) -> JsonObject:
    """Validate one single-error report and every dimension relationship."""

    _exact_fields(
        value,
        {
            "schema",
            "error_id",
            "role",
            "corpus_digest",
            "baseline_digest",
            "scope",
            "dimensions",
            "limitations",
        },
        label="error report",
    )
    if value.get("schema") != ERROR_REPORT_SCHEMA:
        raise ResearchResultError("Unsupported error report schema")
    role = _text(value, "role", label="error report")
    if role != "error_analyst":
        raise ResearchResultError(f"Unsupported error report role: {role}")
    error_id = _text(value, "error_id", label="error report")
    corpus_digest = _digest(value, "corpus_digest", label="error report")
    baseline_digest = _digest(
        value, "baseline_digest", label="error report"
    )
    if (
        expected_corpus_digest is not None
        and corpus_digest != expected_corpus_digest
    ):
        raise ResearchResultError("Error report uses a different corpus")
    if (
        expected_baseline_digest is not None
        and baseline_digest != expected_baseline_digest
    ):
        raise ResearchResultError("Error report uses a different baseline")
    eligible, scope = _validate_scope(
        value,
        label="error report",
        allowed_trajectory_ids=allowed_trajectory_ids,
    )

    raw_dimensions = value.get("dimensions")
    if not isinstance(raw_dimensions, list):
        raise ResearchResultError("dimensions must be a list")
    dimension_names: set[str] = set()
    normalized_dimensions: list[JsonObject] = []
    known_derivations = set(known_derivation_ids)
    warnings: list[str] = []
    for index, raw_dimension in enumerate(raw_dimensions):
        label = f"dimensions[{index}]"
        if not isinstance(raw_dimension, Mapping):
            raise ResearchResultError(f"{label} must be an object")
        _exact_fields(
            raw_dimension,
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
        dimension = _text(raw_dimension, "dimension", label=label)
        if dimension not in ERROR_DIMENSIONS:
            raise ResearchResultError(
                f"{label}.dimension is unsupported: {dimension}"
            )
        if dimension in dimension_names:
            raise ResearchResultError(f"Duplicate dimension: {dimension}")
        dimension_names.add(dimension)
        claim = _text(raw_dimension, "claim", label=label)
        observed = _string_list(
            raw_dimension,
            "observed_trajectory_ids",
            label=label,
            allow_empty=True,
        )
        checked_absent = _string_list(
            raw_dimension,
            "checked_absent_trajectory_ids",
            label=label,
            allow_empty=True,
        )
        _validate_denominator(
            observed=observed,
            checked_absent=checked_absent,
            eligible=eligible,
            label=label,
        )
        confidence = raw_dimension.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ResearchResultError(
                f"{label}.confidence must be within 0..1"
            )
        evidence = _evidence_list(
            raw_dimension.get("evidence"),
            label=f"{label}.evidence",
            allow_empty=False,
        )
        counterevidence = _evidence_list(
            raw_dimension.get("counterevidence"),
            label=f"{label}.counterevidence",
            allow_empty=True,
        )
        observed_set = set(observed)
        trajectory_evidence = {
            str(item["run_id"])
            for item in evidence
            if isinstance(item.get("run_id"), str)
            and isinstance(item.get("seq"), int)
        }
        missing_trajectory_evidence = observed_set - trajectory_evidence
        if missing_trajectory_evidence:
            warnings.append(
                f"{label} lacks original Trajectory evidence for "
                f"{sorted(missing_trajectory_evidence)}"
            )
        evidence_trajectory_ids = trajectory_evidence | {
            str(item["run_id"])
            for item in counterevidence
            if isinstance(item.get("run_id"), str)
        }
        if not evidence_trajectory_ids.issubset(set(eligible)):
            raise ResearchResultError(
                f"{label} cites a Trajectory outside the research scope"
            )
        derivation_ids = _string_list(
            raw_dimension,
            "derivation_ids",
            label=label,
            allow_empty=True,
        )
        unknown_derivations = set(derivation_ids) - known_derivations
        if unknown_derivations:
            raise ResearchResultError(
                f"{label} cites unknown derivations: "
                f"{sorted(unknown_derivations)}"
            )
        normalized_dimensions.append(
            {
                "dimension": dimension,
                "claim": claim,
                "observed_trajectory_ids": observed,
                "checked_absent_trajectory_ids": checked_absent,
                "evidence": evidence,
                "counterevidence": counterevidence,
                "confidence": float(confidence),
                "derivation_ids": derivation_ids,
                "limitations": _string_list(
                    raw_dimension,
                    "limitations",
                    label=label,
                    allow_empty=True,
                ),
            }
        )

    limitations = _string_list(
        value,
        "limitations",
        label="error report",
        allow_empty=bool(normalized_dimensions),
    )
    return {
        "schema": ERROR_REPORT_SCHEMA,
        "role": role,
        "error_id": error_id,
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
        "scope": scope,
        "dimensions": normalized_dimensions,
        "limitations": limitations,
        "validation_warnings": warnings,
    }


def _validate_evidence_references(
    raw_references: Any,
    *,
    label: str,
    bundle_root: str | Path,
) -> None:
    """Resolve one evidence list against one frozen research corpus."""

    if not isinstance(raw_references, list):
        raise ResearchResultError(f"{label} must be a list")
    for raw_reference in raw_references:
        if not isinstance(raw_reference, Mapping):
            raise ResearchResultError(
                f"{label} entries must be evidence objects"
            )
        try:
            EvidenceRef.from_dict(raw_reference).validate(bundle_root)
        except EvidenceError as error:
            raise ResearchResultError(str(error)) from error


def validate_error_identification_evidence(
    result: Mapping[str, Any],
    *,
    bundle_root: str | Path,
) -> None:
    """Resolve every anchor evidence reference against the research corpus."""

    errors = result.get("errors", [])
    for index, error in enumerate(errors):
        if not isinstance(error, Mapping):
            continue
        _validate_evidence_references(
            error.get("anchor_evidence", []),
            label=f"errors[{index}].anchor_evidence",
            bundle_root=bundle_root,
        )


def validate_error_report_evidence(
    result: Mapping[str, Any],
    *,
    bundle_root: str | Path,
) -> None:
    """Resolve every dimension evidence reference against the research corpus."""

    dimensions = result.get("dimensions", [])
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, Mapping):
            continue
        for field in ("evidence", "counterevidence"):
            _validate_evidence_references(
                dimension.get(field, []),
                label=f"dimensions[{index}].{field}",
                bundle_root=bundle_root,
            )

