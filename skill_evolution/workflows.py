"""Asynchronous workflow coordinators that exchange file object IDs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_evolution.agents import (
    AgentRunResult,
    MultiPiOrchestrator,
)
from skill_evolution.analysis import (
    AnalysisCampaignRepository,
    ExperimentRequestRepository,
    load_approved_capability_contract,
    load_approved_skill_contract,
    validate_optimization_hypothesis,
)
from skill_evolution.evidence import EvidenceError, resolve_inside
from skill_evolution.candidates import (
    CandidateError,
    CandidateRepository,
    CandidateSkill,
    SkillVersion,
)
from skill_evolution.comparison import (
    ComparisonRepository,
    HarnessAttempt,
    RunAttempt,
    SandboxBackend,
)
from skill_evolution.storage import JsonObject, StorageError


@dataclass(frozen=True)
class AnalysisRoundResult:
    """Framework records emitted by one multi-agent analysis round."""

    campaign: JsonObject
    specialists: tuple[AgentRunResult, ...]
    synthesis: AgentRunResult
    experiment_request_ids: tuple[str, ...]


class AnalysisWorkflow:
    """Connect file-backed campaigns to independent specialist AgentRuns."""

    def __init__(
        self,
        *,
        campaigns: AnalysisCampaignRepository,
        requests: ExperimentRequestRepository,
        orchestrator: MultiPiOrchestrator,
    ) -> None:
        self.campaigns = campaigns
        self.requests = requests
        self.orchestrator = orchestrator

    def run_round(
        self,
        campaign_id: str,
        *,
        evidence_bundle: Path,
        context: Mapping[str, Any],
    ) -> AnalysisRoundResult:
        """Run one fresh round; agents never mutate campaign state directly."""

        try:
            contract_path = resolve_inside(
                evidence_bundle,
                "skill_contract.json",
            )
        except EvidenceError:
            legacy_path = resolve_inside(
                evidence_bundle,
                "capability-contract.json",
            )
            load_approved_capability_contract(legacy_path)
        else:
            load_approved_skill_contract(contract_path)
        self.orchestrator.preflight_analysis()
        started = self.campaigns.start_round(campaign_id)
        if started.get("status") == "inconclusive":
            raise StorageError("Campaign reached its maximum round count")
        round_number = len(started["rounds"])
        specialists, synthesis = self.orchestrator.run_analysis_round(
            campaign_id=campaign_id,
            round_number=round_number,
            evidence_bundle=evidence_bundle,
            context=context,
        )
        self.campaigns.record_specialists(
            campaign_id,
            [run.agent_run_id for run in specialists],
        )
        request_ids: list[str] = []
        failed = (
            synthesis.status != "succeeded" or synthesis.result is None
        )
        if not failed:
            for request in synthesis.result.get("evidence_requests", []):
                if not isinstance(request, Mapping):
                    continue
                saved = self.requests.create(
                    campaign_id=campaign_id,
                    round_number=round_number,
                    request=request,
                )
                request_ids.append(str(saved["id"]))
            for hypothesis in synthesis.result.get(
                "optimization_hypotheses",
                [],
            ):
                if isinstance(hypothesis, Mapping):
                    validate_optimization_hypothesis(hypothesis)
        campaign = self.campaigns.finish_round(
            campaign_id,
            synthesis_run_id=synthesis.agent_run_id,
            synthesis_result=synthesis.result,
            request_ids=request_ids,
            failed=failed,
        )
        return AnalysisRoundResult(
            campaign=campaign,
            specialists=tuple(specialists),
            synthesis=synthesis,
            experiment_request_ids=tuple(request_ids),
        )


EvidenceProducer = Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
NextRound = Callable[[str], Any]


class EvidenceLoopCoordinator:
    """Fulfil an approved request, then chain harness and the next round."""

    def __init__(
        self,
        *,
        campaigns: AnalysisCampaignRepository,
        requests: ExperimentRequestRepository,
    ) -> None:
        self.campaigns = campaigns
        self.requests = requests

    def fulfil_approved(
        self,
        request_id: str,
        *,
        produce_evidence: EvidenceProducer,
        run_next_round: NextRound,
    ) -> JsonObject:
        """Reject proposed requests and automatically continue approved ones."""

        request = self.requests.repository.load(request_id)
        if request.get("status") != "approved":
            raise StorageError(
                "Evidence production requires explicit request approval"
            )
        campaign_id = str(request["campaign_id"])
        campaign_before = self.campaigns.repository.load(campaign_id)
        if campaign_before.get("status") != "awaiting_evidence":
            raise StorageError(
                "Evidence production is closed because the analysis campaign "
                f"is {campaign_before.get('status')!r}"
            )
        running = self.requests.begin(request_id)
        try:
            evidence_refs = [
                dict(item) for item in produce_evidence(running)
            ]
            if not evidence_refs:
                raise RuntimeError("Evidence producer returned no references")
            self.requests.finish(
                request_id,
                result_refs=evidence_refs,
            )
            campaign = self.campaigns.mark_evidence_ready(
                campaign_id,
                evidence_refs=evidence_refs,
            )
            if campaign.get("status") == "evidence_ready":
                run_next_round(campaign_id)
            return self.requests.repository.load(request_id)
        except Exception:
            try:
                self.requests.finish(
                    request_id,
                    result_refs=[],
                    status="failed",
                )
            except StorageError:
                pass
            raise


@dataclass(frozen=True)
class CandidateProposalResult:
    """Visible result of proposing and freezing one atomic candidate."""

    candidate: CandidateSkill
    proposer_run: AgentRunResult


@dataclass(frozen=True)
class JudgeWorkflowResult:
    """Comparison and candidate states after one independent Judge attempt."""

    comparison: JsonObject
    candidate: CandidateSkill
    judge_run: AgentRunResult


class CandidateWorkflow:
    """Generate one candidate per hypothesis and plan isolated comparison."""

    def __init__(
        self,
        *,
        candidates: CandidateRepository,
        comparisons: ComparisonRepository,
    ) -> None:
        self.candidates = candidates
        self.comparisons = comparisons

    def prepare_candidate(
        self,
        *,
        parent_skill: SkillVersion,
        hypothesis: Mapping[str, Any],
        analysis_campaign_id: str,
    ) -> CandidateSkill:
        """Prepare an editable full copy for a proposer AgentRun."""

        return self.candidates.prepare(
            parent_skill=parent_skill,
            hypothesis=hypothesis,
            analysis_campaign_id=analysis_campaign_id,
        )

    def freeze_after_proposer(
        self,
        candidate_id: str,
        *,
        proposer_run: AgentRunResult,
    ) -> CandidateProposalResult:
        """Freeze only a successful proposer workspace and compute its diff."""

        if proposer_run.status != "succeeded":
            candidate = self.candidates.mark_status(
                candidate_id,
                status="proposal_failed",
                detail={
                    "agent_run_id": proposer_run.agent_run_id,
                    "status": proposer_run.status,
                    "error": proposer_run.error,
                },
            )
            return CandidateProposalResult(candidate, proposer_run)
        try:
            candidate = self.candidates.finalize(candidate_id)
        except CandidateError as error:
            candidate = self.candidates.mark_status(
                candidate_id,
                status="proposal_failed",
                detail={
                    "agent_run_id": proposer_run.agent_run_id,
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                },
            )
        else:
            manifest = self.candidates.repository.load(candidate_id)
            actual_files = {
                str(item["path"])
                for item in manifest.get("file_changes", [])
                if isinstance(item, Mapping)
                and isinstance(item.get("path"), str)
            }
            claimed_files = set(
                proposer_run.result.get("files_touched", [])
                if proposer_run.result is not None
                else []
            )
            if claimed_files != actual_files:
                candidate = self.candidates.mark_status(
                    candidate_id,
                    status="proposal_failed",
                    detail={
                        "agent_run_id": proposer_run.agent_run_id,
                        "reason": "files_touched_mismatch",
                        "claimed_files": sorted(claimed_files),
                        "framework_diff_files": sorted(actual_files),
                    },
                )
        return CandidateProposalResult(candidate, proposer_run)

    def create_comparison(
        self,
        *,
        proposal: CandidateProposalResult,
        triggering_task_case_id: str,
        regression_task_case_id: str,
    ) -> JsonObject:
        """Plan the accepted 13-run maximum only for a frozen candidate."""

        candidate = proposal.candidate
        if candidate.status != "ready_for_smoke":
            raise StorageError("Candidate is not ready for comparison")
        return self.comparisons.create(
            candidate_id=candidate.candidate_id,
            baseline_skill_version=candidate.parent_version,
            triggering_task_case_id=triggering_task_case_id,
            regression_task_case_id=regression_task_case_id,
            proposer_agent_run_id=proposal.proposer_run.agent_run_id,
        )

    def execute_comparison(
        self,
        comparison_id: str,
        *,
        sandbox: SandboxBackend,
        run_attempt: RunAttempt,
        run_harness: HarnessAttempt,
    ) -> JsonObject:
        """Execute only through the fail-closed sandbox repository boundary."""

        result = self.comparisons.execute(
            comparison_id,
            sandbox=sandbox,
            run_attempt=run_attempt,
            run_harness=run_harness,
        )
        candidate_id = str(result["candidate_id"])
        if result["status"] == "awaiting_sandbox":
            self.candidates.mark_status(
                candidate_id,
                status="awaiting_sandbox",
                detail=result.get("sandbox"),
            )
        elif result["status"] == "not_runnable":
            self.candidates.mark_status(
                candidate_id,
                status="not_runnable",
                detail={"comparison_id": comparison_id},
            )
        return result

    def record_judge(
        self,
        comparison_id: str,
        *,
        judge_run: AgentRunResult,
    ) -> JudgeWorkflowResult:
        """Record every Judge attempt and sync a valid effect to the candidate."""

        comparison = self.comparisons.repository.load(comparison_id)
        candidate_id = str(comparison["candidate_id"])
        if judge_run.status != "succeeded" or judge_run.result is None:
            updated = self.comparisons.record_judge_failure(
                comparison_id,
                agent_run_id=judge_run.agent_run_id,
                status=judge_run.status,
                error=judge_run.error,
            )
            return JudgeWorkflowResult(
                comparison=updated,
                candidate=self.candidates.load(candidate_id),
                judge_run=judge_run,
            )
        updated = self.comparisons.record_effect(
            comparison_id,
            judge_run.result,
        )
        candidate = self.candidates.record_validation(
            candidate_id,
            comparison_id=comparison_id,
            classification=str(updated["gate_classification"]),
        )
        return JudgeWorkflowResult(
            comparison=updated,
            candidate=candidate,
            judge_run=judge_run,
        )
