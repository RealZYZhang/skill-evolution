#!/usr/bin/env python3
"""Run one approved task N times and preserve every trajectory and session."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prompt_approval import (
    ApprovedPrompt,
    RenderedPrompt,
    load_approved_prompt,
    render_execution_prompt,
)
from scripts.task_case import (
    DEFAULT_EXPECTED_ARTIFACTS,
    TaskCase,
    load_task_case,
)
from scripts.trajectory_spike import TrajectoryResult, run_trajectory_spike
from skill_evolution.analysis import load_approved_skill_contract
from skill_evolution.hierarchy import (
    SkillHierarchyRepository,
)
from skill_evolution.storage import new_object_id


REPLAY_SCHEMA = "replay.campaign.v1"
TrajectoryRunner = Callable[..., TrajectoryResult]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _new_campaign_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _validate_inputs(
    skill_path: Path,
    replay_count: int,
    timeout: float,
) -> dict[str, Any]:
    if not (skill_path / "SKILL.md").is_file():
        raise FileNotFoundError(
            f"Skill entrypoint not found: {skill_path}"
        )
    if replay_count <= 0:
        raise ValueError("replay_count must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    return load_approved_skill_contract(skill_path / "skill_contract.json")


@dataclass(frozen=True)
class ReplayCampaignResult:
    """Final replay campaign directory and manifest."""

    campaign_directory: Path
    manifest: dict[str, Any]
    skill_id: str | None = None
    revision_id: str | None = None
    execution_set_id: str | None = None


def _prompt_manifest(
    template: ApprovedPrompt,
    rendered: RenderedPrompt,
    campaign_directory: Path,
) -> dict[str, Any]:
    prompt_directory = campaign_directory / "prompt"
    prompt_directory.mkdir()
    template_snapshot = prompt_directory / "template.md"
    approval_snapshot = prompt_directory / "approval.json"
    rendered_snapshot = prompt_directory / "rendered.md"
    shutil.copy2(template.path, template_snapshot)
    shutil.copy2(template.approval_path, approval_snapshot)
    rendered_snapshot.write_text(rendered.text, encoding="utf-8")
    return {
        "prompt_id": template.prompt_id,
        "version": template.version,
        "status": "approved",
        "approved_by": template.approved_by,
        "approved_at": template.approved_at,
        "template_sha256": template.content_sha256,
        "template_snapshot": "prompt/template.md",
        "approval_snapshot": "prompt/approval.json",
        "rendered_snapshot": "prompt/rendered.md",
        "rendered_bytes": len(rendered.text.encode("utf-8")),
        "skill_entrypoint": str(rendered.skill_path),
    }


def run_replay_campaign(
    *,
    skill_path: str | os.PathLike[str],
    source_path: str | os.PathLike[str] | None = None,
    task_case: TaskCase | None = None,
    prompt_path: str | os.PathLike[str],
    replay_count: int,
    output_root: str | os.PathLike[str] | None = None,
    runtime_root: str | os.PathLike[str] = ".skill-evolution",
    timeout: float = 900.0,
    pi_command: Sequence[str] | str | None = None,
    extra_pi_args: Sequence[str] = (),
    trajectory_runner: TrajectoryRunner = run_trajectory_spike,
) -> ReplayCampaignResult:
    """Run a fixed, approved task repeatedly and retain every attempt."""

    resolved_skill = Path(skill_path).resolve()
    if task_case is not None and source_path is not None:
        raise ValueError("Provide task_case or source_path, not both")
    if task_case is None:
        if source_path is None:
            raise ValueError("task_case or source_path is required")
        resolved_task_case = TaskCase.for_file(source_path)
    else:
        resolved_task_case = task_case
    approved_prompt = load_approved_prompt(prompt_path)
    skill_contract = _validate_inputs(
        resolved_skill,
        replay_count,
        timeout,
    )
    rendered_prompt = render_execution_prompt(
        approved_prompt,
        resolved_skill,
        resolved_task_case.prompt_payload(),
    )

    if output_root is None:
        return _run_hierarchy_replay(
            resolved_skill=resolved_skill,
            resolved_task_case=resolved_task_case,
            approved_prompt=approved_prompt,
            rendered_prompt=rendered_prompt,
            skill_contract=skill_contract,
            replay_count=replay_count,
            runtime_root=runtime_root,
            timeout=timeout,
            pi_command=pi_command,
            extra_pi_args=extra_pi_args,
            trajectory_runner=trajectory_runner,
        )

    campaign_id = _new_campaign_id()
    campaign_directory = Path(output_root).resolve() / campaign_id
    runs_directory = campaign_directory / "runs"
    runs_directory.mkdir(parents=True)
    prompt_record = _prompt_manifest(
        approved_prompt,
        rendered_prompt,
        campaign_directory,
    )

    started_at = _utc_now()
    started_monotonic = time.monotonic()
    manifest: dict[str, Any] = {
        "schema": REPLAY_SCHEMA,
        "campaign_id": campaign_id,
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "replay_count_requested": replay_count,
        "skill": {
            "source_path": str(resolved_skill),
            "contract": {
                "package_file": "skill_contract.json",
                "schema": skill_contract["schema"],
                "skill_id": skill_contract["skill_id"],
                "version": skill_contract["version"],
                "approved_by": skill_contract["approved_by"],
                "approved_at": skill_contract["approved_at"],
            },
        },
        "task": {
            "task_case": resolved_task_case.record_payload(),
            "source_path": (
                str(resolved_task_case.source_path)
                if resolved_task_case.source_path is not None
                else None
            ),
            "prompt": prompt_record,
        },
        "execution": {
            "mode": "sequential",
            "timeout_seconds": timeout,
        },
        "runs": [],
        "summary": {
            "trajectory_count": 0,
            "succeeded": 0,
            "failed": 0,
            "orchestration_failed": 0,
        },
    }
    manifest_path = campaign_directory / "replay.json"
    _atomic_write_json(manifest_path, manifest)

    for index in range(1, replay_count + 1):
        attempt_started_at = _utc_now()
        try:
            result = trajectory_runner(
                skill_path=resolved_skill,
                task_case=resolved_task_case,
                prompt=rendered_prompt.text,
                output_root=runs_directory,
                timeout=timeout,
                pi_command=pi_command,
                extra_pi_args=extra_pi_args,
            )
            relative_run_path = str(
                result.run_directory.relative_to(campaign_directory)
            )
            run_record: dict[str, Any] = {
                "index": index,
                "status": result.outcome["status"],
                "started_at": result.outcome.get(
                    "started_at",
                    attempt_started_at,
                ),
                "ended_at": result.outcome.get("ended_at"),
                "duration_ms": result.outcome.get("duration_ms"),
                "run_id": result.run_directory.name,
                "path": relative_run_path,
                "trajectory": f"{relative_run_path}/trajectory.jsonl",
                "session": f"{relative_run_path}/pi-session.jsonl",
                "session_status": result.outcome["session"]["status"],
                "artifact": result.outcome.get("artifact"),
                "artifacts": result.outcome.get("artifacts", []),
                "failure_stage": result.outcome.get("failure_stage"),
                "error": result.outcome.get("error"),
            }
            manifest["summary"]["trajectory_count"] += 1
            manifest["summary"][result.outcome["status"]] += 1
        except Exception as error:
            run_record = {
                "index": index,
                "status": "orchestration_failed",
                "started_at": attempt_started_at,
                "ended_at": _utc_now(),
                "run_id": None,
                "path": None,
                "trajectory": None,
                "session": None,
                "session_status": None,
                "failure_stage": "create_trajectory",
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
            manifest["summary"]["orchestration_failed"] += 1

        manifest["runs"].append(run_record)
        _atomic_write_json(manifest_path, manifest)

    summary = manifest["summary"]
    if summary["trajectory_count"] != replay_count:
        status = "failed"
    elif summary["failed"]:
        status = "completed_with_run_failures"
    else:
        status = "completed"
    manifest["status"] = status
    manifest["ended_at"] = _utc_now()
    manifest["duration_ms"] = round(
        (time.monotonic() - started_monotonic) * 1000
    )
    _atomic_write_json(manifest_path, manifest)
    return ReplayCampaignResult(campaign_directory, manifest)


def _run_hierarchy_replay(
    *,
    resolved_skill: Path,
    resolved_task_case: TaskCase,
    approved_prompt: ApprovedPrompt,
    rendered_prompt: RenderedPrompt,
    skill_contract: Mapping[str, Any],
    replay_count: int,
    runtime_root: str | os.PathLike[str],
    timeout: float,
    pi_command: Sequence[str] | str | None,
    extra_pi_args: Sequence[str],
    trajectory_runner: TrajectoryRunner,
) -> ReplayCampaignResult:
    """Create an ExecutionSet whose members are direct Skill children."""

    hierarchy = SkillHierarchyRepository(runtime_root)
    revision = hierarchy.register_revision(resolved_skill)
    skill_id = str(revision.manifest["skill_id"])
    revision_id = str(revision.manifest["revision_id"])
    set_id = new_object_id("set")
    set_manifest = hierarchy.create_execution_set(
        skill_id=skill_id,
        revision_id=revision_id,
        purpose="replay",
        task={
            "task_case": resolved_task_case.record_payload(),
            "source_path": (
                str(resolved_task_case.source_path)
                if resolved_task_case.source_path is not None
                else None
            ),
        },
        runtime={"mode": "sequential", "timeout_seconds": timeout},
        provenance={"workflow": "replay", "legacy_campaign_id": None},
        set_id=set_id,
    )
    set_directory = hierarchy.execution_set_directory(skill_id, set_id)
    prompt_record = _prompt_manifest(
        approved_prompt,
        rendered_prompt,
        set_directory,
    )
    set_manifest["task"]["prompt"] = prompt_record
    set_manifest["status"] = "running"
    set_manifest = hierarchy.replace_execution_set(
        skill_id, set_id, set_manifest
    )

    started = time.monotonic()
    succeeded = 0
    failed = 0
    for _index in range(1, replay_count + 1):
        try:
            result = trajectory_runner(
                skill_path=resolved_skill,
                task_case=resolved_task_case,
                prompt=rendered_prompt.text,
                timeout=timeout,
                pi_command=pi_command,
                extra_pi_args=extra_pi_args,
                hierarchy_root=runtime_root,
                execution_origin="replay",
                execution_set_id=set_id,
            )
            execution = result.execution_manifest
            if execution is None:
                raise RuntimeError(
                    "Hierarchy replay runner did not return an execution"
                )
        except Exception as error:
            prepared = hierarchy.prepare_execution(
                skill_id=skill_id,
                revision_id=revision_id,
                origin="replay",
                execution_set_id=set_id,
            )
            execution = dict(prepared.manifest)
            execution.update(
                {
                    "status": "orchestration_failed",
                    "ended_at": _utc_now(),
                    "duration_ms": 0,
                    "task": resolved_task_case.record_payload(),
                    "setup": {
                        "orchestration_error": {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                    },
                }
            )
            hierarchy.finalize_execution(
                skill_id,
                str(execution["execution_id"]),
                execution,
            )
        execution_id = str(execution["execution_id"])
        set_manifest["execution_ids"].append(execution_id)
        if execution["status"] == "succeeded":
            succeeded += 1
        else:
            failed += 1
        set_manifest = hierarchy.replace_execution_set(
            skill_id, set_id, set_manifest
        )

    set_manifest["status"] = (
        "completed" if failed == 0 else "completed_with_failures"
    )
    set_manifest["ended_at"] = _utc_now()
    set_manifest["runtime"] = {
        **dict(set_manifest["runtime"]),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "requested": replay_count,
        "succeeded": succeeded,
        "failed": failed,
        "contract": {
            "schema": skill_contract["schema"],
            "skill_id": skill_contract["skill_id"],
            "version": skill_contract["version"],
        },
    }
    set_manifest = hierarchy.replace_execution_set(
        skill_id, set_id, set_manifest
    )
    return ReplayCampaignResult(
        campaign_directory=set_directory,
        manifest=set_manifest,
        skill_id=skill_id,
        revision_id=revision_id,
        execution_set_id=set_id,
    )


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Skill directory")
    task_input = parser.add_mutually_exclusive_group(required=True)
    task_input.add_argument(
        "--source",
        help="Legacy file input; converted to task.case.v1",
    )
    task_input.add_argument(
        "--task-case",
        help="Path to a task.case.v1 JSON file",
    )
    parser.add_argument(
        "--expected-artifact",
        action="append",
        default=[],
        help=(
            "Expected path relative to the run workspace; repeat for multiple "
            "artifacts (only with --source)"
        ),
    )
    parser.add_argument(
        "--prompt-file",
        required=True,
        help="Versioned prompt file with an approved sidecar",
    )
    parser.add_argument(
        "-n",
        "--replays",
        type=int,
        required=True,
        help="Number of independent trajectories to collect",
    )
    parser.add_argument(
        "--output-root",
        help=(
            "Deprecated legacy campaign root; providing it disables the "
            "Skill-first hierarchy for compatibility"
        ),
    )
    parser.add_argument(
        "--runtime-root",
        default=".skill-evolution",
        help="Skill-first runtime root",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Seconds to wait for each agent_settled",
    )
    parser.add_argument(
        "--pi-command",
        help="Pi executable command; defaults to PATH/npm discovery",
    )
    parser.add_argument(
        "--pi-arg",
        action="append",
        default=[],
        help="Additional Pi argument; repeat and use --pi-arg=--flag",
    )
    options = parser.parse_args(arguments)
    if options.task_case and options.expected_artifact:
        parser.error("--expected-artifact cannot be used with --task-case")
    if options.task_case:
        task_case = load_task_case(options.task_case)
    else:
        task_case = TaskCase.for_file(
            options.source,
            expected_artifacts=(
                options.expected_artifact
                or DEFAULT_EXPECTED_ARTIFACTS
            ),
        )

    result = run_replay_campaign(
        skill_path=options.skill,
        task_case=task_case,
        prompt_path=options.prompt_file,
        replay_count=options.replays,
        output_root=options.output_root,
        runtime_root=options.runtime_root,
        timeout=options.timeout,
        pi_command=options.pi_command,
        extra_pi_args=options.pi_arg,
    )
    print(result.campaign_directory)
    return 0 if result.manifest["status"] == "completed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
