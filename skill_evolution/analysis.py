"""File-backed analysis campaigns and evidence-request contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any

from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    StorageError,
    load_json_object,
    new_object_id,
    utc_now,
)


ANALYSIS_CAMPAIGN_SCHEMA = "analysis.campaign.v1"
AGENT_RESULT_SCHEMA = "analysis.agent_result.v1"
EXPERIMENT_REQUEST_SCHEMA = "experiment.request.v1"
CAPABILITY_CONTRACT_SCHEMA = "skill.capability_contract.v1"
SKILL_CONTRACT_SCHEMA = "skill.contract.v2"
OPTIMIZATION_HYPOTHESIS_SCHEMA = "optimization.hypothesis.v1"

_CONTRACT_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$"
)
_SEMANTIC_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

REQUEST_PRIORITIES = {
    "harness_measurement": 1,
    "existing_trajectory": 2,
    "replay_experiment": 3,
    "human_evidence": 4,
}
_LEGACY_REQUEST_TYPES = {"existing_trace": "existing_trajectory"}
TERMINAL_CAMPAIGN_STATUSES = {"completed", "inconclusive", "failed"}


class AnalysisContractError(ValueError):
    """Raised when an analysis object violates its public contract."""


def _require_non_empty_text(
    value: Mapping[str, Any],
    field: str,
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise AnalysisContractError(f"{field} must be a non-empty string")
    return item.strip()


def _require_string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    item = value.get(field)
    if not isinstance(item, list) or not all(
        isinstance(element, str) and element.strip() for element in item
    ):
        raise AnalysisContractError(f"{field} must be a list of strings")
    if not item and not allow_empty:
        raise AnalysisContractError(f"{field} must not be empty")
    return [element.strip() for element in item]


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise AnalysisContractError(
            f"{label} fields do not match the schema; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _require_contract_id(value: Mapping[str, Any], field: str) -> str:
    item = _require_non_empty_text(value, field)
    if not _CONTRACT_ID_PATTERN.fullmatch(item):
        raise AnalysisContractError(
            f"{field} must be a lowercase registry identifier"
        )
    return item


def _require_semantic_version(
    value: Mapping[str, Any],
    field: str,
) -> str:
    item = _require_non_empty_text(value, field)
    if not _SEMANTIC_VERSION_PATTERN.fullmatch(item):
        raise AnalysisContractError(f"{field} must use MAJOR.MINOR.PATCH")
    return item


def _require_unique_string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    items = _require_string_list(value, field, allow_empty=allow_empty)
    if len(items) != len(set(items)):
        raise AnalysisContractError(f"{field} must not contain duplicates")
    return items


def _require_registry_ids(
    value: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    items = _require_unique_string_list(
        value,
        field,
        allow_empty=allow_empty,
    )
    if any(not _CONTRACT_ID_PATTERN.fullmatch(item) for item in items):
        raise AnalysisContractError(
            f"{field} entries must be lowercase registry identifiers"
        )
    return items


def _require_aware_timestamp(
    value: Mapping[str, Any],
    field: str,
) -> str:
    item = _require_non_empty_text(value, field)
    try:
        parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
    except ValueError as error:
        raise AnalysisContractError(
            f"{field} must be an ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise AnalysisContractError(f"{field} must include a timezone")
    return item


def validate_skill_contract_document(
    value: Mapping[str, Any],
) -> JsonObject:
    """Validate the accepted thin Skill Contract v2 document."""

    _require_exact_fields(
        value,
        {
            "schema",
            "skill_id",
            "version",
            "status",
            "owner",
            "approved_by",
            "approved_at",
            "supersedes",
            "runtime",
            "evaluation",
        },
        label="Skill Contract",
    )
    if value.get("schema") != SKILL_CONTRACT_SCHEMA:
        raise AnalysisContractError("Unsupported Skill Contract schema")
    skill_id = _require_contract_id(value, "skill_id")
    version = _require_semantic_version(value, "version")
    status = value.get("status")
    if status not in {"proposed", "approved"}:
        raise AnalysisContractError(
            "Skill Contract status must be proposed or approved"
        )
    owner = _require_non_empty_text(value, "owner")
    if status == "approved":
        approved_by: str | None = _require_non_empty_text(
            value,
            "approved_by",
        )
        approved_at: str | None = _require_aware_timestamp(
            value,
            "approved_at",
        )
    else:
        if value.get("approved_by") is not None:
            raise AnalysisContractError(
                "Proposed Skill Contract approved_by must be null"
            )
        if value.get("approved_at") is not None:
            raise AnalysisContractError(
                "Proposed Skill Contract approved_at must be null"
            )
        approved_by = None
        approved_at = None

    raw_predecessor = value.get("supersedes")
    predecessor: JsonObject | None
    if raw_predecessor is None:
        predecessor = None
    elif isinstance(raw_predecessor, Mapping):
        _require_exact_fields(
            raw_predecessor,
            {"schema", "version", "path"},
            label="Skill Contract supersedes",
        )
        predecessor = {
            "schema": _require_non_empty_text(raw_predecessor, "schema"),
            "version": _require_non_empty_text(raw_predecessor, "version"),
            "path": _require_non_empty_text(raw_predecessor, "path"),
        }
    else:
        raise AnalysisContractError(
            "Skill Contract supersedes must be an object or null"
        )

    raw_runtime = value.get("runtime")
    if not isinstance(raw_runtime, Mapping):
        raise AnalysisContractError("runtime must be an object")
    _require_exact_fields(
        raw_runtime,
        {
            "required_tools",
            "allowed_tools",
            "allowed_permissions",
            "network",
            "credentials_in_sandbox",
            "dependencies",
            "assets",
        },
        label="Skill Contract runtime",
    )
    required_tools = _require_registry_ids(
        raw_runtime,
        "required_tools",
    )
    allowed_tools = _require_registry_ids(
        raw_runtime,
        "allowed_tools",
    )
    if not set(required_tools).issubset(allowed_tools):
        raise AnalysisContractError(
            "runtime.required_tools must be a subset of allowed_tools"
        )
    allowed_permissions = _require_registry_ids(
        raw_runtime,
        "allowed_permissions",
    )
    network = raw_runtime.get("network")
    if network not in {
        "forbidden",
        "explicit_approval_required",
        "allowed",
    }:
        raise AnalysisContractError("Unsupported runtime.network policy")
    credentials = raw_runtime.get("credentials_in_sandbox")
    if not isinstance(credentials, bool):
        raise AnalysisContractError(
            "runtime.credentials_in_sandbox must be boolean"
        )
    dependencies = _require_registry_ids(
        raw_runtime,
        "dependencies",
        allow_empty=True,
    )
    assets = _require_registry_ids(
        raw_runtime,
        "assets",
        allow_empty=True,
    )

    raw_evaluation = value.get("evaluation")
    if not isinstance(raw_evaluation, Mapping):
        raise AnalysisContractError("evaluation must be an object")
    _require_exact_fields(
        raw_evaluation,
        {"suite_refs"},
        label="Skill Contract evaluation",
    )
    suite_refs = _require_registry_ids(
        raw_evaluation,
        "suite_refs",
    )

    return {
        "schema": SKILL_CONTRACT_SCHEMA,
        "skill_id": skill_id,
        "version": version,
        "status": status,
        "owner": owner,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "supersedes": predecessor,
        "runtime": {
            "required_tools": required_tools,
            "allowed_tools": allowed_tools,
            "allowed_permissions": allowed_permissions,
            "network": network,
            "credentials_in_sandbox": credentials,
            "dependencies": dependencies,
            "assets": assets,
        },
        "evaluation": {"suite_refs": suite_refs},
    }


def load_approved_skill_contract(
    path: str | os.PathLike[str],
) -> JsonObject:
    """Load the package Skill Contract only after explicit approval."""

    resolved = Path(path).resolve()
    if resolved.name != "skill_contract.json":
        raise AnalysisContractError(
            "The active Skill Contract must be named skill_contract.json"
        )
    try:
        raw_contract = load_json_object(resolved)
    except StorageError as error:
        raise AnalysisContractError(
            f"Skill Contract could not be loaded: {error}"
        ) from error
    contract = validate_skill_contract_document(raw_contract)
    if contract["status"] != "approved":
        raise AnalysisContractError(
            "Skill Contract is not approved by the project owner"
        )
    return contract


def validate_capability_contract(value: Mapping[str, Any]) -> JsonObject:
    """Validate an owner-reviewed skill capability contract."""

    if value.get("schema") != CAPABILITY_CONTRACT_SCHEMA:
        raise AnalysisContractError("Unsupported capability contract schema")
    skill_id = _require_non_empty_text(value, "skill_id")
    version = _require_non_empty_text(value, "version")
    status = value.get("status")
    if status not in {"proposed", "approved"}:
        raise AnalysisContractError(
            "Capability contract status must be proposed or approved"
        )
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise AnalysisContractError("capabilities must be a non-empty list")
    normalized: list[JsonObject] = []
    identifiers: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, Mapping):
            raise AnalysisContractError("Each capability must be an object")
        capability_id = _require_non_empty_text(capability, "id")
        if capability_id in identifiers:
            raise AnalysisContractError(
                f"Duplicate capability id: {capability_id}"
            )
        identifiers.add(capability_id)
        normalized.append(
            {
                "id": capability_id,
                "claim": _require_non_empty_text(capability, "claim"),
                "delivery_modes": _require_string_list(
                    capability,
                    "delivery_modes",
                ),
                "formats": _require_string_list(capability, "formats"),
                "required_evidence": _require_string_list(
                    capability,
                    "required_evidence",
                ),
            }
        )
    approved_by = value.get("approved_by")
    approved_at = value.get("approved_at")
    if status == "approved":
        approved_by = _require_non_empty_text(value, "approved_by")
        approved_at = _require_non_empty_text(value, "approved_at")
    return {
        "schema": CAPABILITY_CONTRACT_SCHEMA,
        "skill_id": skill_id,
        "version": version,
        "status": status,
        "capabilities": normalized,
        "approved_by": approved_by,
        "approved_at": approved_at,
    }


def load_approved_capability_contract(
    path: str | os.PathLike[str],
) -> JsonObject:
    """Load a capability contract only after explicit owner approval."""

    contract = validate_capability_contract(
        load_json_object(Path(path).resolve())
    )
    if contract["status"] != "approved":
        raise AnalysisContractError(
            "Capability contract is not approved by the project owner"
        )
    return contract


def validate_agent_result(value: Mapping[str, Any]) -> JsonObject:
    """Validate the common structured output returned by an analysis agent."""

    if value.get("schema") != AGENT_RESULT_SCHEMA:
        raise AnalysisContractError("Unsupported agent-result schema")
    role = _require_non_empty_text(value, "role")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise AnalysisContractError("findings must be a list")
    normalized_findings: list[JsonObject] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise AnalysisContractError("Each finding must be an object")
        confidence = finding.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(
            confidence, bool
        ):
            raise AnalysisContractError("finding confidence must be numeric")
        if not 0 <= float(confidence) <= 1:
            raise AnalysisContractError(
                "finding confidence must be between 0 and 1"
            )
        evidence = finding.get("evidence")
        counterevidence = finding.get("counterevidence", [])
        if not isinstance(evidence, list) or not evidence:
            raise AnalysisContractError(
                "Every finding must cite at least one evidence reference"
            )
        if not isinstance(counterevidence, list):
            raise AnalysisContractError("counterevidence must be a list")
        normalized_findings.append(
            {
                "id": _require_non_empty_text(finding, "id"),
                "claim": _require_non_empty_text(finding, "claim"),
                "confidence": float(confidence),
                "evidence": evidence,
                "counterevidence": counterevidence,
                "optimization_point": finding.get("optimization_point"),
            }
        )
    requests = value.get("evidence_requests", [])
    if not isinstance(requests, list):
        raise AnalysisContractError("evidence_requests must be a list")
    hypotheses = value.get("optimization_hypotheses", [])
    if not isinstance(hypotheses, list):
        raise AnalysisContractError("optimization_hypotheses must be a list")
    return {
        "schema": AGENT_RESULT_SCHEMA,
        "role": role,
        "findings": normalized_findings,
        "evidence_requests": requests,
        "optimization_hypotheses": hypotheses,
        "missing_roles": value.get("missing_roles", []),
        "limitations": value.get("limitations", []),
    }


def validate_optimization_hypothesis(
    value: Mapping[str, Any],
) -> JsonObject:
    """Validate one evidence-backed and atomic optimization hypothesis."""

    if value.get("schema") != OPTIMIZATION_HYPOTHESIS_SCHEMA:
        raise AnalysisContractError("Unsupported optimization hypothesis schema")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise AnalysisContractError("Hypothesis must cite evidence")
    protected = _require_string_list(
        value,
        "protected_dimensions",
        allow_empty=True,
    )
    return {
        "schema": OPTIMIZATION_HYPOTHESIS_SCHEMA,
        "id": _require_non_empty_text(value, "id"),
        "problem": _require_non_empty_text(value, "problem"),
        "proposed_change": _require_non_empty_text(
            value,
            "proposed_change",
        ),
        "expected_effect": _require_non_empty_text(
            value,
            "expected_effect",
        ),
        "protected_dimensions": protected,
        "evidence": list(evidence),
        "atomic": True,
    }


def validate_experiment_request(value: Mapping[str, Any]) -> JsonObject:
    """Validate and normalize one request for additional evidence."""

    if value.get("schema") != EXPERIMENT_REQUEST_SCHEMA:
        raise AnalysisContractError("Unsupported experiment request schema")
    raw_request_type = value.get("request_type")
    request_type = _LEGACY_REQUEST_TYPES.get(
        raw_request_type,
        raw_request_type,
    )
    if request_type not in REQUEST_PRIORITIES:
        raise AnalysisContractError(
            f"Unsupported evidence request type: {request_type!r}"
        )
    normalized: JsonObject = {
        "schema": EXPERIMENT_REQUEST_SCHEMA,
        "request_type": request_type,
        "hypothesis": _require_non_empty_text(value, "hypothesis"),
        "why_existing_evidence_is_insufficient": _require_non_empty_text(
            value,
            "why_existing_evidence_is_insufficient",
        ),
        "supports_if": _require_non_empty_text(value, "supports_if"),
        "refutes_if": _require_non_empty_text(value, "refutes_if"),
        "priority": REQUEST_PRIORITIES[request_type],
    }
    if request_type == "harness_measurement":
        normalized["measurements"] = _require_string_list(
            value,
            "measurements",
        )
    elif request_type == "existing_trajectory":
        normalized["selection"] = _require_non_empty_text(value, "selection")
    elif request_type == "human_evidence":
        normalized["question"] = _require_non_empty_text(value, "question")
    else:
        changed = _require_string_list(value, "changed_variables")
        held = _require_string_list(value, "held_constant_variables")
        task_cases = _require_string_list(value, "task_case_ids")
        harnesses = _require_string_list(value, "required_harnesses")
        replay_count = value.get("replay_count")
        if not isinstance(replay_count, int) or replay_count <= 0:
            raise AnalysisContractError(
                "replay_count must be a positive integer"
            )
        budget = value.get("budget")
        if not isinstance(budget, Mapping):
            raise AnalysisContractError("budget must be an object")
        normalized.update(
            {
                "changed_variables": changed,
                "held_constant_variables": held,
                "task_case_ids": task_cases,
                "formats": _require_string_list(value, "formats"),
                "skill_version": _require_non_empty_text(
                    value,
                    "skill_version",
                ),
                "runtime": dict(value.get("runtime", {})),
                "replay_count": replay_count,
                "required_harnesses": harnesses,
                "budget": dict(budget),
            }
        )
    return normalized


class ExperimentRequestRepository:
    """Persist evidence requests and explicit owner approvals."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root)

    def create(
        self,
        *,
        campaign_id: str,
        round_number: int,
        request: Mapping[str, Any],
    ) -> JsonObject:
        """Create one proposed request; no replay is started by this action."""

        normalized = validate_experiment_request(request)
        request_id = new_object_id("request")
        manifest: JsonObject = {
            **normalized,
            "id": request_id,
            "campaign_id": campaign_id,
            "round": round_number,
            "status": "proposed",
            "approval": None,
            "result_refs": [],
        }
        self.repository.create(request_id, manifest)
        return self.repository.load(request_id)

    def approve(
        self,
        request_id: str,
        *,
        approved_by: str,
    ) -> JsonObject:
        """Record explicit owner approval without executing the request."""

        if not approved_by.strip():
            raise AnalysisContractError("approved_by must not be empty")
        return self.repository.update(
            request_id,
            {
                "status": "approved",
                "approval": {
                    "approved_by": approved_by.strip(),
                    "approved_at": utc_now(),
                },
            },
            expected_status="proposed",
        )

    def begin(self, request_id: str) -> JsonObject:
        """Move an approved request to running."""

        return self.repository.update(
            request_id,
            {"status": "running", "started_at": utc_now()},
            expected_status="approved",
        )

    def finish(
        self,
        request_id: str,
        *,
        result_refs: Sequence[Mapping[str, Any]],
        status: str = "completed",
    ) -> JsonObject:
        """Seal request results while keeping every referenced attempt visible."""

        if status not in {"completed", "failed", "inconclusive"}:
            raise AnalysisContractError("Invalid terminal request status")
        return self.repository.update(
            request_id,
            {
                "status": status,
                "ended_at": utc_now(),
                "result_refs": [dict(item) for item in result_refs],
            },
            expected_status="running",
        )


