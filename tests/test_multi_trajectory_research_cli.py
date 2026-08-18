"""Tests for the gated internal multi-Trajectory research command line."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.multi_trajectory_research import _run_cli
from scripts.prompt_approval import PromptApprovalError
from skill_evolution.hierarchy import SkillHierarchyRepository
from skill_evolution.research_agent_runtime import ResearchAgentRuntimeError
from skill_evolution.research_workflow import ResearchWorkflowError
from skill_evolution.storage import StorageError, load_json_object
from tests.test_research_corpus import _fixture
from tests.test_research_workflow import (
    _benchmark,
    _corpus,
    _strict_harness_acceptance,
    _write_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MultiTrajectoryResearchCliTests(unittest.TestCase):
    """CLI gates remain fail-closed and never publish a product analysis."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_root, self.execution_ids = _fixture(
            self.root / "hierarchy-fixture"
        )
        self.research_root = self.root / "research-batches"
        self.agent_runs_root = self.root / "agent-runs"
        # Unapproved prompts fixture: live-command gates must fail closed
        # regardless of which production prompts happen to be approved.
        self.prompts_root = self.root / "prompts"
        self.prompts_root.mkdir()
        for filename in (
            "behavior-pattern-research-v1.md",
            "conditions-coverage-research-v1.md",
            "outcome-consistency-research-v1.md",
            "resource-efficiency-research-v1.md",
        ):
            prompt = self.prompts_root / filename
            prompt.write_text("Proposed research protocol.\n", encoding="utf-8")
            _write_json(
                prompt.with_name(prompt.name + ".approval.json"),
                {
                    "schema": "prompt.approval.v1",
                    "status": "proposed",
                    "prompt_id": f"analysis.{filename.split('-research')[0]}",
                    "version": "1",
                    "prompt_file": prompt.name,
                    "content_sha256": None,
                    "approved_by": None,
                    "approved_at": None,
                },
            )
        self.harness_context = self.root / "research-harness-context-v1.json"
        _write_json(self.harness_context, {"schema": "fixture.context.v1"})
        _write_json(
            self.harness_context.with_name(
                self.harness_context.name + ".approval.json"
            ),
            {
                "schema": "prompt.approval.v1",
                "status": "proposed",
                "prompt_id": "analysis.research-harness-context",
                "version": "1",
                "prompt_file": self.harness_context.name,
                "content_sha256": None,
                "approved_by": None,
                "approved_at": None,
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _arguments(self, *arguments: str) -> list[str]:
        return [
            "--project-root",
            str(PROJECT_ROOT),
            "--runtime-root",
            str(self.runtime_root),
            "--research-root",
            str(self.research_root),
            "--agent-runs-root",
            str(self.agent_runs_root),
            "--prompts-root",
            str(self.prompts_root),
            "--research-harness-context",
            str(self.harness_context),
            *arguments,
        ]

    def _selection(
        self,
        execution_ids: list[str] | None = None,
    ) -> list[str]:
        arguments = ["--skill-id", "test-skill"]
        for execution_id in execution_ids or self.execution_ids:
            arguments.extend(["--execution-id", execution_id])
        arguments.extend(["--objective", "behavior_patterns"])
        return arguments

    def _invoke(self, *arguments: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = _run_cli(self._arguments(*arguments))
        return status, json.loads(output.getvalue())

    def _product_multi_trajectory_files(self) -> list[Path]:
        directory = SkillHierarchyRepository(
            self.runtime_root
        ).multi_trajectory_analyses_directory("test-skill")
        return sorted(directory.rglob("*")) if directory.exists() else []

    def _prepare_harness_batch(self, batch_id: str) -> Path:
        corpus = _corpus(self.root / f"{batch_id}-input")
        status, prepared = self._invoke(
            "prepare",
            "--corpus-directory",
            str(corpus.directory),
            "--batch-id",
            batch_id,
        )
        self.assertEqual(status, 0)
        self.assertEqual(prepared["status"], "prepared")
        with patch(
            "skill_evolution.research_workflow.run_harness_acceptance",
            side_effect=_strict_harness_acceptance,
        ) as runner:
            status, harness = self._invoke(
                "validate-harness",
                "--batch-id",
                batch_id,
            )
        self.assertEqual(status, 0)
        self.assertEqual(harness["status"], "harness_validated")
        runner.assert_called_once()
        return corpus.directory

    def test_assess_returns_ready_and_not_ready_exit_codes(self) -> None:
        ready_status, ready = self._invoke(
            "assess",
            *self._selection(),
        )
        not_ready_status, not_ready = self._invoke(
            "assess",
            *self._selection(self.execution_ids[:1]),
        )

        self.assertEqual(ready_status, 0)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(not_ready_status, 3)
        self.assertEqual(not_ready["status"], "not_ready")
        self.assertTrue(not_ready["issues"])
        self.assertFalse(self.research_root.exists())
        self.assertFalse(self.agent_runs_root.exists())
        self.assertEqual(self._product_multi_trajectory_files(), [])

    def test_build_prepare_status_harness_and_benchmark_are_gated(self) -> None:
        corpus_directory = self.root / "built-corpus"
        build_status, built = self._invoke(
            "build-corpus",
            *self._selection(),
            "--destination",
            str(corpus_directory),
        )

        self.assertEqual(build_status, 0)
        self.assertEqual(built["status"], "ready")
        self.assertEqual(Path(built["directory"]), corpus_directory.resolve())

        prepare_status, prepared = self._invoke(
            "prepare",
            "--corpus-directory",
            str(corpus_directory),
            "--batch-id",
            "batch-cli",
        )
        self.assertEqual(prepare_status, 0)
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(
            prepared["readiness"],
            load_json_object(corpus_directory / "readiness.json"),
        )

        status_code, persisted = self._invoke(
            "status",
            "--batch-id",
            "batch-cli",
        )
        self.assertEqual(status_code, 0)
        self.assertEqual(persisted, prepared)

        with patch(
            "skill_evolution.research_workflow.run_harness_acceptance",
            side_effect=_strict_harness_acceptance,
        ) as runner:
            harness_status, harness = self._invoke(
                "validate-harness",
                "--batch-id",
                "batch-cli",
            )
        runner.assert_called_once()
        self.assertEqual(harness_status, 0)
        self.assertEqual(harness["status"], "harness_validated")
        self.assertEqual(
            set(harness["harness_validation"]["checks"]),
            {
                "corpus_preflight",
                "navigation_index",
                "evidence_roundtrip",
                "fake_agent_research_loop",
                "sandbox_isolation",
                "resource_limits",
                "structured_submission",
            },
        )

        readiness = built["readiness"]
        benchmark = _benchmark(
            self.root / "validation-benchmark.json",
            skill_id=str(readiness["skill_id"]),
            revision_id=str(readiness["revision_id"]),
            execution_ids=list(readiness["execution_ids"]),
        )
        freeze_status, frozen = self._invoke(
            "freeze-benchmark",
            "--batch-id",
            "batch-cli",
            "--benchmark-file",
            str(benchmark),
        )

        self.assertEqual(freeze_status, 0)
        self.assertIsNotNone(frozen["validation_benchmark"])
        self.assertFalse(self.agent_runs_root.exists())
        self.assertEqual(self._product_multi_trajectory_files(), [])

    def test_live_commands_fail_closed_before_creating_agent_runs(self) -> None:
        corpus_directory = self._prepare_harness_batch("blocked-batch")
        readiness = load_json_object(corpus_directory / "readiness.json")
        benchmark = _benchmark(
            self.root / "blocked-benchmark.json",
            skill_id=str(readiness["skill_id"]),
            revision_id=str(readiness["revision_id"]),
            execution_ids=list(readiness["execution_ids"]),
        )
        self._invoke(
            "freeze-benchmark",
            "--batch-id",
            "blocked-batch",
            "--benchmark-file",
            str(benchmark),
        )

        with self.assertRaisesRegex(
            (PromptApprovalError, ResearchAgentRuntimeError),
            "not approved",
        ):
            self._invoke("run-smoke", "--batch-id", "blocked-batch")
        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "passed smoke cycle",
        ):
            self._invoke("issue-capability", "--batch-id", "blocked-batch")
        with self.assertRaises((ResearchWorkflowError, StorageError)):
            self._invoke(
                "import-capability",
                "--batch-id",
                "blocked-batch",
                "--source-batch-id",
                "missing-source",
            )
        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "validated single-agent research",
        ):
            self._invoke("run-specialists", "--batch-id", "blocked-batch")

        _, persisted = self._invoke(
            "status",
            "--batch-id",
            "blocked-batch",
        )
        self.assertEqual(persisted["status"], "harness_validated")
        self.assertEqual(persisted["validation_cycles"], [])
        self.assertIsNone(persisted["specialist_board_id"])
        self.assertFalse(self.agent_runs_root.exists())
        self.assertEqual(self._product_multi_trajectory_files(), [])

    def test_check_prompts_uses_global_root_and_checks_harness_context(self) -> None:
        prompts = self.root / "check-prompts-fixture"
        prompts.mkdir()
        prompt = prompts / "behavior-pattern-research-v1.md"
        prompt.write_text("Proposed research protocol.\n", encoding="utf-8")
        approval = prompt.with_name(prompt.name + ".approval.json")
        _write_json(
            approval,
            {
                "schema": "prompt.approval.v1",
                "status": "proposed",
                "prompt_id": "analysis.behavior-pattern-research",
                "version": "1",
                "prompt_file": prompt.name,
                "content_sha256": None,
                "approved_by": None,
                "approved_at": None,
            },
        )
        harness_context = self.root / "research-harness-context-v1.json"
        _write_json(harness_context, {"schema": "fixture.context.v1"})
        harness_approval = harness_context.with_name(
            harness_context.name + ".approval.json"
        )
        _write_json(
            harness_approval,
            {
                "schema": "prompt.approval.v1",
                "status": "proposed",
                "prompt_id": "analysis.research-harness-context",
                "version": "1",
                "prompt_file": harness_context.name,
                "content_sha256": None,
                "approved_by": None,
                "approved_at": None,
            },
        )

        with self.assertRaisesRegex(PromptApprovalError, "not approved"):
            self._invoke(
                "--prompts-root",
                str(prompts),
                "--research-harness-context",
                str(harness_context),
                "check-prompts",
                "--mode",
                "smoke",
            )

        prompt_digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
        _write_json(
            approval,
            {
                "schema": "prompt.approval.v1",
                "status": "approved",
                "prompt_id": "analysis.behavior-pattern-research",
                "version": "1",
                "prompt_file": prompt.name,
                "content_sha256": prompt_digest,
                "approved_by": "project-owner",
                "approved_at": "2026-08-14T12:00:00+08:00",
            },
        )
        with self.assertRaisesRegex(PromptApprovalError, "not approved"):
            self._invoke(
                "--prompts-root",
                str(prompts),
                "--research-harness-context",
                str(harness_context),
                "check-prompts",
                "--mode",
                "smoke",
            )

        self.assertEqual(load_json_object(harness_approval)["status"], "proposed")


if __name__ == "__main__":
    unittest.main()
