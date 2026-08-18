#!/usr/bin/env python3
"""Legacy gated internal multi-Trajectory research (decision 0027).

Superseded by scripts/error_analysis.py per decision 0032; kept for the frozen
batch/certificate data, the deterministic Harness acceptance, and tests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prompt_approval import PromptApprovalError, load_approved_prompt
from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AgentRole,
    ModelConfiguration,
    MultiPiOrchestrator,
    default_agent_specs,
)
from skill_evolution.research_agent_runtime import (
    ResearchAgentRuntimeError,
    ResearchPiAgentRuntime,
)
from skill_evolution.research_capability import ResearchCapabilityError
from skill_evolution.research_corpus import (
    RESEARCH_OBJECTIVES,
    ResearchCorpusBuilder,
    ResearchCorpusError,
    ResearchCorpusResult,
    verify_research_corpus,
)
from skill_evolution.research_harness_acceptance import HarnessAcceptanceError
from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    ResearchSandboxError,
)
from skill_evolution.research_workflow import (
    FAILURE_CATEGORIES,
    REVIEW_CHECKS,
    ResearchWorkflow,
    ResearchWorkflowError,
)
from skill_evolution.storage import StorageError, load_json_object


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_corpus(directory: str) -> ResearchCorpusResult:
    verified = verify_research_corpus(Path(directory).resolve())
    readiness = load_json_object(verified.directory / "readiness.json")
    return ResearchCorpusResult(
        directory=verified.directory,
        manifest=verified.manifest,
        corpus_map=verified.corpus_map,
        navigation_index=verified.navigation_index,
        baseline=verified.baseline,
        readiness=readiness,
        corpus_digest=verified.content_sha256,
        baseline_digest=verified.baseline_sha256,
    )


def _condition_groups(path: str | None) -> dict[str, str] | None:
    if path is None:
        return None
    value = load_json_object(Path(path).resolve())
    if not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ResearchCorpusError(
            "Condition groups must map Execution IDs to text labels"
        )
    return {str(key): str(item) for key, item in value.items()}


def _builder(options: argparse.Namespace) -> ResearchCorpusBuilder:
    return ResearchCorpusBuilder(
        options.runtime_root,
        evaluation_suites_root=options.evaluation_suites_root,
        project_root=options.project_root,
    )


def _sandbox(options: argparse.Namespace) -> DockerResearchSandbox:
    return DockerResearchSandbox(
        docker_command=options.docker_command,
        image=options.research_image,
    )


def _model(options: argparse.Namespace) -> ModelConfiguration:
    default = ModelConfiguration.from_project_configuration()
    return ModelConfiguration(
        provider=options.provider or default.provider,
        model=options.model or default.model,
        thinking=options.thinking or default.thinking,
    )


def _offline_workflow(options: argparse.Namespace) -> ResearchWorkflow:
    return ResearchWorkflow(
        options.research_root,
        repository_root=options.project_root,
    )


def _agent_workflow(options: argparse.Namespace) -> ResearchWorkflow:
    sandbox = _sandbox(options)
    runtime = ResearchPiAgentRuntime(
        agent_runs_root=options.agent_runs_root,
        research_extension_path=options.research_tools_extension,
        research_output_extension_path=options.research_output_extension,
        research_harness_context_path=options.research_harness_context,
        sandbox=sandbox,
        model=_model(options),
        pi_command=options.pi_command,
        repository_root=options.project_root,
    )
    orchestrator = MultiPiOrchestrator(
        runtime=runtime,
        specs=default_agent_specs(options.prompts_root),
        max_parallel_agents=1,
    )
    return ResearchWorkflow(
        options.research_root,
        orchestrator=orchestrator,
        repository_root=options.project_root,
    )


def _assess(options: argparse.Namespace) -> int:
    readiness = _builder(options).assess_readiness(
        skill_id=options.skill_id,
        execution_ids=options.execution_ids,
        objectives=options.objectives,
        evaluation_suite_id=options.evaluation_suite_id,
        condition_groups=_condition_groups(options.condition_groups_file),
    )
    _print(readiness)
    return 0 if readiness["status"] == "ready" else 3


def _build(options: argparse.Namespace) -> int:
    corpus = _builder(options).build(
        skill_id=options.skill_id,
        execution_ids=options.execution_ids,
        objectives=options.objectives,
        destination=options.destination,
        evaluation_suite_id=options.evaluation_suite_id,
        condition_groups=_condition_groups(options.condition_groups_file),
    )
    _print(
        {
            "status": "ready",
            "corpus_id": corpus.corpus_id,
            "corpus_digest": corpus.corpus_digest,
            "baseline_digest": corpus.baseline_digest,
            "directory": str(corpus.directory),
            "readiness": corpus.readiness,
        }
    )
    return 0


def _prepare(options: argparse.Namespace) -> int:
    batch = _offline_workflow(options).prepare(
        _load_corpus(options.corpus_directory),
        batch_id=options.batch_id,
    )
    _print(batch)
    return 0


def _status(options: argparse.Namespace) -> int:
    _print(_offline_workflow(options).load(options.batch_id))
    return 0


def _validate_harness(options: argparse.Namespace) -> int:
    batch = _offline_workflow(options).run_harness_validation(
        options.batch_id,
        sandbox=_sandbox(options),
        pi_command=options.pi_command,
        research_harness_context_path=options.research_harness_context,
    )
    _print(batch)
    return 0 if batch["status"] == "harness_validated" else 3


def _freeze_benchmark(options: argparse.Namespace) -> int:
    batch = _offline_workflow(options).freeze_validation_benchmark(
        options.batch_id,
        benchmark_file=options.benchmark_file,
    )
    _print(batch)
    return 0


def _run_smoke(options: argparse.Namespace) -> int:
    batch = _agent_workflow(options).run_single_agent_validation_cycle(
        options.batch_id,
        repair_summary=options.repair_summary,
        repair_categories=options.repair_categories,
    )
    _print(batch)
    return 0 if batch["status"] != "failed" else 3


def _review_smoke(options: argparse.Namespace) -> int:
    checks = {
        name: getattr(options, name) == "pass" for name in REVIEW_CHECKS
    }
    batch = _offline_workflow(options).review_single_agent_attempt(
        options.batch_id,
        attempt_id=options.attempt_id,
        reviewer=options.reviewer,
        checks=checks,
        failure_categories=options.failure_categories,
    )
    _print(batch)
    return 0 if batch["status"] != "failed" else 3


def _issue_capability(options: argparse.Namespace) -> int:
    batch = _agent_workflow(options).issue_capability_certification(
        options.batch_id
    )
    _print(batch)
    return 0


def _import_capability(options: argparse.Namespace) -> int:
    batch = _agent_workflow(options).import_capability_certification(
        options.batch_id,
        source_batch_id=options.source_batch_id,
    )
    _print(batch)
    return 0


def _run_specialists(options: argparse.Namespace) -> int:
    batch = _agent_workflow(options).run_specialists(options.batch_id)
    _print(batch)
    return 0 if batch["status"] == "specialists_completed" else 3


def _retry_specialist(options: argparse.Namespace) -> int:
    batch = _agent_workflow(options).retry_specialist(
        options.batch_id,
        role=AgentRole(options.role),
    )
    _print(batch)
    return 0 if batch["status"] == "specialists_completed" else 3


def _board(options: argparse.Namespace) -> int:
    _print(_offline_workflow(options).specialist_board(options.batch_id))
    return 0


def _check_prompts(options: argparse.Namespace) -> int:
    roles = (
        [AgentRole.BEHAVIOR_PATTERN]
        if options.mode == "smoke"
        else list(ACTIVE_SPECIALIST_ROLES)
    )
    specs = default_agent_specs(options.prompts_root)
    checked = []
    for role in roles:
        approved = load_approved_prompt(specs[role].prompt_path)
        checked.append(
            {
                "role": role.value,
                "prompt_id": approved.prompt_id,
                "version": approved.version,
                "content_sha256": approved.content_sha256,
            }
        )
    load_approved_prompt(options.research_harness_context)
    _print({"status": "approved", "prompts": checked})
    return 0


def _add_batch_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-id", required=True)


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skill-id", required=True)
    parser.add_argument(
        "--execution-id",
        dest="execution_ids",
        action="append",
        required=True,
    )
    parser.add_argument(
        "--objective",
        dest="objectives",
        action="append",
        choices=sorted(RESEARCH_OBJECTIVES),
        required=True,
    )
    parser.add_argument("--evaluation-suite-id")
    parser.add_argument("--condition-groups-file")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--docker-command")
    parser.add_argument("--research-image", default="python:3.11-slim")
    parser.add_argument("--pi-command")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking")


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
    )
    parser.add_argument("--runtime-root", default=".skill-evolution")
    parser.add_argument(
        "--evaluation-suites-root",
        default="evaluation-suites",
    )
    parser.add_argument(
        "--research-root",
        default=".skill-evolution/internal-research/batches",
    )
    parser.add_argument(
        "--agent-runs-root",
        default=".skill-evolution/internal-research/agent-runs",
    )
    parser.add_argument("--prompts-root", default="prompts/analysis")
    parser.add_argument(
        "--research-tools-extension",
        default="extensions/research-tools.ts",
    )
    parser.add_argument(
        "--research-output-extension",
        default="extensions/research-output.ts",
    )
    parser.add_argument(
        "--research-harness-context",
        default="prompts/analysis/research-harness-context-v1.json",
    )
    _add_runtime_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    assess = subparsers.add_parser(
        "assess",
        help="Return ready or exact evidence collection requirements",
    )
    _add_selection(assess)
    assess.set_defaults(handler=_assess)

    build = subparsers.add_parser(
        "build-corpus",
        help="Freeze one ready, content-addressed research corpus",
    )
    _add_selection(build)
    build.add_argument("--destination", required=True)
    build.set_defaults(handler=_build)

    prepare = subparsers.add_parser(
        "prepare",
        help="Create an internal batch from a verified ready corpus",
    )
    prepare.add_argument("--corpus-directory", required=True)
    prepare.add_argument("--batch-id")
    prepare.set_defaults(handler=_prepare)

    status = subparsers.add_parser("status", help="Read one batch state")
    _add_batch_identity(status)
    status.set_defaults(handler=_status)

    harness = subparsers.add_parser(
        "validate-harness",
        help="Execute and freeze all deterministic Harness acceptance checks",
    )
    _add_batch_identity(harness)
    harness.set_defaults(handler=_validate_harness)

    benchmark = subparsers.add_parser(
        "freeze-benchmark",
        help="Freeze an approved hidden benchmark outside Agent evidence",
    )
    _add_batch_identity(benchmark)
    benchmark.add_argument("--benchmark-file", required=True)
    benchmark.set_defaults(handler=_freeze_benchmark)

    smoke = subparsers.add_parser(
        "run-smoke",
        help="Run exactly two independent behavior research attempts",
    )
    _add_batch_identity(smoke)
    smoke.add_argument("--repair-summary")
    smoke.add_argument(
        "--repair-category",
        dest="repair_categories",
        action="append",
        choices=sorted(FAILURE_CATEGORIES),
    )
    smoke.set_defaults(handler=_run_smoke)

    review = subparsers.add_parser(
        "review-smoke",
        help="Record one human review against the frozen hidden benchmark",
    )
    _add_batch_identity(review)
    review.add_argument("--attempt-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument(
        "--failure-category",
        dest="failure_categories",
        action="append",
        choices=sorted(FAILURE_CATEGORIES),
    )
    for name in REVIEW_CHECKS:
        review.add_argument(
            f"--{name.replace('_', '-')}",
            choices=("pass", "fail"),
            required=True,
        )
    review.set_defaults(handler=_review_smoke)

    issue = subparsers.add_parser(
        "issue-capability",
        help="Seal the two reviewed smoke attempts as a capability certificate",
    )
    _add_batch_identity(issue)
    issue.set_defaults(handler=_issue_capability)

    capability_import = subparsers.add_parser(
        "import-capability",
        help="Import an issued certificate into a Harness-validated batch",
    )
    _add_batch_identity(capability_import)
    capability_import.add_argument("--source-batch-id", required=True)
    capability_import.set_defaults(handler=_import_capability)

    specialists = subparsers.add_parser(
        "run-specialists",
        help="Run four isolated specialists without synthesis",
    )
    _add_batch_identity(specialists)
    specialists.set_defaults(handler=_run_specialists)

    retry = subparsers.add_parser(
        "retry-specialist",
        help="Retry one failed specialist as a new append-only attempt",
    )
    _add_batch_identity(retry)
    retry.add_argument(
        "--role",
        choices=[role.value for role in ACTIVE_SPECIALIST_ROLES],
        required=True,
    )
    retry.set_defaults(handler=_retry_specialist)

    board = subparsers.add_parser(
        "board",
        help="Read the non-aggregated four-specialist result board",
    )
    _add_batch_identity(board)
    board.set_defaults(handler=_board)

    prompts = subparsers.add_parser(
        "check-prompts",
        help="Require approved role protocols and Harness context",
    )
    prompts.add_argument(
        "--mode",
        choices=("smoke", "specialists"),
        required=True,
    )
    prompts.set_defaults(handler=_check_prompts)

    options = parser.parse_args(arguments)
    return int(options.handler(options))


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (
        FileNotFoundError,
        HarnessAcceptanceError,
        OSError,
        PromptApprovalError,
        ResearchAgentRuntimeError,
        ResearchCapabilityError,
        ResearchCorpusError,
        ResearchSandboxError,
        ResearchWorkflowError,
        StorageError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
