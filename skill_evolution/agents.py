"""Role contracts and orchestration for independent analysis agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shutil
from typing import Any, Protocol

from skill_evolution.analysis import validate_agent_result
from skill_evolution.config import load_project_configuration
from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    new_object_id,
    utc_now,
)


AGENT_RUN_SCHEMA = "analysis.agent_run.v1"
AGENT_TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "invalid_output",
    "timed_out",
    "indeterminate",
}


class AgentRole(str, Enum):
    """Fixed MVP roles with separate responsibilities and prompts."""

    BEHAVIOR_PATTERN = "behavior_pattern_analyst"
    CONDITIONS_COVERAGE = "conditions_coverage_analyst"
    OUTCOME_CONSISTENCY = "outcome_consistency_analyst"
    CAPABILITY_COVERAGE = "capability_coverage_analyst"
    RESOURCE_EFFICIENCY = "resource_efficiency_analyst"
    SYNTHESIS = "synthesis_agent"
    CANDIDATE_PROPOSER = "candidate_proposer"
    REPLAY_JUDGE = "replay_judge"
    TRAJECTORY_ERROR_ANALYST = "trajectory_error_analyst"
    ERROR_IDENTIFIER = "error_identifier"
    ERROR_ANALYST = "error_analyst"


LEGACY_SPECIALIST_ROLES = (
    AgentRole.OUTCOME_CONSISTENCY,
    AgentRole.CAPABILITY_COVERAGE,
    AgentRole.RESOURCE_EFFICIENCY,
)

SPECIALIST_ROLES = (
    AgentRole.BEHAVIOR_PATTERN,
    AgentRole.CONDITIONS_COVERAGE,
    AgentRole.OUTCOME_CONSISTENCY,
    AgentRole.RESOURCE_EFFICIENCY,
)

# Make the product meaning explicit while retaining the original import name.
ACTIVE_SPECIALIST_ROLES = SPECIALIST_ROLES


class AgentOrchestrationError(RuntimeError):
    """Raised when the framework cannot safely schedule an agent."""


@dataclass(frozen=True)
class ModelConfiguration:
    """Explicit model configuration recorded for each AgentRun."""

    provider: str
    model: str
    thinking: str

    @classmethod
    def from_project_configuration(cls) -> ModelConfiguration:
        """Create the default model boundary from root ``config.yaml``."""

        settings = load_project_configuration().pi_agent
        return cls(
            provider=settings.provider,
            model=settings.model,
            thinking=settings.thinking,
        )

    def to_dict(self) -> JsonObject:
        """Serialize the model boundary."""

        return {
            "provider": self.provider,
            "model": self.model,
            "thinking": self.thinking,
        }


@dataclass(frozen=True)
class AgentSpec:
    """One role's reviewed prompt, tools, and research-lab contract."""

    role: AgentRole
    prompt_path: Path
    tool_mode: str = "read_only"
    timeout_seconds: float = 900.0
    research_lab_profile: str | None = None
    submission_tool: str | None = None

    def __post_init__(self) -> None:
        if self.tool_mode not in {"read_only", "candidate"}:
            raise ValueError("tool_mode must be read_only or candidate")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            self.role is not AgentRole.CANDIDATE_PROPOSER
            and self.tool_mode != "read_only"
        ):
            raise ValueError("Only CandidateProposer may receive write tools")
        for field, value in (
            ("research_lab_profile", self.research_lab_profile),
            ("submission_tool", self.submission_tool),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field} must be non-empty text or None")


@dataclass(frozen=True)
class AgentRunResult:
    """Terminal result from one independent runtime process."""

    agent_run_id: str
    role: AgentRole
    status: str
    result: JsonObject | None
    error: JsonObject | None
    run_directory: Path


