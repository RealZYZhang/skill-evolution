#!/usr/bin/env python3
"""Run deterministic checks over one action-level trajectory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.storage import atomic_write_json
from skill_evolution.storage import new_object_id, utc_now
from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    SkillHierarchyRepository,
)
from skill_evolution.trajectory_precheck import precheck_trajectory


def precheck_execution(
    *,
    runtime_root: str,
    skill_id: str,
    execution_id: str,
) -> tuple[Path, dict[str, object]]:
    """Run and attach a deterministic precheck to one hierarchy Execution."""

    hierarchy = SkillHierarchyRepository(runtime_root)
    execution = hierarchy.load_execution(skill_id, execution_id)
    trajectory_relative = execution["trajectory"]["path"]
    if not isinstance(trajectory_relative, str):
        raise ValueError("Execution does not contain a trajectory")
    trajectory_path = (
        hierarchy.execution_directory(skill_id, execution_id)
        / trajectory_relative
    )
    report = precheck_trajectory(trajectory_path)
    analysis_id = new_object_id("precheck")
    record = {
        "schema": ANALYSIS_RECORD_SCHEMA,
        "analysis_id": analysis_id,
        "skill_id": skill_id,
        "revision_id": execution["revision_id"],
        "scope": "single_execution",
        "execution_id": execution_id,
        "execution_set_id": None,
        "kind": "precheck",
        "producer": "deterministic",
        "status": (
            "accepted"
            if report["integrity"]["status"] == "valid"
            else "failed"
        ),
        "input_refs": [
            {"kind": "trajectory", "execution_id": execution_id}
        ],
        "result_refs": [
            {"schema": "trajectory.precheck.v1", "path": "result.json"}
        ],
        "attempts": [],
        "created_at": utc_now(),
        "ended_at": utc_now(),
        "provenance": None,
    }
    directory, _ = hierarchy.create_analysis(record)
    output = directory / "result.json"
    atomic_write_json(output, report)
    return output, report


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trajectory", nargs="?", help="Legacy path to one trajectory.jsonl file"
    )
    parser.add_argument(
        "--output",
        help="Optional destination for trajectory.precheck.v1 JSON.",
    )
    parser.add_argument("--runtime-root", default=".skill-evolution")
    parser.add_argument("--skill-id")
    parser.add_argument("--execution-id")
    options = parser.parse_args(arguments)
    if options.skill_id is not None or options.execution_id is not None:
        if options.skill_id is None or options.execution_id is None:
            parser.error(
                "--skill-id and --execution-id must be provided together"
            )
        if options.trajectory is not None or options.output is not None:
            parser.error(
                "Hierarchy precheck chooses its own trajectory and output path"
            )
        output, report = precheck_execution(
            runtime_root=options.runtime_root,
            skill_id=options.skill_id,
            execution_id=options.execution_id,
        )
        print(output)
    elif options.trajectory is None:
        parser.error("Provide a trajectory or --skill-id/--execution-id")
    else:
        report = precheck_trajectory(options.trajectory)
    if options.output and options.trajectory is not None:
        output = Path(options.output).resolve()
        atomic_write_json(output, report)
        print(output)
    elif options.trajectory is not None and options.output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["integrity"]["status"] == "valid" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
