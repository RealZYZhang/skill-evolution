"""Strict contracts for semantic error analysis of one trajectory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skill_evolution.evidence import EvidenceRef
from skill_evolution.storage import JsonObject


TRAJECTORY_ERROR_REPORT_SCHEMA = "analysis.trajectory_error_report.v1"
TRAJECTORY_ERROR_ANALYST_ROLE = "trajectory_error_analyst"

TRAJECTORY_ASSESSMENTS = {
    "no_observed_error",
    "errors_recovered",
    "terminal_failure",
    "incomplete_or_indeterminate",
    "invalid_or_inconsistent",
    "insufficient_evidence",
}
INCIDENT_DISPOSITIONS = {
    "terminal",
    "recovered",
    "expected_control_flow",
    "latent",
    "capture_integrity",
}
CAUSAL_ROLES = {
    "root_cause",
    "contributing_cause",
    "symptom",
    "unrelated",
    "unknown",
}
ATTRIBUTION_TARGETS = {
    "skill",
    "task_or_input",
    "runtime_or_environment",
    "tool_or_dependency",
    "model_or_provider",
    "framework_or_capture",
    "harness",
    "unknown",
}
SKILL_FIX_APPLICABILITY = {"yes", "no", "uncertain"}
INTEGRITY_STATUSES = {"valid", "invalid", "incomplete"}

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


class TrajectoryAnalysisContractError(ValueError):
    """Raised when a semantic single-trajectory report violates its contract."""


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    raise TrajectoryAnalysisContractError(
        f"{label} fields do not match the schema; "
        f"missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )


def _text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise TrajectoryAnalysisContractError(
            f"{label}.{field} must be a non-empty string"
        )
    return item.strip()


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
        raise TrajectoryAnalysisContractError(
            f"{label}.{field} must be null or a non-empty string"
        )
    return item.strip()


def _enum(
    value: Mapping[str, Any],
    field: str,
    allowed: set[str],
    *,
    label: str,
) -> str:
    item = value.get(field)
    if item not in allowed:
        raise TrajectoryAnalysisContractError(
            f"{label}.{field} must be one of {sorted(allowed)}"
        )
    return str(item)


def _string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    label: str,
    unique: bool = False,
) -> list[str]:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise TrajectoryAnalysisContractError(
            f"{label}.{field} must be a list of non-empty strings"
        )
    items = [item.strip() for item in raw]
    if unique and len(items) != len(set(items)):
        raise TrajectoryAnalysisContractError(
            f"{label}.{field} must not contain duplicates"
        )
    return items


def _evidence_list(
    raw: Any,
    *,
    label: str,
    allow_empty: bool = False,
) -> list[JsonObject]:
    if not isinstance(raw, list):
        raise TrajectoryAnalysisContractError(f"{label} must be a list")
    if not raw and not allow_empty:
        raise TrajectoryAnalysisContractError(
            f"{label} must cite at least one evidence reference"
        )
    references: list[JsonObject] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise TrajectoryAnalysisContractError(
                f"{label}[{index}] must be an object"
            )
        unexpected = set(item) - _EVIDENCE_FIELDS
        if unexpected:
            raise TrajectoryAnalysisContractError(
                f"{label}[{index}] has unexpected fields: "
                f"{sorted(unexpected)}"
            )
        try:
            reference = EvidenceRef.from_dict(item)
        except ValueError as error:
            raise TrajectoryAnalysisContractError(
                f"{label}[{index}] is invalid: {error}"
            ) from error
        references.append(reference.to_dict())
    return references


def _context_value(
    context: Mapping[str, Any],
    field: str,
) -> str:
    item = context.get(field)
    if not isinstance(item, str) or not item:
        raise TrajectoryAnalysisContractError(
            f"Trajectory analysis context requires {field}"
        )
    return item


def _context_signal_ids(context: Mapping[str, Any]) -> list[str]:
    raw = context.get("precheck_signal_ids")
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise TrajectoryAnalysisContractError(
            "Trajectory analysis context requires precheck_signal_ids"
        )
    if len(raw) != len(set(raw)):
        raise TrajectoryAnalysisContractError(
            "Trajectory analysis context signal ids must be unique"
        )
    return list(raw)


def _validate_incident(
    value: Mapping[str, Any],
    *,
    index: int,
    known_signals: set[str],
    interpreted_signals: set[str],
) -> JsonObject:
    label = f"incidents[{index}]"
    _exact_fields(
        value,
        {
            "id",
            "source_signal_ids",
            "disposition",
            "causal_role",
            "attributed_to",
            "phase",
            "claim",
            "confidence",
            "evidence",
            "counterevidence",
        },
        label=label,
    )
    incident_id = _text(value, "id", label=label)
    source_signal_ids = _string_list(
        value,
        "source_signal_ids",
        label=label,
        unique=True,
    )
    unknown = set(source_signal_ids) - known_signals
    if unknown:
        raise TrajectoryAnalysisContractError(
            f"{label}.source_signal_ids are not in the precheck: "
            f"{sorted(unknown)}"
        )
    unmarked = set(source_signal_ids) - interpreted_signals
    if unmarked:
        raise TrajectoryAnalysisContractError(
            f"{label}.source_signal_ids are not marked interpreted: "
            f"{sorted(unmarked)}"
        )
    disposition = _enum(
        value,
        "disposition",
        INCIDENT_DISPOSITIONS,
        label=label,
    )
    if not source_signal_ids and disposition != "capture_integrity":
        raise TrajectoryAnalysisContractError(
            f"{label} requires source_signal_ids"
        )
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise TrajectoryAnalysisContractError(
            f"{label}.confidence must be between 0 and 1"
        )
    return {
        "id": incident_id,
        "source_signal_ids": source_signal_ids,
        "disposition": disposition,
        "causal_role": _enum(
            value,
            "causal_role",
            CAUSAL_ROLES,
            label=label,
        ),
        "attributed_to": _enum(
            value,
            "attributed_to",
            ATTRIBUTION_TARGETS,
            label=label,
        ),
        "phase": _optional_text(value, "phase", label=label),
        "claim": _text(value, "claim", label=label),
        "confidence": float(confidence),
        "evidence": _evidence_list(
            value.get("evidence"),
            label=f"{label}.evidence",
        ),
        "counterevidence": _evidence_list(
            value.get("counterevidence"),
            label=f"{label}.counterevidence",
            allow_empty=True,
        ),
    }


def _validate_causal_chain(
    raw: Any,
    *,
    incident_ids: set[str],
) -> list[JsonObject]:
    if not isinstance(raw, list):
        raise TrajectoryAnalysisContractError("causal_chain must be a list")
    chains: list[JsonObject] = []
    for index, item in enumerate(raw):
        label = f"causal_chain[{index}]"
        if not isinstance(item, Mapping):
            raise TrajectoryAnalysisContractError(f"{label} must be an object")
        _exact_fields(
            item,
            {
                "from_incident_id",
                "to_incident_id",
                "relationship",
                "evidence",
            },
            label=label,
        )
        source = _text(item, "from_incident_id", label=label)
        target = _text(item, "to_incident_id", label=label)
        if source not in incident_ids or target not in incident_ids:
            raise TrajectoryAnalysisContractError(
                f"{label} must reference existing incidents"
            )
        if source == target:
            raise TrajectoryAnalysisContractError(
                f"{label} cannot relate an incident to itself"
            )
        chains.append(
            {
                "from_incident_id": source,
                "to_incident_id": target,
                "relationship": _text(
                    item,
                    "relationship",
                    label=label,
                ),
                "evidence": _evidence_list(
                    item.get("evidence"),
                    label=f"{label}.evidence",
                ),
            }
        )
    return chains


def validate_trajectory_error_report(
    value: Mapping[str, Any],
    context: Mapping[str, Any],
) -> JsonObject:
    """Validate one semantic report against its frozen precheck context."""

    _exact_fields(
        value,
        {
            "schema",
            "role",
            "run_id",
            "precheck",
            "trajectory_assessment",
            "primary_incident_id",
            "summary",
            "summary_evidence",
            "incidents",
            "causal_chain",
            "skill_fix_applicability",
            "repair_target",
            "additional_evidence_needed",
            "limitations",
        },
        label="TrajectoryErrorReport",
    )
    if value.get("schema") != TRAJECTORY_ERROR_REPORT_SCHEMA:
        raise TrajectoryAnalysisContractError(
            "Unsupported TrajectoryErrorReport schema"
        )
    if value.get("role") != TRAJECTORY_ERROR_ANALYST_ROLE:
        raise TrajectoryAnalysisContractError(
            "TrajectoryErrorReport role must be trajectory_error_analyst"
        )

    expected_run_id = _context_value(context, "run_id")
    if value.get("run_id") != expected_run_id:
        raise TrajectoryAnalysisContractError(
            "TrajectoryErrorReport run_id does not match the context"
        )
    expected_report_path = _context_value(
        context,
        "trajectory_precheck_path",
    )
    expected_deterministic = _context_value(
        context,
        "precheck_deterministic_status",
    )
    expected_integrity = _context_value(
        context,
        "precheck_integrity_status",
    )
    if expected_integrity not in INTEGRITY_STATUSES:
        raise TrajectoryAnalysisContractError(
            "Context has an unsupported precheck integrity status"
        )
    known_signal_ids = _context_signal_ids(context)
    known_signals = set(known_signal_ids)

    raw_precheck = value.get("precheck")
    if not isinstance(raw_precheck, Mapping):
        raise TrajectoryAnalysisContractError("precheck must be an object")
    _exact_fields(
        raw_precheck,
        {
            "report_path",
            "deterministic_status",
            "integrity_status",
            "interpreted_signal_ids",
            "uninterpreted_signal_ids",
        },
        label="precheck",
    )
    if raw_precheck.get("report_path") != expected_report_path:
        raise TrajectoryAnalysisContractError(
            "precheck.report_path does not match the frozen context"
        )
    if raw_precheck.get("deterministic_status") != expected_deterministic:
        raise TrajectoryAnalysisContractError(
            "precheck.deterministic_status was not propagated exactly"
        )
    if raw_precheck.get("integrity_status") != expected_integrity:
        raise TrajectoryAnalysisContractError(
            "precheck.integrity_status was not propagated exactly"
        )
    interpreted = _string_list(
        raw_precheck,
        "interpreted_signal_ids",
        label="precheck",
        unique=True,
    )
    uninterpreted = _string_list(
        raw_precheck,
        "uninterpreted_signal_ids",
        label="precheck",
        unique=True,
    )
    overlap = set(interpreted) & set(uninterpreted)
    if overlap:
        raise TrajectoryAnalysisContractError(
            f"Signal ids cannot be both interpreted and uninterpreted: "
            f"{sorted(overlap)}"
        )
    if set(interpreted) | set(uninterpreted) != known_signals:
        raise TrajectoryAnalysisContractError(
            "The report must partition every frozen precheck signal id"
        )

    raw_incidents = value.get("incidents")
    if not isinstance(raw_incidents, list):
        raise TrajectoryAnalysisContractError("incidents must be a list")
    incidents: list[JsonObject] = []
    incident_ids: set[str] = set()
    covered_signals: set[str] = set()
    for index, raw_incident in enumerate(raw_incidents):
        if not isinstance(raw_incident, Mapping):
            raise TrajectoryAnalysisContractError(
                f"incidents[{index}] must be an object"
            )
        incident = _validate_incident(
            raw_incident,
            index=index,
            known_signals=known_signals,
            interpreted_signals=set(interpreted),
        )
        incident_id = str(incident["id"])
        if incident_id in incident_ids:
            raise TrajectoryAnalysisContractError(
                "Incident ids must be unique within the report"
            )
        incident_ids.add(incident_id)
        covered_signals.update(incident["source_signal_ids"])
        incidents.append(incident)
    if covered_signals != set(interpreted):
        raise TrajectoryAnalysisContractError(
            "Every interpreted signal must be covered by an incident"
        )

    assessment = _enum(
        value,
        "trajectory_assessment",
        TRAJECTORY_ASSESSMENTS,
        label="TrajectoryErrorReport",
    )
    if expected_integrity == "invalid" and assessment != "invalid_or_inconsistent":
        raise TrajectoryAnalysisContractError(
            "An invalid precheck requires invalid_or_inconsistent"
        )
    if (
        expected_integrity == "incomplete"
        or expected_deterministic in {"incomplete", "indeterminate"}
    ) and assessment != "incomplete_or_indeterminate":
        raise TrajectoryAnalysisContractError(
            "An incomplete precheck requires incomplete_or_indeterminate"
        )
    dispositions = {str(item["disposition"]) for item in incidents}
    if assessment == "errors_recovered" and "recovered" not in dispositions:
        raise TrajectoryAnalysisContractError(
            "errors_recovered requires a recovered incident"
        )
    if assessment == "terminal_failure" and "terminal" not in dispositions:
        raise TrajectoryAnalysisContractError(
            "terminal_failure requires a terminal incident"
        )
    if assessment == "no_observed_error" and dispositions - {
        "expected_control_flow"
    }:
        raise TrajectoryAnalysisContractError(
            "no_observed_error can only contain expected-control incidents"
        )

    primary_incident_id = _optional_text(
        value,
        "primary_incident_id",
        label="TrajectoryErrorReport",
    )
    if primary_incident_id is not None and primary_incident_id not in incident_ids:
        raise TrajectoryAnalysisContractError(
            "primary_incident_id must reference an incident"
        )
    if assessment in {"errors_recovered", "terminal_failure"} and (
        primary_incident_id is None
    ):
        raise TrajectoryAnalysisContractError(
            f"{assessment} requires primary_incident_id"
        )
    if assessment == "no_observed_error" and primary_incident_id is not None:
        raise TrajectoryAnalysisContractError(
            "no_observed_error requires a null primary_incident_id"
        )

    applicability = _enum(
        value,
        "skill_fix_applicability",
        SKILL_FIX_APPLICABILITY,
        label="TrajectoryErrorReport",
    )
    repair_target = _optional_text(
        value,
        "repair_target",
        label="TrajectoryErrorReport",
    )
    if applicability == "yes" and repair_target is None:
        raise TrajectoryAnalysisContractError(
            "A supported Skill fix requires repair_target"
        )
    if applicability == "no" and repair_target is not None:
        raise TrajectoryAnalysisContractError(
            "A non-applicable Skill fix requires a null repair_target"
        )
    additional_evidence = _string_list(
        value,
        "additional_evidence_needed",
        label="TrajectoryErrorReport",
    )
    limitations = _string_list(
        value,
        "limitations",
        label="TrajectoryErrorReport",
    )
    if uninterpreted and not (additional_evidence or limitations):
        raise TrajectoryAnalysisContractError(
            "Uninterpreted signals require an evidence need or limitation"
        )

    return {
        "schema": TRAJECTORY_ERROR_REPORT_SCHEMA,
        "role": TRAJECTORY_ERROR_ANALYST_ROLE,
        "run_id": expected_run_id,
        "precheck": {
            "report_path": expected_report_path,
            "deterministic_status": expected_deterministic,
            "integrity_status": expected_integrity,
            "interpreted_signal_ids": interpreted,
            "uninterpreted_signal_ids": uninterpreted,
        },
        "trajectory_assessment": assessment,
        "primary_incident_id": primary_incident_id,
        "summary": _text(value, "summary", label="TrajectoryErrorReport"),
        "summary_evidence": _evidence_list(
            value.get("summary_evidence"),
            label="summary_evidence",
        ),
        "incidents": incidents,
        "causal_chain": _validate_causal_chain(
            value.get("causal_chain"),
            incident_ids=incident_ids,
        ),
        "skill_fix_applicability": applicability,
        "repair_target": repair_target,
        "additional_evidence_needed": additional_evidence,
        "limitations": limitations,
    }
