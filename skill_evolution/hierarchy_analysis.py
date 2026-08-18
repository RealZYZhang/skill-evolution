"""Application services for Skill-owned multi-Trajectory analysis attempts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    MULTI_TRAJECTORY_VIEW_SCHEMA,
    SkillHierarchyRepository,
    validate_multi_trajectory_view,
)
from skill_evolution.storage import (
    JsonObject,
    atomic_write_json,
    new_object_id,
    utc_now,
)


class HierarchyAnalysisError(ValueError):
    """Raised when an analysis is attached to an invalid subject or result."""


class HierarchyAnalysisService:
    """Own multi-Trajectory analysis records and internal AgentRun attempts."""

    def __init__(self, runtime_root: str | Path) -> None:
        self.repository = SkillHierarchyRepository(runtime_root)

    def prepare_multi(
        self,
        *,
        skill_id: str,
        execution_set_id: str,
        kind: str = "multi_trajectory",
        input_refs: Sequence[Mapping[str, Any]] = (),
        provenance: Mapping[str, Any] | None = None,
        analysis_id: str | None = None,
    ) -> tuple[Path, JsonObject]:
        """Create one planned analysis bound to a same-revision Execution Set."""

        execution_set = self.repository.load_execution_set(
            skill_id, execution_set_id
        )
        identifier = analysis_id or new_object_id("analysis")
        record: JsonObject = {
            "schema": ANALYSIS_RECORD_SCHEMA,
            "analysis_id": identifier,
            "skill_id": skill_id,
            "revision_id": execution_set["revision_id"],
            "scope": "execution_set",
            "execution_id": None,
            "execution_set_id": execution_set_id,
            "kind": kind,
            "producer": "agent" if kind == "multi_trajectory" else "composite",
            "status": "planned",
            "input_refs": [
                {
                    "kind": "execution_set",
                    "execution_set_id": execution_set_id,
                },
                *[dict(item) for item in input_refs],
            ],
            "result_refs": [],
            "attempts": [],
            "created_at": utc_now(),
            "ended_at": None,
            "provenance": dict(provenance) if provenance else None,
        }
        return self.repository.create_analysis(record)

    def start(self, skill_id: str, analysis_id: str) -> JsonObject:
        """Mark a planned multi-Trajectory analysis as running."""

        record = self.repository.load_analysis(skill_id, analysis_id)
        if record["status"] != "planned":
            raise HierarchyAnalysisError("Analysis is not planned")
        record["status"] = "running"
        self.repository.replace_analysis(record)
        return record

    def finish(
        self,
        *,
        skill_id: str,
        analysis_id: str,
        status: str,
        attempts: Sequence[Mapping[str, Any]],
        result_refs: Sequence[Mapping[str, Any]],
        user_report: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """Seal attempts and publish only a validated multi-Trajectory user report."""

        record = self.repository.load_analysis(skill_id, analysis_id)
        if status not in {
            "accepted",
            "unavailable",
            "failed",
            "invalid_output",
            "timed_out",
            "indeterminate",
            "inconclusive",
        }:
            raise HierarchyAnalysisError("Unsupported terminal analysis status")
        directory = self.repository.analysis_directory(record)
        refs = [dict(item) for item in result_refs]
        if user_report is not None:
            report = validate_multi_trajectory_view(user_report)
            identity = (
                report["analysis_id"],
                report["skill_id"],
                report["revision_id"],
            )
            expected = (
                record["analysis_id"],
                record["skill_id"],
                record["revision_id"],
            )
            if identity != expected:
                raise HierarchyAnalysisError(
                    "Multi-Trajectory report identity differs from its analysis"
                )
            atomic_write_json(directory / "user-report.json", report)
            refs.append(
                {
                    "schema": MULTI_TRAJECTORY_VIEW_SCHEMA,
                    "path": "user-report.json",
                }
            )
        record["status"] = status
        record["attempts"] = [dict(item) for item in attempts]
        record["result_refs"] = refs
        record["ended_at"] = utc_now()
        self.repository.replace_analysis(record)
        return record

    def unavailable_report(
        self,
        *,
        skill_id: str,
        analysis_id: str,
        message: str,
    ) -> JsonObject:
        """Build a safe user projection when semantic conclusions are unusable."""

        record = self.repository.load_analysis(skill_id, analysis_id)
        execution_set = self.repository.load_execution_set(
            skill_id, str(record["execution_set_id"])
        )
        report: JsonObject = {
            "schema": MULTI_TRAJECTORY_VIEW_SCHEMA,
            "analysis_id": analysis_id,
            "skill_id": skill_id,
            "revision_id": record["revision_id"],
            "generated_at": utc_now(),
            "analysis": {"status": "unavailable", "message": message},
            "overview": {
                "title": "多 Trajectory 结论暂不可用",
                "summary": (
                    "执行集合与确定性结果仍可查看，但当前没有通过质量检查的"
                    "跨运行语义结论。"
                ),
            },
            "execution_set": {
                "execution_set_id": execution_set["set_id"],
                "execution_ids": execution_set["execution_ids"],
                "purpose": execution_set["purpose"],
            },
            "patterns": [],
            "findings": [],
            "evidence": [],
            "recommendation": {
                "summary": "修复分析交付后重新运行；当前不修改 Skill。",
                "next_steps": ["检查 AgentRun 状态与结果格式。"],
            },
            "provenance": {
                "analysis_id": analysis_id,
                "semantic_status": "unavailable",
            },
        }
        return validate_multi_trajectory_view(report)
