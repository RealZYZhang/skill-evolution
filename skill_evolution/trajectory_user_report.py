"""Build and validate the five-layer user report for one trajectory analysis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from skill_evolution.evidence import EvidenceRef
from skill_evolution.storage import (
    JsonObject,
    StorageError,
    atomic_write_json,
    load_json_object,
    utc_now,
)
from skill_evolution.trajectory_analysis import validate_trajectory_error_report


TRAJECTORY_USER_REPORT_SCHEMA = "analysis.single_trajectory_view.v1"
TRAJECTORY_USER_REPORT_FILENAME = "user-report.json"

ANALYSIS_STATUSES = {"accepted", "unavailable"}
TRAJECTORY_DATA_STATUSES = {"complete", "incomplete", "invalid"}
TASK_RESULT_STATUSES = {"completed", "failed", "uncertain"}
ERROR_STATUSES = {"none", "recovered", "unresolved", "uncertain"}
SKILL_RECOMMENDATION_STATUSES = {"change", "no_change", "uncertain"}
TIMELINE_TONES = {"neutral", "positive", "warning", "danger"}
EVIDENCE_KINDS = {"trajectory", "precheck", "artifact", "validator"}
EVIDENCE_STRENGTHS = {"high", "medium", "low", "insufficient"}
SKILL_CHANGE_VALUES = {"yes", "no", "uncertain"}
RECOMMENDATION_DECISIONS = {
    "no_change",
    "evaluate_skill_change",
    "collect_evidence",
    "rerun_analysis",
}
SEMANTIC_REPORT_STATUSES = {
    "accepted",
    "invalid_output",
    "failed",
    "timed_out",
    "indeterminate",
}


class TrajectoryUserReportError(ValueError):
    """Raised when a user-facing single-trajectory report is invalid."""


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    raise TrajectoryUserReportError(
        f"{label} fields do not match; "
        f"missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )


def _text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise TrajectoryUserReportError(
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
        raise TrajectoryUserReportError(
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
        raise TrajectoryUserReportError(
            f"{label}.{field} must be one of {sorted(allowed)}"
        )
    return str(item)


def _string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    label: str,
) -> list[str]:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise TrajectoryUserReportError(
            f"{label}.{field} must be a list of non-empty strings"
        )
    items = [item.strip() for item in raw]
    if len(items) != len(set(items)):
        raise TrajectoryUserReportError(
            f"{label}.{field} must not contain duplicates"
        )
    return items


def _status_card(
    value: Any,
    *,
    label: str,
    allowed: set[str],
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TrajectoryUserReportError(f"{label} must be an object")
    _exact_fields(value, {"status", "label", "detail"}, label=label)
    return {
        "status": _enum(value, "status", allowed, label=label),
        "label": _text(value, "label", label=label),
        "detail": _text(value, "detail", label=label),
    }


def validate_trajectory_user_report(value: Mapping[str, Any]) -> JsonObject:
    """Validate and normalize one complete five-layer user report."""

    _exact_fields(
        value,
        {
            "schema",
            "run_id",
            "generated_at",
            "analysis",
            "overview",
            "narrative",
            "incidents",
            "evidence",
            "recommendation",
            "provenance",
        },
        label="TrajectoryUserReport",
    )
    if value.get("schema") != TRAJECTORY_USER_REPORT_SCHEMA:
        raise TrajectoryUserReportError("Unsupported trajectory user report schema")
    run_id = _text(value, "run_id", label="TrajectoryUserReport")
    generated_at = _text(value, "generated_at", label="TrajectoryUserReport")

    raw_analysis = value.get("analysis")
    if not isinstance(raw_analysis, Mapping):
        raise TrajectoryUserReportError("analysis must be an object")
    _exact_fields(
        raw_analysis,
        {"status", "title", "message"},
        label="analysis",
    )
    analysis = {
        "status": _enum(
            raw_analysis,
            "status",
            ANALYSIS_STATUSES,
            label="analysis",
        ),
        "title": _text(raw_analysis, "title", label="analysis"),
        "message": _text(raw_analysis, "message", label="analysis"),
    }

    raw_overview = value.get("overview")
    if not isinstance(raw_overview, Mapping):
        raise TrajectoryUserReportError("overview must be an object")
    _exact_fields(
        raw_overview,
        {
            "trajectory_data",
            "task_result",
            "error_assessment",
            "skill_recommendation",
        },
        label="overview",
    )
    overview = {
        "trajectory_data": _status_card(
            raw_overview.get("trajectory_data"),
            label="overview.trajectory_data",
            allowed=TRAJECTORY_DATA_STATUSES,
        ),
        "task_result": _status_card(
            raw_overview.get("task_result"),
            label="overview.task_result",
            allowed=TASK_RESULT_STATUSES,
        ),
        "error_assessment": _status_card(
            raw_overview.get("error_assessment"),
            label="overview.error_assessment",
            allowed=ERROR_STATUSES,
        ),
        "skill_recommendation": _status_card(
            raw_overview.get("skill_recommendation"),
            label="overview.skill_recommendation",
            allowed=SKILL_RECOMMENDATION_STATUSES,
        ),
    }

    evidence, evidence_ids = _validate_evidence(value.get("evidence"))
    narrative = _validate_narrative(
        value.get("narrative"),
        evidence_ids=evidence_ids,
    )
    incidents = _validate_incidents(
        value.get("incidents"),
        evidence_ids=evidence_ids,
    )
    if analysis["status"] == "unavailable" and incidents:
        raise TrajectoryUserReportError(
            "An unavailable analysis cannot expose semantic incidents"
        )
    recommendation = _validate_recommendation(
        value.get("recommendation")
    )
    provenance = _validate_provenance(value.get("provenance"))
    expected_analysis_status = (
        "accepted"
        if provenance["semantic_report_status"] == "accepted"
        else "unavailable"
    )
    if analysis["status"] != expected_analysis_status:
        raise TrajectoryUserReportError(
            "analysis.status conflicts with semantic_report_status"
        )

    return {
        "schema": TRAJECTORY_USER_REPORT_SCHEMA,
        "run_id": run_id,
        "generated_at": generated_at,
        "analysis": analysis,
        "overview": overview,
        "narrative": narrative,
        "incidents": incidents,
        "evidence": evidence,
        "recommendation": recommendation,
        "provenance": provenance,
    }


def _validate_evidence(raw: Any) -> tuple[list[JsonObject], set[str]]:
    if not isinstance(raw, list):
        raise TrajectoryUserReportError("evidence must be a list")
    evidence: list[JsonObject] = []
    identifiers: set[str] = set()
    for index, item in enumerate(raw):
        label = f"evidence[{index}]"
        if not isinstance(item, Mapping):
            raise TrajectoryUserReportError(f"{label} must be an object")
        _exact_fields(
            item,
            {"id", "kind", "title", "summary", "locator"},
            label=label,
        )
        identifier = _text(item, "id", label=label)
        if identifier in identifiers:
            raise TrajectoryUserReportError("Evidence ids must be unique")
        identifiers.add(identifier)
        raw_locator = item.get("locator")
        if not isinstance(raw_locator, Mapping):
            raise TrajectoryUserReportError(f"{label}.locator must be an object")
        try:
            locator = EvidenceRef.from_dict(raw_locator).to_dict()
        except ValueError as error:
            raise TrajectoryUserReportError(
                f"{label}.locator is invalid: {error}"
            ) from error
        evidence.append(
            {
                "id": identifier,
                "kind": _enum(
                    item,
                    "kind",
                    EVIDENCE_KINDS,
                    label=label,
                ),
                "title": _text(item, "title", label=label),
                "summary": _text(item, "summary", label=label),
                "locator": locator,
            }
        )
    return evidence, identifiers


def _validate_narrative(
    raw: Any,
    *,
    evidence_ids: set[str],
) -> JsonObject:
    if not isinstance(raw, Mapping):
        raise TrajectoryUserReportError("narrative must be an object")
    _exact_fields(raw, {"summary", "timeline"}, label="narrative")
    timeline_raw = raw.get("timeline")
    if not isinstance(timeline_raw, list) or not timeline_raw:
        raise TrajectoryUserReportError(
            "narrative.timeline must contain at least one item"
        )
    timeline: list[JsonObject] = []
    identifiers: set[str] = set()
    for index, item in enumerate(timeline_raw):
        label = f"narrative.timeline[{index}]"
        if not isinstance(item, Mapping):
            raise TrajectoryUserReportError(f"{label} must be an object")
        _exact_fields(
            item,
            {"id", "label", "detail", "tone", "evidence_ids"},
            label=label,
        )
        identifier = _text(item, "id", label=label)
        if identifier in identifiers:
            raise TrajectoryUserReportError("Timeline ids must be unique")
        identifiers.add(identifier)
        references = _string_list(item, "evidence_ids", label=label)
        _require_known_evidence(references, evidence_ids, label=label)
        timeline.append(
            {
                "id": identifier,
                "label": _text(item, "label", label=label),
                "detail": _text(item, "detail", label=label),
                "tone": _enum(
                    item,
                    "tone",
                    TIMELINE_TONES,
                    label=label,
                ),
                "evidence_ids": references,
            }
        )
    return {
        "summary": _text(raw, "summary", label="narrative"),
        "timeline": timeline,
    }


def _validate_incidents(
    raw: Any,
    *,
    evidence_ids: set[str],
) -> list[JsonObject]:
    if not isinstance(raw, list):
        raise TrajectoryUserReportError("incidents must be a list")
    incidents: list[JsonObject] = []
    identifiers: set[str] = set()
    for index, item in enumerate(raw):
        label = f"incidents[{index}]"
        if not isinstance(item, Mapping):
            raise TrajectoryUserReportError(f"{label} must be an object")
        _exact_fields(
            item,
            {
                "id",
                "title",
                "impact",
                "recovery",
                "attribution",
                "evidence_strength",
                "skill_change",
                "evidence_ids",
                "counterevidence_ids",
            },
            label=label,
        )
        identifier = _text(item, "id", label=label)
        if identifier in identifiers:
            raise TrajectoryUserReportError("Incident ids must be unique")
        identifiers.add(identifier)
        supporting = _string_list(item, "evidence_ids", label=label)
        counter = _string_list(
            item,
            "counterevidence_ids",
            label=label,
        )
        _require_known_evidence(supporting, evidence_ids, label=label)
        _require_known_evidence(counter, evidence_ids, label=label)
        incidents.append(
            {
                "id": identifier,
                "title": _text(item, "title", label=label),
                "impact": _text(item, "impact", label=label),
                "recovery": _text(item, "recovery", label=label),
                "attribution": _text(item, "attribution", label=label),
                "evidence_strength": _enum(
                    item,
                    "evidence_strength",
                    EVIDENCE_STRENGTHS,
                    label=label,
                ),
                "skill_change": _enum(
                    item,
                    "skill_change",
                    SKILL_CHANGE_VALUES,
                    label=label,
                ),
                "evidence_ids": supporting,
                "counterevidence_ids": counter,
            }
        )
    return incidents


def _require_known_evidence(
    references: Sequence[str],
    evidence_ids: set[str],
    *,
    label: str,
) -> None:
    unknown = set(references) - evidence_ids
    if unknown:
        raise TrajectoryUserReportError(
            f"{label} references unknown evidence: {sorted(unknown)}"
        )


def _validate_recommendation(raw: Any) -> JsonObject:
    if not isinstance(raw, Mapping):
        raise TrajectoryUserReportError("recommendation must be an object")
    _exact_fields(
        raw,
        {"decision", "summary", "next_steps"},
        label="recommendation",
    )
    next_steps = _string_list(
        raw,
        "next_steps",
        label="recommendation",
    )
    if not next_steps:
        raise TrajectoryUserReportError(
            "recommendation.next_steps must not be empty"
        )
    return {
        "decision": _enum(
            raw,
            "decision",
            RECOMMENDATION_DECISIONS,
            label="recommendation",
        ),
        "summary": _text(raw, "summary", label="recommendation"),
        "next_steps": next_steps,
    }


def _validate_provenance(raw: Any) -> JsonObject:
    if not isinstance(raw, Mapping):
        raise TrajectoryUserReportError("provenance must be an object")
    _exact_fields(
        raw,
        {
            "analysis_id",
            "agent_run_id",
            "semantic_report_status",
            "semantic_report_path",
            "precheck_path",
        },
        label="provenance",
    )
    return {
        "analysis_id": _text(raw, "analysis_id", label="provenance"),
        "agent_run_id": _text(raw, "agent_run_id", label="provenance"),
        "semantic_report_status": _enum(
            raw,
            "semantic_report_status",
            SEMANTIC_REPORT_STATUSES,
            label="provenance",
        ),
        "semantic_report_path": _optional_text(
            raw,
            "semantic_report_path",
            label="provenance",
        ),
        "precheck_path": _text(
            raw,
            "precheck_path",
            label="provenance",
        ),
    }


class _EvidenceRegistry:
    """Deduplicate EvidenceRefs while adding presentation text."""

    def __init__(self) -> None:
        self.items: list[JsonObject] = []
        self._ids_by_reference: dict[str, str] = {}

    def add(
        self,
        locator: Mapping[str, Any],
        *,
        kind: str,
        title: str,
        summary: str,
    ) -> str:
        normalized = EvidenceRef.from_dict(locator).to_dict()
        key = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = self._ids_by_reference.get(key)
        if existing is not None:
            return existing
        identifier = f"evidence-{len(self.items) + 1}"
        self._ids_by_reference[key] = identifier
        self.items.append(
            {
                "id": identifier,
                "kind": kind,
                "title": title,
                "summary": summary,
                "locator": normalized,
            }
        )
        return identifier


def build_trajectory_user_report(
    *,
    precheck: Mapping[str, Any],
    semantic_report: Mapping[str, Any] | None,
    semantic_status: str,
    analysis_id: str,
    agent_run_id: str,
    context: Mapping[str, Any],
    generated_at: str | None = None,
) -> JsonObject:
    """Build one five-layer view without exposing unvalidated semantics."""

    if precheck.get("schema") != "trajectory.precheck.v1":
        raise TrajectoryUserReportError("Unsupported trajectory precheck schema")
    run_id = precheck.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise TrajectoryUserReportError("Precheck requires run_id")
    if context.get("run_id") != run_id:
        raise TrajectoryUserReportError("Precheck and context run_id conflict")
    if semantic_status not in {
        "succeeded",
        "invalid_output",
        "failed",
        "timed_out",
        "indeterminate",
    }:
        raise TrajectoryUserReportError("Unsupported semantic analysis status")

    validated_semantic: JsonObject | None = None
    if semantic_status == "succeeded":
        if semantic_report is None:
            raise TrajectoryUserReportError(
                "A succeeded semantic run requires a validated report"
            )
        validated_semantic = validate_trajectory_error_report(
            semantic_report,
            context,
        )
    elif semantic_report is not None:
        raise TrajectoryUserReportError(
            "Unaccepted semantic output must not enter the user report"
        )

    registry = _EvidenceRegistry()
    report_path = context.get("trajectory_precheck_path")
    if not isinstance(report_path, str) or not report_path:
        raise TrajectoryUserReportError("Context requires trajectory_precheck_path")
    integrity_id = registry.add(
        _report_reference(report_path, "/integrity/status"),
        kind="precheck",
        title="Trajectory 完整性检查",
        summary="基础检查确认了 trajectory 数据是否完整且可用于分析。",
    )
    outcome_id = registry.add(
        _report_reference(report_path, "/outcome/status"),
        kind="precheck",
        title="执行结束状态",
        summary="基础检查记录了执行流程是否正常结束。",
    )
    signal_ids = _register_precheck_signals(precheck, registry)
    recovery_ids = _register_recovery_candidates(
        precheck,
        registry,
        report_path=report_path,
    )

    if validated_semantic is None:
        report = _build_unavailable_report(
            precheck=precheck,
            semantic_status=semantic_status,
            analysis_id=analysis_id,
            agent_run_id=agent_run_id,
            generated_at=generated_at or utc_now(),
            report_path=report_path,
            integrity_id=integrity_id,
            outcome_id=outcome_id,
            signal_ids=signal_ids,
            recovery_ids=recovery_ids,
            evidence=registry.items,
        )
    else:
        report = _build_accepted_report(
            precheck=precheck,
            semantic_report=validated_semantic,
            analysis_id=analysis_id,
            agent_run_id=agent_run_id,
            generated_at=generated_at or utc_now(),
            report_path=report_path,
            integrity_id=integrity_id,
            outcome_id=outcome_id,
            registry=registry,
        )
    return validate_trajectory_user_report(report)


def _report_reference(report_path: str, pointer: str) -> JsonObject:
    return {
        "schema": "evidence.ref.v1",
        "report_path": report_path,
        "json_pointer": pointer,
    }


def _register_precheck_signals(
    precheck: Mapping[str, Any],
    registry: _EvidenceRegistry,
) -> list[str]:
    raw_signals = precheck.get("signals")
    if not isinstance(raw_signals, list):
        raise TrajectoryUserReportError("Precheck signals must be a list")
    identifiers: list[str] = []
    for index, signal in enumerate(raw_signals):
        if not isinstance(signal, Mapping):
            raise TrajectoryUserReportError("Precheck signals must be objects")
        locator = signal.get("evidence")
        if not isinstance(locator, Mapping):
            raise TrajectoryUserReportError(
                f"Precheck signal {index} requires evidence"
            )
        facts = signal.get("facts")
        facts = facts if isinstance(facts, Mapping) else {}
        tool_name = facts.get("tool_name")
        target = facts.get("target_path")
        detail = "基础检查发现一次未成功的执行信号。"
        if isinstance(tool_name, str) and tool_name:
            detail = f"基础检查发现 {tool_name} 操作没有成功。"
        if isinstance(target, str) and target:
            detail += f"涉及目标：{target}。"
        identifiers.append(
            registry.add(
                locator,
                kind="trajectory",
                title=f"异常迹象 {index + 1}",
                summary=detail,
            )
        )
    return identifiers


def _register_recovery_candidates(
    precheck: Mapping[str, Any],
    registry: _EvidenceRegistry,
    *,
    report_path: str,
) -> list[str]:
    raw_candidates = precheck.get("candidate_recoveries")
    if not isinstance(raw_candidates, list):
        raw_candidates = []
    identifiers: list[str] = []
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, Mapping):
            raise TrajectoryUserReportError(
                "Precheck recovery candidates must be objects"
            )
        sequence = candidate.get("later_succeeded_seq")
        summary = (
            "后续出现了可能相关的成功动作，"
            "但它本身不能证明问题已恢复。"
        )
        if isinstance(sequence, int):
            summary = (
                f"后续第 {sequence} 步成功，"
                "但仍需判断它是否修复了同一影响。"
            )
        identifiers.append(
            registry.add(
                _report_reference(
                    report_path,
                    f"/candidate_recoveries/{index}",
                ),
                kind="precheck",
                title=f"可能的恢复行为 {index + 1}",
                summary=summary,
            )
        )
    return identifiers


def _trajectory_data_card(precheck: Mapping[str, Any]) -> JsonObject:
    integrity = precheck.get("integrity")
    integrity = integrity if isinstance(integrity, Mapping) else {}
    status = integrity.get("status")
    if status == "valid":
        return {
            "status": "complete",
            "label": "Trajectory 数据完整",
            "detail": "基础检查没有发现会阻止分析的数据缺口。",
        }
    if status == "incomplete":
        return {
            "status": "incomplete",
            "label": "Trajectory 数据不完整",
            "detail": "部分执行记录缺失，后续判断会受到限制。",
        }
    return {
        "status": "invalid",
        "label": "Trajectory 数据无效",
        "detail": "基础检查发现记录矛盾，不能据此完成可靠归因。",
    }


def _task_result_card(
    precheck: Mapping[str, Any],
    *,
    semantic_accepted: bool,
) -> JsonObject:
    outcome = precheck.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    status = outcome.get("status")
    if status == "failed":
        return {
            "status": "failed",
            "label": "执行失败",
            "detail": "执行记录表明本次流程没有正常完成。",
        }
    if status == "succeeded":
        suffix = (
            "语义分析已提供任务影响判断。"
            if semantic_accepted
            else "输出内容是否满足任务要求仍需有效语义分析。"
        )
        return {
            "status": "completed",
            "label": "执行流程已完成",
            "detail": f"执行记录显示流程正常结束。{suffix}",
        }
    return {
        "status": "uncertain",
        "label": "执行结果无法确定",
        "detail": "现有记录不足以确认本次执行是否完成。",
    }


def _build_unavailable_report(
    *,
    precheck: Mapping[str, Any],
    semantic_status: str,
    analysis_id: str,
    agent_run_id: str,
    generated_at: str,
    report_path: str,
    integrity_id: str,
    outcome_id: str,
    signal_ids: list[str],
    recovery_ids: list[str],
    evidence: list[JsonObject],
) -> JsonObject:
    signal_count = len(signal_ids)
    recovery_count = len(recovery_ids)
    analysis_title, analysis_message = _unavailable_message(semantic_status)
    timeline = [
        {
            "id": "timeline-integrity",
            "label": "完成基础数据检查",
            "detail": (
                "Trajectory 的结构、顺序和结束状态已完成确定性检查。"
            ),
            "tone": "positive",
            "evidence_ids": [integrity_id, outcome_id],
        }
    ]
    if signal_count:
        timeline.append(
            {
                "id": "timeline-signals",
                "label": f"发现 {signal_count} 个需要解释的异常迹象",
                "detail": "这些迹象不能直接等同于最终任务错误。",
                "tone": "warning",
                "evidence_ids": signal_ids,
            }
        )
    if recovery_count:
        timeline.append(
            {
                "id": "timeline-recovery-candidates",
                "label": f"发现 {recovery_count} 个可能的恢复行为",
                "detail": (
                    "它们尚未被证明真正消除了前序问题的影响。"
                ),
                "tone": "neutral",
                "evidence_ids": recovery_ids,
            }
        )
    timeline.append(
        {
            "id": "timeline-analysis-unavailable",
            "label": analysis_title,
            "detail": analysis_message,
            "tone": "danger",
            "evidence_ids": [],
        }
    )
    return {
        "schema": TRAJECTORY_USER_REPORT_SCHEMA,
        "run_id": str(precheck["run_id"]),
        "generated_at": generated_at,
        "analysis": {
            "status": "unavailable",
            "title": analysis_title,
            "message": analysis_message,
        },
        "overview": {
            "trajectory_data": _trajectory_data_card(precheck),
            "task_result": _task_result_card(
                precheck,
                semantic_accepted=False,
            ),
            "error_assessment": {
                "status": "uncertain",
                "label": "异常影响尚未判断",
                "detail": (
                    f"基础检查发现 {signal_count} 个异常迹象，"
                    "但当前没有通过质量检查的语义结论。"
                ),
            },
            "skill_recommendation": {
                "status": "uncertain",
                "label": "暂不建议修改 Skill",
                "detail": "当前证据不能证明问题来自 Skill 指令。",
            },
        },
        "narrative": {
            "summary": (
                f"基础检查发现 {signal_count} 个异常迹象和 "
                f"{recovery_count} 个可能的恢复行为。"
                "由于语义分析报告未通过质量检查，"
                "目前不能判断这些异常"
                "是否影响最终任务，也不能据此修改 Skill。"
            ),
            "timeline": timeline,
        },
        "incidents": [],
        "evidence": evidence,
        "recommendation": {
            "decision": "rerun_analysis",
            "summary": (
                "先修复分析报告的交付设计，"
                "再重新判断异常影响和 Skill 责任。"
            ),
            "next_steps": [
                "更新分析说明并提供完整的正确报告示例。",
                "先用一条 trajectory 验证新报告能够通过质量检查。",
                "验证成功后再继续分析其余 trajectory。",
            ],
        },
        "provenance": {
            "analysis_id": analysis_id,
            "agent_run_id": agent_run_id,
            "semantic_report_status": semantic_status,
            "semantic_report_path": None,
            "precheck_path": report_path,
        },
    }


def _unavailable_message(status: str) -> tuple[str, str]:
    if status == "invalid_output":
        return (
            "语义分析暂不可用",
            "分析过程已完成，但最终报告没有通过质量检查。"
            "下方只展示基础事实。",
        )
    if status == "timed_out":
        return (
            "语义分析超时",
            "分析没有在限定时间内完成。下方只展示基础事实。",
        )
    if status == "indeterminate":
        return (
            "语义分析状态不确定",
            "系统无法确认分析是否完整结束。"
            "下方只展示基础事实。",
        )
    return (
        "语义分析未完成",
        "分析过程发生错误。下方只展示基础检查确认的事实。",
    )


_ASSESSMENT_VIEW = {
    "no_observed_error": (
        "none",
        "未发现实际错误",
        "已解释的异常迹象属于预期控制流或无关观察。",
    ),
    "errors_recovered": (
        "recovered",
        "发生错误，但已经恢复",
        "后续行为修复了错误影响，并有任务目标证据支持恢复。",
    ),
    "terminal_failure": (
        "unresolved",
        "存在未恢复错误",
        "至少一个问题导致本次任务或执行失败。",
    ),
    "incomplete_or_indeterminate": (
        "uncertain",
        "执行状态无法确定",
        "记录不完整或执行终态无法确认。",
    ),
    "invalid_or_inconsistent": (
        "uncertain",
        "数据不支持可靠判断",
        "Trajectory 存在矛盾，不能据此完成因果判断。",
    ),
    "insufficient_evidence": (
        "uncertain",
        "证据不足",
        "现有证据不足以判断异常影响、恢复或责任边界。",
    ),
}

_ATTRIBUTION_LABELS = {
    "skill": "Skill 指令",
    "task_or_input": "任务或输入",
    "runtime_or_environment": "运行环境",
    "tool_or_dependency": "工具或依赖",
    "model_or_provider": "模型或服务商",
    "framework_or_capture": "框架或记录过程",
    "harness": "验证流程",
    "unknown": "暂时无法确定",
}

_DISPOSITION_LABELS = {
    "terminal": "未恢复问题",
    "recovered": "已恢复问题",
    "expected_control_flow": "预期控制行为",
    "latent": "潜在问题",
    "capture_integrity": "记录完整性问题",
}

_RECOVERY_LABELS = {
    "terminal": "未恢复，并影响了本次任务或执行。",
    "recovered": "后续行为已经修复其影响。",
    "expected_control_flow": (
        "不需要恢复，这是预期执行过程的一部分。"
    ),
    "latent": "尚未造成终止，但可能影响结果或后续执行。",
    "capture_integrity": "需要修复记录或补充证据后再判断。",
}


def _build_accepted_report(
    *,
    precheck: Mapping[str, Any],
    semantic_report: Mapping[str, Any],
    analysis_id: str,
    agent_run_id: str,
    generated_at: str,
    report_path: str,
    integrity_id: str,
    outcome_id: str,
    registry: _EvidenceRegistry,
) -> JsonObject:
    summary_ids = _register_semantic_evidence(
        semantic_report.get("summary_evidence"),
        registry,
        title="整体结论证据",
        summary="这条证据支持单 trajectory 的整体分析结论。",
    )
    incidents: list[JsonObject] = []
    timeline: list[JsonObject] = [
        {
            "id": "timeline-integrity",
            "label": "完成基础数据检查",
            "detail": "Trajectory 的结构和执行状态已经过确定性检查。",
            "tone": "positive",
            "evidence_ids": [integrity_id, outcome_id],
        }
    ]
    raw_incidents = semantic_report.get("incidents")
    if not isinstance(raw_incidents, list):
        raise TrajectoryUserReportError("Semantic incidents must be a list")
    for index, incident in enumerate(raw_incidents):
        if not isinstance(incident, Mapping):
            raise TrajectoryUserReportError("Semantic incidents must be objects")
        disposition = str(incident["disposition"])
        supporting = _register_semantic_evidence(
            incident.get("evidence"),
            registry,
            title=f"问题 {index + 1} 的支持证据",
            summary=(
                "这条证据支持当前问题的影响、恢复或归因判断。"
            ),
        )
        counter = _register_semantic_evidence(
            incident.get("counterevidence"),
            registry,
            title=f"问题 {index + 1} 的反向证据",
            summary="这条证据限制或反驳当前问题判断。",
        )
        confidence = incident.get("confidence")
        strength = _confidence_label(confidence)
        incident_id = str(incident["id"])
        phase = incident.get("phase")
        title_prefix = (
            str(phase) if isinstance(phase, str) and phase else f"问题 {index + 1}"
        )
        incidents.append(
            {
                "id": incident_id,
                "title": (
                    f"{title_prefix}："
                    f"{_DISPOSITION_LABELS.get(disposition, '需要关注')}"
                ),
                "impact": str(incident["claim"]),
                "recovery": _RECOVERY_LABELS.get(
                    disposition,
                    "恢复状态暂时无法确定。",
                ),
                "attribution": _ATTRIBUTION_LABELS.get(
                    str(incident["attributed_to"]),
                    "暂时无法确定",
                ),
                "evidence_strength": strength,
                "skill_change": (
                    "yes"
                    if semantic_report["skill_fix_applicability"] == "yes"
                    and incident["attributed_to"] == "skill"
                    else "no"
                    if semantic_report["skill_fix_applicability"] == "no"
                    else "uncertain"
                ),
                "evidence_ids": supporting,
                "counterevidence_ids": counter,
            }
        )
        timeline.append(
            {
                "id": f"timeline-incident-{index + 1}",
                "label": _DISPOSITION_LABELS.get(
                    disposition,
                    f"问题 {index + 1}",
                ),
                "detail": str(incident["claim"]),
                "tone": _disposition_tone(disposition),
                "evidence_ids": supporting,
            }
        )

    assessment = str(semantic_report["trajectory_assessment"])
    error_status, error_label, error_detail = _ASSESSMENT_VIEW[assessment]
    timeline.append(
        {
            "id": "timeline-conclusion",
            "label": error_label,
            "detail": str(semantic_report["summary"]),
            "tone": _assessment_tone(assessment),
            "evidence_ids": summary_ids,
        }
    )
    skill_card, recommendation = _skill_recommendation(semantic_report)
    return {
        "schema": TRAJECTORY_USER_REPORT_SCHEMA,
        "run_id": str(precheck["run_id"]),
        "generated_at": generated_at,
        "analysis": {
            "status": "accepted",
            "title": "语义分析已通过质量检查",
            "message": "以下结论可以回到保存证据进行核对。",
        },
        "overview": {
            "trajectory_data": _trajectory_data_card(precheck),
            "task_result": _task_result_card(
                precheck,
                semantic_accepted=True,
            ),
            "error_assessment": {
                "status": error_status,
                "label": error_label,
                "detail": error_detail,
            },
            "skill_recommendation": skill_card,
        },
        "narrative": {
            "summary": str(semantic_report["summary"]),
            "timeline": timeline,
        },
        "incidents": incidents,
        "evidence": registry.items,
        "recommendation": recommendation,
        "provenance": {
            "analysis_id": analysis_id,
            "agent_run_id": agent_run_id,
            "semantic_report_status": "accepted",
            "semantic_report_path": "result.json",
            "precheck_path": report_path,
        },
    }


def _register_semantic_evidence(
    raw: Any,
    registry: _EvidenceRegistry,
    *,
    title: str,
    summary: str,
) -> list[str]:
    if not isinstance(raw, list):
        raise TrajectoryUserReportError("Semantic evidence must be a list")
    identifiers: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TrajectoryUserReportError("Semantic evidence must be objects")
        identifiers.append(
            registry.add(
                item,
                kind=_evidence_kind(item),
                title=title,
                summary=summary,
            )
        )
    return identifiers


def _evidence_kind(reference: Mapping[str, Any]) -> str:
    if "seq" in reference:
        return "trajectory"
    if "artifact_path" in reference:
        return "artifact"
    return "validator" if "campaign_id" in reference else "precheck"


def _confidence_label(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "insufficient"
    if value >= 0.8:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"


def _disposition_tone(disposition: str) -> str:
    if disposition in {"recovered", "expected_control_flow"}:
        return "positive"
    if disposition == "terminal":
        return "danger"
    return "warning"


def _assessment_tone(assessment: str) -> str:
    if assessment in {"no_observed_error", "errors_recovered"}:
        return "positive"
    if assessment == "terminal_failure":
        return "danger"
    return "warning"


def _skill_recommendation(
    semantic_report: Mapping[str, Any],
) -> tuple[JsonObject, JsonObject]:
    applicability = str(semantic_report["skill_fix_applicability"])
    repair_target = semantic_report.get("repair_target")
    additional = semantic_report.get("additional_evidence_needed")
    additional = additional if isinstance(additional, list) else []
    if applicability == "yes":
        target = (
            str(repair_target)
            if isinstance(repair_target, str) and repair_target
            else "已识别的 Skill 行为边界"
        )
        return (
            {
                "status": "change",
                "label": "建议进入 Skill 改进评估",
                "detail": f"证据支持检查并修复：{target}。",
            },
            {
                "decision": "evaluate_skill_change",
                "summary": (
                    "将本次发现作为改进候选，不直接自动修改 Skill。"
                ),
                "next_steps": [
                    "检查相似 trajectory 是否重复出现同一问题。",
                    "定义最小修改和对应的回归测试。",
                    "通过候选对比后再决定是否发布。",
                ],
            },
        )
    if applicability == "no":
        return (
            {
                "status": "no_change",
                "label": "不建议修改 Skill",
                "detail": "当前问题没有被可靠归因到 Skill 指令。",
            },
            {
                "decision": "no_change",
                "summary": "保留本次证据，不根据单条 trajectory 修改 Skill。",
                "next_steps": [
                    "继续保留该 trajectory 作为运行证据。",
                    "若相同问题再次出现，再进行跨 trajectory 聚合判断。",
                ],
            },
        )
    steps = [str(item) for item in additional if isinstance(item, str) and item]
    if not steps:
        steps = [
            "补充能够判断异常影响和责任边界的任务或产物证据。"
        ]
    return (
        {
            "status": "uncertain",
            "label": "暂时无法判断是否修改 Skill",
            "detail": "现有证据不足以把问题可靠归因到 Skill。",
        },
        {
            "decision": "collect_evidence",
            "summary": "先补充证据，再决定是否进入 Skill 改进评估。",
            "next_steps": steps,
        },
    )


def write_trajectory_user_report_from_agent_run(
    agent_run_directory: str | Path,
) -> Path:
    """Create one immutable user report beside a preserved AgentRun."""

    directory = Path(agent_run_directory).resolve()
    manifest = load_json_object(directory / "manifest.json")
    if manifest.get("role") != "trajectory_error_analyst":
        raise TrajectoryUserReportError(
            "AgentRun is not a single-trajectory semantic analysis"
        )
    context = load_json_object(directory / "workspace" / "context.json")
    evidence_root = (directory / "workspace" / "evidence").resolve()
    precheck_path = context.get("trajectory_precheck_path")
    if not isinstance(precheck_path, str) or not precheck_path:
        raise TrajectoryUserReportError("AgentRun context lacks a precheck path")
    precheck_source = (evidence_root / precheck_path).resolve()
    if not precheck_source.is_relative_to(evidence_root):
        raise TrajectoryUserReportError("Precheck path leaves the evidence root")
    precheck = load_json_object(precheck_source)
    status = manifest.get("status")
    if not isinstance(status, str):
        raise TrajectoryUserReportError("AgentRun manifest lacks status")
    semantic_report: JsonObject | None = None
    if status == "succeeded":
        semantic_report = load_json_object(directory / "result.json")
    analysis_id = manifest.get("campaign_id")
    agent_run_id = manifest.get("id")
    if not isinstance(analysis_id, str) or not analysis_id:
        raise TrajectoryUserReportError("AgentRun manifest lacks analysis id")
    if not isinstance(agent_run_id, str) or not agent_run_id:
        raise TrajectoryUserReportError("AgentRun manifest lacks agent run id")
    report = build_trajectory_user_report(
        precheck=precheck,
        semantic_report=semantic_report,
        semantic_status=status,
        analysis_id=analysis_id,
        agent_run_id=agent_run_id,
        context=context,
        generated_at=(
            str(manifest["ended_at"])
            if isinstance(manifest.get("ended_at"), str)
            else utc_now()
        ),
    )
    destination = directory / TRAJECTORY_USER_REPORT_FILENAME
    if destination.exists():
        raise StorageError(f"User report already exists: {destination}")
    atomic_write_json(destination, report)
    return destination
