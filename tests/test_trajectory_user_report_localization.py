"""Verify reviewed Chinese projections preserve the source report contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.localize_trajectory_user_report import publish_localization
from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.trajectory_user_report import build_trajectory_user_report
from skill_evolution.trajectory_user_report_localization import (
    TrajectoryUserReportLocalizationError,
    localize_trajectory_user_report,
)
from tests.test_skill_explorer_data import _write_execution, _write_skill


def _source_report(run_id: str = "run-1") -> dict[str, object]:
    precheck = {
        "schema": "trajectory.precheck.v1",
        "run_id": run_id,
        "deterministic_status": "completed",
        "integrity": {"status": "valid"},
        "outcome": {"status": "succeeded"},
        "signals": [],
        "candidate_recoveries": [],
        "artifacts": [],
    }
    semantic = {
        "schema": "analysis.trajectory_error_report.v1",
        "role": "trajectory_error_analyst",
        "run_id": run_id,
        "precheck": {
            "report_path": "reports/trajectory-precheck.json",
            "deterministic_status": "completed",
            "integrity_status": "valid",
            "interpreted_signal_ids": [],
            "uninterpreted_signal_ids": [],
        },
        "trajectory_assessment": "no_observed_error",
        "primary_incident_id": None,
        "summary": "The run completed without an observed error.",
        "summary_evidence": [
            {
                "schema": "evidence.ref.v1",
                "report_path": "reports/trajectory-precheck.json",
                "json_pointer": "/outcome/status",
            }
        ],
        "incidents": [],
        "causal_chain": [],
        "skill_fix_applicability": "no",
        "repair_target": None,
        "additional_evidence_needed": [],
        "limitations": [],
    }
    return build_trajectory_user_report(
        precheck=precheck,
        semantic_report=semantic,
        semantic_status="succeeded",
        analysis_id="analysis-1",
        agent_run_id="agent-run-1",
        context={
            "run_id": run_id,
            "trajectory_precheck_path": "reports/trajectory-precheck.json",
            "precheck_deterministic_status": "completed",
            "precheck_integrity_status": "valid",
            "precheck_signal_ids": [],
        },
        generated_at="2026-08-12T00:00:00+00:00",
    )


class TrajectoryUserReportLocalizationTests(unittest.TestCase):
    """Require complete reviewed text while preserving evidence and identity."""

    def test_localizes_summary_without_changing_source(self) -> None:
        source = _source_report()
        localized = localize_trajectory_user_report(
            source,
            {
                "schema": "analysis.single_trajectory_localization_input.v1",
                "locale": "zh-CN",
                "run_id": "run-1",
                "summary": "本次执行完成，未观察到错误。",
                "skill_recommendation_detail": None,
                "incidents": [],
            },
        )

        self.assertEqual(
            localized["narrative"]["summary"],
            "本次执行完成，未观察到错误。",
        )
        self.assertEqual(
            source["narrative"]["summary"],
            "The run completed without an observed error.",
        )
        self.assertEqual(localized["evidence"], source["evidence"])
        self.assertEqual(localized["provenance"], source["provenance"])

    def test_rejects_localization_without_chinese_text(self) -> None:
        with self.assertRaisesRegex(
            TrajectoryUserReportLocalizationError,
            "Simplified Chinese",
        ):
            localize_trajectory_user_report(
                _source_report(),
                {
                    "schema": "analysis.single_trajectory_localization_input.v1",
                    "locale": "zh-CN",
                    "run_id": "run-1",
                    "summary": "English only",
                    "skill_recommendation_detail": None,
                    "incidents": [],
                },
            )

    def test_rejects_incomplete_incident_coverage(self) -> None:
        with self.assertRaisesRegex(
            TrajectoryUserReportLocalizationError,
            "cover every source incident",
        ):
            localize_trajectory_user_report(
                _source_report(),
                {
                    "schema": "analysis.single_trajectory_localization_input.v1",
                    "locale": "zh-CN",
                    "run_id": "run-1",
                    "summary": "中文摘要。",
                    "skill_recommendation_detail": None,
                    "incidents": [
                        {
                            "id": "incident-1",
                            "title": "多余问题",
                            "impact": "这条问题并不存在于原报告。",
                        }
                    ],
                },
            )

    def test_publishes_source_bound_projection_to_an_accepted_analysis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            _write_execution(root / "runtime", package)
            repository = SkillHierarchyRepository(root / "runtime")
            execution = repository.load_execution("viewer-skill", "execution-1")
            record = {
                "schema": "analysis.record.v1",
                "analysis_id": "analysis-1",
                "skill_id": "viewer-skill",
                "revision_id": execution["revision_id"],
                "scope": "single_execution",
                "execution_id": "execution-1",
                "execution_set_id": None,
                "kind": "trajectory_error",
                "producer": "agent",
                "status": "accepted",
                "input_refs": [],
                "result_refs": [],
                "attempts": [],
                "created_at": "2026-08-12T00:00:00+00:00",
                "ended_at": "2026-08-12T00:01:00+00:00",
                "provenance": None,
            }
            directory, record = repository.create_analysis(record)
            source_path = directory / "user-report.json"
            source = _source_report("execution-1")
            source_path.write_text(
                json.dumps(source, ensure_ascii=False),
                encoding="utf-8",
            )
            record["result_refs"] = [
                {
                    "path": "user-report.json",
                    "schema": "analysis.single_trajectory_view.v1",
                }
            ]
            repository.replace_analysis(record)
            localization_path = root / "reviewed.json"
            localization_path.write_text(
                json.dumps(
                    {
                        "schema": (
                            "analysis.single_trajectory_localization_input.v1"
                        ),
                        "locale": "zh-CN",
                        "run_id": "execution-1",
                        "summary": "本次执行完成，未观察到错误。",
                        "skill_recommendation_detail": None,
                        "incidents": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            localized_path = publish_localization(
                runtime_root=root / "runtime",
                skill_id="viewer-skill",
                execution_id="execution-1",
                analysis_id="analysis-1",
                localization_path=localization_path,
            )

            updated = repository.load_analysis(
                "viewer-skill",
                "analysis-1",
                execution_id="execution-1",
            )
            localized_refs = [
                reference
                for reference in updated["result_refs"]
                if reference.get("locale") == "zh-CN"
            ]
            self.assertTrue(localized_path.is_file())
            self.assertEqual(len(localized_refs), 1)
            self.assertEqual(
                localized_refs[0]["localized_from_sha256"],
                hashlib.sha256(source_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                json.loads(source_path.read_text(encoding="utf-8")),
                source,
            )


if __name__ == "__main__":
    unittest.main()
