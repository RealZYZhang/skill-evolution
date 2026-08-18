"""Atomic candidate skills with framework-computed diffs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import shutil
from typing import Any

from skill_evolution.analysis import validate_optimization_hypothesis
from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    StorageError,
    new_object_id,
    utc_now,
)


CANDIDATE_SKILL_SCHEMA = "candidate.skill.v1"


class CandidateError(ValueError):
    """Raised when a candidate is empty, unsafe, or not reproducible."""


@dataclass(frozen=True)
class SkillVersion:
    """A complete executable skill version stored in one directory."""

    skill_id: str
    version: str
    content_path: Path


@dataclass(frozen=True)
class CandidateSkill(SkillVersion):
    """A SkillVersion derived from exactly one optimization hypothesis."""

    candidate_id: str
    parent_version: str
    parent_snapshot_path: Path
    diff_path: Path
    manifest_path: Path
    status: str


def _walk_regular_files(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise CandidateError(f"Skill directory does not exist: {root}")
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CandidateError(f"Skill may not contain symlinks: {path}")
        if path.is_file() and path.name != ".DS_Store":
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _compute_changes(
    parent: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
) -> tuple[list[JsonObject], str]:
    changes: list[JsonObject] = []
    patches: list[str] = []
    for relative_path in sorted(set(parent) | set(candidate)):
        before = parent.get(relative_path)
        after = candidate.get(relative_path)
        if before == after:
            continue
        if before is None:
            operation = "added"
        elif after is None:
            operation = "deleted"
        else:
            operation = "modified"
        binary = not (
            (before is None or _is_utf8(before))
            and (after is None or _is_utf8(after))
        )
        change: JsonObject = {
            "path": relative_path,
            "operation": operation,
            "content_type": "binary" if binary else "text",
            "before_bytes": len(before) if before is not None else None,
            "after_bytes": len(after) if after is not None else None,
        }
        changes.append(change)
        if binary:
            patches.append(
                f"Binary file {relative_path}: {operation} "
                f"({change['before_bytes']} -> {change['after_bytes']} bytes)\n"
            )
            continue
        before_lines = (
            before.decode("utf-8").splitlines(keepends=True)
            if before is not None
            else []
        )
        after_lines = (
            after.decode("utf-8").splitlines(keepends=True)
            if after is not None
            else []
        )
        patches.extend(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )
    return changes, "".join(patches)


def _copy_skill(source: Path, destination: Path) -> None:
    _walk_regular_files(source)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".DS_Store"),
    )


class CandidateRepository:
    """Prepare and finalize candidates without modifying the active skill."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root)

    def prepare(
        self,
        *,
        parent_skill: SkillVersion,
        hypothesis: Mapping[str, Any],
        analysis_campaign_id: str,
    ) -> CandidateSkill:
        """Create isolated parent and editable workspace copies."""

        normalized_hypothesis = validate_optimization_hypothesis(hypothesis)
        candidate_id = new_object_id("candidate")
        directory = self.repository.object_directory(candidate_id)
        parent_snapshot = directory / "parent-snapshot"
        workspace = directory / "workspace"
        directory.mkdir(parents=True)
        try:
            _copy_skill(parent_skill.content_path.resolve(), parent_snapshot)
            _copy_skill(parent_skill.content_path.resolve(), workspace)
            parent_inventory = _walk_regular_files(
                parent_skill.content_path.resolve()
            )
            manifest: JsonObject = {
                "schema": CANDIDATE_SKILL_SCHEMA,
                "id": candidate_id,
                "status": "drafting",
                "skill_id": parent_skill.skill_id,
                "version": f"{parent_skill.version}+{candidate_id}",
                "parent_version": parent_skill.version,
                "parent_source_path": str(
                    parent_skill.content_path.resolve()
                ),
                "parent_snapshot_path": "parent-snapshot",
                "workspace_path": "workspace",
                "content_path": None,
                "diff_path": None,
                "file_changes": [],
                "source_hypothesis": normalized_hypothesis,
                "analysis_campaign_id": analysis_campaign_id,
                "validation_status": "not_tested",
                "attempts": [],
                "parent_inventory": {
                    path: len(content)
                    for path, content in parent_inventory.items()
                },
            }
            self.repository.create(candidate_id, manifest)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self.load(candidate_id)

    def finalize(self, candidate_id: str) -> CandidateSkill:
        """Compute the authoritative diff and freeze complete candidate content."""

        manifest = self.repository.load(candidate_id)
        if manifest.get("status") != "drafting":
            raise StorageError("Only a drafting candidate can be finalized")
        directory = self.repository.object_directory(candidate_id)
        parent_snapshot = directory / "parent-snapshot"
        workspace = directory / "workspace"
        parent_files = _walk_regular_files(parent_snapshot)
        workspace_files = _walk_regular_files(workspace)
        if "SKILL.md" not in workspace_files:
            raise CandidateError("Candidate must contain SKILL.md")
        changes, unified_diff = _compute_changes(
            parent_files,
            workspace_files,
        )
        if not changes:
            raise CandidateError("Candidate has no changes")

        source_parent = Path(
            str(manifest["parent_source_path"])
        ).resolve()
        if _walk_regular_files(source_parent) != parent_files:
            raise CandidateError(
                "Active parent skill changed while candidate was being drafted"
            )

        content_path = directory / "content"
        if content_path.exists():
            raise CandidateError("Candidate content is already frozen")
        _copy_skill(workspace, content_path)
        diff_path = directory / "diff.patch"
        diff_path.write_text(unified_diff, encoding="utf-8")
        self.repository.update(
            candidate_id,
            {
                "status": "ready_for_smoke",
                "content_path": "content",
                "diff_path": "diff.patch",
                "file_changes": changes,
                "finalized_at": utc_now(),
            },
            expected_status="drafting",
        )
        return self.load(candidate_id)

    def mark_status(
        self,
        candidate_id: str,
        *,
        status: str,
        detail: Mapping[str, Any] | None = None,
    ) -> CandidateSkill:
        """Record a visible pipeline state without deleting the candidate."""

        allowed = {
            "proposal_failed",
            "awaiting_sandbox",
            "not_runnable",
            "awaiting_human_review",
        }
        if status not in allowed:
            raise CandidateError(f"Unsupported candidate status: {status}")
        manifest = self.repository.load(candidate_id)
        self.repository.update(
            candidate_id,
            {
                "status": status,
                "status_detail": (
                    dict(detail) if detail is not None else None
                ),
            },
            expected_status=str(manifest["status"]),
        )
        return self.load(candidate_id)

    def record_validation(
        self,
        candidate_id: str,
        *,
        comparison_id: str,
        classification: str,
    ) -> CandidateSkill:
        """Attach a visible comparison result without deleting the candidate."""

        manifest = self.repository.load(candidate_id)
        attempts = list(manifest.get("attempts", []))
        attempts.append(
            {
                "comparison_id": comparison_id,
                "classification": classification,
                "recorded_at": utc_now(),
            }
        )
        self.repository.update(
            candidate_id,
            {
                "validation_status": classification,
                "attempts": attempts,
                "status": "awaiting_human_review",
            },
            expected_status={
                "ready_for_smoke",
                "awaiting_sandbox",
                "not_runnable",
                "awaiting_human_review",
            },
        )
        return self.load(candidate_id)

    def load(self, candidate_id: str) -> CandidateSkill:
        """Load a candidate domain object from its manifest."""

        manifest = self.repository.load(candidate_id)
        directory = self.repository.object_directory(candidate_id)
        return CandidateSkill(
            skill_id=str(manifest["skill_id"]),
            version=str(manifest["version"]),
            content_path=(
                directory / str(manifest.get("content_path") or "workspace")
            ),
            candidate_id=candidate_id,
            parent_version=str(manifest["parent_version"]),
            parent_snapshot_path=directory / "parent-snapshot",
            diff_path=directory / str(manifest.get("diff_path") or "diff.patch"),
            manifest_path=self.repository.manifest_path(candidate_id),
            status=str(manifest["status"]),
        )