@dataclass(frozen=True)
class SpecialistRunOutcome:
    """One specialist result or one isolated orchestration exception."""

    role: AgentRole
    run: AgentRunResult | None
    exception: JsonObject | None

    def __post_init__(self) -> None:
        if (self.run is None) == (self.exception is None):
            raise ValueError(
                "Specialist outcome requires exactly one run or exception"
            )
        if self.run is not None and self.run.role is not self.role:
            raise ValueError("Specialist outcome role differs from its run")
        if (
            self.run is not None
            and self.run.status not in AGENT_TERMINAL_STATUSES
        ):
            raise ValueError("Specialist run has a non-terminal status")

    @property
    def status(self) -> str:
        """Return the run status or the framework failure status."""

        return self.run.status if self.run is not None else "failed"


class AgentRuntime(Protocol):
    """Runtime boundary used by the orchestrator and fake tests."""

    def research_capability_identity(self, spec: AgentSpec) -> JsonObject:
        """Return the exact runnable identity certified by behavior smokes."""

    def run(
        self,
        *,
        spec: AgentSpec,
        campaign_id: str,
        round_number: int,
        context: Mapping[str, Any],
        evidence_bundle: Path,
        candidate_workspace: Path | None = None,
    ) -> AgentRunResult:
        """Execute one role in a fresh process and session."""


