#!/usr/bin/env python3
"""Run one approved semantic error analysis from a frozen trajectory precheck."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.agents import AgentRole, AgentSpec
from skill_evolution.analysis import (
    AnalysisContractError,
    load_approved_skill_contract,
)
from skill_evolution.evidence import (
    EvidenceError,
    SingleTrajectoryEvidenceBundleBuilder,
)
from skill_evolution.layout import RuntimeLayout
from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    SkillHierarchyRepository,
)
from skill_evolution.pi_runtime import PiAgentRuntime, PiAgentRuntimeError
from skill_evolution.storage import (
    StorageError,
    load_json_object,
    new_object_id,
    utc_now,
)
from skill_evolution.trajectory_user_report import (
    write_trajectory_user_report_from_agent_run,
)


DEFAULT_PROMPT = "prompts/analysis/trajectory-error-analysis-v2.md"
DEFAULT_ANALYZER_CONTRACT = "skills/analyze-single-trajectory/skill_contract.json"
DEFAULT_EXTENSION = "extensions/root-jail.ts"
DEFAULT_OUTPUT_EXTENSION = "extensions/trajectory-error-output.ts"


def _precheck_context(precheck: Mapping[str, object]) -> dict[str, object]:
    if precheck.get("schema") != "trajectory.precheck.v1":
        raise ValueError("Unsupported trajectory precheck schema")
    run_id = precheck.get("run_id")
    deterministic_status = precheck.get("deterministic_status")
    integrity = precheck.get("integrity")
    signals = precheck.get("signals")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Trajectory precheck requires run_id")
    if not isinstance(deterministic_status, str) or not deterministic_status:
        raise ValueError("Trajectory precheck requires deterministic_status")
    if not isinstance(integrity, Mapping):
        raise ValueError("Trajectory precheck requires integrity")
    integrity_status = integrity.get("status")
    if integrity_status not in {"valid", "invalid", "incomplete"}:
        raise ValueError("Trajectory precheck has unsupported integrity status")
    if not isinstance(signals, list):
        raise ValueError("Trajectory precheck signals must be a list")
    signal_ids: list[str] = []
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise ValueError("Trajectory precheck signals must be objects")
        signal_id = signal.get("id")
        if not isinstance(signal_id, str) or not signal_id:
            raise ValueError("Trajectory precheck signal requires id")
        signal_ids.append(signal_id)
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("Trajectory precheck signal ids must be unique")
    return {
        "run_id": run_id,
        "precheck_deterministic_status": deterministic_status,
        "precheck_integrity_status": integrity_status,
        "precheck_signal_ids": signal_ids,
    }


def run_single_trajectory_analysis(
    *,
    trajectory_path: str | os.PathLike[str],
    precheck_path: str | os.PathLike[str],
    runtime_root: str | os.PathLike[str] = ".skill-evolution",
    prompt_path: str | os.PathLike[str] = DEFAULT_PROMPT,
    analyzer_contract_path: str | os.PathLike[str] = (
        DEFAULT_ANALYZER_CONTRACT
    ),
    subject_contract_path: str | os.PathLike[str] | None = None,
    task_context_path: str | os.PathLike[str] | None = None,
    extension_path: str | os.PathLike[str] = DEFAULT_EXTENSION,
    output_extension_path: str | os.PathLike[str] = (
        DEFAULT_OUTPUT_EXTENSION
    ),
    timeout_seconds: float = 900.0,
    pi_command: Sequence[str] | str | None = None,
    subject_skill_id: str | None = None,
    subject_execution_id: str | None = None,
) -> dict[str, object]:
    """Freeze one input, then run one independent TrajectoryErrorAnalyst."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    analyzer_contract = load_approved_skill_contract(
        analyzer_contract_path
    )
    if subject_contract_path is not None:
        load_approved_skill_contract(subject_contract_path)

    precheck_source = Path(precheck_path).resolve()
    precheck = load_json_object(precheck_source)
    context = _precheck_context(precheck)
    run_id = str(context["run_id"])
    if (subject_skill_id is None) != (subject_execution_id is None):
        raise ValueError(
            "subject_skill_id and subject_execution_id must be provided together"
        )

    spec = AgentSpec(
        role=AgentRole.TRAJECTORY_ERROR_ANALYST,
        prompt_path=Path(prompt_path).resolve(),
        tool_mode="read_only",
        timeout_seconds=timeout_seconds,
    )
    analysis_id = new_object_id("trajectory-analysis")
    hierarchy: SkillHierarchyRepository | None = None
    analysis_record: dict[str, object] | None = None
    analysis_directory: Path | None = None
    if subject_skill_id is not None and subject_execution_id is not None:
        hierarchy = SkillHierarchyRepository(runtime_root)
        execution = hierarchy.load_execution(
            subject_skill_id, subject_execution_id
        )
        if execution["execution_id"] != run_id:
            raise ValueError(
                "Trajectory precheck run_id does not match the subject execution"
            )
        analysis_record = {
            "schema": ANALYSIS_RECORD_SCHEMA,
            "analysis_id": analysis_id,
            "skill_id": subject_skill_id,
            "revision_id": execution["revision_id"],
            "scope": "single_execution",
            "execution_id": subject_execution_id,
            "execution_set_id": None,
            "kind": "trajectory_error",
            "producer": "agent",
            "status": "running",
            "input_refs": [
                {"kind": "trajectory", "execution_id": subject_execution_id},
                {"kind": "precheck", "path": str(precheck_source)},
            ],
            "result_refs": [],
            "attempts": [],
            "created_at": utc_now(),
            "ended_at": None,
            "provenance": None,
        }
        prospective = hierarchy.analysis_directory(analysis_record)
        agent_runs_root = prospective / "attempts"
        evidence_destination = prospective / "evidence" / new_object_id(
            "evidence"
        )
    else:
        layout = RuntimeLayout.from_root(runtime_root)
        layout.ensure_legacy()
        agent_runs_root = layout.agent_runs
        evidence_bundle_id = new_object_id("evidence")
        evidence_destination = (
            layout.analyses / "evidence-bundles" / evidence_bundle_id
        )
    runtime = PiAgentRuntime(
        agent_runs_root=agent_runs_root,
        extension_path=extension_path,
        structured_output_extension_path=output_extension_path,
        pi_command=pi_command,
    )
    runtime.preflight([spec])
    if hierarchy is not None and analysis_record is not None:
        analysis_directory, analysis_record = hierarchy.create_analysis(
            analysis_record
        )
        evidence_bundle_id = evidence_destination.name
    evidence_bundle = SingleTrajectoryEvidenceBundleBuilder().build(
        trajectory_path=trajectory_path,
        precheck_path=precheck_source,
        destination=evidence_destination,
        analyzer_contract_path=analyzer_contract_path,
        subject_contract_path=subject_contract_path,
        task_context_path=task_context_path,
    )
    bundle = load_json_object(evidence_bundle / "bundle.json")
    run_record = bundle["runs"][0]
    assert isinstance(run_record, Mapping)
    context.update(
        {
            "analysis_id": analysis_id,
            "evidence_bundle_id": evidence_bundle_id,
            "evidence_root": ".",
            "trajectory_path": run_record["trajectory"],
            "trajectory_precheck_path": bundle["precheck"],
            "analyzer_contract_path": bundle["analyzer_contract"],
            "analyzer_contract": {
                "skill_id": analyzer_contract["skill_id"],
                "version": analyzer_contract["version"],
                "approved_by": analyzer_contract["approved_by"],
                "approved_at": analyzer_contract["approved_at"],
            },
            "skill_contract_path": bundle.get("subject_contract"),
            "task_context_path": bundle.get("task_context"),
            "artifact_paths": run_record.get("artifacts", []),
            "single_run_only": True,
        }
    )
    result = runtime.run(
        spec=spec,
        campaign_id=analysis_id,
        round_number=1,
        context=context,
        evidence_bundle=evidence_bundle,
    )
    user_report_path = write_trajectory_user_report_from_agent_run(
        result.run_directory
    )
    if (
        hierarchy is not None
        and analysis_record is not None
        and analysis_directory is not None
    ):
        relative_attempt = result.run_directory.relative_to(
            analysis_directory
        ).as_posix()
        analysis_record["status"] = _analysis_status(result.status)
        analysis_record["ended_at"] = utc_now()
        analysis_record["attempts"] = [
            {
                "agent_run_id": result.agent_run_id,
                "path": relative_attempt,
                "status": result.status,
            }
        ]
        analysis_record["result_refs"] = [
            {
                "schema": "analysis.single_trajectory_view.v1",
                "path": user_report_path.relative_to(
                    analysis_directory
                ).as_posix(),
            }
        ]
        if (result.run_directory / "result.json").is_file():
            analysis_record["result_refs"].append(
                {
                    "schema": "analysis.trajectory_error_report.v1",
                    "path": (
                        Path(relative_attempt) / "result.json"
                    ).as_posix(),
                }
            )
        hierarchy.replace_analysis(analysis_record)
    return {
        "schema": "analysis.trajectory_error_invocation.v1",
        "analysis_id": analysis_id,
        "run_id": run_id,
        "status": result.status,
        "agent_run_id": result.agent_run_id,
        "agent_run_directory": str(result.run_directory),
        "evidence_bundle_id": evidence_bundle_id,
        "evidence_bundle": str(evidence_bundle),
        "user_report": str(user_report_path),
        "result": result.result,
        "error": result.error,
        "skill_id": subject_skill_id,
        "execution_id": subject_execution_id,
        "hierarchy_analysis": (
            str(analysis_directory) if analysis_directory is not None else None
        ),
    }


