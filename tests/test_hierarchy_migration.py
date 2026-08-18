"""Tests for auditable planning and cutover into the Skill hierarchy."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.hierarchy_migration import (
    HierarchyMigrationError,
    SkillHierarchyMigration,
)


def _record(
    run_id: str,
    sequence: int,
    record_type: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": run_id,
        "seq": sequence,
        "observed_at": f"2026-07-25T00:00:0{sequence}+00:00",
        "elapsed_ms": sequence,
        "source": "framework",
        "type": record_type,
        "payload": payload,
    }


def _write_legacy_campaign(runtime: Path) -> Path:
    campaign = runtime / "replays" / "campaign-1"
    run = campaign / "runs" / "run-1"
    package = run / "artifacts" / "skill"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: Legacy Skill\ndescription: old\n---\n",
        encoding="utf-8",
    )
    (run / "artifacts" / "input.md").write_text("input\n", encoding="utf-8")
    (run / "artifacts" / "output.html").write_text(
        "<!doctype html><title>result</title>", encoding="utf-8"
    )
    (run / "pi-session.jsonl").write_text("{}\n", encoding="utf-8")
    records = [
        _record(
            "run-1",
            1,
            "trajectory_started",
            {
                "manifest": {
                    "task_case": {"task_case_id": "case-1"},
                    "skill": {"snapshot_path": "artifacts/skill"},
                }
            },
        ),
        _record(
            "run-1",
            2,
            "artifact_registered",
            {
                "artifact_role": "input",
                "artifact": {"path": "artifacts/input.md"},
            },
        ),
        _record(
            "run-1",
            3,
            "artifact_registered",
            {
                "artifact_role": "output",
                "artifact": {"path": "artifacts/output.html"},
            },
        ),
        _record(
            "run-1",
            4,
            "trajectory_finished",
            {
                "outcome": {
                    "status": "succeeded",
                    "started_at": "2026-07-25T00:00:01+00:00",
                    "ended_at": "2026-07-25T00:00:04+00:00",
                    "duration_ms": 3,
                    "session": {"status": "complete"},
                }
            },
        ),
        _record(
            "run-1",
            5,
            "trajectory_sealed",
            {"record_count": 5, "outcome_status": "succeeded"},
        ),
    ]
    (run / "trajectory.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in records),
        encoding="utf-8",
    )
    campaign.mkdir(parents=True, exist_ok=True)
    (campaign / "replay.json").write_text(
        json.dumps(
            {
                "schema": "replay.campaign.v1",
                "campaign_id": "campaign-1",
                "status": "completed",
                "started_at": "2026-07-25T00:00:01+00:00",
                "ended_at": "2026-07-25T00:00:04+00:00",
                "skill": {"source_path": "/repo/skills/legacy-skill"},
                "task": {"task_case_id": "case-1"},
                "execution": {"mode": "sequential"},
                "runs": [
                    {
                        "index": 1,
                        "run_id": "run-1",
                        "path": "runs/run-1",
                        "status": "succeeded",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return run


def _mappings() -> dict[str, object]:
    return {
        "schema": "skill.hierarchy_mapping.v1",
        "title": "fixture",
        "mappings": [
            {
                "skill_id": "legacy-skill",
                "source_suffixes": ["skills/legacy-skill"],
            }
        ],
    }


class HierarchyMigrationTests(unittest.TestCase):
    """Require complete ownership and byte-preserving migration."""

    def test_dry_run_blocks_unowned_exploratory_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            _write_legacy_campaign(runtime)
            (runtime / "spikes" / "spike-1").mkdir(parents=True)
            migration = SkillHierarchyMigration(runtime, _mappings())

            plan = migration.plan(write_manifest=False, migration_id="migration-1")

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["counts"]["executions"], 1)
            self.assertIn("not a complete new Execution", plan["unresolved"][0]["error"])
            with self.assertRaisesRegex(HierarchyMigrationError, "Blocked"):
                migration.apply(plan, confirmation="migration-1")

    def test_apply_preserves_payload_hashes_and_removes_legacy_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            source_run = _write_legacy_campaign(runtime)
            before = (source_run / "trajectory.jsonl").read_bytes()
            migration = SkillHierarchyMigration(runtime, _mappings())
            plan = migration.plan(migration_id="migration-1")

            completed = migration.apply(plan, confirmation="migration-1")

            self.assertEqual(completed["status"], "completed")
            repository = SkillHierarchyRepository(runtime)
            execution = repository.load_execution("legacy-skill", "run-1")
            self.assertEqual(execution["origin"], "replay")
            self.assertEqual(
                execution["legacy"]["campaign_id"], "campaign-1"
            )
            migrated_trajectory = (
                repository.execution_directory("legacy-skill", "run-1")
                / "payload"
                / "trajectory.jsonl"
            )
            self.assertEqual(migrated_trajectory.read_bytes(), before)
            self.assertFalse((runtime / "replays").exists())
            revision = repository.load_revision(
                "legacy-skill", execution["revision_id"]
            )
            self.assertEqual(
                revision["contract"]["status"], "missing_at_execution"
            )
            self.assertTrue(repository.is_cutover_complete())

    def test_complete_standalone_trajectory_becomes_direct_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            source_run = _write_legacy_campaign(runtime)
            standalone = runtime / "trajectories" / "standalone-1"
            package = standalone / "artifacts" / "skill"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_bytes(
                (source_run / "artifacts" / "skill" / "SKILL.md").read_bytes()
            )
            (standalone / "artifacts" / "input.md").write_text(
                "input\n", encoding="utf-8"
            )
            records = [
                _record(
                    "standalone-1",
                    1,
                    "trajectory_started",
                    {
                        "manifest": {
                            "started_at": "2026-07-25T00:00:01+00:00",
                            "task_case": {"task_case_id": "standalone"},
                            "skill": {
                                "source_path": "/repo/skills/legacy-skill"
                            },
                        }
                    },
                ),
                _record(
                    "standalone-1",
                    2,
                    "trajectory_finished",
                    {
                        "outcome": {
                            "status": "succeeded",
                            "started_at": "2026-07-25T00:00:01+00:00",
                            "ended_at": "2026-07-25T00:00:02+00:00",
                            "duration_ms": 1000,
                        }
                    },
                ),
                _record(
                    "standalone-1",
                    3,
                    "trajectory_sealed",
                    {"record_count": 3},
                ),
            ]
            (standalone / "trajectory.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            migration = SkillHierarchyMigration(runtime, _mappings())

            plan = migration.plan(migration_id="migration-standalone")

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["counts"]["executions"], 2)
            migration.apply(plan, confirmation="migration-standalone")
            repository = SkillHierarchyRepository(runtime)
            execution = repository.load_execution(
                "legacy-skill", "standalone-1"
            )
            self.assertEqual(execution["origin"], "direct")
            self.assertIsNone(execution["execution_set_id"])
            self.assertFalse((runtime / "trajectories").exists())

    def test_apply_requires_exact_dry_run_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            _write_legacy_campaign(runtime)
            migration = SkillHierarchyMigration(runtime, _mappings())
            plan = migration.plan(write_manifest=False, migration_id="migration-1")

            with self.assertRaisesRegex(HierarchyMigrationError, "confirmation"):
                migration.apply(plan, confirmation="migration-other")

    def test_interrupted_apply_restores_every_moved_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            source_run = _write_legacy_campaign(runtime)
            before = (source_run / "trajectory.jsonl").read_bytes()
            migration = SkillHierarchyMigration(runtime, _mappings())
            plan = migration.plan(migration_id="migration-1")

            with patch.object(
                migration,
                "_apply_single_analyses",
                side_effect=RuntimeError("simulated interruption"),
            ):
                with self.assertRaisesRegex(
                    HierarchyMigrationError, "rolled back"
                ):
                    migration.apply(plan, confirmation="migration-1")

            restored = (
                runtime
                / "replays"
                / "campaign-1"
                / "runs"
                / "run-1"
                / "trajectory.jsonl"
            )
            self.assertEqual(restored.read_bytes(), before)
            self.assertFalse((runtime / "skills").exists())
            journal = json.loads(
                (
                    runtime
                    / "migrations"
                    / "migration-1"
                    / "manifest.json"
                ).read_text()
            )
            self.assertEqual(journal["status"], "rolled_back")
