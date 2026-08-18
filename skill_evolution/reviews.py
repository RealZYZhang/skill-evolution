"""Human promotion disclosures and explicit release decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from typing import Any

from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    new_object_id,
    utc_now,
)


REVIEW_PACKAGE_SCHEMA = "review.package.v1"


class ReviewError(ValueError):
    """Raised when a promotion disclosure is incomplete."""


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{field} must not be empty")
    return value.strip()


class ReviewRepository:
    """Keep all candidate disclosures visible through a human decision."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root)

    def create(
        self,
        *,
        candidate_id: str,
        skill_description: str,
        trajectory_description: str,
        discovered_problem: str,
        proposed_repair: str,
        feasibility_explanation: str,
        evidence_refs: Sequence[Mapping[str, Any]],
        diff_path: str,
        comparison_id: str | None,
        gate_classification: str,
    ) -> JsonObject:
        """Create the mandatory owner-facing promotion disclosure."""

        if not evidence_refs:
            raise ReviewError("Review disclosure must cite evidence")
        review_id = new_object_id("review")
        manifest: JsonObject = {
            "schema": REVIEW_PACKAGE_SCHEMA,
            "id": review_id,
            "status": "awaiting_human_approval",
            "candidate_id": candidate_id,
            "disclosure": {
                "skill_is": _required_text(
                    skill_description,
                    "skill_description",
                ),
                "trajectory_looks_like": _required_text(
                    trajectory_description,
                    "trajectory_description",
                ),
                "problem_found": _required_text(
                    discovered_problem,
                    "discovered_problem",
                ),
                "proposed_repair": _required_text(
                    proposed_repair,
                    "proposed_repair",
                ),
                "why_feasible_or_not": _required_text(
                    feasibility_explanation,
                    "feasibility_explanation",
                ),
            },
            "evidence_refs": [dict(item) for item in evidence_refs],
            "diff_path": _required_text(diff_path, "diff_path"),
            "comparison_id": comparison_id,
            "gate_classification": gate_classification,
            "decision": None,
        }
        self.repository.create(review_id, manifest)
        return self.repository.load(review_id)

    def decide(
        self,
        review_id: str,
        *,
        decision: str,
        decided_by: str,
        rationale: str,
    ) -> JsonObject:
        """Approve or reject promotion; automated gates cannot call this."""

        if decision not in {"approved_for_release", "rejected"}:
            raise ReviewError("Unsupported human review decision")
        return self.repository.update(
            review_id,
            {
                "status": decision,
                "decision": {
                    "decision": decision,
                    "decided_by": _required_text(
                        decided_by,
                        "decided_by",
                    ),
                    "decided_at": utc_now(),
                    "rationale": _required_text(rationale, "rationale"),
                },
            },
            expected_status="awaiting_human_approval",
        )
