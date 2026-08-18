#!/usr/bin/env python3
"""Run error-centric multi-Trajectory analysis: identify all errors, then one
subagent per error that reports only the problematic dimensions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.agents import (
    AgentRole,
    ModelConfiguration,
    default_agent_specs,
)
from skill_evolution.research_agent_runtime import (
    ResearchAgentRuntimeError,
    ResearchPiAgentRuntime,
)
from skill_evolution.research_corpus import (
    ResearchCorpusError,
    verify_research_corpus,
)
from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    MULTI_TRAJECTORY_ERRORS_SCHEMA,
    SkillHierarchyRepository,
)
from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    ResearchSandboxError,
)
from skill_evolution.storage import (
    atomic_write_json,
    load_json_object,
    new_object_id,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _publish_product(
    *,
    verification,
    runtime_root: str,
    identification: dict,
    reports: list[dict],
    failed: int,
    identification_agent_run_id: str | None,
) -> dict:
    """Persist the error list and per-error reports as a product record."""

    readiness = load_json_object(verification.directory / "readiness.json")
    skill_id = str(readiness["skill_id"])
    revision_id = str(readiness["revision_id"])
    hierarchy = SkillHierarchyRepository(Path(runtime_root))
    wanted = set(verification.execution_ids)
    set_id: str | None = None
    for execution_set in hierarchy.list_execution_sets(skill_id):
        if str(execution_set["revision_id"]) != revision_id:
            continue
        if set(execution_set.get("execution_ids", [])) == wanted:
            set_id = str(execution_set["set_id"])
            break
    if set_id is None:
        raise ResearchCorpusError(
            "No execution set matches the corpus executions for publishing"
        )

    analysis_id = new_object_id("multi-trajectory-analysis")
    now = utc_now()
    record: dict = {
        "schema": ANALYSIS_RECORD_SCHEMA,
        "analysis_id": analysis_id,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "scope": "execution_set",
        "execution_id": None,
        "execution_set_id": set_id,
        "kind": "multi_trajectory",
        "producer": "agent",
        "status": "running",
        "input_refs": [
            {"kind": "execution_set", "execution_set_id": set_id}
        ],
        "result_refs": [],
        "attempts": [],
        "created_at": now,
        "ended_at": None,
        "provenance": {
            "source": "error_analysis",
            "corpus_digest": verification.content_sha256,
            "baseline_digest": verification.baseline_sha256,
            "identification_agent_run": identification_agent_run_id,
        },
    }
    directory, _created = hierarchy.create_analysis(record)

    identification_scope = identification.get("scope", {}) or {}
    view: dict = {
        "schema": MULTI_TRAJECTORY_ERRORS_SCHEMA,
        "analysis_id": analysis_id,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "generated_at": now,
        "scope": {
            "eligible_trajectory_ids": identification_scope.get(
                "eligible_trajectory_ids", []
            ),
            "reviewed_trajectory_ids": identification_scope.get(
                "reviewed_trajectory_ids", []
            ),
            "counterexample_search": identification_scope.get(
                "counterexample_search", ""
            ),
        },
        "errors": [
            {
                field: error.get(field)
                for field in (
                    "error_id",
                    "title",
                    "summary",
                    "anchor_evidence",
                    "observed_trajectory_ids",
                    "checked_absent_trajectory_ids",
                    "suggested_dimensions",
                    "notes",
                )
            }
            for error in identification.get("errors", [])
        ],
        "reports": [
            {
                "error_id": item.get("error_id"),
                "dimensions": (item.get("report") or {}).get("dimensions", []),
                "limitations": (item.get("report") or {}).get(
                    "limitations", []
                ),
                "validation_warnings": (item.get("report") or {}).get(
                    "validation_warnings", []
                ),
            }
            for item in reports
            if item.get("status") == "succeeded" and item.get("report")
        ],
        "limitations": identification.get("limitations", []),
    }
    atomic_write_json(directory / "errors-view.json", view)

    record["status"] = "accepted" if failed == 0 else "unavailable"
    record["ended_at"] = now
    record["result_refs"] = [
        {
            "schema": MULTI_TRAJECTORY_ERRORS_SCHEMA,
            "path": "errors-view.json",
        }
    ]
    record["attempts"] = [
        {
            "agent_run_id": item.get("agent_run_id"),
            "status": item.get("status"),
        }
        for item in reports
        if item.get("agent_run_id")
    ]
    hierarchy.replace_analysis(record)
    return {
        "analysis_id": analysis_id,
        "skill_id": skill_id,
        "revision_id": revision_id,
        "execution_set_id": set_id,
        "directory": str(directory),
        "status": record["status"],
    }


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _model(options: argparse.Namespace) -> ModelConfiguration:
    default = ModelConfiguration.from_project_configuration()
    return ModelConfiguration(
        provider=options.provider or default.provider,
        model=options.model or default.model,
        thinking=options.thinking or default.thinking,
    )


def _sandbox(options: argparse.Namespace) -> DockerResearchSandbox:
    return DockerResearchSandbox(
        docker_command=options.docker_command,
        image=options.research_image,
    )


def _runtime(options: argparse.Namespace) -> ResearchPiAgentRuntime:
    return ResearchPiAgentRuntime(
        agent_runs_root=options.agent_runs_root,
        research_extension_path=options.research_tools_extension,
        research_output_extension_path=options.research_output_extension,
        research_harness_context_path=options.research_harness_context,
        sandbox=_sandbox(options),
        model=_model(options),
        pi_command=options.pi_command,
        repository_root=options.project_root,
    )


def _error_description(error: dict) -> dict:
    """Forward the main agent's structured error record to one subagent."""

    return {
        "error_id": error.get("error_id"),
        "title": error.get("title"),
        "summary": error.get("summary"),
        "anchor_evidence": error.get("anchor_evidence"),
        "observed_trajectory_ids": error.get("observed_trajectory_ids"),
        "checked_absent_trajectory_ids": error.get(
            "checked_absent_trajectory_ids"
        ),
        "suggested_dimensions": error.get("suggested_dimensions"),
        "notes": error.get("notes"),
    }


