"""Tests for the concrete fail-closed candidate Pi replay adapter."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.prompt_approval import approve_prompt
from scripts.task_case import TaskCase
from scripts.trajectory_spike import TrajectoryResult
from skill_evolution.candidates import SkillVersion
from skill_evolution.comparison import ComparisonError
from skill_evolution.sandbox_replay import SandboxedPiReplayRunner
from tests.test_trajectory_spike import write_approved_skill_contract


class SandboxedPiReplayRunnerTests(unittest.TestCase):
    """Only a validated Docker context may reach Pi trajectory capture."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.baseline = self._skill("baseline", "# Baseline\n")
        self.candidate = self._skill("candidate", "# Candidate\n")
        source = self.root / "source.md"
        source.write_text("# Source\n", encoding="utf-8")
        self.task_case = TaskCase.for_file(
            source,
            task_case_id="task-1",
        )
        self.prompt = self.root / "execution-v2.md"
        self.prompt.write_text(
            "Skill:\n{{SKILL_CONTENT}}\nTask:\n{{TASK_CASE}}\n",
            encoding="utf-8",
        )
        self.prompt.with_name(
            self.prompt.name + ".approval.json"
        ).write_text(
            json.dumps(
                {
                    "schema": "prompt.approval.v1",
                    "status": "proposed",
                    "prompt_id": "execution.test",
                    "version": "2",
                    "prompt_file": self.prompt.name,
                    "content_sha256": None,
                    "approved_by": None,
                    "approved_at": None,
                }
            ),
            encoding="utf-8",
        )
        approve_prompt(self.prompt, approved_by="test-owner")
        self.extension = self.root / "docker-tool-router.ts"
        self.extension.write_text("export default () => {};\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _skill(self, name: str, content: str) -> SkillVersion:
        directory = self.root / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
        write_approved_skill_contract(directory, skill_id="skill")
        return SkillVersion(
            skill_id="skill",
            version=name,
            content_path=directory,
        )

    def _runner(self) -> SandboxedPiReplayRunner:
        return SandboxedPiReplayRunner(
            baseline_skill=self.baseline,
            candidate_skill=self.candidate,
            task_cases={"task-1": self.task_case},
            execution_prompt_path=self.prompt,
            docker_extension_path=self.extension,
        )

    def _sandbox_context(self, attempt: Path) -> dict[str, object]:
        return {
            "backend": "docker_tool_router",
            "network": "none",
            "credentials_in_container": False,
            "host_workspace": str(attempt / "artifacts"),
            "tool_environment": {
                "SKILL_EVOLUTION_DOCKER_CONTAINER": "container-1",
                "SKILL_EVOLUTION_DOCKER_COMMAND": "/usr/bin/docker",
            },
        }

    @patch("skill_evolution.sandbox_replay.run_trajectory_spike")
    def test_candidate_attempt_uses_exact_docker_policy(
        self,
        run_trajectory,
    ) -> None:
        attempt = self.root / "attempt-1"
        (attempt / "artifacts").mkdir(parents=True)
        run_trajectory.return_value = TrajectoryResult(
            run_directory=attempt,
            outcome={
                "status": "succeeded",
                "failure_stage": None,
                "error": None,
                "session": {"status": "complete"},
                "artifacts": [{"path": "artifacts/output.html"}],
            },
        )

        result = self._runner()(
            {"variant": "candidate", "task_case_id": "task-1"},
            self._sandbox_context(attempt),
            attempt,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["skill_version"], "candidate")
        call = run_trajectory.call_args.kwargs
        self.assertEqual(call["skill_path"], self.candidate.content_path)
        self.assertEqual(
            call["execution_policy"].mode,
            "docker_tool_router",
        )
        self.assertEqual(
            call["execution_policy"].exact_run_directory,
            attempt,
        )
        self.assertEqual(
            result["execution_attestation"]["host_fallback_allowed"],
            False,
        )

    @patch("skill_evolution.sandbox_replay.run_trajectory_spike")
    def test_invalid_sandbox_context_never_reaches_pi(
        self,
        run_trajectory,
    ) -> None:
        attempt = self.root / "attempt-2"
        (attempt / "artifacts").mkdir(parents=True)
        context = self._sandbox_context(attempt)
        context["network"] = "bridge"

        with self.assertRaisesRegex(ComparisonError, "no network"):
            self._runner()(
                {"variant": "baseline", "task_case_id": "task-1"},
                context,
                attempt,
            )
        run_trajectory.assert_not_called()

    def test_candidate_cannot_change_the_approved_skill_contract(self) -> None:
        path = self.candidate.content_path / "skill_contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["runtime"]["network"] = "allowed"
        path.write_text(json.dumps(contract), encoding="utf-8")

        with self.assertRaisesRegex(
            ComparisonError,
            "preserve the approved skill_contract.json",
        ):
            self._runner()


if __name__ == "__main__":
    unittest.main()