class AnalysisCampaignRepository:
    """Manage at most three file-backed rounds for one frozen evidence batch."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_rounds: int = 3,
    ) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        self.repository = ManifestRepository(root)
        self.max_rounds = max_rounds

    def create(
        self,
        *,
        replay_campaign_id: str,
        evidence_bundle: str,
        harness_versions: Mapping[str, str],
    ) -> JsonObject:
        """Create a campaign bound to one immutable replay and harness snapshot."""

        if not replay_campaign_id:
            raise AnalysisContractError("replay_campaign_id must not be empty")
        campaign_id = new_object_id("analysis")
        manifest: JsonObject = {
            "schema": ANALYSIS_CAMPAIGN_SCHEMA,
            "id": campaign_id,
            "status": "ready",
            "replay_campaign_id": replay_campaign_id,
            "evidence_bundle": evidence_bundle,
            "harness_versions": dict(harness_versions),
            "max_rounds": self.max_rounds,
            "rounds": [],
            "conclusion": None,
        }
        self.repository.create(campaign_id, manifest)
        return self.repository.load(campaign_id)

    def start_round(self, campaign_id: str) -> JsonObject:
        """Start a fresh-session analysis round."""

        manifest = self.repository.load(campaign_id)
        if manifest.get("status") not in {"ready", "evidence_ready"}:
            raise StorageError(
                f"Campaign cannot start a round from {manifest.get('status')}"
            )
        rounds = list(manifest.get("rounds", []))
        if len(rounds) >= self.max_rounds:
            return self.repository.update(
                campaign_id,
                {
                    "status": "inconclusive",
                    "conclusion": {
                        "reason": "maximum_analysis_rounds_reached",
                    },
                },
                expected_status=manifest["status"],
            )
        rounds.append(
            {
                "round": len(rounds) + 1,
                "status": "running",
                "started_at": utc_now(),
                "specialist_runs": [],
                "synthesis_run": None,
                "request_ids": [],
            }
        )
        return self.repository.update(
            campaign_id,
            {"status": "analyzing", "rounds": rounds},
            expected_status=manifest["status"],
        )

    def record_specialists(
        self,
        campaign_id: str,
        agent_run_ids: Sequence[str],
    ) -> JsonObject:
        """Record all specialist attempts, including failures."""

        manifest = self.repository.load(campaign_id)
        if manifest.get("status") != "analyzing":
            raise StorageError("Campaign is not analyzing")
        rounds = list(manifest.get("rounds", []))
        current = dict(rounds[-1])
        current["specialist_runs"] = list(agent_run_ids)
        rounds[-1] = current
        return self.repository.update(
            campaign_id,
            {"rounds": rounds},
            expected_status="analyzing",
        )

    def finish_round(
        self,
        campaign_id: str,
        *,
        synthesis_run_id: str,
        synthesis_result: Mapping[str, Any] | None,
        request_ids: Sequence[str] = (),
        failed: bool = False,
    ) -> JsonObject:
        """Finish one round and choose a framework state from structured output."""

        manifest = self.repository.load(campaign_id)
        if manifest.get("status") != "analyzing":
            raise StorageError("Campaign is not analyzing")
        rounds = list(manifest.get("rounds", []))
        current = dict(rounds[-1])
        current["synthesis_run"] = synthesis_run_id
        current["request_ids"] = list(request_ids)
        current["ended_at"] = utc_now()

        if failed or synthesis_result is None:
            current["status"] = "failed"
            status = "failed"
            conclusion: JsonObject | None = {
                "reason": "synthesis_agent_failed",
            }
        elif request_ids:
            current["status"] = "awaiting_evidence"
            if len(rounds) >= self.max_rounds:
                status = "inconclusive"
                conclusion = {
                    "reason": "maximum_analysis_rounds_reached",
                    "last_synthesis": dict(synthesis_result),
                }
            else:
                status = "awaiting_evidence"
                conclusion = None
        else:
            current["status"] = "completed"
            status = "completed"
            conclusion = dict(synthesis_result)
        rounds[-1] = current
        return self.repository.update(
            campaign_id,
            {
                "status": status,
                "rounds": rounds,
                "conclusion": conclusion,
            },
            expected_status="analyzing",
        )

    def mark_evidence_ready(
        self,
        campaign_id: str,
        *,
        evidence_refs: Sequence[Mapping[str, Any]],
    ) -> JsonObject:
        """Attach approved evidence results and allow the next fresh round."""

        manifest = self.repository.load(campaign_id)
        rounds = list(manifest.get("rounds", []))
        if not rounds:
            raise StorageError("Campaign has no analysis round")
        current = dict(rounds[-1])
        current["new_evidence_refs"] = [
            dict(reference) for reference in evidence_refs
        ]
        current["status"] = "evidence_ready"
        rounds[-1] = current
        return self.repository.update(
            campaign_id,
            {"status": "evidence_ready", "rounds": rounds},
            expected_status="awaiting_evidence",
        )
