"""Tests for deterministic, fail-closed multi-trajectory research corpora."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    SkillHierarchyRepository,
    execution_manifest_from_payload,
)
from skill_evolution.research_corpus import (
    BEHAVIOR_PATTERNS,
    CONDITIONS_COVERAGE,
    RESULT_RELIABILITY,
    RESOURCE_EFFICIENCY,
    ResearchCorpusBuilder,
    ResearchCorpusError,
    verify_research_corpus,
)
from skill_evolution.storage import atomic_write_json
from skill_evolution.trajectory_precheck import precheck_trajectory


ROOT = Path(__file__).resolve().parents[1]
CURRENT_FIVE = (
    "20260725T154836Z-a58d6715",
    "20260725T155939Z-885eacfe",
    "20260725T160532Z-62d5c057",
    "20260725T161117Z-551972b5",
    "20260725T161732Z-9b0938dc",
)


def _write_skill(root: Path) -> Path:
    skill = root / "test-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        """---
name: Test Skill
description: Research-corpus fixture.
---

# Test Skill
""",
        encoding="utf-8",
    )
    atomic_write_json(
        skill / "skill_contract.json",
        {
            "schema": "skill.contract.v2",
            "skill_id": "test-skill",
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
        },
    )
    return skill


def _record(run_id: str, sequence: int, kind: str, payload: dict) -> dict:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": run_id,
        "seq": sequence,
        "observed_at": f"2026-08-09T00:00:{sequence:02d}Z",
        "elapsed_ms": sequence * 10,
        "source": "fixture",
        "type": kind,
        "payload": payload,
    }


def _write_accepted_reports(
    repository: SkillHierarchyRepository,
    *,
    revision_id: str,
    run_id: str,
    trajectory: Path,
) -> None:
    reports = (
        ("precheck", "trajectory.precheck.v1", precheck_trajectory(trajectory), "deterministic"),
        (
            "trajectory_error",
            "analysis.trajectory_error_report.v1",
            {
                "schema": "analysis.trajectory_error_report.v1",
                "run_id": run_id,
                "summary": "accepted single-trajectory report",
                "api_key": "must-not-enter-the-corpus",
            },
            "agent",
        ),
    )
    for kind, schema, result, producer in reports:
        identifier_kind = kind.replace("_", "-")
        analysis_id = f"{run_id}-{identifier_kind}"
        record = {
            "schema": ANALYSIS_RECORD_SCHEMA,
            "analysis_id": analysis_id,
            "skill_id": "test-skill",
            "revision_id": revision_id,
            "scope": "single_execution",
            "execution_id": run_id,
            "execution_set_id": None,
            "kind": kind,
            "producer": producer,
            "status": "accepted",
            "input_refs": [],
            "result_refs": [{"path": "result.json", "schema": schema}],
            "attempts": [],
            "created_at": "2026-08-09T00:01:00Z",
            "ended_at": "2026-08-09T00:01:01Z",
            "provenance": None,
        }
        directory, _ = repository.create_analysis(record)
        atomic_write_json(directory / "result.json", result)


def _create_execution(
    repository: SkillHierarchyRepository,
    *,
    revision_id: str,
    index: int,
    large_script: bool = False,
    input_text: str = "# Shared input\n",
    model_id: str = "fixture-model",
    include_comparison_runtime: bool = True,
) -> str:
    run_id = f"execution-{index}"
    execution = repository.prepare_execution(
        skill_id="test-skill",
        revision_id=revision_id,
        origin="direct",
        execution_id=run_id,
    )
    artifacts = execution.payload_directory / "artifacts"
    artifacts.mkdir()
    input_path = artifacts / "input.md"
    output_path = artifacts / "output.html"
    script_path = artifacts / "generate.py"
    input_path.write_text(input_text, encoding="utf-8")
    output_path.write_text(
        f"<!doctype html><title>Run {index}</title>", encoding="utf-8"
    )
    padding = "x" * 2_100_000 if large_script else "small"
    script_content = (
        "output_path = 'output.html'\n"
        f"payload = '{padding}'\n"
        "# LARGE_TRAJECTORY_END\n"
    )
    script_path.write_text(script_content, encoding="utf-8")
    records = [
        _record(
            run_id,
            1,
            "trajectory_started",
            {
                "manifest": {
                    "started_at": "2026-08-09T00:00:00Z",
                    "runtime": (
                        {
                            "platform": "fixture-platform",
                            "python": "3.11.0",
                            "model": {
                                "provider": "fixture-provider",
                                "id": model_id,
                                "api": "fixture-api",
                            },
                            "thinking_level": "off",
                            "pi_args": [
                                "--no-extensions",
                                "--tools",
                                "read,write,bash",
                                "--thinking",
                                "off",
                            ],
                        }
                        if include_comparison_runtime
                        else {}
                    ),
                    "task_case": {
                        "task_case_id": f"case-{index}",
                        "capability_tags": ["format:markdown"],
                        "expected_artifacts": ["artifacts/output.html"],
                    },
                }
            },
        ),
        _record(
            run_id,
            2,
            "message_action",
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "I will inspect the output."},
                    ],
                    "usage": {
                        "input": 10 + index,
                        "output": 5,
                        "cacheRead": 2,
                        "cacheWrite": 1,
                        "totalTokens": 999_999,
                        "cost": {"total": 0.01},
                    },
                }
            },
        ),
        _record(
            run_id,
            3,
            "tool_action",
            {
                "tool_name": "write",
                "status": "failed",
                "duration_ms": 3,
                "arguments": {
                    "path": str(output_path),
                    "content": "direct output",
                },
                "api_key": "trajectory-secret",
                "error": {"type": "write_failed", "message": "too large"},
            },
        ),
        _record(
            run_id,
            4,
            "tool_action",
            {
                "tool_name": "write",
                "status": "succeeded",
                "duration_ms": 4,
                "arguments": {
                    "path": str(script_path),
                    "content": script_content,
                },
            },
        ),
        _record(
            run_id,
            5,
            "tool_action",
            {
                "tool_name": "bash",
                "status": "succeeded",
                "duration_ms": 5,
                "arguments": {"command": "python3 generate.py"},
            },
        ),
        _record(
            run_id,
            6,
            "tool_action",
            {
                "tool_name": "bash",
                "status": "succeeded",
                "duration_ms": 2,
                "arguments": {"command": f"grep -q '<title>' {output_path}"},
            },
        ),
        _record(
            run_id,
            7,
            "artifact_registered",
            {
                "artifact_role": "input",
                "artifact": {
                    "path": "artifacts/input.md",
                    "exists": True,
                    "bytes": input_path.stat().st_size,
                },
            },
        ),
        _record(
            run_id,
            8,
            "artifact_registered",
            {
                "artifact_role": "output",
                "artifact": {
                    "path": "artifacts/output.html",
                    "exists": True,
                    "bytes": output_path.stat().st_size,
                },
            },
        ),
        _record(
            run_id,
            9,
            "trajectory_finished",
            {
                "outcome": {
                    "status": "succeeded",
                    "started_at": "2026-08-09T00:00:00Z",
                    "ended_at": "2026-08-09T00:00:09Z",
                    "duration_ms": 900 + index,
                    "session": {"status": "missing"},
                }
            },
        ),
        _record(
            run_id,
            10,
            "trajectory_sealed",
            {"status": "succeeded", "record_count": 10},
        ),
    ]
    trajectory = execution.payload_directory / "trajectory.jsonl"
    trajectory.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    manifest = execution_manifest_from_payload(
        execution_directory=execution.directory,
        skill_id="test-skill",
        revision_id=revision_id,
        execution_id=run_id,
        origin="direct",
    )
    repository.finalize_execution("test-skill", run_id, manifest)
    _write_accepted_reports(
        repository,
        revision_id=revision_id,
        run_id=run_id,
        trajectory=trajectory,
    )
    return run_id


def _fixture(root: Path, *, large_script: bool = False) -> tuple[Path, list[str]]:
    packages = root / "packages"
    runtime = root / "runtime"
    repository = SkillHierarchyRepository(runtime)
    revision = repository.register_revision(_write_skill(packages))
    revision_id = str(revision.manifest["revision_id"])
    execution_ids = [
        _create_execution(
            repository,
            revision_id=revision_id,
            index=index,
            large_script=large_script and index == 1,
        )
        for index in range(1, 4)
    ]
    return runtime, execution_ids


def _write_approved_suite(root: Path) -> tuple[Path, str]:
    suites = root / "evaluation-suites"
    cases = root / "task-cases"
    suites.mkdir()
    cases.mkdir()
    references = []
    for index, condition in ((1, "group-a"), (2, "group-a"), (3, "group-b")):
        task_case_id = f"case-{index}"
        relative = f"task-cases/{task_case_id}.json"
        atomic_write_json(
            root / relative,
            {
                "schema": "task.case.v1",
                "task_case_id": task_case_id,
                "delivery": "inline_text",
                "input": {"text": f"Task {index}"},
                "expected_artifacts": ["output.html"],
                "capability_tags": ["format:test"],
                "budget": {},
            },
        )
        references.append(
            {
                "task_case_id": task_case_id,
                "path": relative,
                "conditions": {"fixture-group": condition},
            }
        )
    suite_id = "test-suite-v1"
    atomic_write_json(
        suites / f"{suite_id}.json",
        {
            "schema": "evaluation.suite.v1",
            "suite_id": suite_id,
            "skill_id": "test-skill",
            "version": "1.0.0",
            "status": "approved",
            "owner": "test-owner",
            "approved_by": "test-owner",
            "approved_at": "2026-08-14T00:00:00Z",
            "task_cases": references,
            "coverage_dimensions": [
                {
                    "id": "fixture-group",
                    "required_values": ["group-a", "group-b"],
                }
            ],
            "readiness": {
                "minimum_distinct_condition_groups": 2,
                "minimum_samples_per_condition_group": 1,
            },
        },
    )
    return suites, suite_id


def _rewrite_trajectory_usage(
    runtime: Path,
    execution_id: str,
    *,
    remove_field: str,
) -> None:
    trajectory = (
        runtime
        / "skills/test-skill/executions"
        / execution_id
        / "payload/trajectory.jsonl"
    )
    records = [json.loads(line) for line in trajectory.read_text().splitlines()]
    usage = records[1]["payload"]["message"]["usage"]
    del usage[remove_field]
    trajectory.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


class ResearchCorpusTests(unittest.TestCase):
    """Prove research evidence stays raw, navigable, and immutable."""

    def test_build_is_deterministic_and_preserves_large_script_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root, large_script=True)
            builder = ResearchCorpusBuilder(runtime)

            first = builder.build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "corpus-one",
            )
            second = builder.build(
                skill_id="test-skill",
                execution_ids=list(reversed(execution_ids)),
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "corpus-two",
            )

            self.assertEqual(first.manifest, second.manifest)
            self.assertEqual(first.content_sha256, second.content_sha256)
            self.assertEqual(first.baseline_digest, second.baseline_digest)
            self.assertNotIn(str(root), json.dumps(first.manifest))
            self.assertFalse(
                SkillHierarchyRepository(runtime)
                .multi_trajectory_analyses_directory("test-skill")
                .exists()
            )
            large = next(
                item
                for item in first.navigation_index["scripts"]
                if item["run_id"] == "execution-1"
                and item["event"] == "created"
            )
            self.assertGreater(len(large["content"]), 2_000_000)
            self.assertTrue(large["content"].endswith("# LARGE_TRAJECTORY_END\n"))
            self.assertEqual(large["evidence"], {"run_id": "execution-1", "seq": 4})
            recovery = next(
                item
                for item in first.navigation_index["entries"]
                if item["run_id"] == "execution-1" and "recovery" in item["flags"]
            )
            self.assertEqual(recovery["recovered_failure_seqs"], [3])
            trajectory_text = (
                first.directory / "runs/execution-1/trajectory.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn("[HIDDEN_MODEL_REASONING]", trajectory_text)
            self.assertNotIn("private reasoning", trajectory_text)
            self.assertNotIn("trajectory-secret", trajectory_text)
            reports = list(
                (first.directory / "runs/execution-1/single-reports").rglob(
                    "*.json"
                )
            )
            self.assertTrue(reports)
            self.assertNotIn(
                "must-not-enter-the-corpus",
                "".join(path.read_text(encoding="utf-8") for path in reports),
            )
            self.assertNotIn("totalTokens", json.dumps(first.baseline))

    def test_verify_rejects_tampering_extra_files_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            corpus = ResearchCorpusBuilder(runtime).build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "source",
            )
            verified = verify_research_corpus(
                corpus.directory,
                expected_content_sha256=corpus.content_sha256,
                expected_baseline_sha256=corpus.baseline_digest,
            )
            self.assertEqual(verified.execution_ids, tuple(execution_ids))

            tampered = root / "tampered"
            shutil.copytree(corpus.directory, tampered)
            (tampered / "baseline.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ResearchCorpusError, "verification"):
                verify_research_corpus(tampered)

            extra = root / "extra"
            shutil.copytree(corpus.directory, extra)
            (extra / "unlisted.txt").write_text("unlisted", encoding="utf-8")
            with self.assertRaisesRegex(ResearchCorpusError, "inventory"):
                verify_research_corpus(extra)

            linked = root / "linked"
            shutil.copytree(corpus.directory, linked)
            (linked / "escape").symlink_to(root / "outside")
            with self.assertRaisesRegex(ResearchCorpusError, "symbolic link"):
                verify_research_corpus(linked)

            with self.assertRaisesRegex(ResearchCorpusError, "session"):
                verify_research_corpus(
                    corpus.directory,
                    expected_content_sha256="0" * 64,
                )

    def test_invalid_json_and_missing_sequence_fail_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            trajectory = (
                runtime
                / "skills/test-skill/executions/execution-1/payload/trajectory.jsonl"
            )
            original = trajectory.read_text(encoding="utf-8")
            lines = original.splitlines()
            lines[2] = "not-json"
            trajectory.write_text("\n".join(lines) + "\n", encoding="utf-8")
            builder = ResearchCorpusBuilder(runtime)
            readiness = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
            )
            self.assertEqual(readiness["status"], "not_ready")
            self.assertEqual(
                readiness["issues"][0]["code"], "execution_not_research_ready"
            )

            records = [json.loads(line) for line in original.splitlines()]
            del records[2]["seq"]
            trajectory.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            destination = root / "must-not-exist"
            with self.assertRaisesRegex(ResearchCorpusError, "not ready"):
                builder.build(
                    skill_id="test-skill",
                    execution_ids=execution_ids,
                    objectives=[BEHAVIOR_PATTERNS],
                    destination=destination,
                )
            self.assertFalse(destination.exists())

    def test_objective_readiness_and_proposed_suite_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            builder = ResearchCorpusBuilder(
                runtime,
                evaluation_suites_root=ROOT / "evaluation-suites",
                project_root=ROOT,
            )
            too_few = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[BEHAVIOR_PATTERNS],
            )
            self.assertEqual(too_few["status"], "not_ready")
            self.assertIn(
                "insufficient_behavior_samples",
                {item["code"] for item in too_few["issues"]},
            )
            reliable = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESULT_RELIABILITY],
            )
            self.assertEqual(reliable["status"], "not_ready")
            comparable = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESULT_RELIABILITY],
                condition_groups={
                    execution_ids[0]: "same-task-and-condition",
                    execution_ids[1]: "same-task-and-condition",
                },
            )
            self.assertEqual(comparable["status"], "ready")

            repository = SkillHierarchyRepository(runtime)
            revision_id = str(
                repository.load_execution("test-skill", execution_ids[0])[
                    "revision_id"
                ]
            )
            unrelated = _create_execution(
                repository,
                revision_id=revision_id,
                index=4,
                input_text="# Different input\n",
            )
            false_group = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], unrelated],
                objectives=[RESULT_RELIABILITY],
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    unrelated: "same-label-only",
                },
            )
            self.assertEqual(false_group["status"], "not_ready")
            self.assertIn(
                "condition_group_not_comparable",
                {item["code"] for item in false_group["issues"]},
            )

            different_model = _create_execution(
                repository,
                revision_id=revision_id,
                index=5,
                model_id="different-model",
            )
            model_group = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], different_model],
                objectives=[RESULT_RELIABILITY],
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    different_model: "same-label-only",
                },
            )
            self.assertEqual(model_group["status"], "not_ready")
            self.assertIn(
                "condition_group_not_comparable",
                {item["code"] for item in model_group["issues"]},
            )

            unknown_runtime = _create_execution(
                repository,
                revision_id=revision_id,
                index=6,
                include_comparison_runtime=False,
            )
            incomplete_group = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], unknown_runtime],
                objectives=[RESULT_RELIABILITY],
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    unknown_runtime: "same-label-only",
                },
            )
            incomplete_issue = next(
                item
                for item in incomplete_group["issues"]
                if item["code"] == "condition_group_not_comparable"
            )
            self.assertIn(
                unknown_runtime,
                incomplete_issue["missing_runtime_facts"],
            )
            coverage = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[CONDITIONS_COVERAGE],
                evaluation_suite_id="document-html-visualizer-v2",
            )
            self.assertEqual(coverage["status"], "not_ready")
            self.assertIn(
                "evaluation_suite_not_approved",
                {item["code"] for item in coverage["issues"]},
            )

    def test_mixed_revisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            repository = SkillHierarchyRepository(runtime)
            package = root / "packages/test-skill"
            skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
            (package / "SKILL.md").write_text(
                skill_text + "\nRevision two.\n", encoding="utf-8"
            )
            revision = repository.register_revision(
                package, lifecycle="historical"
            )
            fourth = _create_execution(
                repository,
                revision_id=str(revision.manifest["revision_id"]),
                index=4,
            )

            readiness = ResearchCorpusBuilder(runtime).assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], fourth],
                objectives=[RESULT_RELIABILITY],
            )

            self.assertEqual(readiness["status"], "not_ready")
            self.assertIn(
                "mixed_revisions", {item["code"] for item in readiness["issues"]}
            )

    def test_resource_samples_must_be_complete_and_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            builder = ResearchCorpusBuilder(runtime)
            same_group = {execution_id: "same" for execution_id in execution_ids}

            complete = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESOURCE_EFFICIENCY],
                condition_groups={
                    execution_ids[0]: "same",
                    execution_ids[1]: "same",
                },
            )
            self.assertEqual(complete["status"], "ready")

            different = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESOURCE_EFFICIENCY],
                condition_groups={
                    execution_ids[0]: "one",
                    execution_ids[1]: "two",
                },
            )
            self.assertEqual(different["status"], "not_ready")

            _rewrite_trajectory_usage(
                runtime,
                execution_ids[0],
                remove_field="cacheWrite",
            )
            incomplete = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESOURCE_EFFICIENCY],
                condition_groups=same_group,
            )
            self.assertEqual(incomplete["status"], "not_ready")
            self.assertIn(
                "insufficient_resource_samples",
                {item["code"] for item in incomplete["issues"]},
            )

            corpus = builder.build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[RESOURCE_EFFICIENCY],
                condition_groups=same_group,
                destination=root / "resource-corpus",
            )
            row = next(
                item
                for item in corpus.baseline["runs"]
                if item["run_id"] == execution_ids[0]
            )
            self.assertFalse(row["resource_complete"])
            self.assertIsNone(row["cache_write_tokens"])
            self.assertIsNone(row["input_tokens"])

    def test_approved_suite_readiness_and_normalized_mapping_are_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            suites, suite_id = _write_approved_suite(root)
            builder = ResearchCorpusBuilder(
                runtime,
                evaluation_suites_root=suites,
                project_root=root,
            )
            mismatched_conditions = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], execution_ids[2]],
                objectives=[CONDITIONS_COVERAGE, RESULT_RELIABILITY],
                evaluation_suite_id=suite_id,
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    execution_ids[2]: "same-label-only",
                },
            )
            self.assertEqual(mismatched_conditions["status"], "not_ready")
            self.assertIn(
                "condition_group_not_comparable",
                {item["code"] for item in mismatched_conditions["issues"]},
            )

            reliability_only = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], execution_ids[2]],
                objectives=[RESULT_RELIABILITY],
                evaluation_suite_id=suite_id,
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    execution_ids[2]: "same-label-only",
                },
            )
            self.assertEqual(reliability_only["status"], "not_ready")
            self.assertIn(
                "condition_group_not_comparable",
                {item["code"] for item in reliability_only["issues"]},
            )

            comparable_corpus = builder.build(
                skill_id="test-skill",
                execution_ids=execution_ids[:2],
                objectives=[RESULT_RELIABILITY],
                evaluation_suite_id=suite_id,
                condition_groups={
                    execution_ids[0]: "same-condition",
                    execution_ids[1]: "same-condition",
                },
                destination=root / "reliability-corpus",
            )
            self.assertIsNone(comparable_corpus.readiness["coverage"])
            self.assertEqual(
                comparable_corpus.manifest["evaluation_suite"],
                "evaluation/suite.json",
            )
            verify_research_corpus(comparable_corpus.directory)

            corpus = builder.build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[CONDITIONS_COVERAGE],
                evaluation_suite_id=suite_id,
                destination=root / "coverage-corpus",
            )

            readiness = json.loads(
                (corpus.directory / "readiness.json").read_text(encoding="utf-8")
            )
            suite = json.loads(
                (corpus.directory / "evaluation/suite.json").read_text(
                    encoding="utf-8"
                )
            )
            mapping = json.loads(
                (
                    corpus.directory / "evaluation/task-condition-map.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(readiness["status"], "ready")
            self.assertEqual(suite["status"], "approved")
            self.assertEqual(
                {item["run_id"] for item in mapping["execution_mapping"]},
                set(execution_ids),
            )
            self.assertEqual(len(mapping["task_cases"]), 3)
            verify_research_corpus(corpus.directory)

            repository = SkillHierarchyRepository(runtime)
            revision_id = str(
                repository.load_execution(
                    "test-skill", execution_ids[0]
                )["revision_id"]
            )
            unmapped_execution_id = _create_execution(
                repository,
                revision_id=revision_id,
                index=4,
            )
            missing_comparison_mapping = builder.assess_readiness(
                skill_id="test-skill",
                execution_ids=[execution_ids[0], unmapped_execution_id],
                objectives=[RESULT_RELIABILITY],
                evaluation_suite_id=suite_id,
                condition_groups={
                    execution_ids[0]: "same-label-only",
                    unmapped_execution_id: "same-label-only",
                },
            )
            self.assertEqual(missing_comparison_mapping["status"], "not_ready")
            self.assertIn(
                "comparison_task_case_mapping_missing",
                {
                    item["code"]
                    for item in missing_comparison_mapping["issues"]
                },
            )

            (corpus.directory / "evaluation/suite.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ResearchCorpusError, "verification"):
                verify_research_corpus(corpus.directory)

    def test_corpus_redacts_untrusted_strings_and_excludes_binary_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            execution_root = (
                runtime
                / "skills/test-skill/executions"
                / execution_ids[0]
            )
            trajectory_path = execution_root / "payload/trajectory.jsonl"
            records = [
                json.loads(line)
                for line in trajectory_path.read_text(encoding="utf-8").splitlines()
            ]
            message = records[1]["payload"]["message"]
            message["reasoning_content"] = "reasoning-must-not-leak"
            message["note"] = "Authorization: Bearer trajectory-token"
            message["env"] = "API_KEY=environment-token"
            trajectory_path.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )

            manifest_path = execution_root / "execution.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["task"]["environment"] = "TOKEN=task-token"
            script = execution_root / "payload/artifacts/generate.py"
            script.write_text(
                script.read_text(encoding="utf-8")
                + "# Authorization: Bearer artifact-token\n",
                encoding="utf-8",
            )
            binary = execution_root / "payload/artifacts/sample.bin"
            binary.write_bytes(b"\x00binary-secret\xff")
            manifest["supporting_artifacts"] = [
                {
                    "artifact_id": "generator-script",
                    "path": "payload/artifacts/generate.py",
                    "bytes": script.stat().st_size,
                    "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                    "media_type": "text/x-python",
                },
                {
                    "artifact_id": "binary-sample",
                    "path": "payload/artifacts/sample.bin",
                    "bytes": binary.stat().st_size,
                    "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "media_type": "application/octet-stream",
                },
            ]
            atomic_write_json(manifest_path, manifest)

            corpus = ResearchCorpusBuilder(runtime).build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "redacted-corpus",
            )
            serialized = ""
            for path in corpus.directory.rglob("*"):
                if path.is_file():
                    try:
                        serialized += path.read_text(encoding="utf-8")
                    except UnicodeError:
                        self.fail(f"Unexpected binary evidence entered corpus: {path}")
            for secret in (
                "reasoning-must-not-leak",
                "trajectory-token",
                "environment-token",
                "task-token",
                "artifact-token",
                "binary-secret",
            ):
                self.assertNotIn(secret, serialized)
            self.assertIn("[HIDDEN_MODEL_REASONING]", serialized)
            self.assertIn("output_path", serialized)

            run = next(
                item
                for item in corpus.manifest["runs"]
                if item["execution_id"] == execution_ids[0]
            )
            generator = next(
                item
                for item in run["artifacts"]
                if item["artifact_id"] == "generator-script"
            )
            binary_record = next(
                item
                for item in run["artifacts"]
                if item["artifact_id"] == "binary-sample"
            )
            self.assertTrue(generator["available"])
            self.assertTrue(
                generator["path"].startswith(
                    f"runs/{execution_ids[0]}/artifacts/supporting/"
                )
            )
            self.assertEqual(generator["redaction"], "sanitized")
            self.assertFalse(binary_record["available"])
            self.assertEqual(
                binary_record["exclusion_reason"], "binary_or_unsupported"
            )
            self.assertIn("artifact-token", script.read_text(encoding="utf-8"))

    def test_artifact_namespace_prevents_cross_role_overwrite_and_reserved_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            manifest_path = (
                runtime
                / "skills/test-skill/executions"
                / execution_ids[0]
                / "execution.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["outputs"][0]["path"] = manifest["inputs"][0]["path"]
            manifest["outputs"][0]["bytes"] = manifest["inputs"][0]["bytes"]
            manifest["outputs"][0]["sha256"] = manifest["inputs"][0]["sha256"]
            atomic_write_json(manifest_path, manifest)

            corpus = ResearchCorpusBuilder(runtime).build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "namespaced-corpus",
            )
            run = next(
                item
                for item in corpus.manifest["runs"]
                if item["execution_id"] == execution_ids[0]
            )
            paths = [item["path"] for item in run["artifacts"] if item["available"]]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertTrue(any("/artifacts/input/" in path for path in paths))
            self.assertTrue(any("/artifacts/output/" in path for path in paths))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            execution_root = (
                runtime
                / "skills/test-skill/executions"
                / execution_ids[0]
            )
            manifest_path = execution_root / "execution.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            trajectory_path = execution_root / "payload/trajectory.jsonl"
            manifest["outputs"][0].update(
                {
                    "path": "payload/trajectory.jsonl",
                    "bytes": trajectory_path.stat().st_size,
                    "sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
                }
            )
            atomic_write_json(manifest_path, manifest)
            readiness = ResearchCorpusBuilder(runtime).assess_readiness(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
            )
            self.assertEqual(readiness["status"], "not_ready")
            self.assertIn(
                "reserved corpus file name",
                " ".join(item["message"] for item in readiness["issues"]),
            )

    def test_verifier_rejects_a_forged_redaction_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, execution_ids = _fixture(root)
            corpus = ResearchCorpusBuilder(runtime).build(
                skill_id="test-skill",
                execution_ids=execution_ids,
                objectives=[BEHAVIOR_PATTERNS],
                destination=root / "corpus",
            )
            manifest_path = corpus.directory / "corpus.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["redaction"] = {
                "hidden_reasoning": True,
                "credentials": True,
                "pi_session_included": False,
            }
            body = {
                key: value
                for key, value in manifest.items()
                if key not in {"schema", "corpus_id", "content_sha256"}
            }
            digest = hashlib.sha256(
                json.dumps(
                    body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            manifest["content_sha256"] = digest
            manifest["corpus_id"] = f"corpus-{digest[:20]}"
            atomic_write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ResearchCorpusError, "redaction policy"):
                verify_research_corpus(corpus.directory)

    def test_current_five_success_trajectories_pass_behavior_smoke(self) -> None:
        builder = ResearchCorpusBuilder(ROOT / ".skill-evolution")

        readiness = builder.assess_readiness(
            skill_id="document-html-visualizer",
            execution_ids=CURRENT_FIVE,
            objectives=[BEHAVIOR_PATTERNS],
        )

        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["revision_id"], "rev-d06ece0ddc22cb38")


if __name__ == "__main__":
    unittest.main()
