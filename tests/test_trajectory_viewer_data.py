"""Tests for read-only trajectory viewer data normalization."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.trajectory_viewer_data import (
    ReplayRepository,
    ViewerDataError,
)
from tests.trajectory_viewer_fixtures import create_campaign


class ReplayRepositoryTest(unittest.TestCase):
    def test_campaign_setup_metrics_and_tool_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(
                replays_root,
                run_specs=[
                    {
                        "run_id": "run-1",
                        "tool_status": "failed",
                    },
                    {
                        "run_id": "run-2",
                        "tool_status": "succeeded",
                    },
                ],
            )
            repository = ReplayRepository(replays_root)

            listing = repository.list_campaigns()
            self.assertEqual(listing["schema"], "viewer.api.v1")
            self.assertEqual(len(listing["campaigns"]), 1)
            self.assertEqual(listing["campaigns"][0]["load_status"], "ok")

            campaign = repository.get_campaign("campaign-1").to_dict()
            self.assertEqual(campaign["summary"]["run_count"], 2)
            self.assertEqual(
                [run["record_count"] for run in campaign["runs"]],
                [12, 12],
            )
            self.assertEqual(campaign["runs"][0]["turn_count"], 1)
            self.assertEqual(campaign["runs"][0]["message_count"], 3)
            self.assertEqual(campaign["runs"][0]["tool_count"], 1)
            self.assertEqual(campaign["runs"][0]["failed_tool_count"], 1)
            self.assertEqual(
                campaign["runs"][0]["usage"]["reported_cost_total"],
                0.0125,
            )
            self.assertEqual(
                campaign["setup"]["common"]["model"]["id"],
                "model-a",
            )
            self.assertEqual(
                campaign["setup"]["common"]["tools"],
                ["read", "write", "bash"],
            )
            self.assertTrue(
                campaign["setup"]["skill"]["same_across_runs"]
            )
            self.assertTrue(
                campaign["setup"]["input"]["same_across_runs"]
            )
            self.assertTrue(
                all(
                    run["prompt_matches_rendered"]
                    for run in campaign["setup"]["runs"]
                )
            )

            run = repository.get_run("campaign-1", "run-1").to_dict()
            self.assertEqual(
                run["relations"]["call-1"],
                {
                    "assistant_seq": 7,
                    "tool_result_seq": 9,
                    "tool_action_seq": 8,
                },
            )
            turn = next(
                group
                for group in run["timeline"]
                if group["kind"] == "turn"
            )
            self.assertEqual(turn["label"], "Turn 1")
            self.assertEqual(
                [action["seq"] for action in turn["actions"]],
                [5, 6, 7, 8, 9, 10],
            )

    def test_legacy_action_file_is_read_through_trajectory_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            campaign = create_campaign(replays_root)
            run = campaign / "runs/run-1"
            current = run / "trajectory.jsonl"
            records = [
                json.loads(line)
                for line in current.read_text(encoding="utf-8").splitlines()
            ]
            type_aliases = {
                "trace_started": "trajectory_started",
                "trace_finished": "trajectory_finished",
                "trace_sealed": "trajectory_sealed",
            }
            for record in records:
                record["schema"] = "trace.actions.v1"
                record["type"] = type_aliases.get(record["type"], record["type"])
            legacy = run / "trace.jsonl"
            legacy.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            current.unlink()

            detail = ReplayRepository(replays_root).get_run(
                "campaign-1",
                "run-1",
            ).to_dict()

        self.assertEqual(detail["summary"]["load_status"], "ok")
        self.assertTrue(detail["summary"]["sealed"])
        self.assertIn(
            "trajectory_started",
            [item["type"] for item in detail["timeline"][0]["actions"]],
        )

    def test_setup_records_differences_without_hiding_variants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(
                replays_root,
                run_specs=[
                    {
                        "run_id": "run-1",
                        "model_id": "model-a",
                        "skill_content": "skill one\n",
                    },
                    {
                        "run_id": "run-2",
                        "model_id": "model-b",
                        "skill_content": "skill two\n",
                    },
                ],
            )

            campaign = ReplayRepository(replays_root).get_campaign(
                "campaign-1"
            ).to_dict()

            self.assertNotIn("model", campaign["setup"]["common"])
            self.assertEqual(
                [item["field"] for item in campaign["setup"]["differences"]],
                ["model"],
            )
            self.assertFalse(
                campaign["setup"]["skill"]["same_across_runs"]
            )
            self.assertEqual(
                [
                    item["content"]
                    for item in campaign["setup"]["skill"]["variants"]
                ],
                ["skill one\n", "skill two\n"],
            )

    def test_invalid_json_sequence_gap_and_unknown_type_are_visible(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            campaign = create_campaign(replays_root)
            trajectory = (
                campaign / "runs" / "run-1" / "trajectory.jsonl"
            )
            records = [
                json.loads(line)
                for line in trajectory.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            records[9]["type"] = "future_runtime_marker"
            records[0]["schema"] = "trajectory.actions.v2"
            records = [
                record for record in records if record["seq"] != 4
            ]
            trajectory.write_text(
                "{not valid json}\n"
                + "".join(
                    json.dumps(record, ensure_ascii=False) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )

            run = ReplayRepository(replays_root).get_run(
                "campaign-1",
                "run-1",
            ).to_dict()

            self.assertEqual(run["summary"]["load_status"], "partial")
            issue_codes = {issue["code"] for issue in run["issues"]}
            self.assertIn("trajectory_json_invalid", issue_codes)
            self.assertIn("sequence_gap", issue_codes)
            self.assertIn("unsupported_trajectory_schema", issue_codes)
            unknown = [
                action
                for group in run["timeline"]
                for action in group["actions"]
                if action["type"] == "future_runtime_marker"
            ]
            self.assertEqual(len(unknown), 1)
            self.assertFalse(unknown[0]["known_type"])

    def test_hidden_reasoning_is_redacted_without_changing_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            campaign = create_campaign(replays_root)
            trajectory = (
                campaign / "runs" / "run-1" / "trajectory.jsonl"
            )
            original = trajectory.read_text(encoding="utf-8")
            records = [
                json.loads(line) for line in original.splitlines()
            ]
            records[6]["payload"]["message"]["content"].append(
                {
                    "type": "thinking",
                    "thinking": "private model reasoning",
                }
            )
            trajectory.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            saved = trajectory.read_text(encoding="utf-8")

            run = ReplayRepository(replays_root).get_run(
                "campaign-1",
                "run-1",
            ).to_dict()

            assistant = next(
                record
                for record in run["records"]
                if record["seq"] == 7
            )
            self.assertEqual(
                assistant["payload"]["message"]["content"][-1],
                {"type": "thinking", "redacted": True},
            )
            self.assertIn("private model reasoning", saved)

    def test_unreadable_campaign_is_listed_instead_of_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            (replays_root / "broken-campaign").mkdir(parents=True)

            listing = ReplayRepository(replays_root).list_campaigns()

            self.assertEqual(len(listing["campaigns"]), 1)
            campaign = listing["campaigns"][0]
            self.assertEqual(campaign["campaign_id"], "broken-campaign")
            self.assertEqual(campaign["load_status"], "error")
            self.assertEqual(
                campaign["issues"][0]["code"],
                "manifest_missing",
            )

    def test_path_traversal_and_escaping_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replays_root = root / "replays"
            campaign = create_campaign(replays_root)
            manifest_path = campaign / "replay.json"
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["runs"][0]["path"] = "../outside"
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            repository = ReplayRepository(replays_root)

            with self.assertRaisesRegex(
                ViewerDataError,
                "leaves its campaign",
            ):
                repository.get_run_file(
                    "campaign-1",
                    "run-1",
                    "artifact",
                )

            campaign = create_campaign(
                root / "second-replays",
                campaign_id="campaign-2",
            )
            outside = root / "outside.html"
            outside.write_text("<h1>outside</h1>", encoding="utf-8")
            artifact = (
                campaign
                / "runs"
                / "run-1"
                / "artifacts"
                / "output.html"
            )
            artifact.unlink()
            artifact.symlink_to(outside)
            second_repository = ReplayRepository(root / "second-replays")

            with self.assertRaisesRegex(
                ViewerDataError,
                "leaves its campaign",
            ):
                second_repository.get_run_file(
                    "campaign-2",
                    "run-1",
                    "artifact",
                )

    def test_run_files_resolve_from_allow_listed_manifest_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            repository = ReplayRepository(replays_root)

            artifact = repository.get_run_file(
                "campaign-1",
                "run-1",
                "artifact",
            )
            session = repository.get_run_file(
                "campaign-1",
                "run-1",
                "session",
            )

            self.assertEqual(artifact.name, "output.html")
            self.assertEqual(session.name, "pi-session.jsonl")
            with self.assertRaises(ViewerDataError):
                repository.get_run_file(
                    "campaign-1",
                    "run-1",
                    "arbitrary",
                )

    def test_failed_and_missing_trajectories_remain_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            campaign_directory = create_campaign(
                replays_root,
                run_specs=[
                    {"run_id": "run-1", "status": "failed"},
                    {"run_id": "run-2", "status": "failed"},
                ],
            )
            (
                campaign_directory
                / "runs"
                / "run-2"
                / "trajectory.jsonl"
            ).unlink()

            campaign = ReplayRepository(replays_root).get_campaign(
                "campaign-1"
            ).to_dict()

            self.assertEqual(len(campaign["runs"]), 2)
            self.assertEqual(
                [run["status"] for run in campaign["runs"]],
                ["failed", "failed"],
            )
            self.assertEqual(campaign["runs"][0]["load_status"], "ok")
            self.assertEqual(campaign["runs"][1]["load_status"], "error")
            self.assertEqual(
                campaign["runs"][1]["issues"][0]["code"],
                "trajectory_missing",
            )


if __name__ == "__main__":
    unittest.main()
