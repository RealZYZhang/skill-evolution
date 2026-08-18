"""Fake integration tests for specialist-only and legacy synthesis runs."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AgentOrchestrationError,
    AgentRole,
    AgentRunResult,
    AgentSpec,
    LEGACY_SPECIALIST_ROLES,
    MultiPiOrchestrator,
    SPECIALIST_ROLES,
    default_agent_specs,
)


def agent_result(
    role: AgentRole,
    *,
    missing_roles: list[str] | None = None,
) -> dict[str, object]:
    """Build a minimal schema-valid structured analysis result."""

    return {
        "schema": "analysis.agent_result.v1",
        "role": role.value,
        "findings": [],
        "evidence_requests": [],
        "optimization_hypotheses": [],
        "missing_roles": missing_roles or [],
        "limitations": [],
    }


def specs(root: Path) -> dict[AgentRole, AgentSpec]:
    """Create all fixed-role specs used by orchestration tests."""

    root.mkdir(parents=True)
    result: dict[AgentRole, AgentSpec] = {}
    for role in AgentRole:
        path = root / f"{role.value}.md"
        path.write_text(f"Run {role.value}.", encoding="utf-8")
        result[role] = AgentSpec(
            role=role,
            prompt_path=path,
            tool_mode=(
                "candidate"
                if role is AgentRole.CANDIDATE_PROPOSER
                else "read_only"
            ),
        )
    return result


class FakeAgentRuntime:
    """Persist fake per-process artifacts while returning controlled outcomes."""

    def __init__(
        self,
        root: Path,
        *,
        failed_role: AgentRole | None = None,
        raised_role: AgentRole | None = None,
        synthesis_reports_missing: bool = True,
    ) -> None:
        self.root = root
        self.failed_role = failed_role
        self.raised_role = raised_role
        self.synthesis_reports_missing = synthesis_reports_missing
        self.calls: list[dict[str, object]] = []
        self.preflight_calls: list[list[AgentRole]] = []
        self._lock = threading.Lock()

    def preflight(self, agent_specs) -> None:
        """Record exactly which prompts a workflow requires."""

        self.preflight_calls.append([spec.role for spec in agent_specs])

    def run(
        self,
        *,
        spec: AgentSpec,
        campaign_id: str,
        round_number: int,
        context,
        evidence_bundle: Path,
        candidate_workspace: Path | None = None,
    ) -> AgentRunResult:
        with self._lock:
            index = len(self.calls) + 1
            run_id = f"agent-run-{index}-{spec.role.value}"
            run_directory = self.root / run_id
            run_directory.mkdir()
            self.calls.append(
                {
                    "role": spec.role,
                    "campaign_id": campaign_id,
                    "round_number": round_number,
                    "context": dict(context),
                    "evidence_bundle": evidence_bundle,
                    "candidate_workspace": candidate_workspace,
                    "run_directory": run_directory,
                }
            )
        trajectory = run_directory / "trajectory.jsonl"
        session = run_directory / "pi-session.jsonl"
        trajectory.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "type": "trajectory_started",
                    "role": spec.role.value,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session.write_text(
            json.dumps(
                {
                    "type": "session",
                    "id": run_id,
                    "role": spec.role.value,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        if spec.role is self.raised_role:
            raise RuntimeError("controlled orchestration exception")
        if spec.role is self.failed_role:
            error = {"type": "FakeFailure", "message": "controlled failure"}
            (run_directory / "error.json").write_text(
                json.dumps(error),
                encoding="utf-8",
            )
            return AgentRunResult(
                agent_run_id=run_id,
                role=spec.role,
                status="failed",
                result=None,
                error=error,
                run_directory=run_directory,
            )
        missing: list[str] = []
        if spec.role is AgentRole.SYNTHESIS:
            requested = list(context.get("missing_roles", []))
            if self.synthesis_reports_missing:
                missing = requested
        result = agent_result(spec.role, missing_roles=missing)
        (run_directory / "result.json").write_text(
            json.dumps(result),
            encoding="utf-8",
        )
        return AgentRunResult(
            agent_run_id=run_id,
            role=spec.role,
            status="succeeded",
            result=result,
            error=None,
            run_directory=run_directory,
        )


class CapabilityAgentRuntime(FakeAgentRuntime):
    """Expose one controlled capability identity for proxy tests."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.capability_specs: list[AgentSpec] = []

    def research_capability_identity(
        self,
        spec: AgentSpec,
    ) -> dict[str, object]:
        self.capability_specs.append(spec)
        return {
            "schema": "research.capability_identity.v1",
            "role": spec.role.value,
        }


