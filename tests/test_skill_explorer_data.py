"""Tests for Skill-first Viewer projections and file boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.skill_explorer_data import SkillExplorerRepository
from skill_evolution.hierarchy import (
    SkillHierarchyRepository,
    execution_manifest_from_payload,
)
from skill_evolution.trajectory_user_report import build_trajectory_user_report


def _write_skill(root: Path) -> Path:
    package = root / "viewer-skill"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: Viewer Skill\ndescription: fixture\n---\n\n# Viewer\n",
        encoding="utf-8",
    )
    (package / "skill_contract.json").write_text(
        json.dumps(
            {
                "schema": "skill.contract.v2",
                "skill_id": "viewer-skill",
                "version": "1.0.0",
                "status": "approved",
                "owner": "owner",
                "approved_by": "owner",
                "approved_at": "2026-08-09T00:00:00Z",
                "supersedes": None,
                "runtime": {
                    "required_tools": ["filesystem.read"],
                    "allowed_tools": ["filesystem.read"],
                    "allowed_permissions": ["workspace.input.read"],
                    "network": "forbidden",
                    "credentials_in_sandbox": False,
                    "dependencies": [],
                    "assets": [],
                },
                "evaluation": {"suite_refs": ["suite-1"]},
            }
        ),
        encoding="utf-8",
    )
    return package


def _write_execution(runtime: Path, package: Path) -> None:
    repository = SkillHierarchyRepository(runtime)
    revision = repository.register_revision(package)
    execution_set = repository.create_execution_set(
        skill_id="viewer-skill",
        revision_id=revision.manifest["revision_id"],
        purpose="replay",
        task={"task_case_id": "case-1"},
        runtime={},
        provenance={"legacy_campaign_id": "campaign-1"},
        set_id="set-1",
    )
    execution = repository.prepare_execution(
        skill_id="viewer-skill",
        revision_id=revision.manifest["revision_id"],
        origin="replay",
        execution_set_id="set-1",
        execution_id="execution-1",
    )
    artifacts = execution.payload_directory / "artifacts"
    artifacts.mkdir()
    (artifacts / "input.md").write_text("input", encoding="utf-8")
    (artifacts / "output.html").write_text(
        "<!doctype html><title>output</title>", encoding="utf-8"
    )
    records = [
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 1,
            "type": "trajectory_started",
            "source": "framework",
            "payload": {"manifest": {"task_case": {"task_case_id": "case-1"}}},
        },
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 2,
            "type": "artifact_registered",
            "source": "framework",
            "payload": {"artifact_role": "input", "artifact": {"path": "artifacts/input.md"}},
        },
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 3,
            "type": "message_action",
            "source": "pi_rpc",
            "payload": {"status": "completed", "reasoning_content": "secret"},
        },
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 4,
            "type": "artifact_registered",
            "source": "framework",
            "payload": {"artifact_role": "output", "artifact": {"path": "artifacts/output.html"}},
        },
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 5,
            "type": "trajectory_finished",
            "source": "framework",
            "payload": {"outcome": {"status": "succeeded", "duration_ms": 10}},
        },
        {
            "schema": "trajectory.actions.v1",
            "run_id": "execution-1",
            "seq": 6,
            "type": "trajectory_sealed",
            "source": "framework",
            "payload": {"record_count": 6},
        },
    ]
    (execution.payload_directory / "trajectory.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in records),
        encoding="utf-8",
    )
    manifest = execution_manifest_from_payload(
        execution_directory=execution.directory,
        skill_id="viewer-skill",
        revision_id=revision.manifest["revision_id"],
        execution_id="execution-1",
        origin="replay",
        execution_set_id="set-1",
    )
    repository.finalize_execution("viewer-skill", "execution-1", manifest)
    execution_set["execution_ids"] = ["execution-1"]
    execution_set["status"] = "completed"
    repository.replace_execution_set("viewer-skill", "set-1", execution_set)
    repository.mark_cutover_complete(
        migration_id="fixture-migration",
        disposition={"fixture": True},
    )


def _write_localized_analysis(runtime: Path) -> Path:
    repository = SkillHierarchyRepository(runtime)
    execution = repository.load_execution("viewer-skill", "execution-1")
    record = {
        "schema": "analysis.record.v1",
        "analysis_id": "analysis-1",
        "skill_id": "viewer-skill",
        "revision_id": execution["revision_id"],
        "scope": "single_execution",
        "execution_id": "execution-1",
        "execution_set_id": None,
        "kind": "trajectory_error",
        "producer": "agent",
        "status": "failed",
        "input_refs": [],
        "result_refs": [],
        "attempts": [],
        "created_at": "2026-08-12T00:00:00+00:00",
        "ended_at": "2026-08-12T00:01:00+00:00",
        "provenance": None,
    }
    directory, record = repository.create_analysis(record)
    precheck = {
        "schema": "trajectory.precheck.v1",
        "run_id": "execution-1",
        "deterministic_status": "completed",
        "integrity": {"status": "valid"},
        "outcome": {"status": "succeeded"},
        "signals": [],
        "candidate_recoveries": [],
        "artifacts": [],
    }
    context = {
        "run_id": "execution-1",
        "trajectory_precheck_path": "reports/trajectory-precheck.json",
    }
    source_report = build_trajectory_user_report(
        precheck=precheck,
        semantic_report=None,
        semantic_status="failed",
        analysis_id="analysis-1",
        agent_run_id="agent-run-1",
        context=context,
        generated_at="2026-08-12T00:01:00+00:00",
    )
    source_report["analysis"]["message"] = "English source report"
    localized_report = json.loads(json.dumps(source_report))
    localized_report["analysis"]["message"] = "中文呈现报告"
    source_path = directory / "user-report.json"
    localized_path = directory / "user-report.zh-CN.json"
    source_path.write_text(
        json.dumps(source_report, ensure_ascii=False),
        encoding="utf-8",
    )
    localized_path.write_text(
        json.dumps(localized_report, ensure_ascii=False),
        encoding="utf-8",
    )
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    record["result_refs"] = [
        {
            "path": "user-report.json",
            "schema": "analysis.single_trajectory_view.v1",
        },
        {
            "path": "user-report.zh-CN.json",
            "schema": "analysis.single_trajectory_view.v1",
            "locale": "zh-CN",
            "localized_from": "user-report.json",
            "localized_from_sha256": source_digest,
        },
    ]
    repository.replace_analysis(record)
    return source_path


class SkillExplorerDataTests(unittest.TestCase):
    """Present Skill-first hierarchy without leaking unsafe Trajectory content."""

    def test_lists_skill_and_expands_execution_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            _write_execution(root / "runtime", package)
            explorer = SkillExplorerRepository(root / "runtime")

            skills = explorer.list_skills()
            detail = explorer.get_execution("viewer-skill", "execution-1")

            self.assertEqual(skills["skills"][0]["display_name"], "Viewer Skill")
            self.assertEqual(detail["input"]["artifacts"][0]["artifact_id"], "input-1")
            self.assertEqual(detail["output"]["artifacts"][0]["artifact_id"], "output-1")
            message = next(
                item
                for item in detail["trajectory"]["records"]
                if item["type"] == "message_action"
            )
            self.assertEqual(
                message["payload"]["reasoning_content"],
                "[REDACTED: hidden reasoning]",
            )

    def test_viewer_reads_do_not_change_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            runtime = root / "runtime"
            _write_execution(runtime, package)
            explorer = SkillExplorerRepository(runtime)
            before = {
                path.relative_to(runtime).as_posix(): (
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in runtime.rglob("*")
                if path.is_file()
            }

            explorer.list_skills()
            explorer.get_skill("viewer-skill")
            explorer.get_execution("viewer-skill", "execution-1")

            after = {
                path.relative_to(runtime).as_posix(): (
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in runtime.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_campaign_compatibility_is_projected_from_execution_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            _write_execution(root / "runtime", package)
            explorer = SkillExplorerRepository(root / "runtime")

            campaigns = explorer.list_campaign_projections()
            campaign = explorer.get_campaign_projection("campaign-1")

            self.assertEqual(campaigns["campaigns"][0]["run_count"], 1)
            self.assertEqual(campaign["runs"][0]["run_id"], "execution-1")

    def test_declared_artifact_resolves_but_unknown_file_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            _write_execution(root / "runtime", package)
            explorer = SkillExplorerRepository(root / "runtime")

            path, media_type = explorer.get_execution_file(
                "viewer-skill", "execution-1", "output-1"
            )

            self.assertEqual(path.name, "output.html")
            self.assertEqual(media_type, "text/html")
            with self.assertRaisesRegex(Exception, "not declared"):
                explorer.get_execution_file(
                    "viewer-skill", "execution-1", "../../secret"
                )

    def test_prefers_current_chinese_localization_without_overwriting_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            runtime = root / "runtime"
            _write_execution(runtime, package)
            source = _write_localized_analysis(runtime)
            source_before = source.read_bytes()

            analyses = SkillExplorerRepository(runtime).get_execution_analyses(
                "viewer-skill",
                "execution-1",
            )

            self.assertEqual(
                analyses["latest_valid_report"]["analysis"]["message"],
                "中文呈现报告",
            )
            self.assertEqual(source.read_bytes(), source_before)

    def test_ignores_localization_when_its_source_report_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_skill(root / "packages")
            runtime = root / "runtime"
            _write_execution(runtime, package)
            source = _write_localized_analysis(runtime)
            source.write_text(
                source.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

            analyses = SkillExplorerRepository(runtime).get_execution_analyses(
                "viewer-skill",
                "execution-1",
            )

            self.assertEqual(
                analyses["latest_valid_report"]["analysis"]["message"],
                "English source report",
            )
