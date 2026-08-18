"""Sandbox-only comparison planning and gate-classification tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from skill_evolution.comparison import (
    ComparisonError,
    ComparisonRepository,
    DockerSandbox,
    SandboxPreflightResult,
    build_run_plan,
    classify_dimensions,
    validate_test_effect,
)


class FakeSandbox:
    """A deterministic sandbox double that exposes every attempted entry."""

    name = "docker_tool_router"

    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.entered_directories: list[Path] = []

    def preflight(self) -> SandboxPreflightResult:
        return SandboxPreflightResult(
            available=self.available,
            backend=self.name,
            detail="ready" if self.available else "daemon unavailable",
        )

    @contextmanager
    def isolated_run(self, run_directory: str | Path):
        if not self.available:
            raise AssertionError("Unavailable sandbox must never be entered")
        directory = Path(run_directory)
        self.entered_directories.append(directory)
        yield {
            "backend": self.name,
            "container_id": f"container-{len(self.entered_directories)}",
            "network": "none",
            "credentials_in_container": False,
        }


class AttestedRunner:
    """Wrap a fake callback with the mandatory production boundary claims."""

    sandbox_backend = "docker_tool_router"
    built_in_tools = False
    host_fallback_allowed = False

    def __init__(self, callback) -> None:
        self.callback = callback

    def __call__(self, planned, sandbox_context, run_directory):
        return self.callback(planned, sandbox_context, run_directory)


class FlakySandbox(FakeSandbox):
    """Fail the first container entry, then behave normally."""

    def __init__(self) -> None:
        super().__init__(available=True)
        self.entry_attempts = 0

    @contextmanager
    def isolated_run(self, run_directory: str | Path):
        self.entry_attempts += 1
        if self.entry_attempts == 1:
            raise ComparisonError("container could not start")
        with super().isolated_run(run_directory) as context:
            yield context


class ComparisonPlanTests(unittest.TestCase):
    """Verify the fixed smoke and paired baseline/candidate replay budget."""

    def test_default_plan_has_thirteen_runs_and_alternates_each_pair(
        self,
    ) -> None:
        plan = build_run_plan(
            triggering_task_case_id="trigger",
            regression_task_case_id="regression",
        )

        self.assertEqual(len(plan), 13)
        self.assertEqual(
            plan[0],
            {
                "phase": "smoke",
                "variant": "candidate",
                "task_case_id": "trigger",
                "repetition": 0,
                "attempt_index": 1,
            },
        )
        paired = plan[1:]
        for offset in range(0, len(paired), 2):
            pair = paired[offset : offset + 2]
            self.assertEqual(
                {item["variant"] for item in pair},
                {"baseline", "candidate"},
            )
            self.assertEqual(
                pair[0]["task_case_id"],
                pair[1]["task_case_id"],
            )
            self.assertEqual(
                pair[0]["repetition"],
                pair[1]["repetition"],
            )
        trigger_pairs = [
            item["variant"]
            for item in paired
            if item["task_case_id"] == "trigger"
        ]
        self.assertEqual(
            trigger_pairs,
            [
                "baseline",
                "candidate",
                "candidate",
                "baseline",
                "baseline",
                "candidate",
            ],
        )
        self.assertEqual(
            [item["attempt_index"] for item in plan],
            list(range(1, 14)),
        )

    def test_plan_above_automatic_budget_requires_explicit_request(self) -> None:
        with self.assertRaisesRegex(
            ComparisonError,
            "expanded replay request",
        ):
            build_run_plan(
                triggering_task_case_id="trigger",
                regression_task_case_id="regression",
                replay_count=4,
                max_runs=13,
            )


class GateClassificationTests(unittest.TestCase):
    """Cover every non-score gate outcome and its hard constraints."""

    def test_gate_has_all_five_classifications(self) -> None:
        cases = {
            "improved": {
                "dimensions": {
                    "correctness": "unchanged",
                    "capability_coverage": "unchanged",
                    "token_use": "improved",
                },
                "runnable": True,
                "complete": True,
            },
            "regressed": {
                "dimensions": {
                    "correctness": "regressed",
                    "token_use": "improved",
                },
                "runnable": True,
                "complete": True,
            },
            "mixed": {
                "dimensions": {
                    "correctness": "unchanged",
                    "token_use": "improved",
                    "duration": "regressed",
                },
                "runnable": True,
                "complete": True,
            },
            "inconclusive": {
                "dimensions": {"correctness": "unchanged"},
                "runnable": True,
                "complete": False,
            },
            "not_runnable": {
                "dimensions": {},
                "runnable": False,
                "complete": False,
            },
        }
        actual = {
            expected: classify_dimensions(**arguments)
            for expected, arguments in cases.items()
        }
        self.assertEqual(actual, {name: name for name in cases})

    def test_protected_regression_overrides_resource_improvement(self) -> None:
        classification = classify_dimensions(
            {
                "correctness": "unchanged",
                "token_use": "improved",
                "accessibility": "regressed",
            },
            runnable=True,
            complete=True,
            protected_dimensions=["accessibility"],
        )
        self.assertEqual(classification, "regressed")

    def test_replay_judge_must_not_reuse_proposer_agent_run(self) -> None:
        effect = {
            "schema": "test.effect.v1",
            "comparison_id": "comparison-1",
            "candidate_id": "candidate-1",
            "judge_agent_run_id": "agent-run-proposer",
            "runnable": True,
            "complete": True,
            "dimensions": {"correctness": "unchanged"},
            "protected_dimensions": [],
            "classification": "inconclusive",
        }
        with self.assertRaisesRegex(
            ComparisonError,
            "different AgentRuns",
        ):
            validate_test_effect(
                effect,
                proposer_agent_run_id="agent-run-proposer",
            )


class ComparisonRepositoryTests(unittest.TestCase):
    """Exercise fail-closed execution and persisted Judge handoff."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ComparisonRepository(
            Path(self.temporary.name) / "comparisons"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create(self) -> dict[str, object]:
        return self.repository.create(
            candidate_id="candidate-1",
            baseline_skill_version="skill-v1",
            triggering_task_case_id="trigger",
            regression_task_case_id="regression",
            proposer_agent_run_id="agent-run-proposer",
        )

    def test_unavailable_sandbox_waits_without_calling_host_callbacks(
        self,
    ) -> None:
        comparison = self._create()
        callback_calls: list[str] = []

        result = self.repository.execute(
            comparison["id"],
            sandbox=FakeSandbox(available=False),
            run_attempt=lambda *_: callback_calls.append("run"),
            run_harness=lambda *_: callback_calls.append("harness"),
        )

        self.assertEqual(result["status"], "awaiting_sandbox")
        self.assertEqual(result["attempts"], [])
        self.assertEqual(callback_calls, [])
        self.assertFalse(result["sandbox"]["available"])

    def test_available_sandbox_rejects_unattested_host_callback(self) -> None:
        comparison = self._create()

        with self.assertRaisesRegex(
            ComparisonError,
            "not bound",
        ):
            self.repository.execute(
                comparison["id"],
                sandbox=FakeSandbox(available=True),
                run_attempt=lambda *_: {
                    "status": "succeeded",
                    "run_id": "unsafe",
                },
                run_harness=lambda *_: {},
            )
        self.assertEqual(
            self.repository.repository.load(comparison["id"])["status"],
            "planned",
        )

    def test_sandbox_start_failure_is_visible_and_retry_is_a_new_attempt(
        self,
    ) -> None:
        comparison = self._create()
        sandbox = FlakySandbox()
        runner = AttestedRunner(
            lambda planned, *_: {
                "status": "succeeded",
                "run_id": f"run-{planned['attempt_index']}",
            }
        )

        first = self.repository.execute(
            comparison["id"],
            sandbox=sandbox,
            run_attempt=runner,
            run_harness=lambda attempts, _: {
                "run_ids": [item.get("run_id") for item in attempts]
            },
        )
        self.assertEqual(first["status"], "awaiting_sandbox")
        self.assertEqual(first["attempts"][0]["status"], "sandbox_failed")
        self.assertEqual(
            first["attempts"][0]["attempt_path"],
            "attempts/01-attempt-01",
        )

        retried = self.repository.execute(
            comparison["id"],
            sandbox=sandbox,
            run_attempt=runner,
            run_harness=lambda attempts, _: {
                "run_ids": [item.get("run_id") for item in attempts]
            },
        )
        self.assertEqual(retried["status"], "awaiting_judge")
        self.assertEqual(len(retried["attempts"]), 14)
        self.assertEqual(
            retried["attempts"][1]["attempt_path"],
            "attempts/01-attempt-02",
        )

    def test_successful_comparison_runs_thirteen_isolated_attempts_and_judge(
        self,
    ) -> None:
        comparison = self._create()
        sandbox = FakeSandbox(available=True)
        run_ids: list[str] = []
        harness_attempt_counts: list[int] = []

        def run_attempt(planned, sandbox_context, run_directory):
            self.assertEqual(sandbox_context["network"], "none")
            self.assertFalse(
                sandbox_context["credentials_in_container"]
            )
            run_id = f"run-{planned['attempt_index']}"
            run_ids.append(run_id)
            return {"status": "succeeded", "run_id": run_id}

        def run_harness(attempts, comparison_directory):
            self.assertEqual(
                comparison_directory,
                self.repository.repository.object_directory(
                    comparison["id"]
                ),
            )
            harness_attempt_counts.append(len(attempts))
            return {
                "trajectory_profile_id": f"profile-{len(attempts)}",
                "artifact_comparison_id": f"artifact-{len(attempts)}",
            }

        executed = self.repository.execute(
            comparison["id"],
            sandbox=sandbox,
            run_attempt=AttestedRunner(run_attempt),
            run_harness=run_harness,
        )

        self.assertEqual(executed["status"], "awaiting_judge")
        self.assertEqual(len(executed["attempts"]), 13)
        self.assertEqual(len(executed["harness_runs"]), 2)
        self.assertEqual(harness_attempt_counts, [1, 13])
        self.assertEqual(executed["harness_runs"][0]["scope"], "smoke")
        self.assertEqual(executed["harness_runs"][1]["scope"], "full")
        self.assertEqual(len(sandbox.entered_directories), 13)
        self.assertEqual(run_ids, [f"run-{index}" for index in range(1, 14)])

        effect = self.repository.record_effect(
            comparison["id"],
            {
                "schema": "test.effect.v1",
                "comparison_id": comparison["id"],
                "candidate_id": "candidate-1",
                "judge_agent_run_id": "agent-run-judge",
                "runnable": True,
                "complete": True,
                "dimensions": {
                    "correctness": "unchanged",
                    "capability_coverage": "unchanged",
                    "token_use": "improved",
                },
                "protected_dimensions": [],
                "classification": "improved",
                "regressions": [],
                "uncertainties": [],
                "evidence": [
                    {
                        "schema": "evidence.ref.v1",
                        "run_id": "run-2",
                        "seq": 4,
                    }
                ],
            },
        )
        self.assertEqual(effect["status"], "completed")
        self.assertEqual(effect["gate_classification"], "improved")
        self.assertEqual(
            effect["test_effect"]["judge_agent_run_id"],
            "agent-run-judge",
        )
        self.assertEqual(
            effect["test_effect"]["proposer_agent_run_id"],
            "agent-run-proposer",
        )
        self.assertEqual(
            effect["review_status"],
            "awaiting_human_review",
        )
        self.assertEqual(
            effect["judge_attempts"][0]["agent_run_id"],
            "agent-run-judge",
        )

    def test_replay_judge_requires_canonical_evidence(self) -> None:
        comparison = self._create()
        effect = {
            "schema": "test.effect.v1",
            "comparison_id": comparison["id"],
            "candidate_id": "candidate-1",
            "judge_agent_run_id": "agent-run-judge",
            "runnable": True,
            "complete": False,
            "dimensions": {"correctness": "inconclusive"},
            "protected_dimensions": [],
            "classification": "inconclusive",
            "regressions": [],
            "uncertainties": [],
            "evidence": [],
        }

        with self.assertRaisesRegex(
            ComparisonError,
            "must cite",
        ):
            validate_test_effect(
                effect,
                proposer_agent_run_id="agent-run-proposer",
            )

    def test_failed_judge_attempt_remains_visible_for_retry(self) -> None:
        comparison = self._create()
        self.repository.repository.update(
            str(comparison["id"]),
            {"status": "awaiting_judge"},
            expected_status="planned",
        )

        updated = self.repository.record_judge_failure(
            str(comparison["id"]),
            agent_run_id="agent-run-invalid",
            status="invalid_output",
            error={"type": "ValueError", "message": "invalid JSON"},
        )

        self.assertEqual(updated["status"], "awaiting_judge")
        self.assertEqual(
            updated["judge_attempts"][0]["status"],
            "invalid_output",
        )

    def test_failed_smoke_is_profiled_before_stopping(self) -> None:
        comparison = self._create()
        harnessed: list[str] = []

        result = self.repository.execute(
            comparison["id"],
            sandbox=FakeSandbox(available=True),
            run_attempt=AttestedRunner(
                lambda *_: {
                    "status": "failed",
                    "run_id": "failed-smoke",
                }
            ),
            run_harness=lambda attempts, _: (
                harnessed.extend(
                    str(item.get("run_id")) for item in attempts
                )
                or {"run_ids": harnessed, "status": "partial"}
            ),
        )

        self.assertEqual(result["status"], "not_runnable")
        self.assertEqual(harnessed, ["failed-smoke"])
        self.assertEqual(
            self.repository.repository.load(comparison["id"])[
                "harness_runs"
            ][0]["run_ids"],
            ["failed-smoke"],
        )


