"""Tests for deterministic single-trajectory prechecks."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.trajectory_precheck import _run_cli
from skill_evolution.skill_contracts import validate_skill_contract
from skill_evolution.trajectory_precheck import precheck_trajectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _record(
    run_id: str,
    sequence: int,
    record_type: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": run_id,
        "seq": sequence,
        "observed_at": "2026-08-06T00:00:00+00:00",
        "elapsed_ms": sequence * 10,
        "source": "framework",
        "type": record_type,
        "payload": payload or {},
    }


def _write_trajectory(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _complete_records(
    run_id: str,
    *,
    output_exists: bool = True,
    outcome_status: str = "succeeded",
) -> list[dict[str, object]]:
    records = [
        _record(run_id, 1, "trajectory_started", {"manifest": {}}),
        _record(run_id, 2, "agent_start"),
        _record(run_id, 3, "agent_end"),
        _record(run_id, 4, "agent_settled"),
        _record(
            run_id,
            5,
            "artifact_registered",
            {
                "artifact_role": "output",
                "artifact_index": 0,
                "artifact": {
                    "path": "artifacts/output.txt",
                    "exists": output_exists,
                    **({"bytes": 2} if output_exists else {}),
                },
            },
        ),
        _record(
            run_id,
            6,
            "trajectory_finished",
            {
                "outcome": {
                    "status": outcome_status,
                    "failure_stage": None,
                    "error": None,
                    "skill_loaded": True,
                    "agent_settled": True,
                    "process_exit_code": 0,
                    "session": {
                        "status": "complete",
                        "line_count": 2,
                        "message_count": 1,
                        "invalid_line_count": 0,
                    },
                    "observer_errors": [],
                }
            },
        ),
        _record(
            run_id,
            7,
            "trajectory_sealed",
            {"status": outcome_status, "record_count": 7},
        ),
    ]
    return records


class TrajectoryPrecheckTests(unittest.TestCase):
    """The precheck extracts facts without making semantic judgments."""

    def test_clean_complete_trajectory_needs_only_artifact_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / "artifacts/output.txt").write_text("ok", encoding="utf-8")
            path = root / "trajectory.jsonl"
            _write_trajectory(path, _complete_records("run-clean"))

            report = precheck_trajectory(path)

        self.assertEqual(report["schema"], "trajectory.precheck.v1")
        self.assertEqual(report["deterministic_status"], "completed_clean")
        self.assertEqual(report["integrity"]["status"], "valid")
        self.assertEqual(report["signals"], [])
        self.assertEqual(
            [item["kind"] for item in report["llm_required_judgments"]],
            ["artifact_semantic_correctness"],
        )

    def test_failed_tool_and_later_success_are_only_a_recovery_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / "artifacts/output.txt").write_text("ok", encoding="utf-8")
            records = _complete_records("run-retry")
            records[1:1] = [
                _record(
                    "run-retry",
                    0,
                    "tool_action",
                    {
                        "tool_name": "write",
                        "status": "failed",
                        "arguments": {"path": "artifacts/output.txt"},
                    },
                ),
                _record(
                    "run-retry",
                    0,
                    "tool_action",
                    {
                        "tool_name": "write",
                        "status": "succeeded",
                        "arguments": {"path": "artifacts/output.txt"},
                    },
                ),
            ]
            for sequence, record in enumerate(records, start=1):
                record["seq"] = sequence
                record["elapsed_ms"] = sequence * 10
            records[-1]["payload"]["record_count"] = len(records)
            path = root / "trajectory.jsonl"
            _write_trajectory(path, records)

            report = precheck_trajectory(path)

        self.assertEqual(report["deterministic_status"], "completed_with_signals")
        self.assertEqual(report["signals"][0]["kind"], "tool_non_success")
        candidate = report["candidate_recoveries"][0]
        self.assertEqual(candidate["basis"], "same_tool_and_target_path")
        self.assertFalse(candidate["proves_recovery"])
        self.assertNotIn("disposition", report["signals"][0])
        self.assertNotIn("causal_role", report["signals"][0])

    def test_invalid_json_sequence_gap_and_missing_seal_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trajectory.jsonl"
            started = _record("run-broken", 1, "trajectory_started")
            finished = _record(
                "run-broken",
                3,
                "trajectory_finished",
                {"outcome": {"status": "failed"}},
            )
            path.write_text(
                json.dumps(started)
                + "\n{not-json}\n"
                + json.dumps(finished)
                + "\n",
                encoding="utf-8",
            )

            report = precheck_trajectory(path)

        self.assertEqual(report["deterministic_status"], "invalid")
        codes = {item["code"] for item in report["integrity"]["issues"]}
        self.assertIn("invalid_json", codes)
        self.assertIn("non_continuous_sequence", codes)
        self.assertIn("missing_sealed", codes)
        self.assertEqual(
            report["integrity"]["sequence"]["gap_ranges"],
            [{"start": 2, "end": 2}],
        )

    def test_legacy_action_file_is_normalized_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / "artifacts/output.txt").write_text("ok", encoding="utf-8")
            records = _complete_records("run-legacy")
            type_aliases = {
                "trace_started": "trajectory_started",
                "trace_finished": "trajectory_finished",
                "trace_sealed": "trajectory_sealed",
            }
            for record in records:
                record["schema"] = "trace.actions.v1"
                record["type"] = type_aliases.get(record["type"], record["type"])
            path = root / "trace.jsonl"
            _write_trajectory(path, records)

            report = precheck_trajectory(path)

        self.assertEqual(report["deterministic_status"], "completed_clean")
        self.assertEqual(report["integrity"]["status"], "valid")
        self.assertEqual(report["integrity"]["source_format"], "legacy")
        self.assertEqual(
            report["lifecycle"]["record_type_counts"]["trajectory_sealed"],
            1,
        )

    def test_failed_outcome_and_missing_output_are_explicit_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _complete_records(
                "run-failed",
                output_exists=False,
                outcome_status="failed",
            )
            outcome = records[-2]["payload"]["outcome"]
            outcome.update(
                {
                    "failure_stage": "inspect_result",
                    "error": {"type": "RuntimeError", "message": "secret"},
                    "session": {
                        "status": "partial",
                        "line_count": 1,
                        "message_count": 0,
                        "invalid_line_count": 1,
                    },
                }
            )
            path = root / "trajectory.jsonl"
            _write_trajectory(path, records)

            report = precheck_trajectory(path)

        self.assertEqual(report["deterministic_status"], "failed")
        kinds = {signal["kind"] for signal in report["signals"]}
        self.assertEqual(
            kinds,
            {"failed_outcome", "artifact_issue"},
        )
        self.assertEqual(report["outcome"]["error_type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(report))

    def test_cli_writes_report_and_returns_nonzero_for_missing_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "precheck.json"
            with redirect_stdout(io.StringIO()):
                exit_code = _run_cli(
                    [str(root / "missing.jsonl"), "--output", str(output)]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["deterministic_status"], "invalid")
        self.assertEqual(
            report["integrity"]["issues"][0]["code"],
            "trajectory_missing",
        )

    def test_packaged_skill_is_approved_for_model_work(self) -> None:
        skill = PROJECT_ROOT / "skills/analyze-single-trajectory"

        report = validate_skill_contract(skill_directory=skill)

        self.assertTrue(report["valid"])
        self.assertTrue(report["dynamic_test_ready"])
        self.assertEqual(report["contract"]["status"], "approved")
        self.assertTrue((skill / "agents/openai.yaml").is_file())
        self.assertTrue((skill / "scripts/precheck_trajectory.py").is_file())
        self.assertTrue((skill / "scripts/analyze_trajectory.py").is_file())


if __name__ == "__main__":
    unittest.main()
