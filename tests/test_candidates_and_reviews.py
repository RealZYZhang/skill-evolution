"""Candidate immutability, diff, visibility, and human-review tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from skill_evolution.candidates import (
    CandidateRepository,
    SkillVersion,
)
from skill_evolution.agents import AgentRole, AgentRunResult
from skill_evolution.comparison import ComparisonRepository
from skill_evolution.reviews import ReviewError, ReviewRepository
from skill_evolution.storage import load_json_object
from skill_evolution.workflows import CandidateWorkflow


def optimization_hypothesis(identifier: str) -> dict[str, object]:
    """Return one valid atomic hypothesis for candidate fixtures."""

    return {
        "schema": "optimization.hypothesis.v1",
        "id": identifier,
        "problem": "The output varies between equivalent runs.",
        "proposed_change": "Specify one stable document component vocabulary.",
        "expected_effect": "Equivalent inputs use the same structure.",
        "protected_dimensions": ["correctness", "capability_coverage"],
        "evidence": [
            {
                "schema": "evidence.ref.v1",
                "run_id": "run-1",
                "seq": 7,
            }
        ],
    }


class CandidateRepositoryTests(unittest.TestCase):
    """Exercise framework-computed changes without touching the parent skill."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.parent = self.root / "active-skill"
        (self.parent / "assets").mkdir(parents=True)
        (self.parent / "SKILL.md").write_text(
            "# Original skill\n\nKeep this fact.\n",
            encoding="utf-8",
        )
        (self.parent / "assets" / "fixture.bin").write_bytes(
            b"\x00\xfforiginal"
        )
        self.parent_version = SkillVersion(
            skill_id="document-visualizer",
            version="1.0.0",
            content_path=self.parent,
        )
        self.repository = CandidateRepository(self.root / "candidates")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_finalize_records_text_and_binary_diff_and_keeps_parent_unchanged(
        self,
    ) -> None:
        """A candidate is a full copy; its edits never leak to the parent."""

        original_text = (self.parent / "SKILL.md").read_bytes()
        original_binary = (
            self.parent / "assets" / "fixture.bin"
        ).read_bytes()
        candidate = self.repository.prepare(
            parent_skill=self.parent_version,
            hypothesis=optimization_hypothesis("hypothesis-1"),
            analysis_campaign_id="analysis-1",
        )

        (candidate.content_path / "SKILL.md").write_text(
            "# Improved skill\n\nKeep this fact.\n",
            encoding="utf-8",
        )
        (candidate.content_path / "assets" / "fixture.bin").write_bytes(
            b"\x00\xffcandidate"
        )
        finalized = self.repository.finalize(candidate.candidate_id)

        self.assertEqual(
            (self.parent / "SKILL.md").read_bytes(),
            original_text,
        )
        self.assertEqual(
            (self.parent / "assets" / "fixture.bin").read_bytes(),
            original_binary,
        )
        self.assertEqual(
            (finalized.content_path / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            "# Improved skill\n\nKeep this fact.\n",
        )
        manifest = load_json_object(finalized.manifest_path)
        changes = {
            item["path"]: item
            for item in manifest["file_changes"]
        }
        self.assertEqual(changes["SKILL.md"]["operation"], "modified")
        self.assertEqual(changes["SKILL.md"]["content_type"], "text")
        self.assertEqual(
            changes["assets/fixture.bin"]["operation"],
            "modified",
        )
        self.assertEqual(
            changes["assets/fixture.bin"]["content_type"],
            "binary",
        )
        diff = finalized.diff_path.read_text(encoding="utf-8")
        self.assertIn("--- a/SKILL.md", diff)
        self.assertIn("+# Improved skill", diff)
        self.assertIn(
            "Binary file assets/fixture.bin: modified",
            diff,
        )

    def test_all_candidates_remain_listed_after_different_outcomes(self) -> None:
        """A failed or gated candidate remains a first-class stored object."""

        first = self.repository.prepare(
            parent_skill=self.parent_version,
            hypothesis=optimization_hypothesis("hypothesis-visible-1"),
            analysis_campaign_id="analysis-1",
        )
        second = self.repository.prepare(
            parent_skill=self.parent_version,
            hypothesis=optimization_hypothesis("hypothesis-visible-2"),
            analysis_campaign_id="analysis-1",
        )
        self.repository.mark_status(
            first.candidate_id,
            status="proposal_failed",
            detail={"reason": "invalid proposer output"},
        )
        self.repository.mark_status(
            second.candidate_id,
            status="awaiting_sandbox",
            detail={"reason": "Docker daemon unavailable"},
        )

        manifests = self.repository.repository.list_manifests()
        by_id = {manifest["id"]: manifest for manifest in manifests}
        self.assertEqual(
            set(by_id),
            {first.candidate_id, second.candidate_id},
        )
        self.assertEqual(
            by_id[first.candidate_id]["status"],
            "proposal_failed",
        )
        self.assertEqual(
            by_id[second.candidate_id]["status"],
            "awaiting_sandbox",
        )

    def test_framework_diff_rejects_proposer_file_claim_mismatch(self) -> None:
        workflow = CandidateWorkflow(
            candidates=self.repository,
            comparisons=ComparisonRepository(self.root / "comparisons"),
        )
        candidate = workflow.prepare_candidate(
            parent_skill=self.parent_version,
            hypothesis=optimization_hypothesis("hypothesis-mismatch"),
            analysis_campaign_id="analysis-1",
        )
        (candidate.content_path / "SKILL.md").write_text(
            "# Updated\n",
            encoding="utf-8",
        )
        proposer = AgentRunResult(
            agent_run_id="agent-run-proposer",
            role=AgentRole.CANDIDATE_PROPOSER,
            status="succeeded",
            result={
                "schema": "candidate.proposal.v1",
                "hypothesis_id": "hypothesis-mismatch",
                "summary": "Changed the entrypoint.",
                "files_touched": ["different.md"],
                "evidence": optimization_hypothesis("e")["evidence"],
            },
            error=None,
            run_directory=self.root / "agent-run",
        )

        proposal = workflow.freeze_after_proposer(
            candidate.candidate_id,
            proposer_run=proposer,
        )

        self.assertEqual(proposal.candidate.status, "proposal_failed")
        manifest = load_json_object(proposal.candidate.manifest_path)
        self.assertEqual(
            manifest["status_detail"]["reason"],
            "files_touched_mismatch",
        )
        self.assertEqual(
            manifest["status_detail"]["framework_diff_files"],
            ["SKILL.md"],
        )

    def test_valid_judge_effect_moves_candidate_to_human_review(self) -> None:
        comparisons = ComparisonRepository(self.root / "comparisons")
        workflow = CandidateWorkflow(
            candidates=self.repository,
            comparisons=comparisons,
        )
        candidate = workflow.prepare_candidate(
            parent_skill=self.parent_version,
            hypothesis=optimization_hypothesis("hypothesis-judge"),
            analysis_campaign_id="analysis-1",
        )
        (candidate.content_path / "SKILL.md").write_text(
            "# Candidate\n",
            encoding="utf-8",
        )
        proposer = AgentRunResult(
            agent_run_id="agent-run-proposer",
            role=AgentRole.CANDIDATE_PROPOSER,
            status="succeeded",
            result={
                "schema": "candidate.proposal.v1",
                "hypothesis_id": "hypothesis-judge",
                "summary": "Changed the entrypoint.",
                "files_touched": ["SKILL.md"],
                "evidence": optimization_hypothesis("e")["evidence"],
            },
            error=None,
            run_directory=self.root / "proposer",
        )
        proposal = workflow.freeze_after_proposer(
            candidate.candidate_id,
            proposer_run=proposer,
        )
        comparison = workflow.create_comparison(
            proposal=proposal,
            triggering_task_case_id="trigger",
            regression_task_case_id="regression",
        )
        comparisons.repository.update(
            str(comparison["id"]),
            {"status": "awaiting_judge"},
            expected_status="planned",
        )
        judge = AgentRunResult(
            agent_run_id="agent-run-judge",
            role=AgentRole.REPLAY_JUDGE,
            status="succeeded",
            result={
                "schema": "test.effect.v1",
                "comparison_id": comparison["id"],
                "candidate_id": candidate.candidate_id,
                "judge_agent_run_id": "agent-run-judge",
                "runnable": True,
                "complete": True,
                "dimensions": {
                    "correctness": "unchanged",
                    "capability_coverage": "unchanged",
                    "token": "improved",
                },
                "protected_dimensions": [],
                "classification": "improved",
                "regressions": [],
                "uncertainties": [],
                "evidence": [
                    {
                        "schema": "evidence.ref.v1",
                        "run_id": "run-1",
                        "seq": 1,
                    }
                ],
            },
            error=None,
            run_directory=self.root / "judge",
        )

        result = workflow.record_judge(
            str(comparison["id"]),
            judge_run=judge,
        )

        self.assertEqual(result.comparison["status"], "completed")
        self.assertEqual(result.candidate.status, "awaiting_human_review")
        self.assertEqual(
            result.comparison["judge_attempts"],
            [
                {
                    "agent_run_id": "agent-run-judge",
                    "status": "succeeded",
                }
            ],
        )
        manifest = load_json_object(result.candidate.manifest_path)
        self.assertEqual(manifest["validation_status"], "improved")


class ReviewRepositoryTests(unittest.TestCase):
    """Verify the mandatory owner-facing disclosure and explicit decision."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ReviewRepository(
            Path(self.temporary.name) / "reviews"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_review_discloses_skill_trajectory_problem_repair_and_rationale(
        self,
    ) -> None:
        """Even a regressed candidate is disclosed before a human decision."""

        review = self.repository.create(
            candidate_id="candidate-1",
            skill_description="A skill that converts documents to HTML.",
            trajectory_description=(
                "An ordered action log with messages, tool calls, and outcomes."
            ),
            discovered_problem="Equivalent runs choose inconsistent layouts.",
            proposed_repair="Constrain the component and token vocabulary.",
            feasibility_explanation=(
                "The diff is runnable, but a protected metric regressed."
            ),
            evidence_refs=[
                {
                    "schema": "evidence.ref.v1",
                    "run_id": "run-1",
                    "seq": 10,
                }
            ],
            diff_path="candidates/candidate-1/diff.patch",
            comparison_id="comparison-1",
            gate_classification="regressed",
        )

        self.assertEqual(review["status"], "awaiting_human_approval")
        self.assertEqual(review["candidate_id"], "candidate-1")
        self.assertEqual(
            set(review["disclosure"]),
            {
                "skill_is",
                "trajectory_looks_like",
                "problem_found",
                "proposed_repair",
                "why_feasible_or_not",
            },
        )
        self.assertEqual(review["gate_classification"], "regressed")
        decided = self.repository.decide(
            review["id"],
            decision="rejected",
            decided_by="project-owner",
            rationale="Correctness protection takes precedence.",
        )
        self.assertEqual(decided["status"], "rejected")
        self.assertEqual(
            decided["decision"]["decided_by"],
            "project-owner",
        )

    def test_review_rejects_missing_evidence_or_disclosure(self) -> None:
        """No candidate can reach review with an incomplete explanation."""

        common = {
            "candidate_id": "candidate-1",
            "skill_description": "Skill",
            "trajectory_description": "Trajectory",
            "discovered_problem": "Problem",
            "proposed_repair": "Repair",
            "feasibility_explanation": "Rationale",
            "diff_path": "diff.patch",
            "comparison_id": None,
            "gate_classification": "inconclusive",
        }
        with self.assertRaisesRegex(ReviewError, "cite evidence"):
            self.repository.create(evidence_refs=[], **common)
        with self.assertRaisesRegex(
            ReviewError,
            "feasibility_explanation",
        ):
            self.repository.create(
                evidence_refs=[{"run_id": "run-1", "seq": 1}],
                **{**common, "feasibility_explanation": " "},
            )


if __name__ == "__main__":
    unittest.main()