class DockerPreflightTests(unittest.TestCase):
    """Ensure any Docker uncertainty produces an unavailable result."""

    @patch("skill_evolution.comparison.subprocess.run")
    def test_image_inspection_exception_fails_closed(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout='"27.0.0"',
                stderr="",
            ),
            OSError("socket disappeared"),
        ]
        sandbox = DockerSandbox(docker_command="/usr/bin/docker")

        result = sandbox.preflight()

        self.assertFalse(result.available)
        self.assertEqual(result.backend, "docker_tool_router")
        self.assertIn("image preflight failed", result.detail.lower())

    @patch("skill_evolution.comparison.subprocess.run")
    def test_container_mounts_only_attempt_artifacts_as_workspace(
        self,
        run,
    ) -> None:
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout="container-1\n",
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        sandbox = DockerSandbox(docker_command="/usr/bin/docker")
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            attempt.mkdir()
            with patch.object(
                sandbox,
                "preflight",
                return_value=SandboxPreflightResult(
                    available=True,
                    backend="docker_tool_router",
                    detail="ready",
                ),
            ):
                with sandbox.isolated_run(attempt) as context:
                    self.assertEqual(
                        Path(context["host_workspace"]),
                        (attempt / "artifacts").resolve(),
                    )
                    self.assertTrue((attempt / "artifacts").is_dir())

            start_command = run.call_args_list[0].args[0]
            mount_index = start_command.index("--mount") + 1
            self.assertIn(
                (
                    f"src={(attempt / 'artifacts').resolve()},"
                    "dst=/workspace,rw"
                ),
                start_command[mount_index],
            )


if __name__ == "__main__":
    unittest.main()
