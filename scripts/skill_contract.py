#!/usr/bin/env python3
"""Validate one package-local Skill Contract and emit a versioned report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.skill_contracts import validate_skill_contract
from skill_evolution.storage import atomic_write_json


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        help=(
            "Contract path; defaults to <skill>/skill_contract.json. "
            "Use an explicit path only for historical v1 validation."
        ),
    )
    parser.add_argument("--skill", required=True)
    parser.add_argument(
        "--task-case",
        action="append",
        default=[],
        help="TaskCase JSON path; repeat for every case in the suite.",
    )
    parser.add_argument(
        "--output",
        help="Optional destination for skill.validation_report.v1 JSON.",
    )
    parser.add_argument(
        "--require-approved",
        action="store_true",
        help="Return failure unless the contract is ready for dynamic tests.",
    )
    options = parser.parse_args(arguments)
    report = validate_skill_contract(
        contract_path=options.contract,
        skill_directory=options.skill,
        task_case_paths=options.task_case,
    )
    if options.output:
        output = Path(options.output).resolve()
        atomic_write_json(output, report)
        print(output)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["valid"]:
        return 1
    if options.require_approved and not report["dynamic_test_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
