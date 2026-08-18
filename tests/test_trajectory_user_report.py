"""Tests for the five-layer user report produced by one trajectory analysis."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.storage import StorageError
from skill_evolution.trajectory_user_report import (
    TrajectoryUserReportError,
    build_trajectory_user_report,
    validate_trajectory_user_report,
    write_trajectory_user_report_from_agent_run,
)


def _precheck() -> dict[str, object]:
    return {
        "schema": "trajectory.precheck.v1",
        "run_id": "run-1",
        "deterministic_status": "completed_with_signals",
        "integrity": {"status": "valid"},
        "outcome": {"status": "succeeded"},
        "signals": [
            {
                "id": "signal-1",
                "facts": {
                    "status": "failed",
                    "tool_name": "write",
                    "target_path": "artifacts/output.html",
                },
                "evidence": {
                    "schema": "evidence.ref.v1",
                    "run_id": "run-1",
                    "seq": 2,
                },
            }
        ],
        "candidate_recoveries": [
            {
                "id": "recovery-1",
                "failed_signal_id": "signal-1",
                "later_succeeded_seq": 3,
                "proves_recovery": False,
            }
        ],
        "artifacts": [],
    }


def _context() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "trajectory_precheck_path": "reports/trajectory-precheck.json",
        "precheck_deterministic_status": "completed_with_signals",
        "precheck_integrity_status": "valid",
        "precheck_signal_ids": ["signal-1"],
    }


def _semantic_report() -> dict[str, object]:
    report_ref = {
        "schema": "evidence.ref.v1",
        "report_path": "reports/trajectory-precheck.json",
        "json_pointer": "/deterministic_status",
    }
    trajectory_ref = {
        "schema": "evidence.ref.v1",
        "run_id": "run-1",
        "seq": 2,
    }
    return {
        "schema": "analysis.trajectory_error_report.v1",
        "role": "trajectory_error_analyst",
        "run_id": "run-1",
        "precheck": {
            "report_path": "reports/trajectory-precheck.json",
            "deterministic_status": "completed_with_signals",
            "integrity_status": "valid",
            "interpreted_signal_ids": ["signal-1"],
            "uninterpreted_signal_ids": [],
        },
        "trajectory_assessment": "errors_recovered",
        "primary_incident_id": "incident-1",
        "summary": "首次写入失败，后续行为修复了同一影响。",
        "summary_evidence": [report_ref],
        "incidents": [
            {
                "id": "incident-1",
                "source_signal_ids": ["signal-1"],
                "disposition": "recovered",
                "causal_role": "root_cause",
                "attributed_to": "tool_or_dependency",
                "phase": "写入产物",
                "claim": "首次写入未生成目标，后续写入成功。",
                "confidence": 0.8,
                "evidence": [trajectory_ref],
                "counterevidence": [],
            }
        ],
        "causal_chain": [],
        "skill_fix_applicability": "no",
        "repair_target": None,
        "additional_evidence_needed": [],
        "limitations": [],
    }


class TrajectoryUserReportTests(unittest.TestCase):
    """The view separates trustworthy facts from accepted semantics."""

    def test_invalid_semantic_output_builds_all_five_layers_safely(
        self,
    ) -> None:
        report = build_trajectory_user_report(
            precheck=_precheck(),
            semantic_report=None,
            semantic_status="invalid_output",
            analysis_id="analysis-1",
            agent_run_id="agent-run-1",
            context=_context(),
            generated_at="2026-08-09T00:00:00+00:00",
        )

        self.assertEqual(report["analysis"]["status"], "unavailable")
        self.assertEqual(report["incidents"], [])
        self.assertEqual(
            report["recommendation"]["decision"],
            "rerun_analysis",
        )
        self.assertEqual(
            report["overview"]["trajectory_data"]["status"],
            "complete",
        )
        self.assertEqual(
            report["overview"]["error_assessment"]["status"],
            "uncertain",
        )
        self.assertEqual(len(report["narrative"]["timeline"]), 4)
        self.assertGreaterEqual(len(report["evidence"]), 4)

    def test_accepted_semantics_become_incident_and_action_guidance(
        self,
    ) -> None:
        report = build_trajectory_user_report(
            precheck=_precheck(),
            semantic_report=_semantic_report(),
            semantic_status="succeeded",
            analysis_id="analysis-1",
            agent_run_id="agent-run-1",
            context=_context(),
            generated_at="2026-08-09T00:00:00+00:00",
        )

        self.assertEqual(report["analysis"]["status"], "accepted")
        self.assertEqual(
            report["overview"]["error_assessment"]["status"],
            "recovered",
        )
        self.assertEqual(len(report["incidents"]), 1)
        self.assertEqual(
            report["incidents"][0]["evidence_strength"],
            "high",
        )
        self.assertEqual(
            report["recommendation"]["decision"],
            "no_change",
        )

    def test_report_rejects_unknown_evidence_and_unavailable_incidents(
        self,
    ) -> None:
        report = build_trajectory_user_report(
            precheck=_precheck(),
            semantic_report=None,
            semantic_status="invalid_output",
            analysis_id="analysis-1",
            agent_run_id="agent-run-1",
            context=_context(),
        )
        report["narrative"]["timeline"][0]["evidence_ids"] = ["missing"]
        with self.assertRaisesRegex(
            TrajectoryUserReportError,
            "unknown evidence",
        ):
            validate_trajectory_user_report(report)

        report = build_trajectory_user_report(
            precheck=_precheck(),
            semantic_report=None,
            semantic_status="invalid_output",
            analysis_id="analysis-1",
            agent_run_id="agent-run-1",
            context=_context(),
        )
        report["incidents"] = [
            {
                "id": "unsafe",
                "title": "Unvalidated claim",
                "impact": "Unknown",
                "recovery": "Unknown",
                "attribution": "Unknown",
                "evidence_strength": "insufficient",
                "skill_change": "uncertain",
                "evidence_ids": [],
                "counterevidence_ids": [],
            }
        ]
        with self.assertRaisesRegex(
            TrajectoryUserReportError,
            "cannot expose semantic incidents",
        ):
            validate_trajectory_user_report(report)

    def test_agent_run_writer_creates_one_immutable_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "agent-run-1"
            evidence = run_directory / "workspace" / "evidence"
            (evidence / "reports").mkdir(parents=True)
            (run_directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": "agent-run-1",
                        "campaign_id": "analysis-1",
                        "role": "trajectory_error_analyst",
                        "status": "invalid_output",
                        "ended_at": "2026-08-09T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (run_directory / "workspace" / "context.json").write_text(
                json.dumps(_context()),
                encoding="utf-8",
            )
            (evidence / "reports" / "trajectory-precheck.json").write_text(
                json.dumps(_precheck()),
                encoding="utf-8",
            )

            output = write_trajectory_user_report_from_agent_run(run_directory)

            self.assertEqual(output.name, "user-report.json")
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["analysis"]["status"], "unavailable")
            with self.assertRaises(StorageError):
                write_trajectory_user_report_from_agent_run(run_directory)


if __name__ == "__main__":
    unittest.main()
