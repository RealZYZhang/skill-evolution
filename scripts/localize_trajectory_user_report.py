#!/usr/bin/env python3
"""Publish one reviewed Chinese single-trajectory report beside its source."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.storage import atomic_write_json, load_json_object
from skill_evolution.trajectory_user_report_localization import (
    SUPPORTED_LOCALE,
    localize_trajectory_user_report,
)


def publish_localization(
    *,
    runtime_root: str | Path,
    skill_id: str,
    execution_id: str,
    analysis_id: str,
    localization_path: str | Path,
) -> Path:
    """Validate, bind, and publish one reviewed locale projection."""

    repository = SkillHierarchyRepository(runtime_root)
    record = repository.load_analysis(
        skill_id,
        analysis_id,
        execution_id=execution_id,
    )
    if record["status"] != "accepted":
        raise ValueError("Only accepted analyses can publish localization")
    directory = repository.analysis_directory(record)
    sources = [
        reference
        for reference in record["result_refs"]
        if reference.get("schema") == "analysis.single_trajectory_view.v1"
        and reference.get("locale") is None
    ]
    if len(sources) != 1:
        raise ValueError("Analysis must contain one source user report")
    if any(
        reference.get("locale") == SUPPORTED_LOCALE
        for reference in record["result_refs"]
    ):
        raise ValueError("Analysis already contains a zh-CN projection")
    source_reference = sources[0]
    source_path = repository.resolve_object_file(
        directory,
        str(source_reference["path"]),
    )
    source = load_json_object(source_path)
    localization = load_json_object(Path(localization_path))
    localized = localize_trajectory_user_report(source, localization)
    localized_path = source_path.with_name("user-report.zh-CN.json")
    if localized_path.exists():
        raise ValueError("Localized report path already exists")
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    localized_relative = localized_path.relative_to(directory).as_posix()
    localized_reference = {
        "path": localized_relative,
        "schema": "analysis.single_trajectory_view.v1",
        "locale": SUPPORTED_LOCALE,
        "localized_from": str(source_reference["path"]),
        "localized_from_sha256": source_digest,
    }
    updated = dict(record)
    updated_refs = list(record["result_refs"])
    source_index = updated_refs.index(source_reference)
    updated_refs.insert(source_index + 1, localized_reference)
    updated["result_refs"] = updated_refs
    atomic_write_json(localized_path, localized)
    repository.replace_analysis(updated)
    return localized_path


def main() -> int:
    """Parse command-line arguments and publish the reviewed projection."""

    parser = argparse.ArgumentParser(
        description="Publish one reviewed zh-CN single-trajectory report."
    )
    parser.add_argument("--runtime-root", default=".skill-evolution")
    parser.add_argument("--skill-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--analysis-id", required=True)
    parser.add_argument("--localization", required=True)
    arguments = parser.parse_args()
    output = publish_localization(
        runtime_root=arguments.runtime_root,
        skill_id=arguments.skill_id,
        execution_id=arguments.execution_id,
        analysis_id=arguments.analysis_id,
        localization_path=arguments.localization,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
