"""Append-only result board for independent multi-Trajectory specialists."""

from __future__ import annotations

from collections.abc import Mapping
import os
import re
import threading
from typing import Any

from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AGENT_TERMINAL_STATUSES,
    AgentRole,
    SpecialistRunOutcome,
)
from skill_evolution.research_artifacts import (
    ResearchArtifactError,
    validate_research_result_reference,
    verify_research_result_reference,
)
from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    new_object_id,
    utc_now,
)


SPECIALIST_BOARD_SCHEMA = "analysis.specialist_board.v1"
SPECIALIST_BOARD_STATUSES = frozenset({"incomplete", "complete"})
SPECIALIST_ROLE_STATUSES = frozenset(
    {"not_started", *AGENT_TERMINAL_STATUSES}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResearchBoardError(ValueError):
    """Raised when a specialist board or append operation is invalid."""


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise ResearchBoardError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchBoardError(f"{label} must be non-empty text")
    return value


def _digest(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not _SHA256_PATTERN.fullmatch(text):
        raise ResearchBoardError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _validate_attempt(
    value: Mapping[str, Any],
    *,
    label: str,
    role: AgentRole,
    corpus_digest: str,
    baseline_digest: str,
) -> JsonObject:
    _exact_fields(
        value,
        {
            "attempt_id",
            "agent_run_id",
            "status",
            "result_ref",
            "error",
            "recorded_at",
        },
        label=label,
    )
    attempt_id = _text(value.get("attempt_id"), label=f"{label}.attempt_id")
    agent_run_id = value.get("agent_run_id")
    if agent_run_id is not None:
        agent_run_id = _text(
            agent_run_id,
            label=f"{label}.agent_run_id",
        )
    status = value.get("status")
    if status not in AGENT_TERMINAL_STATUSES:
        raise ResearchBoardError(f"{label}.status is not terminal")
    result_ref = value.get("result_ref")
    error = value.get("error")
    if status == "succeeded":
        if agent_run_id is None:
            raise ResearchBoardError(
                f"{label} succeeded attempt requires agent_run_id"
            )
        if not isinstance(result_ref, Mapping) or not result_ref:
            raise ResearchBoardError(
                f"{label} succeeded attempt requires result_ref"
            )
        if error is not None:
            raise ResearchBoardError(
                f"{label} succeeded attempt cannot contain error"
            )
        try:
            result_ref = validate_research_result_reference(
                result_ref,
                expected_role=role.value,
                expected_agent_run_id=agent_run_id,
                expected_corpus_digest=corpus_digest,
                expected_baseline_digest=baseline_digest,
            )
        except ResearchArtifactError as artifact_error:
            raise ResearchBoardError(str(artifact_error)) from artifact_error
    elif result_ref is not None:
        raise ResearchBoardError(
            f"{label} unsuccessful attempt cannot publish result_ref"
        )
    if error is not None and not isinstance(error, Mapping):
        raise ResearchBoardError(f"{label}.error must be an object or null")
    return {
        "attempt_id": attempt_id,
        "agent_run_id": agent_run_id,
        "status": status,
        "result_ref": dict(result_ref) if isinstance(result_ref, Mapping) else None,
        "error": dict(error) if isinstance(error, Mapping) else None,
        "recorded_at": _text(
            value.get("recorded_at"),
            label=f"{label}.recorded_at",
        ),
    }


def validate_specialist_board(value: Mapping[str, Any]) -> JsonObject:
    """Validate board identity, role state, and append-only attempt shape."""

    _exact_fields(
        value,
        {
            "schema",
            "id",
            "status",
            "corpus_digest",
            "baseline_digest",
            "required_roles",
            "roles",
            "created_at",
            "updated_at",
        },
        label="SpecialistBoard",
    )
    if value.get("schema") != SPECIALIST_BOARD_SCHEMA:
        raise ResearchBoardError("Unsupported specialist board schema")
    board_id = _text(value.get("id"), label="SpecialistBoard.id")
    required_roles = value.get("required_roles")
    expected_roles = [role.value for role in ACTIVE_SPECIALIST_ROLES]
    if required_roles != expected_roles:
        raise ResearchBoardError(
            "SpecialistBoard.required_roles must contain the four active roles"
        )
    role_values = value.get("roles")
    if not isinstance(role_values, list) or len(role_values) != len(
        ACTIVE_SPECIALIST_ROLES
    ):
        raise ResearchBoardError(
            "SpecialistBoard.roles must contain the four active roles"
        )

    corpus_digest = _digest(
        value.get("corpus_digest"),
        label="SpecialistBoard.corpus_digest",
    )
    baseline_digest = _digest(
        value.get("baseline_digest"),
        label="SpecialistBoard.baseline_digest",
    )
    normalized_roles: list[JsonObject] = []
    all_attempt_ids: set[str] = set()
    for index, (role_value, expected_role) in enumerate(
        zip(role_values, ACTIVE_SPECIALIST_ROLES, strict=True)
    ):
        label = f"SpecialistBoard.roles[{index}]"
        if not isinstance(role_value, Mapping):
            raise ResearchBoardError(f"{label} must be an object")
        _exact_fields(
            role_value,
            {"role", "status", "accepted_attempt_id", "attempts"},
            label=label,
        )
        if role_value.get("role") != expected_role.value:
            raise ResearchBoardError(f"{label}.role is out of order")
        attempts_value = role_value.get("attempts")
        if not isinstance(attempts_value, list):
            raise ResearchBoardError(f"{label}.attempts must be a list")
        attempts: list[JsonObject] = []
        for attempt_index, attempt_value in enumerate(attempts_value):
            if not isinstance(attempt_value, Mapping):
                raise ResearchBoardError(
                    f"{label}.attempts[{attempt_index}] must be an object"
                )
            attempt = _validate_attempt(
                attempt_value,
                label=f"{label}.attempts[{attempt_index}]",
                role=expected_role,
                corpus_digest=corpus_digest,
                baseline_digest=baseline_digest,
            )
            if attempt["attempt_id"] in all_attempt_ids:
                raise ResearchBoardError(
                    "SpecialistBoard attempt IDs must be globally unique"
                )
            all_attempt_ids.add(str(attempt["attempt_id"]))
            attempts.append(attempt)

        role_status = role_value.get("status")
        expected_status = attempts[-1]["status"] if attempts else "not_started"
        if role_status != expected_status or role_status not in (
            SPECIALIST_ROLE_STATUSES
        ):
            raise ResearchBoardError(f"{label}.status differs from latest attempt")
        accepted_attempt_id = role_value.get("accepted_attempt_id")
        expected_accepted = (
            attempts[-1]["attempt_id"]
            if attempts and attempts[-1]["status"] == "succeeded"
            else None
        )
        if accepted_attempt_id != expected_accepted:
            raise ResearchBoardError(
                f"{label}.accepted_attempt_id differs from latest attempt"
            )
        if any(
            attempt["status"] == "succeeded"
            for attempt in attempts[:-1]
        ):
            raise ResearchBoardError(
                f"{label} cannot append attempts after success"
            )
        normalized_roles.append(
            {
                "role": expected_role.value,
                "status": role_status,
                "accepted_attempt_id": accepted_attempt_id,
                "attempts": attempts,
            }
        )

    expected_board_status = (
        "complete"
        if all(role["status"] == "succeeded" for role in normalized_roles)
        else "incomplete"
    )
    if value.get("status") != expected_board_status:
        raise ResearchBoardError(
            "SpecialistBoard.status differs from its role states"
        )
    return {
        "schema": SPECIALIST_BOARD_SCHEMA,
        "id": board_id,
        "status": expected_board_status,
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
        "required_roles": expected_roles,
        "roles": normalized_roles,
        "created_at": _text(
            value.get("created_at"),
            label="SpecialistBoard.created_at",
        ),
        "updated_at": _text(
            value.get("updated_at"),
            label="SpecialistBoard.updated_at",
        ),
    }


class SpecialistBoardRepository:
    """Persist one immutable-corpus board with append-only role attempts."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root, manifest_name="board.json")
        self._lock = threading.RLock()

    def create(
        self,
        *,
        corpus_digest: str,
        baseline_digest: str,
        board_id: str | None = None,
    ) -> JsonObject:
        """Create an incomplete board bound to one corpus and baseline."""

        identifier = board_id or new_object_id("specialist-board")
        now = utc_now()
        manifest: JsonObject = {
            "schema": SPECIALIST_BOARD_SCHEMA,
            "id": identifier,
            "status": "incomplete",
            "corpus_digest": _digest(
                corpus_digest,
                label="corpus_digest",
            ),
            "baseline_digest": _digest(
                baseline_digest,
                label="baseline_digest",
            ),
            "required_roles": [
                role.value for role in ACTIVE_SPECIALIST_ROLES
            ],
            "roles": [
                {
                    "role": role.value,
                    "status": "not_started",
                    "accepted_attempt_id": None,
                    "attempts": [],
                }
                for role in ACTIVE_SPECIALIST_ROLES
            ],
            "created_at": now,
            "updated_at": now,
        }
        validate_specialist_board(manifest)
        self.repository.create(identifier, manifest)
        return self.load(identifier)

    def load(self, board_id: str) -> JsonObject:
        """Load and validate one specialist board."""

        board = validate_specialist_board(self.repository.load(board_id))
        for role_state in board["roles"]:
            role = AgentRole(str(role_state["role"]))
            for attempt in role_state["attempts"]:
                if attempt["status"] != "succeeded":
                    continue
                try:
                    verify_research_result_reference(
                        attempt["result_ref"],
                        expected_role=role.value,
                        expected_agent_run_id=str(attempt["agent_run_id"]),
                        expected_corpus_digest=str(board["corpus_digest"]),
                        expected_baseline_digest=str(board["baseline_digest"]),
                    )
                except ResearchArtifactError as artifact_error:
                    raise ResearchBoardError(str(artifact_error)) from artifact_error
        return board

    def append_attempt(
        self,
        board_id: str,
        *,
        corpus_digest: str,
        baseline_digest: str,
        role: AgentRole,
        status: str,
        agent_run_id: str | None,
        result_ref: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> JsonObject:
        """Append one role attempt without replacing any prior attempt."""

        if role not in ACTIVE_SPECIALIST_ROLES:
            raise ResearchBoardError("Attempt role is not an active specialist")
        with self._lock:
            board = self.load(board_id)
            if board["corpus_digest"] != _digest(
                corpus_digest,
                label="corpus_digest",
            ):
                raise ResearchBoardError("Attempt corpus digest differs from board")
            if board["baseline_digest"] != _digest(
                baseline_digest,
                label="baseline_digest",
            ):
                raise ResearchBoardError(
                    "Attempt baseline digest differs from board"
                )
            if board["status"] == "complete":
                raise ResearchBoardError("Complete board does not accept retries")

            role_index = ACTIVE_SPECIALIST_ROLES.index(role)
            role_state = dict(board["roles"][role_index])
            if role_state["status"] == "succeeded":
                raise ResearchBoardError(
                    "Successful specialist role does not accept retries"
                )
            attempt = _validate_attempt(
                {
                    "attempt_id": attempt_id
                    or new_object_id("specialist-attempt"),
                    "agent_run_id": agent_run_id,
                    "status": status,
                    "result_ref": (
                        dict(result_ref) if result_ref is not None else None
                    ),
                    "error": dict(error) if error is not None else None,
                    "recorded_at": utc_now(),
                },
                label="SpecialistAttempt",
                role=role,
                corpus_digest=str(board["corpus_digest"]),
                baseline_digest=str(board["baseline_digest"]),
            )
            existing_ids = {
                saved["attempt_id"]
                for saved_role in board["roles"]
                for saved in saved_role["attempts"]
            }
            if attempt["attempt_id"] in existing_ids:
                raise ResearchBoardError("Attempt ID already exists")

            role_state["attempts"] = [
                *role_state["attempts"],
                attempt,
            ]
            role_state["status"] = status
            role_state["accepted_attempt_id"] = (
                attempt["attempt_id"] if status == "succeeded" else None
            )
            board["roles"][role_index] = role_state
            board["status"] = (
                "complete"
                if all(
                    saved_role["status"] == "succeeded"
                    for saved_role in board["roles"]
                )
                else "incomplete"
            )
            board["updated_at"] = utc_now()
            validated = validate_specialist_board(board)
            self.repository.replace(board_id, validated)
            return self.load(board_id)

    def record_outcome(
        self,
        board_id: str,
        *,
        corpus_digest: str,
        baseline_digest: str,
        outcome: SpecialistRunOutcome,
        result_ref: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> JsonObject:
        """Append one orchestrator outcome, including a framework exception."""

        run = outcome.run
        return self.append_attempt(
            board_id,
            corpus_digest=corpus_digest,
            baseline_digest=baseline_digest,
            role=outcome.role,
            status=outcome.status,
            agent_run_id=run.agent_run_id if run is not None else None,
            result_ref=result_ref,
            error=(
                outcome.exception
                if outcome.exception is not None
                else (run.error if run is not None else None)
            ),
            attempt_id=attempt_id,
        )
