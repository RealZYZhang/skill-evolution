"""Materialize immutable comparison batches for the deterministic Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
from typing import Any

from scripts.harness import run_harness
from skill_evolution.comparison import ComparisonError
from skill_evolution.evidence import EvidenceError, resolve_inside
from skill_evolution.storage import JsonObject, atomic_write_json


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ComparisonError(
                f"Comparison attempt may not contain symlinks: {path}"
            )


def _trajectory_name(root: Path) -> str:
    if (root / "trajectory.jsonl").is_file():
        return "trajectory.jsonl"
    if (root / "trace.jsonl").is_file():
        return "trace.jsonl"
    return "trajectory.jsonl"


class ComparisonHarnessRunner:
    """Copy completed attempts into one replay-shaped frozen Harness input."""

    def __init__(
        self,
        *,
        output_root: str | os.PathLike[str] = (
            ".skill-evolution/harness-runs"
        ),
        capture_screenshots: bool = True,
        chrome_command: str | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.capture_screenshots = capture_screenshots
        self.chrome_command = chrome_command

    def __call__(
        self,
        attempts: Sequence[Mapping[str, Any]],
        comparison_directory: Path,
    ) -> JsonObject:
        """Create one immutable batch and run both Harness components."""

        comparison = comparison_directory.resolve()
        if not comparison.is_dir():
            raise ComparisonError(
                f"Comparison directory does not exist: {comparison}"
            )
        batch_root = comparison / "harness-inputs"
        batch_root.mkdir(exist_ok=True)
        batch_numbers = [
            int(path.name.removeprefix("batch-"))
            for path in batch_root.iterdir()
            if path.is_dir()
            and path.name.startswith("batch-")
            and path.name.removeprefix("batch-").isdigit()
        ]
        batch_number = max(batch_numbers, default=0) + 1
        campaign_id = f"{comparison.name}-batch-{batch_number:03d}"
        campaign = batch_root / f"batch-{batch_number:03d}"
        runs_directory = campaign / "runs"
        runs_directory.mkdir(parents=True, exist_ok=False)

        run_records: list[JsonObject] = []
        succeeded = 0
        failed = 0
        orchestration_failed = 0
        for index, raw_attempt in enumerate(attempts, start=1):
            attempt = dict(raw_attempt)
            status = str(attempt.get("status", "unknown"))
            run_id = attempt.get("run_id")
            attempt_path = attempt.get("attempt_path")
            run_record: JsonObject = {
                "index": index,
                "status": status,
                "run_id": run_id if isinstance(run_id, str) else None,
                "path": None,
                "trajectory": None,
                "session": None,
                "session_status": attempt.get("session_status"),
                "artifact": None,
                "artifacts": attempt.get("artifacts", []),
                "failure_stage": attempt.get("failure_stage"),
                "error": attempt.get("error"),
                "comparison_attempt_index": attempt.get("attempt_index"),
                "comparison_workflow_attempt": attempt.get(
                    "workflow_attempt"
                ),
            }
            if isinstance(run_id, str) and isinstance(attempt_path, str):
                if Path(run_id).name != run_id or run_id in {".", ".."}:
                    raise ComparisonError(
                        f"Unsafe comparison run_id: {run_id}"
                    )
                try:
                    source = resolve_inside(comparison, attempt_path)
                except EvidenceError as error:
                    raise ComparisonError(str(error)) from error
                if not source.is_dir():
                    raise ComparisonError(
                        f"Comparison attempt is not a directory: {source}"
                    )
                _reject_symlinks(source)
                destination = runs_directory / run_id
                shutil.copytree(source, destination)
                relative = f"runs/{run_id}"
                run_record.update(
                    {
                        "path": relative,
                        "trajectory": f"{relative}/{_trajectory_name(destination)}",
                        "session": f"{relative}/pi-session.jsonl",
                    }
                )
                artifacts = run_record["artifacts"]
                if isinstance(artifacts, list) and artifacts:
                    run_record["artifact"] = artifacts[0]

            if status == "succeeded":
                succeeded += 1
            elif status == "orchestration_failed":
                orchestration_failed += 1
            else:
                failed += 1
            run_records.append(run_record)

        replay_manifest: JsonObject = {
            "schema": "replay.campaign.v1",
            "campaign_id": campaign_id,
            "status": (
                "completed"
                if failed == 0 and orchestration_failed == 0
                else "completed_with_run_failures"
            ),
            "replay_count_requested": len(attempts),
            "execution": {
                "mode": "comparison_batch",
                "source_comparison_id": comparison.name,
            },
            "runs": run_records,
            "summary": {
                "trajectory_count": sum(
                    item["trajectory"] is not None for item in run_records
                ),
                "succeeded": succeeded,
                "failed": failed,
                "orchestration_failed": orchestration_failed,
            },
        }
        atomic_write_json(campaign / "replay.json", replay_manifest)
        harness = run_harness(
            replay_campaign_directory=campaign,
            output_root=self.output_root,
            capture_screenshots=self.capture_screenshots,
            chrome_command=self.chrome_command,
        )
        return {
            "schema": "comparison.harness_result.v1",
            "status": harness.manifest["status"],
            "harness_run_id": harness.manifest["harness_run_id"],
            "harness_path": str(harness.harness_directory),
            "input_campaign": str(campaign.relative_to(comparison)),
            "input_attempt_count": len(attempts),
            "trajectory_profile": "trajectory-profile.json",
            "artifact_comparison": "artifact-comparison.json",
        }
