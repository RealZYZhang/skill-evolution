"""Fail-closed candidate comparison planning and effect classification."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterator, Protocol

from skill_evolution.evidence import EvidenceError, EvidenceRef
from skill_evolution.storage import (
    JsonObject,
    ManifestRepository,
    StorageError,
    new_object_id,
    utc_now,
)


COMPARISON_EXPERIMENT_SCHEMA = "comparison.experiment.v1"
TEST_EFFECT_SCHEMA = "test.effect.v1"
GATE_CLASSIFICATIONS = {
    "improved",
    "regressed",
    "mixed",
    "inconclusive",
    "not_runnable",
}
DIMENSION_STATES = {
    "improved",
    "regressed",
    "unchanged",
    "inconclusive",
}
HARD_DIMENSIONS = {"correctness", "capability_coverage"}


class ComparisonError(ValueError):
    """Raised when a candidate experiment violates its approved boundary."""


@dataclass(frozen=True)
class SandboxPreflightResult:
    """Result of checking the mandatory isolated replay backend."""

    available: bool
    backend: str
    detail: str


class SandboxBackend(Protocol):
    """Boundary implemented by a disposable, network-disabled replay sandbox."""

    name: str

    def preflight(self) -> SandboxPreflightResult:
        """Check whether new isolated runs can be created."""

    def isolated_run(
        self,
        run_directory: str | os.PathLike[str],
    ) -> Any:
        """Return a context manager for one disposable run container."""


class DockerSandbox:
    """Check the Docker backend used by the trusted Pi tool router."""

    name = "docker_tool_router"

    def __init__(
        self,
        *,
        docker_command: str | None = None,
        image: str = "python:3.11-slim",
        timeout: float = 10.0,
    ) -> None:
        self.docker_command = docker_command or shutil.which("docker")
        self.image = image
        self.timeout = timeout

    def preflight(self) -> SandboxPreflightResult:
        """Require both the Docker CLI and an accessible daemon."""

        if not self.docker_command:
            return SandboxPreflightResult(
                available=False,
                backend=self.name,
                detail="Docker CLI is not installed.",
            )
        try:
            result = subprocess.run(
                [
                    self.docker_command,
                    "info",
                    "--format",
                    "{{json .ServerVersion}}",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return SandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=f"Docker preflight failed: {error}",
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            return SandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=f"Docker daemon is unavailable: {detail}",
            )
        try:
            image_result = subprocess.run(
                [self.docker_command, "image", "inspect", self.image],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return SandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=f"Sandbox image preflight failed: {error}",
            )
        if image_result.returncode != 0:
            return SandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=(
                    f"Sandbox image {self.image!r} is not present locally; "
                    "the framework will not pull it implicitly."
                ),
            )
        return SandboxPreflightResult(
            available=True,
            backend=self.name,
            detail=(
                f"Docker server {result.stdout.strip()} and local image "
                f"{self.image!r} are available."
            ),
        )

    @contextmanager
    def isolated_run(
        self,
        run_directory: str | os.PathLike[str],
    ) -> Iterator[JsonObject]:
        """Start one network-disabled container and stop only that container."""

        preflight = self.preflight()
        if not preflight.available or not self.docker_command:
            raise ComparisonError(preflight.detail)
        attempt_directory = Path(run_directory).resolve()
        if not attempt_directory.is_dir():
            raise ComparisonError(
                "Sandbox run directory does not exist: "
                f"{attempt_directory}"
            )
        if attempt_directory == Path(attempt_directory.anchor):
            raise ComparisonError("A filesystem root cannot be sandbox-mounted")
        workspace = attempt_directory / "artifacts"
        workspace.mkdir(exist_ok=False)
        mount = f"type=bind,src={workspace},dst=/workspace,rw"
        command = [
            self.docker_command,
            "run",
            "--detach",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            self.image,
            "sh",
            "-c",
            "while :; do sleep 3600; done",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        container_id = result.stdout.strip()
        if result.returncode != 0 or not container_id:
            raise ComparisonError(
                "Could not start disposable sandbox: "
                f"{result.stderr.strip()}"
            )
        try:
            yield {
                "backend": self.name,
                "container_id": container_id,
                "tool_environment": {
                    "SKILL_EVOLUTION_DOCKER_CONTAINER": container_id,
                    "SKILL_EVOLUTION_DOCKER_COMMAND": self.docker_command,
                },
                "network": "none",
                "mounted_workspace": "/workspace",
                "host_workspace": str(workspace),
                "credentials_in_container": False,
            }
        finally:
            subprocess.run(
                [self.docker_command, "stop", "--time", "2", container_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )


def build_run_plan(
    *,
    triggering_task_case_id: str,
    regression_task_case_id: str,
    replay_count: int = 3,
    max_runs: int = 13,
) -> list[JsonObject]:
    """Build one smoke plus alternating paired baseline/candidate attempts."""

    if replay_count <= 0:
        raise ComparisonError("replay_count must be positive")
    if (
        not triggering_task_case_id
        or not regression_task_case_id
        or triggering_task_case_id == regression_task_case_id
    ):
        raise ComparisonError(
            "Triggering and regression TaskCases must be distinct"
        )
    plan: list[JsonObject] = [
        {
            "phase": "smoke",
            "variant": "candidate",
            "task_case_id": triggering_task_case_id,
            "repetition": 0,
        }
    ]
    for task_case_id in (
        triggering_task_case_id,
        regression_task_case_id,
    ):
        for repetition in range(1, replay_count + 1):
            variants = (
                ("baseline", "candidate")
                if repetition % 2 == 1
                else ("candidate", "baseline")
            )
            for variant in variants:
                plan.append(
                    {
                        "phase": "paired",
                        "variant": variant,
                        "task_case_id": task_case_id,
                        "repetition": repetition,
                    }
                )
    if len(plan) > max_runs:
        raise ComparisonError(
            f"Comparison needs {len(plan)} runs, above automatic limit "
            f"{max_runs}; create an expanded replay request."
        )
    for index, attempt in enumerate(plan, start=1):
        attempt["attempt_index"] = index
    return plan


def classify_dimensions(
    dimensions: Mapping[str, str],
    *,
    runnable: bool,
    complete: bool,
    protected_dimensions: Sequence[str] = (),
) -> str:
    """Apply hard constraints without collapsing measurements into a score."""

    if not runnable:
        return "not_runnable"
    if not complete:
        return "inconclusive"
    if not dimensions:
        return "inconclusive"
    for name, state in dimensions.items():
        if state not in DIMENSION_STATES:
            raise ComparisonError(
                f"Invalid state for dimension {name}: {state}"
            )
    if any(state == "inconclusive" for state in dimensions.values()):
        return "inconclusive"
    hard_or_protected = HARD_DIMENSIONS | set(protected_dimensions)
    if any(
        dimensions.get(name) == "regressed"
        for name in hard_or_protected
    ):
        return "regressed"
    states = set(dimensions.values())
    if "regressed" in states and "improved" in states:
        return "mixed"
    if "regressed" in states:
        return "regressed"
    if "improved" in states:
        return "improved"
    return "inconclusive"


def validate_test_effect(
    value: Mapping[str, Any],
    *,
    proposer_agent_run_id: str,
) -> JsonObject:
    """Validate an independent ReplayJudge result and gate classification."""

    if value.get("schema") != TEST_EFFECT_SCHEMA:
        raise ComparisonError("Unsupported test-effect schema")
    judge_run_id = value.get("judge_agent_run_id")
    if not isinstance(judge_run_id, str) or not judge_run_id:
        raise ComparisonError("judge_agent_run_id must not be empty")
    if judge_run_id == proposer_agent_run_id:
        raise ComparisonError(
            "ReplayJudge and CandidateProposer must use different AgentRuns"
        )
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise ComparisonError("dimensions must be an object")
    normalized_dimensions: dict[str, str] = {}
    for name, item in dimensions.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ComparisonError("dimension states must be strings")
        normalized_dimensions[name] = item
    protected = value.get("protected_dimensions", [])
    if not isinstance(protected, list) or not all(
        isinstance(item, str) for item in protected
    ):
        raise ComparisonError("protected_dimensions must be a string list")
    runnable = value.get("runnable")
    complete = value.get("complete")
    if not isinstance(runnable, bool) or not isinstance(complete, bool):
        raise ComparisonError("runnable and complete must be booleans")
    calculated = classify_dimensions(
        normalized_dimensions,
        runnable=runnable,
        complete=complete,
        protected_dimensions=protected,
    )
    claimed = value.get("classification")
    if claimed != calculated:
        raise ComparisonError(
            f"Judge classification {claimed!r} does not match gate "
            f"classification {calculated!r}"
        )
    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ComparisonError(
            "ReplayJudge must cite at least one evidence.ref.v1"
        )
    normalized_evidence: list[JsonObject] = []
    try:
        for reference in raw_evidence:
            if not isinstance(reference, Mapping):
                raise EvidenceError("Evidence reference must be an object")
            normalized_evidence.append(
                EvidenceRef.from_dict(reference).to_dict()
            )
    except EvidenceError as error:
        raise ComparisonError(f"Invalid ReplayJudge evidence: {error}") from error
    return {
        "schema": TEST_EFFECT_SCHEMA,
        "comparison_id": value.get("comparison_id"),
        "candidate_id": value.get("candidate_id"),
        "judge_agent_run_id": judge_run_id,
        "proposer_agent_run_id": proposer_agent_run_id,
        "runnable": runnable,
        "complete": complete,
        "dimensions": normalized_dimensions,
        "protected_dimensions": protected,
        "classification": calculated,
        "regressions": value.get("regressions", []),
        "uncertainties": value.get("uncertainties", []),
        "evidence": normalized_evidence,
    }


class RunAttempt(Protocol):
    """Attested runner that cannot use built-in tools or host fallback."""

    sandbox_backend: str
    built_in_tools: bool
    host_fallback_allowed: bool

    def __call__(
        self,
        planned: Mapping[str, Any],
        sandbox_context: Mapping[str, Any],
        run_directory: Path,
    ) -> Mapping[str, Any]:
        """Execute one planned attempt through its sandbox context."""


HarnessAttempt = Callable[
    [Sequence[Mapping[str, Any]], Path],
    Mapping[str, Any],
]


def _validate_run_attempt_boundary(
    runner: RunAttempt,
    *,
    backend: str,
) -> None:
    if getattr(runner, "sandbox_backend", None) != backend:
        raise ComparisonError(
            "RunAttempt is not bound to the preflighted sandbox backend"
        )
    if getattr(runner, "built_in_tools", None) is not False:
        raise ComparisonError(
            "Automatic replay must disable Pi built-in tools"
        )
    if getattr(runner, "host_fallback_allowed", None) is not False:
        raise ComparisonError(
            "Automatic replay runner must forbid host fallback"
        )


class ComparisonRepository:
    """Persist comparison attempts, harness results, and every candidate effect."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.repository = ManifestRepository(root)

    def create(
        self,
        *,
        candidate_id: str,
        baseline_skill_version: str,
        triggering_task_case_id: str,
        regression_task_case_id: str,
        proposer_agent_run_id: str,
        replay_count: int = 3,
        max_runs: int = 13,
    ) -> JsonObject:
        """Create an automatic comparison inside its accepted run budget."""

        comparison_id = new_object_id("comparison")
        plan = build_run_plan(
            triggering_task_case_id=triggering_task_case_id,
            regression_task_case_id=regression_task_case_id,
            replay_count=replay_count,
            max_runs=max_runs,
        )
        manifest: JsonObject = {
            "schema": COMPARISON_EXPERIMENT_SCHEMA,
            "id": comparison_id,
            "status": "planned",
            "candidate_id": candidate_id,
            "baseline_skill_version": baseline_skill_version,
            "proposer_agent_run_id": proposer_agent_run_id,
            "task_case_ids": [
                triggering_task_case_id,
                regression_task_case_id,
            ],
            "replay_count": replay_count,
            "automatic_run_limit": max_runs,
            "run_plan": plan,
            "attempts": [],
            "harness_runs": [],
            "judge_attempts": [],
            "test_effect": None,
            "sandbox": None,
        }
        self.repository.create(comparison_id, manifest)
        return self.repository.load(comparison_id)

    def execute(
        self,
        comparison_id: str,
        *,
        sandbox: SandboxBackend,
        run_attempt: RunAttempt,
        run_harness: HarnessAttempt,
    ) -> JsonObject:
        """Execute through an attested sandbox runner with no host fallback."""

        manifest = self.repository.load(comparison_id)
        if manifest.get("status") not in {"planned", "awaiting_sandbox"}:
            raise StorageError("Comparison is not ready to execute")
        preflight = sandbox.preflight()
        sandbox_record = {
            "backend": preflight.backend,
            "available": preflight.available,
            "detail": preflight.detail,
            "checked_at": utc_now(),
        }
        if not preflight.available:
            return self.repository.update(
                comparison_id,
                {
                    "status": "awaiting_sandbox",
                    "sandbox": sandbox_record,
                },
                expected_status=manifest["status"],
            )
        if preflight.backend != "docker_tool_router":
            raise ComparisonError(
                "Automatic replay requires the Docker tool-router backend"
            )
        _validate_run_attempt_boundary(
            run_attempt,
            backend=preflight.backend,
        )

        manifest = self.repository.update(
            comparison_id,
            {
                "status": "running",
                "sandbox": sandbox_record,
                "started_at": utc_now(),
            },
            expected_status=manifest["status"],
        )
        attempts = [
            dict(item)
            for item in manifest.get("attempts", [])
            if isinstance(item, Mapping)
        ]
        harness_runs = [
            dict(item)
            for item in manifest.get("harness_runs", [])
            if isinstance(item, Mapping)
        ]
        completed_indexes = {
            item.get("attempt_index")
            for item in attempts
            if item.get("status") == "succeeded"
        }
        for planned in manifest["run_plan"]:
            if planned["attempt_index"] in completed_indexes:
                continue
            workflow_attempt = 1 + sum(
                item.get("attempt_index") == planned["attempt_index"]
                for item in attempts
            )
            attempt_directory = (
                self.repository.object_directory(comparison_id)
                / "attempts"
                / (
                    f"{int(planned['attempt_index']):02d}"
                    f"-attempt-{workflow_attempt:02d}"
                )
            )
            attempt_directory.mkdir(parents=True, exist_ok=False)
            attempt_base = {
                **dict(planned),
                "workflow_attempt": workflow_attempt,
                "attempt_path": str(
                    attempt_directory.relative_to(
                        self.repository.object_directory(comparison_id)
                    )
                ),
            }
            try:
                with sandbox.isolated_run(
                    attempt_directory
                ) as sandbox_context:
                    if not isinstance(sandbox_context, Mapping):
                        raise ComparisonError(
                            "Sandbox context must be a mapping"
                        )
                    result = dict(
                        run_attempt(
                            planned,
                            sandbox_context,
                            attempt_directory,
                        )
                    )
            except ComparisonError as error:
                attempts.append(
                    {
                        **attempt_base,
                        "status": "sandbox_failed",
                        "error": {
                            "type": type(error).__name__,
                            "message": str(error),
                        },
                    }
                )
                return self.repository.update(
                    comparison_id,
                    {
                        "status": "awaiting_sandbox",
                        "attempts": attempts,
                        "sandbox": {
                            **sandbox_record,
                            "available": False,
                            "detail": str(error),
                        },
                    },
                    expected_status="running",
                )
            except Exception as error:
                result = {
                    "status": "orchestration_failed",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                }
            attempt = {**attempt_base, **result}
            attempts.append(attempt)
            self.repository.update(
                comparison_id,
                {"attempts": attempts},
                expected_status="running",
            )
            if planned["phase"] == "smoke":
                try:
                    harness_result = dict(
                        run_harness(
                            tuple(attempts),
                            self.repository.object_directory(comparison_id),
                        )
                    )
                except Exception as error:
                    harness_result = {
                        "status": "failed",
                        "error": {
                            "type": type(error).__name__,
                            "message": str(error),
                        },
                    }
                harness_runs.append(
                    {
                        "scope": "smoke",
                        "attempt_count": len(attempts),
                        **harness_result,
                    }
                )
                self.repository.update(
                    comparison_id,
                    {"harness_runs": harness_runs},
                    expected_status="running",
                )
            if planned["phase"] == "smoke" and result.get(
                "status"
            ) != "succeeded":
                return self.repository.update(
                    comparison_id,
                    {
                        "status": "not_runnable",
                        "attempts": attempts,
                        "ended_at": utc_now(),
                    },
                    expected_status="running",
                )
        try:
            full_harness = dict(
                run_harness(
                    tuple(attempts),
                    self.repository.object_directory(comparison_id),
                )
            )
        except Exception as error:
            full_harness = {
                "status": "failed",
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
        harness_runs.append(
            {
                "scope": "full",
                "attempt_count": len(attempts),
                **full_harness,
            }
        )
        return self.repository.update(
            comparison_id,
            {
                "status": "awaiting_judge",
                "attempts": attempts,
                "harness_runs": harness_runs,
                "ended_at": utc_now(),
            },
            expected_status="running",
        )

    def record_effect(
        self,
        comparison_id: str,
        effect: Mapping[str, Any],
    ) -> JsonObject:
        """Store the independent Judge result without hiding any candidate."""

        manifest = self.repository.load(comparison_id)
        validated = validate_test_effect(
            effect,
            proposer_agent_run_id=str(manifest["proposer_agent_run_id"]),
        )
        if validated.get("comparison_id") not in {None, comparison_id}:
            raise ComparisonError("TestEffect references another comparison")
        validated["comparison_id"] = comparison_id
        status = str(validated["classification"])
        judge_attempts = [
            dict(item)
            for item in manifest.get("judge_attempts", [])
            if isinstance(item, Mapping)
        ]
        judge_attempts.append(
            {
                "agent_run_id": validated["judge_agent_run_id"],
                "status": "succeeded",
            }
        )
        return self.repository.update(
            comparison_id,
            {
                "status": "completed",
                "judge_attempts": judge_attempts,
                "test_effect": validated,
                "gate_classification": status,
                "review_status": "awaiting_human_review",
            },
            expected_status={"awaiting_judge", "not_runnable"},
        )

    def record_judge_failure(
        self,
        comparison_id: str,
        *,
        agent_run_id: str,
        status: str,
        error: Mapping[str, Any] | None,
    ) -> JsonObject:
        """Preserve a failed Judge attempt while allowing a fresh retry."""

        if status == "succeeded" or not status:
            raise ComparisonError("Judge failure requires a failure status")
        manifest = self.repository.load(comparison_id)
        attempts = [
            dict(item)
            for item in manifest.get("judge_attempts", [])
            if isinstance(item, Mapping)
        ]
        attempts.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": dict(error) if error is not None else None,
            }
        )
        return self.repository.update(
            comparison_id,
            {"judge_attempts": attempts},
            expected_status={"awaiting_judge", "not_runnable"},
        )
