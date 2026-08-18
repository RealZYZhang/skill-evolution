#!/usr/bin/env python3
"""Run the deterministic trajectory profiler and HTML comparator together."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.artifact_comparator import HTMLArtifactComparator
from scripts.trajectory_profiler import TrajectoryProfiler
from skill_evolution.storage import JsonObject, atomic_write_json, utc_now
from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    SkillHierarchyRepository,
)


HARNESS_RUN_SCHEMA = "harness.run.v1"


def _new_harness_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class HarnessRunResult:
    """Paths and reports produced by one deterministic harness run."""

    harness_directory: Path
    manifest: JsonObject
    profile: JsonObject
    artifact_comparison: JsonObject
    hierarchy_analysis_directory: Path | None = None


def run_harness(
    *,
    replay_campaign_directory: str | os.PathLike[str],
    output_root: str | os.PathLike[str] = ".skill-evolution/harness-runs",
    capture_screenshots: bool = True,
    chrome_command: str | None = None,
) -> HarnessRunResult:
    """Profile and compare one frozen replay campaign in a shared directory."""

    campaign = Path(replay_campaign_directory).resolve()
    replay_manifest = campaign / "replay.json"
    if not replay_manifest.is_file():
        raise FileNotFoundError(
            f"Replay campaign manifest not found: {replay_manifest}"
        )
    harness_id = _new_harness_id()
    directory = Path(output_root).resolve() / harness_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest_path = directory / "harness.json"
    manifest: JsonObject = {
        "schema": HARNESS_RUN_SCHEMA,
        "harness_run_id": harness_id,
        "kind": "trajectory_profile_and_artifact_comparison",
        "status": "running",
        "started_at": utc_now(),
        "ended_at": None,
        "source": {
            "campaign_id": campaign.name,
            "campaign_path": str(campaign),
        },
        "components": {
            "trajectory_profiler": "trajectory.profile.v1",
            "artifact_comparator": "artifact.comparison.v1",
        },
        "outputs": {},
        "error": None,
    }
    atomic_write_json(manifest_path, manifest)
    try:
        profile = TrajectoryProfiler(campaign.parent).profile_campaign(
            campaign.name
        )
        profile["profile_id"] = harness_id
        profile_path = directory / "trajectory-profile.json"
        atomic_write_json(profile_path, profile)

        comparator = HTMLArtifactComparator(chrome_command=chrome_command)
        comparison = comparator.compare_campaign(
            campaign,
            capture_screenshots=capture_screenshots,
            screenshot_directory=directory / "screenshots",
        )
        comparison["comparison_id"] = harness_id
        comparison_path = directory / "artifact-comparison.json"
        atomic_write_json(comparison_path, comparison)

        component_statuses = {
            "trajectory_profiler": profile.get("load_status"),
            "artifact_comparator": comparison.get("status"),
        }
        if "error" in component_statuses.values():
            status = "completed_partial"
        elif any(
            value in {"partial", "warning"}
            or (
                isinstance(value, str)
                and value.startswith("completed_")
            )
            for value in component_statuses.values()
        ):
            status = "completed_partial"
        else:
            status = "completed"
        manifest.update(
            {
                "status": status,
                "ended_at": utc_now(),
                "component_statuses": component_statuses,
                "outputs": {
                    "trajectory_profile": "trajectory-profile.json",
                    "artifact_comparison": "artifact-comparison.json",
                    "screenshots": (
                        "screenshots" if capture_screenshots else None
                    ),
                },
            }
        )
        atomic_write_json(manifest_path, manifest)
    except Exception as error:
        manifest.update(
            {
                "status": "failed",
                "ended_at": utc_now(),
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
        )
        atomic_write_json(manifest_path, manifest)
        raise
    return HarnessRunResult(
        harness_directory=directory,
        manifest=manifest,
        profile=profile,
        artifact_comparison=comparison,
    )


def run_hierarchy_harness(
    *,
    runtime_root: str | os.PathLike[str],
    skill_id: str,
    execution_set_id: str,
    capture_screenshots: bool = True,
    chrome_command: str | None = None,
) -> HarnessRunResult:
    """Run the Harness from a same-revision ExecutionSet."""

    hierarchy = SkillHierarchyRepository(runtime_root)
    execution_set = hierarchy.load_execution_set(
        skill_id, execution_set_id
    )
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        campaign = materialize_execution_set(
            hierarchy=hierarchy,
            execution_set=execution_set,
            destination=temporary_root / "campaign",
        )
        result = run_harness(
            replay_campaign_directory=campaign,
            output_root=temporary_root / "harness-runs",
            capture_screenshots=capture_screenshots,
            chrome_command=chrome_command,
        )
        analysis_id = f"analysis-{result.manifest['harness_run_id']}"
        record: JsonObject = {
            "schema": ANALYSIS_RECORD_SCHEMA,
            "analysis_id": analysis_id,
            "skill_id": skill_id,
            "revision_id": execution_set["revision_id"],
            "scope": "execution_set",
            "execution_id": None,
            "execution_set_id": execution_set_id,
            "kind": "harness",
            "producer": "composite",
            "status": "accepted",
            "input_refs": [
                {
                    "kind": "execution_set",
                    "execution_set_id": execution_set_id,
                }
            ],
            "result_refs": [
                {
                    "schema": "trajectory.profile.v1",
                    "path": "payload/trajectory-profile.json",
                },
                {
                    "schema": "artifact.comparison.v1",
                    "path": "payload/artifact-comparison.json",
                },
            ],
            "attempts": [],
            "created_at": result.manifest["started_at"],
            "ended_at": result.manifest["ended_at"],
            "provenance": {
                "harness_run_id": result.manifest["harness_run_id"]
            },
        }
        analysis_directory, _ = hierarchy.create_analysis(record)
        payload = analysis_directory / "payload"
        shutil.move(str(result.harness_directory), payload)
        hierarchy.rebuild_indexes()
        return HarnessRunResult(
            harness_directory=payload,
            manifest=result.manifest,
            profile=result.profile,
            artifact_comparison=result.artifact_comparison,
            hierarchy_analysis_directory=analysis_directory,
        )


def materialize_execution_set(
    *,
    hierarchy: SkillHierarchyRepository,
    execution_set: Mapping[str, Any],
    destination: Path,
) -> Path:
    """Build an ephemeral Replay projection for deterministic readers."""

    destination.mkdir(parents=True)
    runs_directory = destination / "runs"
    runs_directory.mkdir()
    runs: list[JsonObject] = []
    succeeded = 0
    failed = 0
    for index, execution_id in enumerate(
        execution_set["execution_ids"], start=1
    ):
        execution = hierarchy.load_execution(
            str(execution_set["skill_id"]), str(execution_id)
        )
        source = hierarchy.execution_directory(
            str(execution_set["skill_id"]), str(execution_id)
        ) / "payload"
        destination_run = runs_directory / str(execution_id)
        shutil.copytree(source, destination_run)
        trajectory_path = execution["trajectory"]["path"]
        trajectory_name = (
            Path(str(trajectory_path)).name if trajectory_path is not None else None
        )
        outputs = execution["outputs"]
        first_output = outputs[0] if outputs else None
        artifact: JsonObject | None = None
        if first_output is not None:
            relative = str(first_output["path"])
            if relative.startswith("payload/"):
                relative = relative[len("payload/") :]
            artifact = {
                "path": relative,
                "exists": first_output.get("sha256") is not None,
                "bytes": first_output.get("bytes"),
            }
        status = str(execution["status"])
        if status == "succeeded":
            succeeded += 1
        else:
            failed += 1
        runs.append(
            {
                "index": index,
                "status": status,
                "started_at": execution["started_at"],
                "ended_at": execution["ended_at"],
                "duration_ms": execution["duration_ms"],
                "run_id": execution_id,
                "path": f"runs/{execution_id}",
                "trajectory": (
                    f"runs/{execution_id}/{trajectory_name}"
                    if trajectory_name is not None
                    else None
                ),
                "session": (
                    f"runs/{execution_id}/pi-session.jsonl"
                    if execution["session"]["path"] is not None
                    else None
                ),
                "session_status": execution["session"]["status"],
                "artifact": artifact,
                "artifacts": [artifact] if artifact is not None else [],
                "failure_stage": None,
                "error": None,
            }
        )
    manifest: JsonObject = {
        "schema": "replay.campaign.v1",
        "campaign_id": execution_set["set_id"],
        "status": execution_set["status"],
        "started_at": execution_set["created_at"],
        "ended_at": execution_set["ended_at"],
        "replay_count_requested": len(runs),
        "skill": {
            "source_path": str(
                hierarchy.revision_directory(
                    str(execution_set["skill_id"]),
                    str(execution_set["revision_id"]),
                )
                / "package"
            )
        },
        "task": dict(execution_set["task"]),
        "execution": dict(execution_set["runtime"]),
        "runs": runs,
        "summary": {
            "trajectory_count": len(runs),
            "succeeded": succeeded,
            "failed": failed,
            "orchestration_failed": sum(
                item["status"] == "orchestration_failed" for item in runs
            ),
        },
        "duration_ms": execution_set["runtime"].get("duration_ms"),
    }
    atomic_write_json(destination / "replay.json", manifest)
    return destination


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--campaign",
        help="Legacy Replay campaign directory containing replay.json",
    )
    source.add_argument(
        "--execution-set",
        help="Skill-first ExecutionSet identifier",
    )
    parser.add_argument(
        "--skill-id",
        help="Skill identifier required with --execution-set",
    )
    parser.add_argument(
        "--runtime-root",
        default=".skill-evolution",
        help="Skill-first runtime root",
    )
    parser.add_argument(
        "--output-root",
        default=".skill-evolution/harness-runs",
    )
    parser.add_argument("--chrome", help="Chrome executable path")
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Skip fixed desktop/mobile screenshots",
    )
    options = parser.parse_args(arguments)
    if options.execution_set is not None:
        if options.skill_id is None:
            parser.error("--skill-id is required with --execution-set")
        result = run_hierarchy_harness(
            runtime_root=options.runtime_root,
            skill_id=options.skill_id,
            execution_set_id=options.execution_set,
            capture_screenshots=not options.no_screenshots,
            chrome_command=options.chrome,
        )
    else:
        result = run_harness(
            replay_campaign_directory=options.campaign,
            output_root=options.output_root,
            capture_screenshots=not options.no_screenshots,
            chrome_command=options.chrome,
        )
    print(result.harness_directory)
    return 0 if result.manifest["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(_run_cli())
