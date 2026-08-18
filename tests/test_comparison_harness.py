"""Tests for comparison-to-Harness batch materialization."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.harness import HarnessRunResult
from skill_evolution.comparison import ComparisonError
from skill_evolution.comparison_harness import ComparisonHarnessRunner


class ComparisonHarnessRunnerTests(unittest.TestCase):
    """Smoke and full batches preserve attempts without modifying originals."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.comparison = self.root / "comparison-1"
        self.attempt = self.comparison / "attempts/01-attempt-01"
        (self.attempt / "artifacts").mkdir(parents=True)
        (self.attempt / "trajectory.jsonl").write_text(
            '{"schema":"trajectory.actions.v1","run_id":"run-1","seq":1}\n',
            encoding="utf-8",
        )
        (self.attempt / "pi-session.jsonl").write_text(
            '{"type":"session"}\n',
            encoding="utf-8",
        )
        (self.attempt / "artifacts/output.html").write_text(
            "<!doctype html><html><body>ok</body></html>",
            encoding="utf-8",
        )
        self.attempt_record = {
            "attempt_index": 1,
            "workflow_attempt": 1,
            "attempt_path": "attempts/01-attempt-01",
            "status": "succeeded",
            "run_id": "run-1",
            "session_status": "complete",
            "artifacts": [
                {
                    "path": "artifacts/output.html",
                    "exists": True,
                    "bytes": 43,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @patch("skill_evolution.comparison_harness.run_harness")
    def test_materializes_replay_batch_and_returns_harness_reference(
        self,
        run_harness,
    ) -> None:
        harness_directory = self.root / "harness-runs/harness-1"
        harness_directory.mkdir(parents=True)
        run_harness.return_value = HarnessRunResult(
            harness_directory=harness_directory,
            manifest={
                "schema": "harness.run.v1",
                "harness_run_id": "harness-1",
                "status": "completed",
            },
            profile={"schema": "trajectory.profile.v1"},
            artifact_comparison={"schema": "artifact.comparison.v1"},
        )
        original = (self.attempt / "trajectory.jsonl").read_bytes()

        result = ComparisonHarnessRunner(
            output_root=self.root / "harness-runs",
            capture_screenshots=False,
        )([self.attempt_record], self.comparison)

        self.assertEqual(result["harness_run_id"], "harness-1")
        campaign = (
            self.comparison
            / "harness-inputs/batch-001"
        )
        manifest = json.loads(
            (campaign / "replay.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["summary"]["trajectory_count"], 1)
        self.assertEqual(manifest["runs"][0]["run_id"], "run-1")
        self.assertTrue(
            (campaign / "runs/run-1/artifacts/output.html").is_file()
        )
        self.assertEqual(
            (self.attempt / "trajectory.jsonl").read_bytes(),
            original,
        )
        run_harness.assert_called_once_with(
            replay_campaign_directory=campaign.resolve(),
            output_root=(self.root / "harness-runs").resolve(),
            capture_screenshots=False,
            chrome_command=None,
        )

    @patch("skill_evolution.comparison_harness.run_harness")
    def test_rejects_attempt_path_escape_before_harness(
        self,
        run_harness,
    ) -> None:
        escaped = {
            **self.attempt_record,
            "attempt_path": "../outside",
        }

        with self.assertRaisesRegex(ComparisonError, "escapes"):
            ComparisonHarnessRunner()([escaped], self.comparison)
        run_harness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
