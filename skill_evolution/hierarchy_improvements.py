"""Skill-owned Candidate, cross-revision Comparison, and Review services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import difflib
from pathlib import Path
from typing import Any

from skill_evolution.hierarchy import (
    HierarchyError,
    SkillHierarchyRepository,
)
from skill_evolution.storage import (
    JsonObject,
    atomic_write_json,
    load_json_object,
    new_object_id,
    utc_now,
)


CANDIDATE_SCHEMA = "skill.candidate.v1"
REVISION_COMPARISON_SCHEMA = "skill.revision_comparison.v1"
SKILL_REVIEW_SCHEMA = "skill.review.v1"


class ImprovementError(ValueError):
    """Raised when an improvement escapes its Skill or revision boundary."""


def _text_diff(parent: Path, candidate: Path) -> tuple[list[JsonObject], str]:
    changes: list[JsonObject] = []
    patch: list[str] = []
    parent_files = {
        path.relative_to(parent).as_posix(): path
        for path in parent.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != ".DS_Store"
    }
    candidate_files = {
        path.relative_to(candidate).as_posix(): path
        for path in candidate.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != ".DS_Store"
    }
    for relative in sorted(set(parent_files) | set(candidate_files)):
        before = parent_files.get(relative)
        after = candidate_files.get(relative)
        before_bytes = before.read_bytes() if before else None
        after_bytes = after.read_bytes() if after else None
        if before_bytes == after_bytes:
            continue
        operation = (
            "added"
            if before is None
            else "deleted"
            if after is None
            else "modified"
        )
        is_text = True
        try:
            before_text = (
                before_bytes.decode("utf-8")
                if before_bytes is not None
                else ""
            )
            after_text = (
                after_bytes.decode("utf-8")
                if after_bytes is not None
                else ""
            )
        except UnicodeDecodeError:
            is_text = False
            before_text = after_text = ""
        changes.append(
            {
                "path": relative,
                "operation": operation,
                "content_type": "text" if is_text else "binary",
                "before_bytes": len(before_bytes) if before_bytes is not None else None,
                "after_bytes": len(after_bytes) if after_bytes is not None else None,
            }
        )
        if is_text:
            patch.extend(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        else:
            patch.append(f"Binary file {relative}: {operation}\n")
    return changes, "".join(patch)


class HierarchyImprovementService:
    """Keep the complete improvement lifecycle beneath the subject Skill."""

    def __init__(self, runtime_root: str | Path) -> None:
        self.repository = SkillHierarchyRepository(runtime_root)

    def create_candidate(
        self,
        *,
        skill_id: str,
        parent_revision_id: str,
        candidate_package: str | Path,
        source_analysis_id: str,
        hypothesis: Mapping[str, Any],
        candidate_id: str | None = None,
    ) -> JsonObject:
        """Freeze a complete Candidate as a new immutable Skill Revision."""

        parent = self.repository.load_revision(skill_id, parent_revision_id)
        candidate_source = Path(candidate_package).resolve()
        candidate_revision = self.repository.register_revision(
            candidate_source,
            lifecycle="candidate",
        )
        if candidate_revision.manifest["skill_id"] != skill_id:
            raise ImprovementError("Candidate contract belongs to another Skill")
        if candidate_revision.manifest["revision_id"] == parent_revision_id:
            raise ImprovementError("Candidate package does not change the Skill")
        identifier = candidate_id or new_object_id("candidate")
        directory = self._candidate_directory(skill_id, identifier)
        if directory.exists():
            raise ImprovementError(f"Candidate already exists: {identifier}")
        directory.mkdir(parents=True)
        parent_package = (
            self.repository.revision_directory(skill_id, parent_revision_id)
            / str(parent["package_path"])
        )
        frozen_package = candidate_revision.directory / "package"
        changes, patch = _text_diff(parent_package, frozen_package)
        if not changes:
            raise ImprovementError("Candidate package has no observable changes")
        (directory / "diff.patch").write_text(patch, encoding="utf-8")
        manifest: JsonObject = {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": identifier,
            "skill_id": skill_id,
            "parent_revision_id": parent_revision_id,
            "candidate_revision_id": candidate_revision.manifest["revision_id"],
            "source_analysis_id": source_analysis_id,
            "hypothesis": dict(hypothesis),
            "status": "ready_for_comparison",
            "file_changes": changes,
            "diff_path": "diff.patch",
            "comparison_ids": [],
            "review_ids": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(directory / "candidate.json", manifest)
        return manifest

    def create_comparison(
        self,
        *,
        skill_id: str,
        candidate_id: str,
        task_case_ids: Sequence[str],
        comparison_id: str | None = None,
    ) -> JsonObject:
        """Create an explicit baseline/candidate cross-revision experiment."""

        candidate = self.load_candidate(skill_id, candidate_id)
        if not task_case_ids or any(not item for item in task_case_ids):
            raise ImprovementError("Comparison requires TaskCase IDs")
        identifier = comparison_id or new_object_id("comparison")
        directory = (
            self._candidate_directory(skill_id, candidate_id)
            / "comparisons"
            / identifier
        )
        if directory.exists():
            raise ImprovementError(f"Comparison already exists: {identifier}")
        directory.mkdir(parents=True)
        manifest: JsonObject = {
            "schema": REVISION_COMPARISON_SCHEMA,
            "comparison_id": identifier,
            "candidate_id": candidate_id,
            "skill_id": skill_id,
            "baseline_revision_id": candidate["parent_revision_id"],
            "candidate_revision_id": candidate["candidate_revision_id"],
            "task_case_ids": list(task_case_ids),
            "status": "planned",
            "execution_ids": [],
            "effect": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(directory / "comparison.json", manifest)
        candidate["comparison_ids"].append(identifier)
        candidate["updated_at"] = utc_now()
        atomic_write_json(
            self._candidate_directory(skill_id, candidate_id) / "candidate.json",
            candidate,
        )
        return manifest

    def attach_comparison_execution(
        self,
        *,
        skill_id: str,
        candidate_id: str,
        comparison_id: str,
        execution_id: str,
    ) -> JsonObject:
        """Attach an Execution only when it uses one compared Revision."""

        comparison = self.load_comparison(skill_id, candidate_id, comparison_id)
        execution = self.repository.load_execution(skill_id, execution_id)
        compared = {
            comparison["baseline_revision_id"],
            comparison["candidate_revision_id"],
        }
        if execution["origin"] != "comparison":
            raise ImprovementError("Comparison member must have comparison origin")
        if execution["comparison_id"] != comparison_id:
            raise ImprovementError("Execution references another Comparison")
        if execution["revision_id"] not in compared:
            raise ImprovementError("Execution Revision is outside the Comparison")
        if execution_id not in comparison["execution_ids"]:
            comparison["execution_ids"].append(execution_id)
        comparison["status"] = "running"
        comparison["updated_at"] = utc_now()
        self._write_comparison(skill_id, candidate_id, comparison)
        return comparison

    def record_comparison_effect(
        self,
        *,
        skill_id: str,
        candidate_id: str,
        comparison_id: str,
        classification: str,
        evidence_refs: Sequence[Mapping[str, Any]],
    ) -> JsonObject:
        """Record an evidence-backed cross-revision effect classification."""

        if classification not in {
            "improved",
            "regressed",
            "mixed",
            "inconclusive",
            "not_runnable",
        }:
            raise ImprovementError("Unsupported comparison classification")
        if not evidence_refs:
            raise ImprovementError("Comparison effect requires evidence")
        comparison = self.load_comparison(skill_id, candidate_id, comparison_id)
        comparison["status"] = "completed"
        comparison["effect"] = {
            "classification": classification,
            "evidence_refs": [dict(item) for item in evidence_refs],
            "recorded_at": utc_now(),
        }
        comparison["updated_at"] = utc_now()
        self._write_comparison(skill_id, candidate_id, comparison)
        return comparison

    def create_review(
        self,
        *,
        skill_id: str,
        candidate_id: str,
        comparison_id: str,
        disclosure: Mapping[str, Any],
        evidence_refs: Sequence[Mapping[str, Any]],
        review_id: str | None = None,
    ) -> JsonObject:
        """Create a human-only release decision package beneath a Candidate."""

        self.load_comparison(skill_id, candidate_id, comparison_id)
        if not disclosure or not evidence_refs:
            raise ImprovementError("Review requires disclosure and evidence")
        identifier = review_id or new_object_id("review")
        directory = (
            self._candidate_directory(skill_id, candidate_id)
            / "reviews"
            / identifier
        )
        if directory.exists():
            raise ImprovementError(f"Review already exists: {identifier}")
        directory.mkdir(parents=True)
        manifest: JsonObject = {
            "schema": SKILL_REVIEW_SCHEMA,
            "review_id": identifier,
            "skill_id": skill_id,
            "candidate_id": candidate_id,
            "comparison_id": comparison_id,
            "status": "awaiting_human_approval",
            "disclosure": dict(disclosure),
            "evidence_refs": [dict(item) for item in evidence_refs],
            "decision": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(directory / "review.json", manifest)
        candidate = self.load_candidate(skill_id, candidate_id)
        candidate["review_ids"].append(identifier)
        candidate["updated_at"] = utc_now()
        atomic_write_json(
            self._candidate_directory(skill_id, candidate_id) / "candidate.json",
            candidate,
        )
        return manifest

    def decide_review(
        self,
        *,
        skill_id: str,
        candidate_id: str,
        review_id: str,
        decision: str,
        decided_by: str,
        rationale: str,
    ) -> JsonObject:
        """Record the explicit human release decision."""

        if decision not in {"approved_for_release", "rejected"}:
            raise ImprovementError("Unsupported review decision")
        path = (
            self._candidate_directory(skill_id, candidate_id)
            / "reviews"
            / review_id
            / "review.json"
        )
        review = load_json_object(path)
        if review.get("status") != "awaiting_human_approval":
            raise ImprovementError("Review is no longer awaiting a decision")
        if not decided_by.strip() or not rationale.strip():
            raise ImprovementError(
                "Review decision identity and rationale are required"
            )
        review["status"] = decision
        review["decision"] = {
            "decision": decision,
            "decided_by": decided_by,
            "rationale": rationale,
            "decided_at": utc_now(),
        }
        review["updated_at"] = utc_now()
        atomic_write_json(path, review)
        return review

    def list_improvements(self, skill_id: str) -> list[JsonObject]:
        """List complete Candidate summaries for the Skill Explorer."""

        root = self.repository.skill_directory(skill_id) / "improvements"
        if not root.is_dir():
            return []
        result: list[JsonObject] = []
        for directory in sorted(root.iterdir(), reverse=True):
            path = directory / "candidate.json"
            if directory.is_dir() and not directory.is_symlink() and path.is_file():
                result.append(load_json_object(path))
        return result

    def load_candidate(self, skill_id: str, candidate_id: str) -> JsonObject:
        return load_json_object(
            self._candidate_directory(skill_id, candidate_id) / "candidate.json"
        )

    def load_comparison(
        self, skill_id: str, candidate_id: str, comparison_id: str
    ) -> JsonObject:
        return load_json_object(
            self._candidate_directory(skill_id, candidate_id)
            / "comparisons"
            / comparison_id
            / "comparison.json"
        )

    def _write_comparison(
        self, skill_id: str, candidate_id: str, comparison: Mapping[str, Any]
    ) -> None:
        path = (
            self._candidate_directory(skill_id, candidate_id)
            / "comparisons"
            / str(comparison["comparison_id"])
            / "comparison.json"
        )
        atomic_write_json(path, comparison)

    def _candidate_directory(self, skill_id: str, candidate_id: str) -> Path:
        if not candidate_id or Path(candidate_id).name != candidate_id:
            raise HierarchyError("candidate_id must be a safe identifier")
        return self.repository.skill_directory(skill_id) / "improvements" / candidate_id
