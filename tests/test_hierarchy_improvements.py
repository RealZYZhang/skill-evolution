"""Tests for Skill-owned Candidate, Comparison, and Review lifecycles."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.hierarchy_improvements import (
    HierarchyImprovementService,
    ImprovementError,
)
from skill_evolution.storage import utc_now


def _package(root: Path, content: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: Improved Skill\ndescription: fixture\n---\n\n{content}\n",
        encoding="utf-8",
    )
    (root / "skill_contract.json").write_text(
        json.dumps(
            {
                "schema": "skill.contract.v2",
                "skill_id": "improved-skill",
                "version": "1.0.0",
                "status": "approved",
                "owner": "owner",
                "approved_by": "owner",
                "approved_at": "2026-08-09T00:00:00Z",
                "supersedes": None,
                "runtime": {
                    "required_tools": ["filesystem.read"],
                    "allowed_tools": ["filesystem.read"],
                    "allowed_permissions": ["workspace.input.read"],
                    "network": "forbidden",
                    "credentials_in_sandbox": False,
                    "dependencies": [],
                    "assets": [],
                },
                "evaluation": {"suite_refs": ["suite-1"]},
            }
        ),
        encoding="utf-8",
    )
    return root


class HierarchyImprovementTests(unittest.TestCase):
    """Allow explicit cross-revision Comparison but retain human promotion."""

    def test_candidate_comparison_execution_and_review_share_skill_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = _package(root / "parent", "Parent instructions")
            candidate_package = _package(root / "candidate", "Improved instructions")
            repository = SkillHierarchyRepository(root / "runtime")
            parent_revision = repository.register_revision(parent)
            service = HierarchyImprovementService(root / "runtime")
            candidate = service.create_candidate(
                skill_id="improved-skill",
                parent_revision_id=parent_revision.manifest["revision_id"],
                candidate_package=candidate_package,
                source_analysis_id="analysis-1",
                hypothesis={"summary": "Clarify one instruction"},
                candidate_id="candidate-1",
            )
            comparison = service.create_comparison(
                skill_id="improved-skill",
                candidate_id="candidate-1",
                task_case_ids=["case-trigger", "case-regression"],
                comparison_id="comparison-1",
            )
            execution = repository.prepare_execution(
                skill_id="improved-skill",
                revision_id=candidate["candidate_revision_id"],
                origin="comparison",
                comparison_id="comparison-1",
                execution_id="execution-1",
            )
            final = dict(execution.manifest)
            final.update(
                {
                    "status": "succeeded",
                    "ended_at": utc_now(),
                    "duration_ms": 1,
                }
            )
            repository.finalize_execution(
                "improved-skill", "execution-1", final
            )

            attached = service.attach_comparison_execution(
                skill_id="improved-skill",
                candidate_id="candidate-1",
                comparison_id="comparison-1",
                execution_id="execution-1",
            )
            effect = service.record_comparison_effect(
                skill_id="improved-skill",
                candidate_id="candidate-1",
                comparison_id="comparison-1",
                classification="inconclusive",
                evidence_refs=[{"execution_id": "execution-1"}],
            )
            review = service.create_review(
                skill_id="improved-skill",
                candidate_id="candidate-1",
                comparison_id="comparison-1",
                disclosure={"summary": "Owner review required"},
                evidence_refs=[{"comparison_id": "comparison-1"}],
                review_id="review-1",
            )

            self.assertEqual(
                comparison["baseline_revision_id"],
                parent_revision.manifest["revision_id"],
            )
            self.assertEqual(attached["execution_ids"], ["execution-1"])
            self.assertEqual(effect["effect"]["classification"], "inconclusive")
            self.assertEqual(review["status"], "awaiting_human_approval")
            self.assertEqual(
                service.list_improvements("improved-skill")[0]["candidate_id"],
                "candidate-1",
            )

    def test_comparison_rejects_execution_from_unrelated_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = _package(root / "parent", "Parent")
            candidate_package = _package(root / "candidate", "Candidate")
            unrelated_package = _package(root / "unrelated", "Unrelated")
            repository = SkillHierarchyRepository(root / "runtime")
            parent_revision = repository.register_revision(parent)
            service = HierarchyImprovementService(root / "runtime")
            service.create_candidate(
                skill_id="improved-skill",
                parent_revision_id=parent_revision.manifest["revision_id"],
                candidate_package=candidate_package,
                source_analysis_id="analysis-1",
                hypothesis={"summary": "change"},
                candidate_id="candidate-1",
            )
            service.create_comparison(
                skill_id="improved-skill",
                candidate_id="candidate-1",
                task_case_ids=["case-1"],
                comparison_id="comparison-1",
            )
            unrelated = repository.register_revision(
                unrelated_package, lifecycle="historical"
            )
            execution = repository.prepare_execution(
                skill_id="improved-skill",
                revision_id=unrelated.manifest["revision_id"],
                origin="comparison",
                comparison_id="comparison-1",
                execution_id="execution-other",
            )

            with self.assertRaisesRegex(ImprovementError, "outside"):
                service.attach_comparison_execution(
                    skill_id="improved-skill",
                    candidate_id="candidate-1",
                    comparison_id="comparison-1",
                    execution_id=execution.manifest["execution_id"],
                )

