"""Tests for deterministic, persistent replay trajectory profiles."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.trajectory_profiler import (
    HARNESS_MANIFEST_FILENAME,
    PROFILE_FILENAME,
    TrajectoryProfiler,
    _run_cli,
)
from scripts.trajectory_viewer_data import ViewerDataError
from tests.trajectory_viewer_fixtures import create_campaign


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _tool_record(
    run_id: str,
    tool_name: str,
    arguments: dict[str, object],
    *,
    status: str = "succeeded",
) -> dict[str, object]:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": run_id,
        "seq": 0,
        "observed_at": "2026-07-25T00:00:30+00:00",
        "elapsed_ms": 300,
        "source": "pi_rpc",
        "type": "tool_action",
        "payload": {
            "tool_call_id": f"call-{tool_name}",
            "tool_name": tool_name,
            "arguments": arguments,
            "status": status,
            "duration_ms": 25,
        },
    }


def _add_strategy_actions(campaign: Path, run_id: str) -> None:
    trajectory = campaign / "runs" / run_id / "trajectory.jsonl"
    records = [
        json.loads(line)
        for line in trajectory.read_text(encoding="utf-8").splitlines()
    ]
    first_tool = next(
        record for record in records if record["type"] == "tool_action"
    )
    first_tool["payload"].update(
        {
            "tool_name": "write",
            "arguments": {
                "path": str(
                    campaign
                    / "runs"
                    / run_id
                    / "artifacts"
                    / "output.html"
                ),
                "content": "<html>too large</html>",
            },
            "status": "failed",
        }
    )
    insertion = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "trajectory_finished"
    )
    artifacts = campaign / "runs" / run_id / "artifacts"
    actions = [
        _tool_record(
            run_id,
            "write",
            {
                "path": str(artifacts / "generate.py"),
                "content": "open('output.html', 'w').write('first')",
            },
        ),
        _tool_record(
            run_id,
            "bash",
            {"command": "python3 generate.py"},
            status="failed",
        ),
        _tool_record(
            run_id,
            "write",
            {
                "path": str(artifacts / "generate.py"),
                "content": "open('output.html', 'w').write('fixed')",
            },
        ),
        _tool_record(
            run_id,
            "bash",
            {"command": "python3 generate.py"},
        ),
        _tool_record(
            run_id,
            "read",
            {"path": str(artifacts / "output.html")},
        ),
        _tool_record(
            run_id,
            "read",
            {
                "path": str(artifacts / "output.html"),
                "offset": 10,
            },
        ),
        _tool_record(
            run_id,
            "bash",
            {"command": "cat >> output.html <<'EOF'\nchunk\nEOF"},
        ),
        _tool_record(
            run_id,
            "write",
            {
                "path": str(artifacts / "output_part1.html"),
                "content": "part one",
            },
        ),
        _tool_record(
            run_id,
            "bash",
            {
                "command": (
                    "cat output_part1.html output_part2.html > output.html"
                )
            },
        ),
    ]
    records[insertion:insertion] = actions
    for sequence, record in enumerate(records, start=1):
        record["seq"] = sequence
    sealed = next(
        record for record in records if record["type"] == "trajectory_sealed"
    )
    sealed["payload"]["record_count"] = len(records)
    trajectory.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


class TrajectoryProfilerTest(unittest.TestCase):
    def test_profiles_resource_components_and_strategy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            campaign = create_campaign(replays_root)
            _add_strategy_actions(campaign, "run-1")

            profile = TrajectoryProfiler(replays_root).profile_campaign(
                "campaign-1"
            )

            self.assertEqual(profile["schema"], "trajectory.profile.v1")
            self.assertIsNone(profile["profile_id"])
            self.assertEqual(profile["load_status"], "ok")
            run = profile["runs"][0]
            resources = run["resources"]
            self.assertEqual(
                resources["tokens"],
                {
                    "input": 100,
                    "output": 20,
                    "cache_read": 50,
                    "cache_write": 0,
                },
            )
            self.assertNotIn("total_tokens", resources)
            self.assertNotIn("total_tokens", profile["aggregate"])
            self.assertEqual(
                run["strategies"]["first_artifact_write"]["status"],
                "failed",
            )
            counts = run["strategies"]["counts"]
            for category in (
                "temporary_generator_created",
                "generator_executed",
                "chunked_artifact_write",
                "partitioned_artifact_write",
                "artifact_merge",
                "repeated_read",
                "retry_after_failure",
                "rework",
            ):
                self.assertGreater(counts[category], 0)
            generator = next(
                item
                for item in run["strategies"]["sequence"]
                if "temporary_generator_created" in item["categories"]
            )
            self.assertEqual(
                set(generator),
                {
                    "seq",
                    "tool_name",
                    "status",
                    "targets",
                    "categories",
                    "evidence",
                },
            )
            self.assertNotIn("arguments", generator)
            self.assertEqual(
                generator["evidence"]["run_id"],
                "run-1",
            )
            retry = run["strategies"]["retries"][0]
            self.assertLess(retry["failed_seq"], retry["retry_seq"])

    def test_aggregate_reports_cv_and_tukey_outliers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            run_ids = [f"run-{index}" for index in range(1, 6)]
            campaign = create_campaign(
                replays_root,
                run_specs=[{"run_id": run_id} for run_id in run_ids],
            )
            manifest_path = campaign / "replay.json"
            manifest = _read_json(manifest_path)
            durations = [10, 10, 10, 10, 100]
            for run, duration in zip(
                manifest["runs"],
                durations,
                strict=True,
            ):
                run["duration_ms"] = duration
            _write_json(manifest_path, manifest)

            aggregate = TrajectoryProfiler(
                replays_root
            ).profile_campaign("campaign-1")["aggregate"]["duration_ms"]

            self.assertEqual(aggregate["min"], 10)
            self.assertEqual(aggregate["median"], 10)
            self.assertEqual(aggregate["max"], 100)
            self.assertEqual(aggregate["mean"], 28.0)
            self.assertAlmostEqual(
                aggregate["coefficient_of_variation"],
                1.285714285714,
            )
            self.assertEqual(aggregate["outlier_run_ids"], ["run-5"])

    def test_persistence_is_atomic_and_does_not_modify_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            campaign = create_campaign(replays_root)
            trajectory = (
                campaign / "runs" / "run-1" / "trajectory.jsonl"
            )
            before = trajectory.read_bytes()

            result = TrajectoryProfiler(replays_root).persist_campaign(
                "campaign-1",
                root / "harness-runs",
            )

            self.assertEqual(before, trajectory.read_bytes())
            self.assertTrue(
                (result.harness_directory / PROFILE_FILENAME).is_file()
            )
            self.assertTrue(
                (
                    result.harness_directory
                    / HARNESS_MANIFEST_FILENAME
                ).is_file()
            )
            self.assertFalse(
                list(result.harness_directory.glob("*.tmp"))
            )
            saved_profile = _read_json(
                result.harness_directory / PROFILE_FILENAME
            )
            saved_manifest = _read_json(
                result.harness_directory / HARNESS_MANIFEST_FILENAME
            )
            self.assertEqual(
                saved_profile["profile_id"],
                saved_manifest["harness_run_id"],
            )
            self.assertEqual(saved_manifest["status"], "completed")
            self.assertEqual(
                saved_manifest["outputs"]["trajectory_profile"],
                PROFILE_FILENAME,
            )

    def test_missing_trajectory_remains_visible_as_error_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            campaign = create_campaign(replays_root)
            (
                campaign / "runs" / "run-1" / "trajectory.jsonl"
            ).unlink()

            profile = TrajectoryProfiler(replays_root).profile_campaign(
                "campaign-1"
            )

            self.assertEqual(profile["load_status"], "error")
            run = profile["runs"][0]
            self.assertEqual(run["load_status"], "error")
            self.assertEqual(run["strategies"]["sequence"], [])
            self.assertIn(
                "trajectory_missing",
                {issue["code"] for issue in run["issues"]},
            )

    def test_persistence_failure_leaves_actionable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            replays_root.mkdir()
            harness_root = root / "harness-runs"

            with self.assertRaisesRegex(
                ViewerDataError,
                "was not found",
            ):
                TrajectoryProfiler(replays_root).persist_campaign(
                    "missing-campaign",
                    harness_root,
                )

            directories = list(harness_root.iterdir())
            self.assertEqual(len(directories), 1)
            manifest = _read_json(
                directories[0] / HARNESS_MANIFEST_FILENAME
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(
                manifest["error"]["type"],
                "ViewerDataError",
            )
            self.assertIn("was not found", manifest["error"]["message"])

    def test_cli_persists_profile_and_prints_harness_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            create_campaign(replays_root)
            output = io.StringIO()

            with redirect_stdout(output):
                status = _run_cli(
                    [
                        "--replays-root",
                        str(replays_root),
                        "--campaign-id",
                        "campaign-1",
                        "--output-root",
                        str(root / "harness-runs"),
                    ]
                )

            self.assertEqual(status, 0)
            harness_directory = Path(output.getvalue().strip())
            self.assertTrue(
                (harness_directory / PROFILE_FILENAME).is_file()
            )

    def test_script_is_directly_invocable(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "trajectory_profiler.py"
        )

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--campaign-id", result.stdout)
        self.assertIn("--output-root", result.stdout)


if __name__ == "__main__":
    unittest.main()
