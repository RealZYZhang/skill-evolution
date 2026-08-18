"""Tests for the viewer's shared trajectory-profile projection."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.trajectory_profiler import TrajectoryProfiler
from scripts.trajectory_profile_view import TrajectoryProfileViewRepository
from tests.trajectory_viewer_fixtures import create_campaign


class TrajectoryProfileViewRepositoryTest(unittest.TestCase):
    def test_fallback_uses_public_profiler_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            create_campaign(replays_root)

            profile = TrajectoryProfileViewRepository(
                replays_root
            ).get_campaign_profile("campaign-1")

            self.assertEqual(profile["schema"], "trajectory.profile.v1")
            self.assertIsNone(profile["profile_id"])
            resources = profile["runs"][0]["resources"]
            self.assertEqual(resources["tokens"]["input"], 100)
            self.assertNotIn("total_tokens", resources)

    def test_newest_valid_persisted_profile_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            create_campaign(replays_root)
            persisted = TrajectoryProfiler(replays_root).persist_campaign(
                "campaign-1",
                root / "harness-runs",
            )
            persisted.profile["aggregate"]["duration_ms"]["median"] = 12345
            profile_path = (
                persisted.harness_directory / "trajectory-profile.json"
            )
            profile_path.write_text(
                json.dumps(persisted.profile),
                encoding="utf-8",
            )

            profile = TrajectoryProfileViewRepository(
                replays_root
            ).get_campaign_profile("campaign-1")

            self.assertEqual(
                profile["profile_id"],
                persisted.harness_manifest["harness_run_id"],
            )
            self.assertEqual(
                profile["aggregate"]["duration_ms"]["median"],
                12345,
            )

    def test_legacy_persisted_profile_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            create_campaign(replays_root)
            persisted = TrajectoryProfiler(replays_root).persist_campaign(
                "campaign-1",
                root / "harness-runs",
            )
            harness = persisted.harness_directory
            current_profile = harness / "trajectory-profile.json"
            profile = json.loads(current_profile.read_text(encoding="utf-8"))
            profile["schema"] = "trace.profile.v1"
            legacy_profile = harness / "trace-profile.json"
            legacy_profile.write_text(json.dumps(profile), encoding="utf-8")
            current_profile.unlink()
            manifest_path = harness / "harness.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["outputs"] = {
                "trace_profile": "trace-profile.json"
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            loaded = TrajectoryProfileViewRepository(
                replays_root
            ).get_campaign_profile("campaign-1")

        self.assertEqual(loaded["schema"], "trajectory.profile.v1")
        self.assertEqual(loaded["source_schema"], "trace.profile.v1")

    def test_invalid_or_escaping_persisted_output_falls_back_safely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            create_campaign(replays_root)
            harness = root / "harness-runs" / "bad-harness"
            harness.mkdir(parents=True)
            (harness / "harness.json").write_text(
                json.dumps(
                    {
                        "schema": "harness.run.v1",
                        "status": "completed",
                        "ended_at": "9999-01-01T00:00:00+00:00",
                        "source": {"campaign_id": "campaign-1"},
                        "outputs": {
                            "trajectory_profile": "../outside.json"
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "harness-runs" / "outside.json").write_text(
                json.dumps(
                    {
                        "schema": "trajectory.profile.v1",
                        "source": {"campaign_id": "campaign-1"},
                    }
                ),
                encoding="utf-8",
            )

            profile = TrajectoryProfileViewRepository(
                replays_root
            ).get_campaign_profile("campaign-1")

            self.assertIsNone(profile["profile_id"])
            self.assertEqual(len(profile["runs"]), 1)

    def test_browser_code_uses_skill_execution_api_not_legacy_usage(
        self,
    ) -> None:
        application = (
            Path(__file__).resolve().parents[1]
            / "web"
            / "trajectory-viewer"
            / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/skills", application)
        self.assertIn("detail.execution", application)
        self.assertNotIn("summary.usage", application)


if __name__ == "__main__":
    unittest.main()
