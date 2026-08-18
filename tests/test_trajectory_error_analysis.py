"""Tests for strict semantic single-trajectory reports and frozen evidence."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.evidence import SingleTrajectoryEvidenceBundleBuilder
from skill_evolution.pi_runtime import (
    _validate_result_evidence,
    _validate_role_result,
)
from skill_evolution.agents import AgentRole
from skill_evolution.trajectory_analysis import (
    TrajectoryAnalysisContractError,
    validate_trajectory_error_report,
)


def _context() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "trajectory_precheck_path": "reports/trajectory-precheck.json",
        "precheck_deterministic_status": "completed_with_signals",
        "precheck_integrity_status": "valid",
        "precheck_signal_ids": ["signal-1"],
    }


def _report() -> dict[str, object]:
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
        "summary": "The failed action was repaired by later evidence.",
        "summary_evidence": [report_ref],
        "incidents": [
            {
                "id": "incident-1",
                "source_signal_ids": ["signal-1"],
                "disposition": "recovered",
                "causal_role": "root_cause",
                "attributed_to": "tool_or_dependency",
                "phase": "artifact_write",
                "claim": "A later action repaired the failed write.",
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


def _trajectory_record(sequence: int) -> dict[str, object]:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": "run-1",
        "seq": sequence,
        "observed_at": "2026-08-07T00:00:00+00:00",
        "elapsed_ms": sequence,
        "source": "framework",
        "type": "tool_action" if sequence == 2 else "trajectory_started",
        "payload": {
            "api_key": "must-not-survive",
            "message": {
                "type": "reasoning",
                "text": "hidden",
            },
        },
    }


class TrajectoryErrorReportTests(unittest.TestCase):
    """Semantic output is strict and bound to its deterministic input."""

    def test_valid_report_is_normalized_for_the_trajectory_role(self) -> None:
        expected = validate_trajectory_error_report(_report(), _context())
        actual = _validate_role_result(
            AgentRole.TRAJECTORY_ERROR_ANALYST,
            _report(),
            _context(),
        )

        self.assertEqual(actual, expected)
        self.assertEqual(actual["trajectory_assessment"], "errors_recovered")
        self.assertEqual(actual["incidents"][0]["confidence"], 0.8)

    def test_rejects_identity_status_and_signal_partition_drift(self) -> None:
        cases: list[tuple[str, object]] = [
            ("run_id", "other-run"),
            ("deterministic_status", "completed_clean"),
            ("interpreted_signal_ids", []),
        ]
        for field, replacement in cases:
            with self.subTest(field=field):
                report = _report()
                if field == "run_id":
                    report[field] = replacement
                else:
                    report["precheck"][field] = replacement
                with self.assertRaises(TrajectoryAnalysisContractError):
                    validate_trajectory_error_report(report, _context())

    def test_rejects_unproven_whole_trajectory_assessment(self) -> None:
        report = _report()
        report["trajectory_assessment"] = "terminal_failure"

        with self.assertRaisesRegex(
            TrajectoryAnalysisContractError,
            "terminal incident",
        ):
            validate_trajectory_error_report(report, _context())

    def test_every_whole_trajectory_assessment_has_a_valid_fixture(self) -> None:
        for assessment in (
            "no_observed_error",
            "errors_recovered",
            "terminal_failure",
            "incomplete_or_indeterminate",
            "invalid_or_inconsistent",
            "insufficient_evidence",
        ):
            with self.subTest(assessment=assessment):
                context = _context()
                report = _report()
                report["trajectory_assessment"] = assessment
                if assessment == "no_observed_error":
                    report["primary_incident_id"] = None
                    report["incidents"][0]["disposition"] = (
                        "expected_control_flow"
                    )
                    report["incidents"][0]["causal_role"] = "unrelated"
                elif assessment == "terminal_failure":
                    report["incidents"][0]["disposition"] = "terminal"
                elif assessment in {
                    "incomplete_or_indeterminate",
                    "invalid_or_inconsistent",
                    "insufficient_evidence",
                }:
                    report["primary_incident_id"] = None
                    report["precheck"]["interpreted_signal_ids"] = []
                    report["precheck"]["uninterpreted_signal_ids"] = [
                        "signal-1"
                    ]
                    report["incidents"] = []
                    report["additional_evidence_needed"] = [
                        "Additional evidence is required."
                    ]
                if assessment == "incomplete_or_indeterminate":
                    context["precheck_deterministic_status"] = "incomplete"
                    context["precheck_integrity_status"] = "incomplete"
                    report["precheck"]["deterministic_status"] = "incomplete"
                    report["precheck"]["integrity_status"] = "incomplete"
                elif assessment == "invalid_or_inconsistent":
                    context["precheck_integrity_status"] = "invalid"
                    report["precheck"]["integrity_status"] = "invalid"

                validated = validate_trajectory_error_report(report, context)

                self.assertEqual(validated["trajectory_assessment"], assessment)


class SingleTrajectoryEvidenceTests(unittest.TestCase):
    """A semantic run sees one redacted trajectory and its frozen precheck."""

    def test_builder_freezes_one_run_and_evidence_refs_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run-1"
            artifacts = run / "artifacts"
            artifacts.mkdir(parents=True)
            trajectory = run / "trajectory.jsonl"
            trajectory.write_text(
                "".join(
                    json.dumps(_trajectory_record(sequence)) + "\n"
                    for sequence in (1, 2)
                ),
                encoding="utf-8",
            )
            (artifacts / "output.html").write_text(
                "<h1>Output</h1>",
                encoding="utf-8",
            )
            precheck = root / "precheck.json"
            precheck.write_text(
                json.dumps(
                    {
                        "schema": "trajectory.precheck.v1",
                        "run_id": "run-1",
                        "deterministic_status": (
                            "completed_with_signals"
                        ),
                        "integrity": {
                            "status": "valid",
                            "source_format": "current",
                        },
                        "signals": [{"id": "signal-1"}],
                    }
                ),
                encoding="utf-8",
            )
            analyzer_contract = root / "analyzer-contract.json"
            analyzer_contract.write_text("{}\n", encoding="utf-8")
            subject_contract = root / "subject-contract.json"
            subject_contract.write_text("{}\n", encoding="utf-8")
            task_context = root / "task.md"
            task_context.write_text("Create output.html.\n", encoding="utf-8")

            bundle = SingleTrajectoryEvidenceBundleBuilder().build(
                trajectory_path=trajectory,
                precheck_path=precheck,
                destination=root / "evidence",
                analyzer_contract_path=analyzer_contract,
                subject_contract_path=subject_contract,
                task_context_path=task_context,
            )

            manifest = json.loads(
                (bundle / "bundle.json").read_text(encoding="utf-8")
            )
            copied_trajectory = (
                bundle / "runs/run-1/trajectory.jsonl"
            ).read_text(encoding="utf-8")
            self.assertEqual(len(manifest["runs"]), 1)
            self.assertEqual(
                manifest["precheck"],
                "reports/trajectory-precheck.json",
            )
            self.assertNotIn("must-not-survive", copied_trajectory)
            self.assertIn("[HIDDEN_MODEL_REASONING]", copied_trajectory)
            self.assertFalse((bundle / "pi-session.jsonl").exists())

            report = _report()
            artifact_path = manifest["runs"][0]["artifacts"][0]
            report["incidents"][0]["evidence"].extend(
                [
                    {
                        "schema": "evidence.ref.v1",
                        "artifact_path": artifact_path,
                        "line": 1,
                    },
                    {
                        "schema": "evidence.ref.v1",
                        "artifact_path": artifact_path,
                        "selector": "h1",
                    },
                ]
            )
            result = validate_trajectory_error_report(report, _context())
            _validate_result_evidence(
                AgentRole.TRAJECTORY_ERROR_ANALYST,
                result,
                bundle_root=bundle,
            )


if __name__ == "__main__":
    unittest.main()
