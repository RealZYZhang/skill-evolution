"""Tests for atomic manifests, queue recovery, and the runtime layout."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest import mock

from skill_evolution.layout import RuntimeLayout
from skill_evolution.storage import (
    ManifestRepository,
    ObjectIdQueue,
    StorageError,
    atomic_write_json,
)


class AtomicStorageTest(unittest.TestCase):
    def test_atomic_write_leaves_complete_json_and_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "objects" / "manifest.json"

            atomic_write_json(
                target,
                {
                    "schema": "test.object.v1",
                    "nested": {"message": "完整"},
                },
            )

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {
                    "schema": "test.object.v1",
                    "nested": {"message": "完整"},
                },
            )
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")),
                [],
            )

    def test_atomic_write_cleans_up_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifest.json"

            with mock.patch(
                "skill_evolution.storage.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "replace failure"):
                    atomic_write_json(target, {"status": "pending"})

            self.assertFalse(target.exists())
            self.assertEqual(
                list(root.glob(f".{target.name}.*.tmp")),
                [],
            )

    def test_manifest_compare_and_swap_preserves_last_valid_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ManifestRepository(temporary)
            repository.create(
                "job-1",
                {
                    "schema": "test.job.v1",
                    "status": "pending",
                    "attempt": 1,
                },
            )

            running = repository.update(
                "job-1",
                {"status": "running"},
                expected_status="pending",
            )

            self.assertEqual(running["status"], "running")
            with self.assertRaisesRegex(StorageError, "status changed"):
                repository.update(
                    "job-1",
                    {"status": "completed"},
                    expected_status="pending",
                )
            self.assertEqual(
                repository.load("job-1")["status"],
                "running",
            )
            with self.assertRaisesRegex(StorageError, "already exists"):
                repository.create("job-1", {"status": "replacement"})


class QueueRecoveryTest(unittest.TestCase):
    def test_recovery_is_idempotent_and_excludes_terminal_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ManifestRepository(temporary)
            repository.create("a-running", {"status": "running"})
            repository.create("b-complete", {"status": "completed"})
            object_queue = ObjectIdQueue()

            first = object_queue.recover(
                repository,
                terminal_statuses={"completed", "failed"},
            )
            second = object_queue.recover(
                repository,
                terminal_statuses={"completed", "failed"},
            )

            self.assertEqual(first, ["a-running"])
            self.assertEqual(second, ["a-running"])
            self.assertEqual(object_queue.get(timeout=0.01), "a-running")
            with self.assertRaises(queue.Empty):
                object_queue.get(timeout=0.01)

    def test_put_deduplicates_until_an_id_is_consumed(self) -> None:
        object_queue = ObjectIdQueue()

        self.assertTrue(object_queue.put("analysis-1"))
        self.assertFalse(object_queue.put("analysis-1"))
        self.assertEqual(object_queue.get(timeout=0.01), "analysis-1")
        self.assertTrue(object_queue.put("analysis-1"))


class RuntimeLayoutTest(unittest.TestCase):
    def test_ensure_creates_only_canonical_hierarchy_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = RuntimeLayout.from_root(Path(temporary) / "runtime")

            layout.ensure()

            expected = {
                layout.skills,
                layout.migrations,
            }
            self.assertTrue(all(path.is_dir() for path in expected))
            self.assertEqual(
                {path for path in layout.root.rglob("*") if path.is_dir()},
                expected,
            )

    def test_explicit_legacy_ensure_creates_compatibility_stores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = RuntimeLayout.from_root(Path(temporary) / "runtime")

            layout.ensure_legacy()

            expected = {
                layout.replays,
                layout.harness_runs,
                layout.analyses,
                layout.agent_runs,
                layout.experiment_requests,
                layout.candidates,
                layout.comparisons,
                layout.reviews,
            }
            self.assertTrue(all(path.is_dir() for path in expected))
            self.assertEqual(
                {path for path in layout.root.rglob("*") if path.is_dir()},
                expected,
            )


if __name__ == "__main__":
    unittest.main()
