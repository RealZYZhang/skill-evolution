"""Tests for the append-only multi-Trajectory specialist result board."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AgentRole,
    SpecialistRunOutcome,
)
from skill_evolution.research_board import (
    ResearchBoardError,
    SpecialistBoardRepository,
    validate_specialist_board,
)
from skill_evolution.research_artifacts import seal_research_result_reference


CORPUS_DIGEST = hashlib.sha256(b"frozen corpus").hexdigest()
BASELINE_DIGEST = hashlib.sha256(b"deterministic baseline").hexdigest()


def result_ref(
    root: Path,
    role: AgentRole,
    attempt: int = 1,
) -> dict[str, object]:
    """Build one board-local specialist result reference."""

    run_id = f"run-{role.value}-{attempt}"
    run_directory = root / "agent-runs" / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    result = {
        "schema": "analysis.multi_trajectory_research.v1",
        "role": role.value,
        "corpus_digest": CORPUS_DIGEST,
        "baseline_digest": BASELINE_DIGEST,
    }
    path = run_directory / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return seal_research_result_reference(
        result_file=path,
        run_directory=run_directory,
        result=result,
        role=role.value,
        agent_run_id=run_id,
        corpus_digest=CORPUS_DIGEST,
        baseline_digest=BASELINE_DIGEST,
    )


class SpecialistBoardRepositoryTests(unittest.TestCase):
    """Verify corpus binding, failure retention, and completion semantics."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SpecialistBoardRepository(
            Path(self.temporary.name) / "boards"
        )
        self.root = Path(self.temporary.name)
        self.board = self.repository.create(
            board_id="board-1",
            corpus_digest=CORPUS_DIGEST,
            baseline_digest=BASELINE_DIGEST,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _append_success(
        self,
        role: AgentRole,
        *,
        attempt: int = 1,
    ) -> dict[str, object]:
        return self.repository.append_attempt(
            "board-1",
            corpus_digest=CORPUS_DIGEST,
            baseline_digest=BASELINE_DIGEST,
            role=role,
            status="succeeded",
            agent_run_id=f"run-{role.value}-{attempt}",
            result_ref=result_ref(self.root, role, attempt),
            attempt_id=f"attempt-{role.value}-{attempt}",
        )

    def test_new_board_requires_all_four_active_specialists(self) -> None:
        self.assertEqual(self.board["status"], "incomplete")
        self.assertEqual(
            self.board["required_roles"],
            [role.value for role in ACTIVE_SPECIALIST_ROLES],
        )
        self.assertEqual(
            [role["status"] for role in self.board["roles"]],
            ["not_started"] * 4,
        )
        validate_specialist_board(self.board)

    def test_failure_then_retry_keeps_attempt_and_completes_board(self) -> None:
        failed_role = AgentRole.BEHAVIOR_PATTERN
        first = self.repository.append_attempt(
            "board-1",
            corpus_digest=CORPUS_DIGEST,
            baseline_digest=BASELINE_DIGEST,
            role=failed_role,
            status="failed",
            agent_run_id="run-behavior-1",
            error={"type": "FakeFailure", "message": "controlled"},
            attempt_id="attempt-behavior-1",
        )
        self.assertEqual(first["status"], "incomplete")

        for role in ACTIVE_SPECIALIST_ROLES:
            if role is not failed_role:
                self._append_success(role)
        completed = self._append_success(failed_role, attempt=2)

        self.assertEqual(completed["status"], "complete")
        behavior = completed["roles"][0]
        self.assertEqual(
            [attempt["status"] for attempt in behavior["attempts"]],
            ["failed", "succeeded"],
        )
        self.assertEqual(
            behavior["accepted_attempt_id"],
            "attempt-behavior_pattern_analyst-2",
        )

    def test_digest_mismatch_rejects_retry_without_changing_board(self) -> None:
        before = self.repository.load("board-1")

        with self.assertRaisesRegex(ResearchBoardError, "corpus digest"):
            self.repository.append_attempt(
                "board-1",
                corpus_digest="0" * 64,
                baseline_digest=BASELINE_DIGEST,
                role=AgentRole.BEHAVIOR_PATTERN,
                status="failed",
                agent_run_id="run-1",
                attempt_id="attempt-1",
            )

        self.assertEqual(self.repository.load("board-1"), before)

    def test_successful_role_cannot_be_overwritten(self) -> None:
        self._append_success(AgentRole.BEHAVIOR_PATTERN)

        with self.assertRaisesRegex(ResearchBoardError, "does not accept"):
            self.repository.append_attempt(
                "board-1",
                corpus_digest=CORPUS_DIGEST,
                baseline_digest=BASELINE_DIGEST,
                role=AgentRole.BEHAVIOR_PATTERN,
                status="failed",
                agent_run_id="run-behavior-2",
                attempt_id="attempt-behavior-2",
            )

        behavior = self.repository.load("board-1")["roles"][0]
        self.assertEqual(len(behavior["attempts"]), 1)
        self.assertEqual(behavior["status"], "succeeded")

    def test_framework_exception_is_recorded_as_failed_attempt(self) -> None:
        outcome = SpecialistRunOutcome(
            role=AgentRole.CONDITIONS_COVERAGE,
            run=None,
            exception={
                "type": "RuntimeError",
                "message": "runtime did not start",
            },
        )

        updated = self.repository.record_outcome(
            "board-1",
            corpus_digest=CORPUS_DIGEST,
            baseline_digest=BASELINE_DIGEST,
            outcome=outcome,
            attempt_id="attempt-framework-1",
        )

        conditions = updated["roles"][1]
        self.assertEqual(conditions["status"], "failed")
        self.assertIsNone(conditions["attempts"][0]["agent_run_id"])
        self.assertEqual(
            conditions["attempts"][0]["error"]["type"],
            "RuntimeError",
        )

    def test_succeeded_attempt_requires_a_result_reference(self) -> None:
        with self.assertRaisesRegex(ResearchBoardError, "result_ref"):
            self.repository.append_attempt(
                "board-1",
                corpus_digest=CORPUS_DIGEST,
                baseline_digest=BASELINE_DIGEST,
                role=AgentRole.RESOURCE_EFFICIENCY,
                status="succeeded",
                agent_run_id="run-resource-1",
                attempt_id="attempt-resource-1",
            )

    def test_result_tamper_and_deletion_invalidate_the_board(self) -> None:
        self._append_success(AgentRole.BEHAVIOR_PATTERN)
        board = self.repository.load("board-1")
        reference = board["roles"][0]["attempts"][0]["result_ref"]
        path = Path(str(reference["path"]))
        path.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ResearchBoardError, "digest changed"):
            self.repository.load("board-1")

        path.unlink()
        with self.assertRaisesRegex(ResearchBoardError, "missing"):
            self.repository.load("board-1")


if __name__ == "__main__":
    unittest.main()