def _run(options: argparse.Namespace) -> int:
    corpus_directory = Path(options.corpus_directory).resolve()
    verification = verify_research_corpus(corpus_directory)
    runtime = _runtime(options)
    specs = default_agent_specs(options.prompts_root)
    identifier_spec = specs[AgentRole.ERROR_IDENTIFIER]
    analyst_spec = specs[AgentRole.ERROR_ANALYST]

    execution_identity_sha256 = runtime.current_execution_identity_sha256(
        identifier_spec
    )
    base_context = {
        "corpus_digest": verification.content_sha256,
        "baseline_digest": verification.baseline_sha256,
        "eligible_trajectory_ids": list(verification.execution_ids),
        "research_execution_identity_sha256": execution_identity_sha256,
    }
    campaign_id = f"error-analysis-{verification.content_sha256[:12]}"

    identifier_run = runtime.run(
        spec=identifier_spec,
        campaign_id=campaign_id,
        round_number=1,
        context=base_context,
        evidence_bundle=corpus_directory,
    )
    if identifier_run.status != "succeeded" or identifier_run.result is None:
        _print(
            {
                "status": "identification_failed",
                "agent_run_id": identifier_run.agent_run_id,
                "run_status": identifier_run.status,
                "error": identifier_run.error,
            }
        )
        return 3

    identification = identifier_run.result
    errors = identification.get("errors", [])

    reports: list[dict] = []
    failed = 0
    for index, error in enumerate(errors, start=1):
        analyst_context = {
            **base_context,
            "error_description": _error_description(error),
        }
        analyst_run = runtime.run(
            spec=analyst_spec,
            campaign_id=campaign_id,
            round_number=1 + index,
            context=analyst_context,
            evidence_bundle=corpus_directory,
        )
        reports.append(
            {
                "error_id": error.get("error_id"),
                "agent_run_id": analyst_run.agent_run_id,
                "status": analyst_run.status,
                "report": analyst_run.result,
                "error": analyst_run.error,
            }
        )
        if analyst_run.status != "succeeded":
            failed += 1

    product_analysis = None
    if options.publish_product:
        product_analysis = _publish_product(
            verification=verification,
            runtime_root=options.runtime_root,
            identification=identification,
            reports=reports,
            failed=failed,
            identification_agent_run_id=identifier_run.agent_run_id,
        )

    _print(
        {
            "status": "succeeded" if failed == 0 else "partial",
            "corpus_digest": verification.content_sha256,
            "baseline_digest": verification.baseline_sha256,
            "identification_agent_run": identifier_run.agent_run_id,
            "error_count": len(errors),
            "failed_report_count": failed,
            "identification": identification,
            "reports": reports,
            "product_analysis": product_analysis,
        }
    )
    return 0 if failed == 0 else 3


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
    )
    parser.add_argument(
        "--agent-runs-root",
        default=".skill-evolution/internal-research/agent-runs",
    )
    parser.add_argument(
        "--prompts-root",
        default="prompts/analysis",
    )
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
    parser.add_argument("--docker-command")
    parser.add_argument("--research-image", default="python:3.11-slim")
    parser.add_argument("--pi-command")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking", default="max")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help=(
            "Identify all errors with the main agent, then analyze each "
            "error with one subagent"
        ),
    )
    run.add_argument(
        "--corpus-directory",
        required=True,
        help="Frozen verified research corpus directory",
    )
    run.add_argument(
        "--publish-product",
        action="store_true",
        help=(
            "Persist the error list and reports as a product "
            "multi-trajectory analysis record"
        ),
    )
    run.add_argument(
        "--runtime-root",
        default=".skill-evolution",
        help="Product hierarchy runtime root for --publish-product",
    )
    run.set_defaults(handler=_run)

    options = parser.parse_args(arguments)
    return int(options.handler(options))


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (
        ResearchAgentRuntimeError,
        ResearchCorpusError,
        ResearchSandboxError,
        KeyError,
    ) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(3)