class MultiPiOrchestratorTests(unittest.TestCase):
    """Verify active isolation, failure visibility, and legacy ordering."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        (self.evidence / "manifest.json").write_text(
            '{"schema":"evidence.bundle.v1"}\n',
            encoding="utf-8",
        )
        self.agent_specs = specs(self.root / "prompts")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_round_uses_distinct_runs_sessions_and_trajectories(
        self,
    ) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        specialists, synthesis = orchestrator.run_analysis_round(
            campaign_id="analysis-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={"frozen_batch": "replay-1"},
        )

        self.assertEqual(
            [run.role for run in specialists],
            list(LEGACY_SPECIALIST_ROLES),
        )
        self.assertEqual(synthesis.role, AgentRole.SYNTHESIS)
        self.assertEqual(len({run.agent_run_id for run in specialists}), 3)
        directories = [run.run_directory for run in specialists]
        self.assertEqual(len(set(directories)), 3)
        session_paths = [
            directory / "pi-session.jsonl" for directory in directories
        ]
        trajectory_paths = [
            directory / "trajectory.jsonl" for directory in directories
        ]
        self.assertEqual(len(set(session_paths)), 3)
        self.assertEqual(len(set(trajectory_paths)), 3)
        self.assertTrue(all(path.is_file() for path in session_paths))
        self.assertTrue(all(path.is_file() for path in trajectory_paths))
        self.assertEqual(
            [call["role"] for call in runtime.calls],
            [*LEGACY_SPECIALIST_ROLES, AgentRole.SYNTHESIS],
        )

    def test_one_specialist_failure_is_saved_and_synthesis_names_missing_role(
        self,
    ) -> None:
        failed_role = AgentRole.CAPABILITY_COVERAGE
        runtime = FakeAgentRuntime(
            self.root / "agent-runs",
            failed_role=failed_role,
        )
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        specialists, synthesis = orchestrator.run_analysis_round(
            campaign_id="analysis-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={},
        )

        by_role = {run.role: run for run in specialists}
        self.assertEqual(by_role[failed_role].status, "failed")
        self.assertTrue(
            (by_role[failed_role].run_directory / "error.json").is_file()
        )
        for role in set(LEGACY_SPECIALIST_ROLES) - {failed_role}:
            self.assertEqual(by_role[role].status, "succeeded")
            self.assertTrue(
                (by_role[role].run_directory / "result.json").is_file()
            )
        self.assertEqual(synthesis.status, "succeeded")
        self.assertEqual(
            synthesis.result["missing_roles"],
            [failed_role.value],
        )
        synthesis_call = runtime.calls[-1]
        self.assertEqual(
            synthesis_call["context"]["missing_roles"],
            [failed_role.value],
        )
        specialist_reports = synthesis_call["context"][
            "specialist_reports"
        ]
        self.assertEqual(len(specialist_reports), 3)
        self.assertEqual(
            {
                report["role"]
                for report in specialist_reports
                if report["status"] == "failed"
            },
            {failed_role.value},
        )

    def test_active_specialists_run_without_synthesis(self) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs={
                role: self.agent_specs[role]
                for role in ACTIVE_SPECIALIST_ROLES
            },
        )

        outcomes = orchestrator.run_specialists_only(
            campaign_id="research-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={"corpus_digest": "a" * 64},
        )

        self.assertEqual(SPECIALIST_ROLES, ACTIVE_SPECIALIST_ROLES)
        self.assertEqual(
            [outcome.role for outcome in outcomes],
            list(ACTIVE_SPECIALIST_ROLES),
        )
        self.assertTrue(all(outcome.status == "succeeded" for outcome in outcomes))
        self.assertEqual(
            [call["role"] for call in runtime.calls],
            list(ACTIVE_SPECIALIST_ROLES),
        )
        self.assertNotIn(
            AgentRole.SYNTHESIS,
            [call["role"] for call in runtime.calls],
        )
        self.assertEqual(
            runtime.preflight_calls,
            [list(ACTIVE_SPECIALIST_ROLES)],
        )
        self.assertEqual(
            len(
                {
                    outcome.run.run_directory
                    for outcome in outcomes
                    if outcome.run is not None
                }
            ),
            4,
        )

    def test_active_specialists_preserve_one_runtime_exception(self) -> None:
        raised_role = AgentRole.CONDITIONS_COVERAGE
        runtime = FakeAgentRuntime(
            self.root / "agent-runs",
            raised_role=raised_role,
        )
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        outcomes = orchestrator.run_specialists_only(
            campaign_id="research-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={},
        )

        by_role = {outcome.role: outcome for outcome in outcomes}
        self.assertEqual(by_role[raised_role].status, "failed")
        self.assertIsNone(by_role[raised_role].run)
        self.assertEqual(
            by_role[raised_role].exception["type"],
            "RuntimeError",
        )
        for role in set(ACTIVE_SPECIALIST_ROLES) - {raised_role}:
            self.assertEqual(by_role[role].status, "succeeded")
            self.assertIsNotNone(by_role[role].run)
        self.assertNotIn(
            AgentRole.SYNTHESIS,
            [call["role"] for call in runtime.calls],
        )

    def test_parallel_specialists_are_blocked_until_certified(self) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        runtime.root.mkdir()

        with self.assertRaisesRegex(ValueError, "not certified"):
            MultiPiOrchestrator(
                runtime=runtime,
                specs=self.agent_specs,
                max_parallel_agents=2,
            )

    def test_capability_identity_proxies_the_behavior_spec(self) -> None:
        runtime = CapabilityAgentRuntime(self.root / "agent-runs")
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        identity = orchestrator.research_capability_identity()

        self.assertEqual(
            identity,
            {
                "schema": "research.capability_identity.v1",
                "role": AgentRole.BEHAVIOR_PATTERN.value,
            },
        )
        self.assertEqual(
            [spec.role for spec in runtime.capability_specs],
            [AgentRole.BEHAVIOR_PATTERN],
        )

    def test_capability_identity_fails_closed_without_runtime_support(
        self,
    ) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        with self.assertRaisesRegex(
            AgentOrchestrationError,
            "cannot attest",
        ):
            orchestrator.research_capability_identity()

    def test_active_specs_bind_research_lab_and_submission_tool(self) -> None:
        configured = default_agent_specs(self.root / "production-prompts")
        expected_files = {
            AgentRole.BEHAVIOR_PATTERN: "behavior-pattern-research-v1.md",
            AgentRole.CONDITIONS_COVERAGE: (
                "conditions-coverage-research-v1.md"
            ),
            AgentRole.OUTCOME_CONSISTENCY: (
                "outcome-consistency-research-v1.md"
            ),
            AgentRole.RESOURCE_EFFICIENCY: (
                "resource-efficiency-research-v1.md"
            ),
        }

        for role, filename in expected_files.items():
            with self.subTest(role=role):
                spec = configured[role]
                self.assertEqual(spec.prompt_path.name, filename)
                self.assertEqual(
                    spec.research_lab_profile,
                    "multi_trajectory_research",
                )
                self.assertEqual(
                    spec.submission_tool,
                    "submit_multi_trajectory_research",
                )

        self.assertIsNone(
            configured[AgentRole.SYNTHESIS].research_lab_profile
        )
        self.assertIsNone(configured[AgentRole.SYNTHESIS].submission_tool)

    def test_synthesis_cannot_hide_a_failed_specialist(self) -> None:
        runtime = FakeAgentRuntime(
            self.root / "agent-runs",
            failed_role=AgentRole.RESOURCE_EFFICIENCY,
            synthesis_reports_missing=False,
        )
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        with self.assertRaisesRegex(
            AgentOrchestrationError,
            "omitted",
        ):
            orchestrator.run_analysis_round(
                campaign_id="analysis-1",
                round_number=1,
                evidence_bundle=self.evidence,
                context={},
            )

    def test_proposer_and_judge_use_separate_role_runs(self) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )
        candidate_workspace = self.root / "candidate"
        candidate_workspace.mkdir()

        proposer = orchestrator.run_candidate_proposer(
            campaign_id="analysis-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={"hypothesis_id": "hypothesis-1"},
            candidate_workspace=candidate_workspace,
        )
        judge = orchestrator.run_replay_judge(
            campaign_id="comparison-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={"proposer_agent_run_id": proposer.agent_run_id},
        )

        self.assertEqual(proposer.role, AgentRole.CANDIDATE_PROPOSER)
        self.assertEqual(judge.role, AgentRole.REPLAY_JUDGE)
        self.assertNotEqual(proposer.agent_run_id, judge.agent_run_id)
        self.assertEqual(
            runtime.calls[-2]["candidate_workspace"],
            candidate_workspace,
        )
        self.assertIsNone(runtime.calls[-1]["candidate_workspace"])

    def test_single_specialist_smoke_does_not_run_other_roles(self) -> None:
        runtime = FakeAgentRuntime(self.root / "agent-runs")
        runtime.root.mkdir()
        orchestrator = MultiPiOrchestrator(
            runtime=runtime,
            specs=self.agent_specs,
        )

        result = orchestrator.run_specialist(
            role=AgentRole.RESOURCE_EFFICIENCY,
            campaign_id="analysis-1",
            round_number=1,
            evidence_bundle=self.evidence,
            context={"smoke": True},
        )

        self.assertEqual(result.role, AgentRole.RESOURCE_EFFICIENCY)
        self.assertEqual(
            [call["role"] for call in runtime.calls],
            [AgentRole.RESOURCE_EFFICIENCY],
        )

        with self.assertRaisesRegex(
            AgentOrchestrationError,
            "not a specialist",
        ):
            orchestrator.run_specialist(
                role=AgentRole.SYNTHESIS,
                campaign_id="analysis-1",
                round_number=1,
                evidence_bundle=self.evidence,
                context={},
            )


if __name__ == "__main__":
    unittest.main()