def _analysis_status(agent_status: str) -> str:
    if agent_status == "succeeded":
        return "accepted"
    if agent_status in {
        "invalid_output",
        "failed",
        "timed_out",
        "indeterminate",
    }:
        return agent_status
    return "failed"


def run_execution_trajectory_analysis(
    *,
    runtime_root: str | os.PathLike[str],
    skill_id: str,
    execution_id: str,
    precheck_path: str | os.PathLike[str],
    prompt_path: str | os.PathLike[str] = DEFAULT_PROMPT,
    analyzer_contract_path: str | os.PathLike[str] = DEFAULT_ANALYZER_CONTRACT,
    extension_path: str | os.PathLike[str] = DEFAULT_EXTENSION,
    output_extension_path: str | os.PathLike[str] = (
        DEFAULT_OUTPUT_EXTENSION
    ),
    timeout_seconds: float = 900.0,
    pi_command: Sequence[str] | str | None = None,
) -> dict[str, object]:
    """Analyze one canonical hierarchy Execution."""

    hierarchy = SkillHierarchyRepository(runtime_root)
    execution = hierarchy.load_execution(skill_id, execution_id)
    execution_directory = hierarchy.execution_directory(skill_id, execution_id)
    trajectory_relative = execution["trajectory"]["path"]
    if not isinstance(trajectory_relative, str):
        raise ValueError("Execution does not contain a trajectory")
    trajectory_path = execution_directory / trajectory_relative
    revision = hierarchy.load_revision(skill_id, str(execution["revision_id"]))
    contract_relative = revision["contract"]["path"]
    subject_contract = (
        hierarchy.revision_directory(skill_id, str(execution["revision_id"]))
        / str(contract_relative)
        if isinstance(contract_relative, str)
        else None
    )
    return run_single_trajectory_analysis(
        trajectory_path=trajectory_path,
        precheck_path=precheck_path,
        runtime_root=runtime_root,
        prompt_path=prompt_path,
        analyzer_contract_path=analyzer_contract_path,
        subject_contract_path=subject_contract,
        extension_path=extension_path,
        output_extension_path=output_extension_path,
        timeout_seconds=timeout_seconds,
        pi_command=pi_command,
        subject_skill_id=skill_id,
        subject_execution_id=execution_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one approved semantic analysis from a frozen trajectory precheck."
        )
    )
    parser.add_argument(
        "trajectory",
        nargs="?",
        help="Legacy path to exactly one trajectory JSONL file",
    )
    parser.add_argument(
        "--precheck",
        required=True,
        help="Path to its frozen trajectory.precheck.v1 report",
    )
    parser.add_argument("--runtime-root", default=".skill-evolution")
    parser.add_argument("--skill-id")
    parser.add_argument("--execution-id")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--analyzer-contract",
        default=DEFAULT_ANALYZER_CONTRACT,
    )
    parser.add_argument("--subject-contract")
    parser.add_argument("--task-context")
    parser.add_argument("--extension", default=DEFAULT_EXTENSION)
    parser.add_argument(
        "--output-extension",
        default=DEFAULT_OUTPUT_EXTENSION,
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--pi-command")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the CLI and return a status suitable for automation."""

    options = _parser().parse_args(arguments)
    try:
        if options.skill_id is not None or options.execution_id is not None:
            if options.skill_id is None or options.execution_id is None:
                raise ValueError(
                    "--skill-id and --execution-id must be provided together"
                )
            if options.trajectory is not None:
                raise ValueError(
                    "Do not provide a legacy trajectory path with --execution-id"
                )
            invocation = run_execution_trajectory_analysis(
                runtime_root=options.runtime_root,
                skill_id=options.skill_id,
                execution_id=options.execution_id,
                precheck_path=options.precheck,
                prompt_path=options.prompt,
                analyzer_contract_path=options.analyzer_contract,
                extension_path=options.extension,
                output_extension_path=options.output_extension,
                timeout_seconds=options.timeout,
                pi_command=options.pi_command,
            )
        else:
            if options.trajectory is None:
                raise ValueError(
                    "Provide a trajectory path or --skill-id/--execution-id"
                )
            invocation = run_single_trajectory_analysis(
                trajectory_path=options.trajectory,
                precheck_path=options.precheck,
                runtime_root=options.runtime_root,
                prompt_path=options.prompt,
                analyzer_contract_path=options.analyzer_contract,
                subject_contract_path=options.subject_contract,
                task_context_path=options.task_context,
                extension_path=options.extension,
                output_extension_path=options.output_extension,
                timeout_seconds=options.timeout,
                pi_command=options.pi_command,
            )
    except (
        AnalysisContractError,
        EvidenceError,
        PiAgentRuntimeError,
        StorageError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(invocation, ensure_ascii=False, indent=2))
    return 0 if invocation["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
