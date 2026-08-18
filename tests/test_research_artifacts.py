"""Tests for immutable references to internal research results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_evolution.research_artifacts import (
    ResearchArtifactError,
    _read_fd_bytes,
    seal_research_result_reference,
    verify_research_result_reference,
)


class ResearchArtifactTests(unittest.TestCase):
    """A stored successful result must remain the exact validated artifact."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run = Path(self.temporary.name) / "agent-run-1"
        self.run.mkdir()
        self.path = self.run / "result.json"
        self.result = {
            "schema": "analysis.multi_trajectory_research.v1",
            "role": "behavior_pattern_analyst",
            "corpus_digest": "a" * 64,
            "baseline_digest": "b" * 64,
        }
        self.path.write_text(
            json.dumps(self.result, sort_keys=True),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seal(self) -> dict[str, object]:
        return seal_research_result_reference(
            result_file=self.path,
            run_directory=self.run,
            result=self.result,
            role="behavior_pattern_analyst",
            agent_run_id="agent-run-1",
            corpus_digest="a" * 64,
            baseline_digest="b" * 64,
        )

    def test_seal_and_verify_binds_file_and_research_identity(self) -> None:
        reference = self._seal()

        verified = verify_research_result_reference(
            reference,
            expected_role="behavior_pattern_analyst",
            expected_agent_run_id="agent-run-1",
            expected_corpus_digest="a" * 64,
            expected_baseline_digest="b" * 64,
        )

        self.assertEqual(verified["sha256"], reference["sha256"])

    def test_tamper_and_delete_are_rejected(self) -> None:
        reference = self._seal()
        self.path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ResearchArtifactError, "digest changed"):
            verify_research_result_reference(
                reference,
                expected_role="behavior_pattern_analyst",
                expected_agent_run_id="agent-run-1",
                expected_corpus_digest="a" * 64,
                expected_baseline_digest="b" * 64,
            )

        self.path.unlink()
        with self.assertRaisesRegex(ResearchArtifactError, "missing"):
            verify_research_result_reference(
                reference,
                expected_role="behavior_pattern_analyst",
                expected_agent_run_id="agent-run-1",
                expected_corpus_digest="a" * 64,
                expected_baseline_digest="b" * 64,
            )

    def test_runtime_result_must_match_saved_file(self) -> None:
        with self.assertRaisesRegex(ResearchArtifactError, "differs"):
            seal_research_result_reference(
                result_file=self.path,
                run_directory=self.run,
                result={**self.result, "role": "resource_efficiency_analyst"},
                role="resource_efficiency_analyst",
                agent_run_id="agent-run-1",
                corpus_digest="a" * 64,
                baseline_digest="b" * 64,
            )

    def test_linked_run_root_is_rejected(self) -> None:
        linked = Path(self.temporary.name) / "linked-run"
        linked.symlink_to(self.run, target_is_directory=True)

        with self.assertRaisesRegex(ResearchArtifactError, "directory is unsafe"):
            seal_research_result_reference(
                result_file=linked / "result.json",
                run_directory=linked,
                result=self.result,
                role="behavior_pattern_analyst",
                agent_run_id="agent-run-1",
                corpus_digest="a" * 64,
                baseline_digest="b" * 64,
            )

    def test_deep_linked_ancestor_cannot_redirect_verification(self) -> None:
        reference = self._seal()
        outside = Path(self.temporary.name) / "outside"
        nested = outside / "nested"
        nested.mkdir(parents=True)
        (nested / "result.json").write_bytes(self.path.read_bytes())
        linked = Path(self.temporary.name) / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        redirected = dict(reference)
        redirected["path"] = str(linked / "nested/result.json")

        with self.assertRaisesRegex(
            ResearchArtifactError,
            "parent contains a symlink|missing or unsafe",
        ):
            verify_research_result_reference(
                redirected,
                expected_role="behavior_pattern_analyst",
                expected_agent_run_id="agent-run-1",
                expected_corpus_digest="a" * 64,
                expected_baseline_digest="b" * 64,
            )

    def test_result_replacement_during_seal_read_is_rejected(self) -> None:
        replacement = self.run / "replacement.json"
        replacement.write_bytes(self.path.read_bytes())
        replaced = False

        def replace_after_read(file_fd):
            nonlocal replaced
            content = _read_fd_bytes(file_fd)
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return content

        with (
            patch(
                "skill_evolution.research_artifacts._read_fd_bytes",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(ResearchArtifactError, "changed while"),
        ):
            self._seal()
        self.assertTrue(replaced)

    def test_result_replacement_during_verify_read_is_rejected(self) -> None:
        reference = self._seal()
        replacement = self.run / "replacement.json"
        replacement.write_bytes(self.path.read_bytes())
        replaced = False

        def replace_after_read(file_fd):
            nonlocal replaced
            content = _read_fd_bytes(file_fd)
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return content

        with (
            patch(
                "skill_evolution.research_artifacts._read_fd_bytes",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(ResearchArtifactError, "changed while"),
        ):
            verify_research_result_reference(
                reference,
                expected_role="behavior_pattern_analyst",
                expected_agent_run_id="agent-run-1",
                expected_corpus_digest="a" * 64,
                expected_baseline_digest="b" * 64,
            )
        self.assertTrue(replaced)


if __name__ == "__main__":
    unittest.main()
