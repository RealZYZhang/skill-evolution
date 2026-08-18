"""Tests for Skill-first contracts, relationships, and indexes."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    MULTI_TRAJECTORY_VIEW_SCHEMA,
    HierarchyError,
    SkillHierarchyRepository,
    validate_multi_trajectory_errors_view,
    validate_multi_trajectory_view,
)
from skill_evolution.hierarchy_analysis import HierarchyAnalysisService
from skill_evolution.storage import utc_now


def _write_skill(root: Path, skill_id: str = "test-skill") -> Path:
    skill = root / skill_id
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        (
            "---\n"
            "name: Test Skill\n"
            "description: A hierarchy fixture.\n"
            "---\n\n"
            "# Test Skill\n"
        ),
        encoding="utf-8",
    )
    contract = {
        "schema": "skill.contract.v2",
        "skill_id": skill_id,
        "version": "1.0.0",
        "status": "approved",
        "owner": "test-owner",
        "approved_by": "test-owner",
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
        "evaluation": {"suite_refs": ["test-suite-v1"]},
    }
    (skill / "skill_contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    return skill


def _final_manifest(
    execution: dict[str, object],
    *,
    status: str = "succeeded",
) -> dict[str, object]:
    value = deepcopy(execution)
    value.update(
        {
            "status": status,
            "ended_at": utc_now(),
            "duration_ms": 12,
            "task": {"task_case_id": "case-1"},
            "trajectory": {
                "path": "payload/trajectory.jsonl",
                "schema": "trajectory.actions.v1",
                "source_format": "current",
                "sealed": True,
            },
            "session": {
                "path": "payload/pi-session.jsonl",
                "status": "complete",
            },
        }
    )
    return value


class SkillHierarchyTests(unittest.TestCase):
    """Keep Skill identity separate from revisions and executions."""

    def test_registers_content_addressed_revision_and_rebuilds_catalog(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")

            first = repository.register_revision(skill)
            second = repository.register_revision(skill)

            self.assertEqual(
                first.manifest["revision_id"], second.manifest["revision_id"]
            )
            self.assertEqual(first.manifest["lifecycle"], "active")
            self.assertEqual(first.manifest["contract"]["status"], "approved")
            self.assertTrue((first.directory / "package" / "SKILL.md").is_file())
            catalog = json.loads(
                (root / "runtime" / "catalog.json").read_text()
            )
            self.assertEqual(catalog["skills"][0]["skill_id"], "test-skill")

            (skill / "SKILL.md").write_text(
                (skill / "SKILL.md").read_text() + "\nChanged.\n",
                encoding="utf-8",
            )
            changed = repository.register_revision(
                skill, lifecycle="historical"
            )
            self.assertNotEqual(
                changed.manifest["revision_id"], first.manifest["revision_id"]
            )

    def test_deleted_indexes_are_fully_rebuilt_from_authoritative_objects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")
            repository.register_revision(skill)
            catalog_path = root / "runtime" / "catalog.json"
            index_path = (
                root / "runtime" / "skills" / "test-skill" / "index.json"
            )
            catalog_path.unlink()
            index_path.unlink()

            catalog = repository.rebuild_indexes()

            self.assertTrue(catalog_path.is_file())
            self.assertTrue(index_path.is_file())
            self.assertEqual(catalog["skills"][0]["revision_count"], 1)

    def test_symlinked_execution_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")
            repository.register_revision(skill)
            outside = root / "outside"
            outside.mkdir()
            executions = (
                root / "runtime" / "skills" / "test-skill" / "executions"
            )
            executions.mkdir()
            (executions / "execution-escape").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(HierarchyError, "unsafe"):
                repository.load_execution("test-skill", "execution-escape")

    def test_legacy_revision_does_not_claim_current_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "legacy"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: Legacy\ndescription: old\n---\n",
                encoding="utf-8",
            )
            repository = SkillHierarchyRepository(root / "runtime")

            revision = repository.register_revision(
                skill,
                lifecycle="historical",
                legacy_skill_id="known-skill",
                legacy_identity={"method": "owner_mapping"},
            )

            self.assertEqual(
                revision.manifest["contract"]["status"],
                "missing_at_execution",
            )
            self.assertIsNone(revision.manifest["contract"]["schema"])

    def test_execution_set_rejects_mixed_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")
            revision_one = repository.register_revision(skill)
            execution_set = repository.create_execution_set(
                skill_id="test-skill",
                revision_id=str(revision_one.manifest["revision_id"]),
                purpose="replay",
                task={},
                runtime={},
                provenance={},
                set_id="set-1",
            )
            first = repository.prepare_execution(
                skill_id="test-skill",
                revision_id=str(revision_one.manifest["revision_id"]),
                origin="replay",
                execution_set_id="set-1",
                execution_id="execution-1",
            )
            repository.finalize_execution(
                "test-skill",
                "execution-1",
                _final_manifest(first.manifest),
            )
            execution_set["execution_ids"] = ["execution-1"]
            repository.replace_execution_set(
                "test-skill", "set-1", execution_set
            )

            (skill / "SKILL.md").write_text(
                (skill / "SKILL.md").read_text() + "\nRevision two.\n",
                encoding="utf-8",
            )
            revision_two = repository.register_revision(
                skill, lifecycle="historical"
            )
            second = repository.prepare_execution(
                skill_id="test-skill",
                revision_id=str(revision_two.manifest["revision_id"]),
                origin="direct",
                execution_id="execution-2",
            )
            repository.finalize_execution(
                "test-skill",
                "execution-2",
                _final_manifest(second.manifest),
            )
            execution_set["execution_ids"].append("execution-2")
            with self.assertRaisesRegex(HierarchyError, "mix Skill revisions"):
                repository.replace_execution_set(
                    "test-skill", "set-1", execution_set
                )

    def test_analysis_is_attached_to_subject_not_global_agent_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")
            revision = repository.register_revision(skill)
            revision_id = str(revision.manifest["revision_id"])
            execution = repository.prepare_execution(
                skill_id="test-skill",
                revision_id=revision_id,
                origin="direct",
                execution_id="execution-1",
            )
            repository.finalize_execution(
                "test-skill",
                "execution-1",
                _final_manifest(execution.manifest),
            )
            record = {
                "schema": ANALYSIS_RECORD_SCHEMA,
                "analysis_id": "analysis-1",
                "skill_id": "test-skill",
                "revision_id": revision_id,
                "scope": "single_execution",
                "execution_id": "execution-1",
                "execution_set_id": None,
                "kind": "trajectory_error",
                "producer": "agent",
                "status": "unavailable",
                "input_refs": [],
                "result_refs": [],
                "attempts": [{"agent_run_id": "attempt-1"}],
                "created_at": utc_now(),
                "ended_at": utc_now(),
                "provenance": None,
            }

            directory, _ = repository.create_analysis(record)

            self.assertEqual(
                directory,
                repository.execution_directory("test-skill", "execution-1")
                / "analyses"
                / "single"
                / "analysis-1",
            )
            self.assertEqual(
                repository.list_analyses(
                    "test-skill", execution_id="execution-1"
                )[0]["status"],
                "unavailable",
            )

    def test_unavailable_multi_trajectory_view_cannot_publish_findings(self) -> None:
        value = {
            "schema": MULTI_TRAJECTORY_VIEW_SCHEMA,
            "analysis_id": "analysis-1",
            "skill_id": "test-skill",
            "revision_id": "rev-1",
            "generated_at": utc_now(),
            "analysis": {"status": "unavailable"},
            "overview": {},
            "execution_set": {},
            "patterns": [],
            "findings": [{"claim": "not validated"}],
            "evidence": [],
            "recommendation": {},
            "provenance": {},
        }

        with self.assertRaisesRegex(HierarchyError, "cannot contain findings"):
            validate_multi_trajectory_view(value)

    def test_multi_analysis_service_keeps_unavailable_findings_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = _write_skill(root / "packages")
            repository = SkillHierarchyRepository(root / "runtime")
            revision = repository.register_revision(skill)
            repository.create_execution_set(
                skill_id="test-skill",
                revision_id=str(revision.manifest["revision_id"]),
                purpose="diagnostic",
                task={},
                runtime={},
                provenance={},
                set_id="set-1",
            )
            service = HierarchyAnalysisService(root / "runtime")
            directory, record = service.prepare_multi(
                skill_id="test-skill",
                execution_set_id="set-1",
                analysis_id="analysis-1",
            )
            service.start("test-skill", "analysis-1")
            report = service.unavailable_report(
                skill_id="test-skill",
                analysis_id="analysis-1",
                message="Model result failed validation.",
            )

            finished = service.finish(
                skill_id="test-skill",
                analysis_id="analysis-1",
                status="invalid_output",
                attempts=[{"agent_run_id": "agent-run-1"}],
                result_refs=[],
                user_report=report,
            )

            self.assertEqual(
                record["revision_id"], revision.manifest["revision_id"]
            )
            self.assertEqual(record["kind"], "multi_trajectory")
            self.assertEqual(
                directory.parent,
                repository.multi_trajectory_analyses_directory("test-skill"),
            )
            self.assertEqual(
                [
                    item["analysis_id"]
                    for item in repository.list_multi_trajectory_analyses(
                        "test-skill"
                    )
                ],
                ["analysis-1"],
            )
            self.assertEqual(report["findings"], [])
            self.assertEqual(finished["status"], "invalid_output")
            self.assertEqual(
                finished["result_refs"][0]["schema"],
                "analysis.multi_trajectory_view.v1",
            )

    def test_multi_trajectory_errors_view_accepts_error_centric_report(self) -> None:
        value = {
            "schema": "analysis.multi_trajectory_errors.v1",
            "analysis_id": "analysis-1",
            "skill_id": "test-skill",
            "revision_id": "rev-1",
            "generated_at": utc_now(),
            "scope": {
                "eligible_trajectory_ids": ["execution-1", "execution-2"],
                "reviewed_trajectory_ids": ["execution-1", "execution-2"],
                "counterexample_search": "searched all runs",
            },
            "errors": [
                {
                    "error_id": "E1",
                    "title": "输出 token 超限",
                    "summary": "内联完整 HTML 触发输出上限",
                    "anchor_evidence": [
                        {"schema": "evidence.ref.v1", "run_id": "execution-1", "seq": 18}
                    ],
                    "observed_trajectory_ids": ["execution-1", "execution-2"],
                    "checked_absent_trajectory_ids": [],
                    "suggested_dimensions": ["behavior", "resource"],
                    "notes": None,
                }
            ],
            "reports": [
                {
                    "error_id": "E1",
                    "dimensions": [
                        {
                            "dimension": "behavior",
                            "claim": "首次整页 write 被拒绝执行",
                            "observed_trajectory_ids": ["execution-1", "execution-2"],
                            "checked_absent_trajectory_ids": [],
                            "evidence": [
                                {
                                    "schema": "evidence.ref.v1",
                                    "run_id": "execution-1",
                                    "seq": 18,
                                }
                            ],
                            "counterevidence": [],
                            "confidence": 0.9,
                            "derivation_ids": [],
                            "limitations": ["仅覆盖同一条件组"],
                        }
                    ],
                    "limitations": [],
                }
            ],
            "limitations": ["全部轨迹均为已恢复型错误"],
        }

        normalized = validate_multi_trajectory_errors_view(value)
        self.assertEqual(normalized["schema"], "analysis.multi_trajectory_errors.v1")
        self.assertEqual(normalized["errors"][0]["error_id"], "E1")
        self.assertEqual(normalized["reports"][0]["dimensions"][0]["dimension"], "behavior")

    def test_multi_trajectory_errors_view_passes_warnings_through(self) -> None:
        value = {
            "schema": "analysis.multi_trajectory_errors.v1",
            "analysis_id": "analysis-1",
            "skill_id": "test-skill",
            "revision_id": "rev-1",
            "generated_at": utc_now(),
            "scope": {
                "eligible_trajectory_ids": ["execution-1"],
                "reviewed_trajectory_ids": ["execution-1"],
                "counterexample_search": "none",
            },
            "errors": [],
            "reports": [
                {
                    "error_id": "E1",
                    "dimensions": [],
                    "limitations": [],
                    "validation_warnings": [
                        "dimensions[0] lacks original Trajectory evidence "
                        "for ['execution-1']"
                    ],
                }
            ],
            "limitations": [],
        }

        normalized = validate_multi_trajectory_errors_view(value)
        self.assertEqual(
            normalized["reports"][0]["validation_warnings"][0],
            "dimensions[0] lacks original Trajectory evidence for "
            "['execution-1']",
        )

    def test_multi_trajectory_errors_view_rejects_unknown_dimension(self) -> None:
        value = {
            "schema": "analysis.multi_trajectory_errors.v1",
            "analysis_id": "analysis-1",
            "skill_id": "test-skill",
            "revision_id": "rev-1",
            "generated_at": utc_now(),
            "scope": {
                "eligible_trajectory_ids": ["execution-1"],
                "reviewed_trajectory_ids": ["execution-1"],
                "counterexample_search": "none",
            },
            "errors": [],
            "reports": [
                {
                    "error_id": "E1",
                    "dimensions": [
                        {
                            "dimension": "aesthetic",
                            "claim": "不属于四个维度",
                            "observed_trajectory_ids": ["execution-1"],
                            "checked_absent_trajectory_ids": [],
                            "evidence": [],
                            "counterevidence": [],
                            "confidence": 0.5,
                            "derivation_ids": [],
                            "limitations": [],
                        }
                    ],
                    "limitations": [],
                }
            ],
            "limitations": [],
        }

        with self.assertRaisesRegex(HierarchyError, "dimension is unsupported"):
            validate_multi_trajectory_errors_view(value)


if __name__ == "__main__":
    unittest.main()
