#!/usr/bin/env python3
"""Reject retired summary-based multi-Pi mutation commands fail closed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.agents import (
    AgentRole,
    LEGACY_SPECIALIST_ROLES,
    MultiPiOrchestrator,
    default_agent_specs,
)
from skill_evolution.analysis import (
    AnalysisCampaignRepository,
    ExperimentRequestRepository,
)
from skill_evolution.evidence import EvidenceBundleBuilder
from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.hierarchy_analysis import HierarchyAnalysisService
from skill_evolution.pi_runtime import PiAgentRuntime
from skill_evolution.workflows import AnalysisWorkflow
from scripts.harness import materialize_execution_set
from scripts.prompt_approval import PromptApprovalError


_RETIRED_MESSAGE = (
    "The legacy summary-based multi-Pi workflow is retired. Use "
    "scripts/multi_trajectory_research.py; new research must pass corpus, "
    "Harness, blind-validation, and Docker-lab gates."
)


def _prepare(options: argparse.Namespace) -> int:
    """Freeze same-revision evidence beneath a Skill analysis object."""

    raise ValueError(_RETIRED_MESSAGE)

    hierarchy = SkillHierarchyRepository(options.runtime_root)
    execution_set = hierarchy.load_execution_set(
        options.skill_id, options.execution_set_id
    )
    harness = hierarchy.load_analysis(
        options.skill_id, options.harness_analysis_id
    )
    if (
        harness["kind"] != "harness"
        or harness["execution_set_id"] != options.execution_set_id
    ):
        raise ValueError(
            "Harness analysis must belong to the selected Execution Set"
        )
    service = HierarchyAnalysisService(options.runtime_root)
    analysis_directory, record = service.prepare_multi(
        skill_id=options.skill_id,
        execution_set_id=options.execution_set_id,
        kind="multi_trajectory",
        input_refs=[
            {
                "kind": "analysis",
                "analysis_id": options.harness_analysis_id,
            }
        ],
        provenance={"prepared_by": "analysis_campaign.py"},
    )
    try:
        evidence = analysis_directory / "evidence"
        harness_directory = hierarchy.analysis_directory(harness) / "payload"
        revision_directory = hierarchy.revision_directory(
            options.skill_id, str(execution_set["revision_id"])
        )
        contract_path = revision_directory / "package" / "skill_contract.json"
        if not contract_path.is_file():
            raise ValueError(
                "Multi-role analysis requires the Contract captured by its Revision"
            )
        with tempfile.TemporaryDirectory() as temporary:
            campaign = materialize_execution_set(
                hierarchy=hierarchy,
                execution_set=execution_set,
                destination=Path(temporary) / "execution-set",
            )
            EvidenceBundleBuilder().build(
                campaign_directory=campaign,
                destination=evidence,
                profile_path=harness_directory / "trajectory-profile.json",
                comparison_path=(
                    harness_directory / "artifact-comparison.json"
                ),
                skill_contract_path=contract_path,
            )
        campaigns = AnalysisCampaignRepository(
            analysis_directory / "workflow" / "campaigns"
        )
        workflow = campaigns.create(
            replay_campaign_id=options.execution_set_id,
            evidence_bundle=str(evidence),
            harness_versions={
                "trajectory_profiler": "trajectory.profile.v1",
                "artifact_comparator": "artifact.comparison.v1",
            },
        )
        record["provenance"] = {
            **dict(record.get("provenance") or {}),
            "workflow_campaign_id": workflow["id"],
            "harness_analysis_id": options.harness_analysis_id,
        }
        hierarchy.replace_analysis(record)
    except Exception:
        shutil.rmtree(analysis_directory, ignore_errors=True)
        hierarchy.rebuild_indexes()
        raise
    print(record["analysis_id"])
    return 0


def _context(
    runtime_root: str,
    skill_id: str,
    analysis_id: str,
) -> tuple[
    HierarchyAnalysisService,
    SkillHierarchyRepository,
    dict[str, object],
    Path,
    AnalysisCampaignRepository,
    dict[str, object],
]:
    service = HierarchyAnalysisService(runtime_root)
    hierarchy = service.repository
    record = hierarchy.load_analysis(skill_id, analysis_id)
    if record["kind"] != "multi_trajectory":
        raise ValueError("Selected analysis is not a multi-trajectory analysis")
    directory = hierarchy.analysis_directory(record)
    campaign_id = (record.get("provenance") or {}).get(
        "workflow_campaign_id"
    )
    if not isinstance(campaign_id, str):
        raise ValueError("Analysis has no internal workflow Campaign")
    campaigns = AnalysisCampaignRepository(directory / "workflow" / "campaigns")
    campaign = campaigns.repository.load(campaign_id)
    return service, hierarchy, record, directory, campaigns, campaign


def _orchestrator(
    options: argparse.Namespace,
    analysis_directory: Path,
    *,
    max_parallel_agents: int,
) -> MultiPiOrchestrator:
    return MultiPiOrchestrator(
        runtime=PiAgentRuntime(
            agent_runs_root=analysis_directory / "attempts",
            extension_path=options.extension,
            pi_command=options.pi_command,
        ),
        specs=default_agent_specs(options.prompts_root),
        max_parallel_agents=max_parallel_agents,
    )


def _run_round(options: argparse.Namespace) -> int:
    """Run one internal round and update the owning analysis envelope."""

    raise ValueError(_RETIRED_MESSAGE)

    (
        service,
        hierarchy,
        record,
        directory,
        campaigns,
        campaign,
    ) = _context(options.runtime_root, options.skill_id, options.analysis_id)
    if record["status"] == "planned":
        service.start(options.skill_id, options.analysis_id)
    workflow = AnalysisWorkflow(
        campaigns=campaigns,
        requests=ExperimentRequestRepository(
            directory / "workflow" / "experiment-requests"
        ),
        orchestrator=_orchestrator(
            options,
            directory,
            max_parallel_agents=options.max_parallel_agents,
        ),
    )
    try:
        result = workflow.run_round(
            str(campaign["id"]),
            evidence_bundle=Path(str(campaign["evidence_bundle"])),
            context={
                "skill_id": options.skill_id,
                "revision_id": record["revision_id"],
                "execution_set_id": record["execution_set_id"],
                "harness_versions": campaign["harness_versions"],
            },
        )
    except Exception:
        report = service.unavailable_report(
            skill_id=options.skill_id,
            analysis_id=options.analysis_id,
            message="Multi-role analysis failed before a valid result was produced.",
        )
        service.finish(
            skill_id=options.skill_id,
            analysis_id=options.analysis_id,
            status="failed",
            attempts=[],
            result_refs=[],
            user_report=report,
        )
        raise
    attempts = [
        {
            "agent_run_id": run.agent_run_id,
            "role": run.role.value,
            "status": run.status,
            "path": f"attempts/{run.agent_run_id}",
        }
        for run in [*result.specialists, result.synthesis]
    ]
    campaign_status = str(result.campaign["status"])
    status = {
        "failed": "failed",
        "inconclusive": "inconclusive",
        "awaiting_evidence": "inconclusive",
    }.get(campaign_status, "unavailable")
    report = service.unavailable_report(
        skill_id=options.skill_id,
        analysis_id=options.analysis_id,
        message=(
            "The internal analysis record is preserved, but it has not been "
            "converted into a validated multi-Trajectory user report."
        ),
    )
    service.finish(
        skill_id=options.skill_id,
        analysis_id=options.analysis_id,
        status=status,
        attempts=attempts,
        result_refs=[
            {
                "schema": "analysis.campaign.v1",
                "path": (
                    "workflow/campaigns/"
                    f"{campaign['id']}/manifest.json"
                ),
            }
        ],
        user_report=report,
    )
    hierarchy.rebuild_indexes()
    print(
        json.dumps(
            {
                "analysis_id": options.analysis_id,
                "status": status,
                "specialist_agent_runs": [
                    run.agent_run_id for run in result.specialists
                ],
                "synthesis_agent_run": result.synthesis.agent_run_id,
                "experiment_requests": list(result.experiment_request_ids),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status not in {"failed", "invalid_output"} else 1


def _run_agent(options: argparse.Namespace) -> int:
    """Run one specialist smoke as an internal attempt of the analysis."""

    raise ValueError(_RETIRED_MESSAGE)

    service, hierarchy, record, directory, _, campaign = _context(
        options.runtime_root, options.skill_id, options.analysis_id
    )
    if record["status"] == "planned":
        record = service.start(options.skill_id, options.analysis_id)
    result = _orchestrator(
        options, directory, max_parallel_agents=1
    ).run_specialist(
        role=AgentRole(options.role),
        campaign_id=str(campaign["id"]),
        round_number=1,
        evidence_bundle=Path(str(campaign["evidence_bundle"])),
        context={
            "smoke": True,
            "skill_id": options.skill_id,
            "revision_id": record["revision_id"],
            "execution_set_id": record["execution_set_id"],
        },
    )
    record["attempts"] = [
        *record["attempts"],
        {
            "agent_run_id": result.agent_run_id,
            "role": result.role.value,
            "status": result.status,
            "path": f"attempts/{result.agent_run_id}",
        },
    ]
    hierarchy.replace_analysis(record)
    print(
        json.dumps(
            {
                "analysis_id": options.analysis_id,
                "analysis_state_changed": True,
                "agent_run_id": result.agent_run_id,
                "role": result.role.value,
                "status": result.status,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


def _approve_request(options: argparse.Namespace) -> int:
    _, _, _, directory, _, _ = _context(
        options.runtime_root, options.skill_id, options.analysis_id
    )
    request = ExperimentRequestRepository(
        directory / "workflow" / "experiment-requests"
    ).approve(options.request_id, approved_by=options.approved_by)
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def _add_analysis_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skill-id", required=True)
    parser.add_argument("--analysis-id", required=True)


def _add_agent_runtime(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompts-root", default="prompts/analysis")
    parser.add_argument("--extension", default="extensions/root-jail.ts")
    parser.add_argument("--pi-command")


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", default=".skill-evolution")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="Retired: use multi_trajectory_research.py",
    )
    prepare.add_argument("--skill-id", required=True)
    prepare.add_argument("--execution-set-id", required=True)
    prepare.add_argument("--harness-analysis-id", required=True)
    prepare.set_defaults(handler=_prepare)

    run_round = subparsers.add_parser(
        "run-round", help="Retired: synthesis is outside the accepted scope"
    )
    _add_analysis_identity(run_round)
    _add_agent_runtime(run_round)
    run_round.add_argument(
        "--max-parallel-agents", type=int, choices=(1, 2, 3), default=1
    )
    run_round.set_defaults(handler=_run_round)

    run_agent = subparsers.add_parser(
        "run-agent", help="Retired: use the gated research workflow"
    )
    _add_analysis_identity(run_agent)
    _add_agent_runtime(run_agent)
    run_agent.add_argument(
        "--role",
        required=True,
        choices=tuple(role.value for role in LEGACY_SPECIALIST_ROLES),
    )
    run_agent.set_defaults(handler=_run_agent)

    approve = subparsers.add_parser(
        "approve-request", help="Approve one analysis-owned evidence request"
    )
    _add_analysis_identity(approve)
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.set_defaults(handler=_approve_request)

    options = parser.parse_args(arguments)
    return int(options.handler(options))


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (
        FileNotFoundError,
        OSError,
        PromptApprovalError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
