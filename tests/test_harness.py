"""Tests for the shared deterministic HarnessRun boundary."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.harness import run_harness, run_hierarchy_harness
from skill_evolution.hierarchy import (
    SkillHierarchyRepository,
    execution_manifest_from_payload,
)
from skill_evolution.storage import load_json_object
from tests.test_skill_hierarchy import _write_skill


class FakeProfiler:
    """Return one stable profile without loading real trajectories."""

    def __init__(self, _root: Path) -> None:
        pass

    def profile_campaign(self, campaign_id: str) -> dict[str, object]:
        return {
            "schema": "trajectory.profile.v1",
            "profile_id": None,
            "load_status": "ok",
            "source": {"campaign_id": campaign_id},
            "runs": [],
            "aggregate": {},
            "issues": [],
        }


class FakeComparator:
    """Return a partial report to exercise component status propagation."""

    def __init__(self, *, chrome_command: str | None = None) -> None:
        self.chrome_command = chrome_command

    def compare_campaign(
        self,
        _campaign: Path,
        *,
        capture_screenshots: bool,
        screenshot_directory: Path,
    ) -> dict[str, object]:
        self.capture_screenshots = capture_screenshots
        self.screenshot_directory = screenshot_directory
        return {
            "schema": "artifact.comparison.v1",
            "comparison_id": None,
            "status": "partial",
            "artifacts": [],
            "pairwise": [],
            "issues": [{"code": "chrome_unavailable"}],
        }


class HarnessRunTests(unittest.TestCase):
    """Profiler and comparator outputs share one atomic HarnessRun manifest."""

    @patch("scripts.harness.HTMLArtifactComparator", FakeComparator)
    @patch("scripts.harness.TrajectoryProfiler", FakeProfiler)
    def test_partial_component_remains_a_successful_visible_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = root / "replays/campaign-1"
            campaign.mkdir(parents=True)
            (campaign / "replay.json").write_text(
                '{"schema":"replay.campaign.v1"}\n',
                encoding="utf-8",
            )

            result = run_harness(
                replay_campaign_directory=campaign,
                output_root=root / "harness-runs",
                capture_screenshots=True,
            )

            self.assertEqual(result.manifest["status"], "completed_partial")
            self.assertEqual(
                result.profile["schema"],
                "trajectory.profile.v1",
            )
            self.assertEqual(
                result.artifact_comparison["schema"],
                "artifact.comparison.v1",
            )
            persisted = load_json_object(
                result.harness_directory / "harness.json"
            )
            self.assertEqual(persisted["status"], "completed_partial")
            self.assertEqual(
                persisted["outputs"]["trajectory_profile"],
                "trajectory-profile.json",
            )
            self.assertTrue(
                (result.harness_directory / "artifact-comparison.json").is_file()
            )

    @patch("scripts.harness.HTMLArtifactComparator", FakeComparator)
    @patch("scripts.harness.TrajectoryProfiler", FakeProfiler)
    def test_execution_set_harness_is_not_presented_as_multi_trajectory_analysis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            repository = SkillHierarchyRepository(runtime)
            revision = repository.register_revision(
                _write_skill(root / "packages")
            )
            revision_id = str(revision.manifest["revision_id"])
            execution_set = repository.create_execution_set(
                skill_id="test-skill",
                revision_id=revision_id,
                purpose="replay",
                task={},
                runtime={},
                provenance={},
                set_id="set-1",
                status="running",
            )
            execution = repository.prepare_execution(
                skill_id="test-skill",
                revision_id=revision_id,
                origin="replay",
                execution_set_id="set-1",
                execution_id="execution-1",
            )
            artifact = execution.payload_directory / "artifacts/output.html"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("<!doctype html><title>ok</title>")
            records = [
                {
                    "schema": "trajectory.actions.v1",
                    "run_id": "execution-1",
                    "seq": 1,
                    "type": "trajectory_started",
                    "payload": {
                        "manifest": {
                            "started_at": "2026-08-09T00:00:00Z",
                            "task_case": {},
                        }
                    },
                },
                {
                    "schema": "trajectory.actions.v1",
                    "run_id": "execution-1",
                    "seq": 2,
                    "type": "artifact_registered",
                    "payload": {
                        "artifact_role": "output",
                        "artifact": {
                            "path": "artifacts/output.html",
                            "exists": True,
                            "bytes": artifact.stat().st_size,
                        },
                    },
                },
                {
                    "schema": "trajectory.actions.v1",
                    "run_id": "execution-1",
                    "seq": 3,
                    "type": "trajectory_finished",
                    "payload": {
                        "outcome": {
                            "status": "succeeded",
                            "started_at": "2026-08-09T00:00:00Z",
                            "ended_at": "2026-08-09T00:00:01Z",
                            "duration_ms": 1000,
                            "session": {"status": "missing"},
                        }
                    },
                },
                {
                    "schema": "trajectory.actions.v1",
                    "run_id": "execution-1",
                    "seq": 4,
                    "type": "trajectory_sealed",
                    "payload": {"status": "succeeded", "record_count": 4},
                },
            ]
            (execution.payload_directory / "trajectory.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records)
            )
            manifest = execution_manifest_from_payload(
                execution_directory=execution.directory,
                skill_id="test-skill",
                revision_id=revision_id,
                execution_id="execution-1",
                origin="replay",
                execution_set_id="set-1",
            )
            repository.finalize_execution(
                "test-skill", "execution-1", manifest
            )
            execution_set["execution_ids"] = ["execution-1"]
            execution_set["status"] = "completed"
            repository.replace_execution_set(
                "test-skill", "set-1", execution_set
            )

            result = run_hierarchy_harness(
                runtime_root=runtime,
                skill_id="test-skill",
                execution_set_id="set-1",
                capture_screenshots=False,
            )

            self.assertIsNotNone(result.hierarchy_analysis_directory)
            assert result.hierarchy_analysis_directory is not None
            analysis = load_json_object(
                result.hierarchy_analysis_directory / "analysis.json"
            )
            self.assertEqual(analysis["kind"], "harness")
            self.assertEqual(analysis["execution_set_id"], "set-1")
            self.assertEqual(
                result.hierarchy_analysis_directory.parent,
                repository.execution_set_analyses_directory(
                    "test-skill", "set-1"
                ),
            )
            self.assertFalse(
                (result.hierarchy_analysis_directory / "user-report.json").exists()
            )
            self.assertEqual(
                repository.list_multi_trajectory_analyses("test-skill"), []
            )
            self.assertEqual(
                [
                    item["analysis_id"]
                    for item in repository.list_execution_set_analyses(
                        "test-skill", set_id="set-1"
                    )
                ],
                [analysis["analysis_id"]],
            )


if __name__ == "__main__":
    unittest.main()
