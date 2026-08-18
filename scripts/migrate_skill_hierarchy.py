#!/usr/bin/env python3
"""Dry-run or apply the auditable Skill-first hierarchy migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.hierarchy_migration import (
    HierarchyMigrationError,
    SkillHierarchyMigration,
    load_identity_mappings,
)
from skill_evolution.storage import load_json_object


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or perform the legacy-to-Skill hierarchy migration."
    )
    parser.add_argument("--runtime-root", default=".skill-evolution")
    parser.add_argument(
        "--mapping",
        default="migrations/skill-hierarchy-map-v1.json",
    )
    parser.add_argument("--migration-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-migration-id",
        help="Exact dry-run migration ID required for --apply.",
    )
    options = parser.parse_args(arguments)
    try:
        mappings = load_identity_mappings(options.mapping)
        migration = SkillHierarchyMigration(options.runtime_root, mappings)
        if options.apply:
            if not options.migration_id or not options.confirm_migration_id:
                parser.error(
                    "--apply requires --migration-id and --confirm-migration-id"
                )
            manifest_path = (
                Path(options.runtime_root)
                / "migrations"
                / options.migration_id
                / "manifest.json"
            )
            result = migration.apply(
                load_json_object(manifest_path),
                confirmation=options.confirm_migration_id,
            )
        else:
            result = migration.plan(migration_id=options.migration_id)
    except (HierarchyMigrationError, OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ready", "completed"} else 2


if __name__ == "__main__":
    raise SystemExit(_run_cli())