class AgentRunRepository:
    """Allocate isolated AgentRun workspaces and atomically update manifests."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root)

    def prepare(
        self,
        *,
        spec: AgentSpec,
        campaign_id: str,
        round_number: int,
        model: ModelConfiguration,
        context: Mapping[str, Any],
        evidence_bundle: Path,
    ) -> tuple[str, Path]:
        """Snapshot prompt, approval, context, and evidence for one attempt."""

        agent_run_id = new_object_id("agent-run")
        directory = self.repository.object_directory(agent_run_id)
        directory.mkdir(parents=True)
        prompt_directory = directory / "prompt"
        prompt_directory.mkdir()
        prompt_snapshot = prompt_directory / "template.md"
        approval_source = spec.prompt_path.with_name(
            spec.prompt_path.name + ".approval.json"
        )
        approval_snapshot = prompt_directory / "approval.json"
        try:
            shutil.copy2(spec.prompt_path, prompt_snapshot)
            shutil.copy2(approval_source, approval_snapshot)
            shutil.copytree(evidence_bundle, directory / "workspace/evidence")
            context_value: JsonObject = {
                **dict(context),
                "agent_run_id": agent_run_id,
                "role": spec.role.value,
                "campaign_id": campaign_id,
                "round": round_number,
            }
            context_value.setdefault("evidence_root", "evidence")
            from skill_evolution.storage import atomic_write_json

            atomic_write_json(
                directory / "workspace/context.json",
                context_value,
            )
            manifest: JsonObject = {
                "schema": AGENT_RUN_SCHEMA,
                "id": agent_run_id,
                "status": "prepared",
                "role": spec.role.value,
                "campaign_id": campaign_id,
                "round": round_number,
                "attempt": 1,
                "tool_mode": spec.tool_mode,
                "research_lab_profile": spec.research_lab_profile,
                "submission_tool": spec.submission_tool,
                "timeout_seconds": spec.timeout_seconds,
                "model": model.to_dict(),
                "prompt": {
                    "template_snapshot": "prompt/template.md",
                    "approval_snapshot": "prompt/approval.json",
                },
                "workspace": "workspace",
                "trajectory": "trajectory.jsonl",
                "pi_session": "pi-session.jsonl",
                "result": "result.json",
                "parse_failure": None,
                "error": None,
            }
            self.repository.create(agent_run_id, manifest)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return agent_run_id, directory

    def mark_running(
        self,
        agent_run_id: str,
        *,
        process_id: int | None,
    ) -> JsonObject:
        """Record process start for a prepared run."""

        return self.repository.update(
            agent_run_id,
            {
                "status": "running",
                "started_at": utc_now(),
                "process_id": process_id,
            },
            expected_status="prepared",
        )

    def finish(
        self,
        agent_run_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        parse_failure: Mapping[str, Any] | None = None,
        session_status: str | None = None,
    ) -> JsonObject:
        """Seal one attempt while preserving invalid or interrupted output."""

        if status not in AGENT_TERMINAL_STATUSES:
            raise AgentOrchestrationError(
                f"Invalid terminal AgentRun status: {status}"
            )
        directory = self.repository.object_directory(agent_run_id)
        if result is not None:
            from skill_evolution.storage import atomic_write_json

            atomic_write_json(directory / "result.json", dict(result))
        return self.repository.update(
            agent_run_id,
            {
                "status": status,
                "ended_at": utc_now(),
                "error": dict(error) if error is not None else None,
                "parse_failure": (
                    dict(parse_failure)
                    if parse_failure is not None
                    else None
                ),
                "session_status": session_status,
            },
            expected_status={"prepared", "running"},
        )


def default_agent_specs(
    prompts_root: str | os.PathLike[str],
) -> dict[AgentRole, AgentSpec]:
    """Return active research and legacy workflow prompt mappings."""

    root = Path(prompts_root).resolve()
    filenames = {
        AgentRole.BEHAVIOR_PATTERN: "behavior-pattern-research-v1.md",
        AgentRole.CONDITIONS_COVERAGE: (
            "conditions-coverage-research-v1.md"
        ),
        AgentRole.OUTCOME_CONSISTENCY: (
            "outcome-consistency-research-v1.md"
        ),
        AgentRole.CAPABILITY_COVERAGE: "capability-coverage-v1.md",
        AgentRole.RESOURCE_EFFICIENCY: (
            "resource-efficiency-research-v1.md"
        ),
        AgentRole.SYNTHESIS: "synthesis-v1.md",
        AgentRole.CANDIDATE_PROPOSER: "candidate-proposer-v1.md",
        AgentRole.REPLAY_JUDGE: "replay-judge-v1.md",
        AgentRole.TRAJECTORY_ERROR_ANALYST: "trajectory-error-analysis-v1.md",
        AgentRole.ERROR_IDENTIFIER: "error-identification-v1.md",
        AgentRole.ERROR_ANALYST: "error-analyst-v1.md",
    }
    return {
        role: AgentSpec(
            role=role,
            prompt_path=root / filename,
            tool_mode=(
                "candidate"
                if role is AgentRole.CANDIDATE_PROPOSER
                else "read_only"
            ),
            submission_tool=(
                "submit_multi_trajectory_research"
                if role in ACTIVE_SPECIALIST_ROLES
                else (
                    "submit_error_identification"
                    if role is AgentRole.ERROR_IDENTIFIER
                    else (
                        "submit_error_report"
                        if role is AgentRole.ERROR_ANALYST
                        else None
                    )
                )
            ),
            research_lab_profile=(
                "multi_trajectory_research"
                if role in ACTIVE_SPECIALIST_ROLES
                or role in {AgentRole.ERROR_IDENTIFIER, AgentRole.ERROR_ANALYST}
                else None
            ),
        )
        for role, filename in filenames.items()
    }


class MultiPiOrchestrator:
    """Run isolated specialists and retain the legacy synthesis workflow."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        specs: Mapping[AgentRole, AgentSpec],
        max_parallel_agents: int = 1,
    ) -> None:
        if max_parallel_agents not in {1, 2, 3}:
            raise ValueError("max_parallel_agents must be between 1 and 3")
        if max_parallel_agents != 1:
            raise ValueError(
                "parallel specialist execution is not certified; "
                "max_parallel_agents must remain 1"
            )
        self.runtime = runtime
        self.specs = dict(specs)
        self.max_parallel_agents = max_parallel_agents

    def preflight_analysis(self) -> None:
        """Validate prerequisites for the legacy synthesis workflow."""

        self._preflight_roles(
            [*LEGACY_SPECIALIST_ROLES, AgentRole.SYNTHESIS]
        )

    def preflight_specialists(
        self,
        roles: Sequence[AgentRole] = ACTIVE_SPECIALIST_ROLES,
    ) -> None:
        """Validate only the requested active research specialists."""

        normalized = self._validate_specialist_roles(roles)
        self._preflight_roles(normalized)

    def research_capability_identity(self) -> JsonObject:
        """Proxy the fail-closed behavior capability identity boundary."""

        spec = self.specs.get(AgentRole.BEHAVIOR_PATTERN)
        if spec is None:
            raise AgentOrchestrationError(
                "Behavior specialist spec is not configured"
            )
        capability = getattr(
            self.runtime,
            "research_capability_identity",
            None,
        )
        if not callable(capability):
            raise AgentOrchestrationError(
                "Agent runtime cannot attest research capability identity"
            )
        identity = capability(spec)
        if not isinstance(identity, Mapping):
            raise AgentOrchestrationError(
                "Agent runtime returned an invalid capability identity"
            )
        return dict(identity)

    def _preflight_roles(self, roles: Sequence[AgentRole]) -> None:
        try:
            specs = [self.specs[role] for role in roles]
        except KeyError as error:
            raise AgentOrchestrationError(
                f"Missing agent spec: {error.args[0].value}"
            ) from error
        preflight = getattr(self.runtime, "preflight", None)
        if callable(preflight):
            preflight(specs)

    def run_candidate_proposer(
        self,
        *,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
        candidate_workspace: Path,
    ) -> AgentRunResult:
        """Run one atomic proposer in a fresh process and writable workspace."""

        role = AgentRole.CANDIDATE_PROPOSER
        self._preflight_roles([role])
        spec = self.specs.get(role)
        if spec is None:
            raise AgentOrchestrationError(
                "CandidateProposer spec is not configured"
            )
        return self.runtime.run(
            spec=spec,
            campaign_id=campaign_id,
            round_number=round_number,
            context=context,
            evidence_bundle=evidence_bundle,
            candidate_workspace=candidate_workspace,
        )

    def run_specialist(
        self,
        *,
        role: AgentRole,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> AgentRunResult:
        """Run one specialist smoke without changing campaign workflow state."""

        if role not in ACTIVE_SPECIALIST_ROLES:
            raise AgentOrchestrationError(
                f"{role.value} is not a specialist role"
            )
        self._preflight_roles([role])
        return self.runtime.run(
            spec=self.specs[role],
            campaign_id=campaign_id,
            round_number=round_number,
            context=context,
            evidence_bundle=evidence_bundle,
        )

    def run_specialists_only(
        self,
        *,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
        roles: Sequence[AgentRole] = ACTIVE_SPECIALIST_ROLES,
    ) -> tuple[SpecialistRunOutcome, ...]:
        """Run active specialists without synthesis and preserve exceptions."""

        normalized = self._validate_specialist_roles(roles)
        self._preflight_roles(normalized)
        if self.max_parallel_agents == 1:
            return tuple(
                self._run_specialist_safely(
                    role=role,
                    campaign_id=campaign_id,
                    round_number=round_number,
                    evidence_bundle=evidence_bundle,
                    context=context,
                )
                for role in normalized
            )

        futures: dict[AgentRole, Future[AgentRunResult]] = {}
        with ThreadPoolExecutor(
            max_workers=self.max_parallel_agents
        ) as executor:
            for role in normalized:
                futures[role] = executor.submit(
                    self.runtime.run,
                    spec=self.specs[role],
                    campaign_id=campaign_id,
                    round_number=round_number,
                    context=dict(context),
                    evidence_bundle=evidence_bundle,
                )
            outcomes = [
                self._future_outcome(role, futures[role])
                for role in normalized
            ]
        return tuple(outcomes)

    def _validate_specialist_roles(
        self,
        roles: Sequence[AgentRole],
    ) -> tuple[AgentRole, ...]:
        normalized = tuple(roles)
        if not normalized:
            raise AgentOrchestrationError(
                "At least one specialist role is required"
            )
        if len(set(normalized)) != len(normalized):
            raise AgentOrchestrationError(
                "Specialist roles must not contain duplicates"
            )
        invalid = [
            role for role in normalized if role not in ACTIVE_SPECIALIST_ROLES
        ]
        if invalid:
            raise AgentOrchestrationError(
                f"{invalid[0].value} is not an active specialist role"
            )
        return normalized

    def _run_specialist_safely(
        self,
        *,
        role: AgentRole,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> SpecialistRunOutcome:
        try:
            run = self.runtime.run(
                spec=self.specs[role],
                campaign_id=campaign_id,
                round_number=round_number,
                context=dict(context),
                evidence_bundle=evidence_bundle,
            )
            return SpecialistRunOutcome(
                role=role,
                run=run,
                exception=None,
            )
        except Exception as error:
            return SpecialistRunOutcome(
                role=role,
                run=None,
                exception=self._exception_record(error),
            )

    def _future_outcome(
        self,
        role: AgentRole,
        future: Future[AgentRunResult],
    ) -> SpecialistRunOutcome:
        try:
            run = future.result()
            return SpecialistRunOutcome(
                role=role,
                run=run,
                exception=None,
            )
        except Exception as error:
            return SpecialistRunOutcome(
                role=role,
                run=None,
                exception=self._exception_record(error),
            )

    @staticmethod
    def _exception_record(error: Exception) -> JsonObject:
        return {
            "type": type(error).__name__,
            "message": str(error),
        }

    def run_replay_judge(
        self,
        *,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> AgentRunResult:
        """Run an independent Judge after comparison evidence is frozen."""

        role = AgentRole.REPLAY_JUDGE
        self._preflight_roles([role])
        spec = self.specs.get(role)
        if spec is None:
            raise AgentOrchestrationError(
                "ReplayJudge spec is not configured"
            )
        return self.runtime.run(
            spec=spec,
            campaign_id=campaign_id,
            round_number=round_number,
            context=context,
            evidence_bundle=evidence_bundle,
        )

    def run_analysis_round(
        self,
        *,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> tuple[list[AgentRunResult], AgentRunResult]:
        """Run fixed specialists independently and synthesize visible failures."""

        specialists = self._run_legacy_specialists(
            campaign_id=campaign_id,
            round_number=round_number,
            evidence_bundle=evidence_bundle,
            context=context,
        )
        specialist_reports: list[JsonObject] = []
        missing_roles: list[str] = []
        for run in specialists:
            specialist_reports.append(
                {
                    "agent_run_id": run.agent_run_id,
                    "role": run.role.value,
                    "status": run.status,
                    "result": run.result,
                    "error": run.error,
                }
            )
            if run.status != "succeeded":
                missing_roles.append(run.role.value)
        synthesis_context = {
            **dict(context),
            "specialist_reports": specialist_reports,
            "missing_roles": missing_roles,
            "instruction": (
                "State every missing role and the resulting limits; do not "
                "infer findings for a failed specialist."
            ),
        }
        synthesis = self.runtime.run(
            spec=self.specs[AgentRole.SYNTHESIS],
            campaign_id=campaign_id,
            round_number=round_number,
            context=synthesis_context,
            evidence_bundle=evidence_bundle,
        )
        if synthesis.status == "succeeded" and synthesis.result is not None:
            validated = validate_agent_result(synthesis.result)
            reported_missing = set(validated.get("missing_roles", []))
            if not set(missing_roles).issubset(reported_missing):
                raise AgentOrchestrationError(
                    "Synthesis omitted one or more failed specialist roles"
                )
        return specialists, synthesis

    def _run_legacy_specialists(
        self,
        *,
        campaign_id: str,
        round_number: int,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> list[AgentRunResult]:
        if self.max_parallel_agents == 1:
            return [
                self.runtime.run(
                    spec=self.specs[role],
                    campaign_id=campaign_id,
                    round_number=round_number,
                    context=context,
                    evidence_bundle=evidence_bundle,
                )
                for role in LEGACY_SPECIALIST_ROLES
            ]
        futures: dict[AgentRole, Future[AgentRunResult]] = {}
        with ThreadPoolExecutor(
            max_workers=self.max_parallel_agents
        ) as executor:
            for role in LEGACY_SPECIALIST_ROLES:
                futures[role] = executor.submit(
                    self.runtime.run,
                    spec=self.specs[role],
                    campaign_id=campaign_id,
                    round_number=round_number,
                    context=context,
                    evidence_bundle=evidence_bundle,
                )
        return [futures[role].result() for role in LEGACY_SPECIALIST_ROLES]
