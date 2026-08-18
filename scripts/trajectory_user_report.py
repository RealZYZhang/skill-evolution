#!/usr/bin/env python3
"""Create one five-layer user report from each preserved trajectory AgentRun."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.storage import StorageError
from skill_evolution.trajectory_user_report import (
    TrajectoryUserReportError,
    write_trajectory_user_report_from_agent_run,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one immutable five-layer user report for each preserved "
            "single-trajectory AgentRun."
        )
    )
    parser.add_argument(
        "agent_runs",
        nargs="+",
        help="One or more trajectory-error AgentRun directories.",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Generate reports and return a stable automation status."""

    options = _parser().parse_args(arguments)
    failed = False
    for raw_path in options.agent_runs:
        try:
            output = write_trajectory_user_report_from_agent_run(raw_path)
        except (
            OSError,
            StorageError,
            TrajectoryUserReportError,
            TypeError,
            ValueError,
        ) as error:
            failed = True
            print(f"error: {raw_path}: {error}", file=sys.stderr)
            continue
        print(output)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
