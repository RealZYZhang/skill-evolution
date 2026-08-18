"""Concrete fail-closed Pi runner for automatic candidate comparisons."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
from typing import Any

from scripts.prompt_approval import (
    ApprovedPrompt,
    load_approved_prompt,
    render_execution_prompt,
)
from scripts.task_case import TaskCase
from scripts.trajectory_spike import (
    TrajectoryExecutionPolicy,
    run_trajectory_spike,
)
from skill_evolution.agents import ModelConfiguration
from skill_evolution.analysis import load_approved_skill_contract
from skill_evolution.candidates import SkillVersion
from skill_evolution.comparison import ComparisonError
from skill_evolution.storage import JsonObject


class SandboxedPiReplayRunner:
    """Run approved baseline/candidate tasks through the Docker tool router."""

    sandbox_backend = "docker_tool_router"
    built_in_tools = False
    host_fallback_allowed = False

    def __init__(
        self,
        *,
        baseline_skill: SkillVersion,
        candidate_skill: SkillVersion,
        task_cases: Mapping[str, TaskCase],
        execution_prompt_path: str | os.PathLike[str],
        docker_extension_path: str | os.PathLike[str],
        model: ModelConfiguration | None = None,
        pi_command: Sequence[str] | str | None = None,
        timeout: float = 900.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.baseline_skill = baseline_skill
        self.candidate_skill = candidate_skill
        baseline_contract = load_approved_skill_contract(
            baseline_skill.content_path / "skill_contract.json"
        )
        candidate_contract = load_approved_skill_contract(
            candidate_skill.content_path / "skill_contract.json"
        )
        if candidate_contract != baseline_contract:
            raise ComparisonError(
                "Candidate Skill must preserve the approved skill_contract.json"
            )
        self.skill_contract = baseline_contract
        self.task_cases = dict(task_cases)
        if not self.task_cases:
            raise ValueError("At least one TaskCase is required")
        for task_case_id, task_case in self.task_cases.items():
            if task_case_id != task_case.task_case_id:
                raise ValueError(
                    "TaskCase mapping key must match task_case_id"
                )
        self.prompt: ApprovedPrompt = load_approved_prompt(
            execution_prompt_path
        )
        self.docker_extension_path = Path(
            docker_extension_path
        ).resolve()
        if not self.docker_extension_path.is_file():
            raise FileNotFoundError(
                "Docker tool-router extension not found: "
                f"{self.docker_extension_path}"
            )
        self.model = model or ModelConfiguration.from_project_configuration()
        if self.model.thinking != "off":
            raise ValueError(
                "Automatic replay currently supports thinking='off' only"
            )
        self.pi_command = pi_command
        self.timeout = timeout

    def __call__(
        self,
        planned: Mapping[str, Any],
        sandbox_context: Mapping[str, Any],
        attempt_directory: Path,
    ) -> JsonObject:
        """Run one planned attempt after validating the sandbox attestation."""

        tool_environment = self._validate_sandbox(
            sandbox_context,
            attempt_directory,
        )
        variant = planned.get("variant")
        if variant == "baseline":
            skill = self.baseline_skill
        elif variant == "candidate":
            skill = self.candidate_skill
        else:
            raise ComparisonError(
                f"Unsupported comparison variant: {variant!r}"
            )
        task_case_id = planned.get("task_case_id")
        if not isinstance(task_case_id, str):
            raise ComparisonError("Planned attempt lacks task_case_id")
        try:
            task_case = self.task_cases[task_case_id]
        except KeyError as error:
            raise ComparisonError(
                f"Unknown comparison TaskCase: {task_case_id}"
            ) from error

        rendered = render_execution_prompt(
            self.prompt,
            skill.content_path,
            task_case.prompt_payload(),
        )
        policy = TrajectoryExecutionPolicy(
            mode="docker_tool_router",
            extension_path=self.docker_extension_path,
            environment=tool_environment,
            exact_run_directory=attempt_directory,
        )
        result = run_trajectory_spike(
            skill_path=skill.content_path,
            task_case=task_case,
            prompt=rendered.text,
            timeout=self.timeout,
            pi_command=self.pi_command,
            extra_pi_args=(
                "--provider",
                self.model.provider,
                "--model",
                self.model.model,
            ),
            execution_policy=policy,
        )
        outcome = result.outcome
        return {
            "status": outcome["status"],
            "run_id": result.run_directory.name,
            "trajectory": "trajectory.jsonl",
            "session": "pi-session.jsonl",
            "session_status": outcome["session"]["status"],
            "artifacts": outcome.get("artifacts", []),
            "failure_stage": outcome.get("failure_stage"),
            "error": outcome.get("error"),
            "skill_version": skill.version,
            "task_case_id": task_case.task_case_id,
            "model": self.model.to_dict(),
            "execution_attestation": {
                "backend": self.sandbox_backend,
                "built_in_tools": False,
                "host_fallback_allowed": False,
                "network": "none",
                "credentials_in_container": False,
                "extension": self.docker_extension_path.name,
            },
        }

    def _validate_sandbox(
        self,
        context: Mapping[str, Any],
        attempt_directory: Path,
    ) -> dict[str, str]:
        if context.get("backend") != self.sandbox_backend:
            raise ComparisonError("Unexpected comparison sandbox backend")
        if context.get("network") != "none":
            raise ComparisonError("Automatic replay sandbox must have no network")
        if context.get("credentials_in_container") is not False:
            raise ComparisonError(
                "Automatic replay cannot expose model credentials"
            )
        expected_workspace = (attempt_directory / "artifacts").resolve()
        host_workspace = context.get("host_workspace")
        if (
            not isinstance(host_workspace, str)
            or Path(host_workspace).resolve() != expected_workspace
        ):
            raise ComparisonError(
                "Sandbox workspace does not match the attempt artifact root"
            )
        environment = context.get("tool_environment")
        if not isinstance(environment, Mapping):
            raise ComparisonError("Sandbox tool environment is missing")
        normalized: dict[str, str] = {}
        for key in (
            "SKILL_EVOLUTION_DOCKER_CONTAINER",
            "SKILL_EVOLUTION_DOCKER_COMMAND",
        ):
            value = environment.get(key)
            if not isinstance(value, str) or not value:
                raise ComparisonError(
                    f"Sandbox tool environment is missing {key}"
                )
            normalized[key] = value
        return normalized
