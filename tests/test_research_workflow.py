"""Tests for gated internal multi-Trajectory research batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from skill_evolution.agents import (
    ACTIVE_SPECIALIST_ROLES,
    AgentRole,
    AgentRunResult,
    AgentSpec,
    MultiPiOrchestrator,
)
from skill_evolution.research_corpus import (
    RESEARCH_BASELINE_SCHEMA,
    RESEARCH_CORPUS_SCHEMA,
    RESEARCH_CORPUS_MAP_SCHEMA,
    RESEARCH_NAVIGATION_INDEX_SCHEMA,
    RESEARCH_OBJECTIVES,
    RESEARCH_REDACTION_POLICY_SCHEMA,
    RESEARCH_READINESS_SCHEMA,
    RESEARCH_TASK_CONDITION_MAP_SCHEMA,
    ResearchCorpusResult,
    verify_research_corpus,
)
from skill_evolution.research_capability import (
    attest_pi_execution_identity,
    build_research_capability_identity,
    build_research_execution_identity,
    research_capability_identity_digest,
    research_execution_identity_digest,
)
from skill_evolution.research_harness_acceptance import (
    HARNESS_ACCEPTANCE_SCHEMA,
    HARNESS_SUBCHECKS,
    HARNESS_VALIDATOR_VERSION,
    _implementation_identity,
    _prepare_audit,
    _prepare_output_destination,
    _seal_audit,
    _write_output_json,
    verify_harness_acceptance_report,
)
from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxLimits,
)
from skill_evolution.research_workflow import (
    BENCHMARK_REQUIRED_FINDINGS,
    HARNESS_CHECKS,
    REVIEW_CHECKS,
    ResearchWorkflow,
    ResearchWorkflowError,
    VALIDATION_BENCHMARK_SCHEMA,
)
from skill_evolution.storage import load_json_object


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX_IMAGE_ID = "sha256:" + "a" * 64


def _sandbox_control_plane(
    *,
    engine_id: str = "fixture-engine-a",
) -> dict[str, object]:
    executable = Path(sys.executable).resolve()
    return {
        "schema": "research.docker_control_plane.v1",
        "resolved_command": [str(executable)],
        "executable": {
            "path": str(executable),
            "bytes": executable.stat().st_size,
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        },
        "interpreters": [],
        "client": {
            "version": "27.0.0",
            "api_version": "1.46",
            "git_commit": "client",
            "go_version": "go1.22",
            "os": "darwin",
            "arch": "arm64",
            "build_time": "2026-08-14T00:00:00Z",
            "context": "fixture",
            "endpoint": "unix:///fixture/docker.sock",
        },
        "daemon": {
            "id": engine_id,
            "version": "27.0.0",
            "api_version": "1.46",
            "min_api_version": "1.24",
            "git_commit": "server",
            "go_version": "go1.22",
            "os": "linux",
            "arch": "arm64",
            "build_time": "2026-08-14T00:00:00Z",
            "kernel_version": "6.6.0",
            "operating_system": "Fixture Linux",
            "os_version": "1",
            "os_type": "linux",
            "architecture": "arm64",
            "security_options": ["name=seccomp"],
            "rootless": False,
            "cgroup_driver": "cgroupfs",
            "cgroup_version": "2",
            "storage_driver": "overlay2",
            "default_runtime": "runc",
            "isolation": None,
        },
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_packaged_pi_entrypoint(root: Path) -> Path:
    """Create one direct package-bound Pi launcher for identity tests."""

    package = root / "fixture-pi-package"
    package.mkdir(parents=True, exist_ok=True)
    _write_json(
        package / "package.json",
        {"name": "fixture-pi-package", "version": "0.81.1"},
    )
    entrypoint = package / "pi"
    entrypoint.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' '0.81.1'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    return entrypoint


def _write_approved_harness_context(root: Path) -> Path:
    """Approve the repository's complete prompt-visible tool context."""

    tools = _REPOSITORY_ROOT / "extensions/research-tools.ts"
    output = _REPOSITORY_ROOT / "extensions/research-output.ts"
    path = root / "research-harness-context.json"
    value = {
        "schema": "prompt.research_harness_context.v1",
        "title": "Approved workflow fixture tool context",
        "version": "1",
        "tool_schema_version": "1",
        "prompt_visible_extensions": [
            {
                "name": "research_tools",
                "file": tools.name,
                "sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
            },
            {
                "name": "research_output",
                "file": output.name,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
        ],
    }
    _write_json(path, value)
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_json(
        path.with_name(path.name + ".approval.json"),
        {
            "schema": "prompt.approval.v1",
            "status": "approved",
            "prompt_id": "analysis.research-harness-context",
            "version": "1",
            "prompt_file": path.name,
            "content_sha256": content_sha256,
            "approved_by": "test-owner",
            "approved_at": "2026-08-14T00:00:00Z",
        },
    )
    return path


class _FakeDockerSandbox(DockerResearchSandbox):
    """Satisfy the production sandbox type boundary without running Docker."""

    def __init__(self) -> None:
        super().__init__(
            docker_command="/fake/docker",
            limits=ResearchSandboxLimits(),
        )


def _canonical_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _strict_harness_acceptance(**arguments):
    """Write a strict, independently reloadable Harness acceptance fixture."""

    corpus = verify_research_corpus(
        arguments["corpus_directory"],
        expected_content_sha256=arguments["expected_corpus_digest"],
        expected_baseline_sha256=arguments["expected_baseline_digest"],
    )
    sandbox = arguments["sandbox"]
    report_path = Path(arguments["report_path"])
    tools_path = _REPOSITORY_ROOT / "extensions/research-tools.ts"
    output_path = _REPOSITORY_ROOT / "extensions/research-output.ts"
    driver_path = _REPOSITORY_ROOT / "extensions/research-harness-driver.ts"
    context_path = Path(arguments["research_harness_context_path"])
    context_approval_path = context_path.with_name(
        context_path.name + ".approval.json"
    )
    implementation = _implementation_identity(
        tools_path=tools_path,
        output_path=output_path,
        driver_path=driver_path,
    )
    output_boundary = _prepare_output_destination(
        report_path,
        trusted_directory=arguments["trusted_output_root"],
    )
    audit_boundary = _prepare_audit(
        output_boundary,
        implementation_paths={
            "research-harness-acceptance.py": (
                _REPOSITORY_ROOT
                / "skill_evolution/research_harness_acceptance.py"
            ),
            "research-agent-runtime.py": (
                _REPOSITORY_ROOT / "skill_evolution/research_agent_runtime.py"
            ),
            "research-tools.ts": tools_path,
            "research-output.ts": output_path,
            "research-harness-driver.ts": driver_path,
            "research-harness-context.json": context_path,
            "research-harness-context-approval.json": (
                context_approval_path
            ),
        },
        implementation=implementation,
    )
    audit_directory = audit_boundary.working_directory
    for mode in (
        "positive",
        "budget",
        "cleanup",
        "duplicate_submission",
        "post_submission",
    ):
        trajectory = audit_directory / f"pi-{mode}/trajectory.jsonl"
        trajectory.parent.mkdir()
        trajectory.write_text("{}\n", encoding="utf-8")
    limits = sandbox.limits.to_dict()
    limits_sha256 = _canonical_digest(limits)
    active_config = {
        "image_id": _SANDBOX_IMAGE_ID,
        "network_mode": "none",
        "network_count": 0,
        "log_driver": "none",
        "readonly_rootfs": True,
        "user": "65534:65534",
        "pids_limit": limits["pids"],
        "nano_cpus": round(limits["cpus"] * 1_000_000_000),
        "memory_bytes": 1024**3,
        "nofile_soft": limits["open_files"],
        "nofile_hard": limits["open_files"],
        "cap_drop": ["ALL"],
        "security_options": ["no-new-privileges"],
        "tmpfs": {
            "/tmp": f"rw,noexec,nosuid,nodev,size={limits['temporary_bytes']}",
            "/work": f"rw,nosuid,nodev,size={limits['work_bytes']}",
        },
        "tool_budgets": {
            "command_timeout_milliseconds": (
                limits["command_timeout_seconds"] * 1000
            ),
            "max_output_bytes": limits["max_output_bytes"],
            "max_tool_calls": limits["max_tool_calls"],
            "max_concurrent_tool_calls": limits["max_concurrent_tool_calls"],
            "max_total_output_bytes": limits["max_total_output_bytes"],
            "max_total_command_milliseconds": limits[
                "max_total_command_milliseconds"
            ],
        },
    }
    active_config_sha256 = _canonical_digest(active_config)
    checks = []
    for name in HARNESS_CHECKS:
        evidence = [
            {
                "kind": "observed_fact",
                "assertion": f"{name}.{subcheck} was exercised",
                "observed": "deterministic fixture passed",
                "sha256": None,
            }
            for subcheck in HARNESS_SUBCHECKS[name]
        ]
        checks.append(
            {
                "name": name,
                "status": "passed",
                "subchecks": [
                    {
                        "name": subcheck,
                        "status": "passed",
                        "evidence_sha256": _canonical_digest(
                            {"evidence": [evidence[index]]}
                        ),
                        "error": None,
                    }
                    for index, subcheck in enumerate(HARNESS_SUBCHECKS[name])
                ],
                "evidence": evidence,
                "error": None,
            }
        )
    pi_execution_identity = attest_pi_execution_identity(
        arguments["pi_command"],
        extra_pi_args=arguments.get("extra_pi_args", ()),
        working_directory=_REPOSITORY_ROOT,
    )
    execution_identity = build_research_execution_identity(
        repository_root=_REPOSITORY_ROOT,
        pi_execution_identity=pi_execution_identity,
        harness_context_sha256=hashlib.sha256(
            context_path.read_bytes()
        ).hexdigest(),
        research_tools_sha256=implementation["research_tools_sha256"],
        research_output_sha256=implementation["research_output_sha256"],
        sandbox_backend=RESEARCH_SANDBOX_BACKEND,
        sandbox_image=sandbox.image,
        sandbox_image_id=_SANDBOX_IMAGE_ID,
        sandbox_limits=limits,
        sandbox_control_plane_identity=_sandbox_control_plane(),
    )
    execution_identity_sha256 = research_execution_identity_digest(
        execution_identity,
        repository_root=_REPOSITORY_ROOT,
        verify_pi_executable=True,
    )
    _write_json(
        audit_directory / "execution-identity.json",
        execution_identity,
    )
    audit = _seal_audit(audit_boundary)
    body = {
        "validator_version": HARNESS_VALIDATOR_VERSION,
        "status": "passed",
        "started_at": "2026-08-14T04:00:00+00:00",
        "ended_at": "2026-08-14T04:00:01+00:00",
        "corpus": {
            "corpus_id": corpus.manifest["corpus_id"],
            "content_sha256": corpus.content_sha256,
            "baseline_sha256": corpus.baseline_sha256,
            "execution_ids": list(corpus.execution_ids),
        },
        "implementation": implementation,
        "execution_identity": execution_identity,
        "execution_identity_sha256": execution_identity_sha256,
        "sandbox": {
            "backend": RESEARCH_SANDBOX_BACKEND,
            "image": sandbox.image,
            "image_id": _SANDBOX_IMAGE_ID,
            "preflight_available": True,
            "limits": limits,
            "limits_sha256": limits_sha256,
            "active_config": active_config,
            "active_config_sha256": active_config_sha256,
        },
        "audit": audit,
        "checks": checks,
    }
    report = {
        "schema": HARNESS_ACCEPTANCE_SCHEMA,
        "content_sha256": _canonical_digest(body),
        **body,
    }
    file_sha256 = _write_output_json(output_boundary, report)
    return verify_harness_acceptance_report(
        report_path,
        expected_file_sha256=file_sha256,
        trusted_output_root=arguments["trusted_output_root"],
        corpus_directory=arguments["corpus_directory"],
        expected_corpus_digest=arguments["expected_corpus_digest"],
        expected_baseline_digest=arguments["expected_baseline_digest"],
        expected_image_id=_SANDBOX_IMAGE_ID,
    )


def _run_harness(
    workflow: ResearchWorkflow,
    batch_id: str,
) -> dict[str, object]:
    runtime = workflow.orchestrator.runtime
    with patch(
        "skill_evolution.research_workflow.run_harness_acceptance",
        side_effect=_strict_harness_acceptance,
    ):
        return workflow.run_harness_validation(
            batch_id,
            sandbox=_FakeDockerSandbox(),
            pi_command=runtime.pi_command,
            research_harness_context_path=runtime.harness_context,
        )


def _corpus(
    root: Path,
    *,
    objectives: set[str] | None = None,
    ready: bool = True,
) -> ResearchCorpusResult:
    evidence = root / "corpus"
    evidence.mkdir(parents=True)
    selected_objectives = sorted(objectives or RESEARCH_OBJECTIVES)
    execution_ids = ["run-1", "run-2", "run-3"]
    coverage_requested = "conditions_coverage" in selected_objectives
    condition_groups = {
        "run-1": "condition-a",
        "run-2": "condition-a",
        "run-3": "condition-b",
    }
    coverage = (
        {
            "suite_id": "suite-1",
            "represented_task_case_ids": ["case-1", "case-2"],
            "zero_sample_task_case_ids": ["case-3"],
        }
        if coverage_requested
        else None
    )
    frozen_readiness = {
        "schema": RESEARCH_READINESS_SCHEMA,
        "status": "ready",
        "skill_id": "test-skill",
        "revision_id": "rev-1",
        "objectives": selected_objectives,
        "execution_ids": execution_ids,
        "condition_groups": condition_groups,
        "coverage": coverage,
        "issues": [],
    }
    corpus_map = {
        "schema": RESEARCH_CORPUS_MAP_SCHEMA,
        "skill_id": "test-skill",
        "revision_id": "rev-1",
        "objectives": selected_objectives,
        "trajectories": [
            {
                "run_id": run_id,
                "status": "succeeded",
                "task_case_id": "case-1" if run_id != "run-3" else "case-2",
                "condition_group": condition_groups[run_id],
                "trajectory_records": 1,
                "accepted_single_report_count": 1,
                "artifact_count": 0,
            }
            for run_id in execution_ids
        ],
        "available_queries": ["search observable trajectory text"],
    }
    navigation_index = {
        "schema": RESEARCH_NAVIGATION_INDEX_SCHEMA,
        "entries": [],
        "scripts": [],
    }
    baseline = {
        "schema": RESEARCH_BASELINE_SCHEMA,
        "results": {
            "eligible": 3,
            "included": 3,
            "excluded": 0,
            "missing": 0,
            "succeeded": 3,
            "failed": 0,
            "success_rate": 1.0,
            "status_counts": {"succeeded": 3},
            "failure_type_counts": {},
        },
        "runs": [
            {
                "run_id": run_id,
                "status": "succeeded",
                "duration_ms": 100,
                "model_calls": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 15,
                "reported_cost_total": 0.01,
                "tool_actions": 0,
                "failed_tool_actions": 0,
                "tool_duration_ms": 0,
                "recovery_count": 0,
                "validation_count": 0,
                "script_create_or_modify_count": 0,
                "script_execute_count": 0,
                "resource_complete": True,
            }
            for run_id in execution_ids
        ],
        "aggregate": {},
    }
    _write_json(evidence / "corpus-map.json", corpus_map)
    _write_json(evidence / "navigation-index.json", navigation_index)
    _write_json(evidence / "baseline.json", baseline)
    _write_json(evidence / "readiness.json", frozen_readiness)
    _write_json(evidence / "revision/revision.json", {"revision_id": "rev-1"})
    run_manifests: list[dict[str, object]] = []
    for run_id in execution_ids:
        run_root = evidence / "runs" / run_id
        task_case_id = "case-1" if run_id != "run-3" else "case-2"
        _write_json(
            run_root / "task.json",
            {
                "schema": "task.case.v1",
                "task_case_id": task_case_id,
                "delivery": "inline_text",
                "input": {"text": f"Task for {run_id}"},
                "expected_artifacts": ["output.html"],
                "capability_tags": [],
                "budget": {},
            },
        )
        trajectory_path = run_root / "trajectory.jsonl"
        trajectory_path.write_text(
            json.dumps(
                {
                    "schema": "trajectory.event.v1",
                    "run_id": run_id,
                    "seq": 1,
                    "type": "run_result",
                    "payload": {"status": "succeeded"},
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        report_path = run_root / "single-reports" / "analysis-1" / "01-report.json"
        _write_json(
            report_path,
            {
                "schema": "analysis.single_trajectory.v2",
                "analysis_id": f"analysis-{run_id}",
                "run_id": run_id,
                "status": "accepted",
            },
        )
        trajectory_digest = hashlib.sha256(trajectory_path.read_bytes()).hexdigest()
        report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        run_manifests.append(
            {
                "execution_id": run_id,
                "status": "succeeded",
                "task": f"runs/{run_id}/task.json",
                "trajectory": {
                    "path": f"runs/{run_id}/trajectory.jsonl",
                    "records": 1,
                    "schema": "trajectory.event.v1",
                    "source_sha256": trajectory_digest,
                    "stored_sha256": trajectory_digest,
                },
                "artifacts": [],
                "single_reports": [
                    {
                        "analysis_id": f"analysis-{run_id}",
                        "kind": "single_trajectory",
                        "schema": "analysis.single_trajectory.v2",
                        "path": (
                            f"runs/{run_id}/single-reports/analysis-1/"
                            "01-report.json"
                        ),
                        "source_sha256": report_digest,
                        "stored_sha256": report_digest,
                    }
                ],
            }
        )

    evaluation_suite_path: str | None = None
    task_condition_map_path: str | None = None
    if coverage_requested:
        suite = {
            "schema": "evaluation.suite.v1",
            "suite_id": "suite-1",
            "skill_id": "test-skill",
            "version": "1.0.0",
            "status": "approved",
            "owner": "project-owner",
            "approved_by": "project-owner",
            "approved_at": "2026-08-14T12:00:00+08:00",
            "task_cases": [
                {
                    "task_case_id": f"case-{index}",
                    "path": f"task-cases/case-{index}.json",
                    "conditions": {"scenario": scenario},
                }
                for index, scenario in enumerate(("a", "b", "c"), start=1)
            ],
            "coverage_dimensions": [
                {"id": "scenario", "required_values": ["a", "b", "c"]}
            ],
            "readiness": {
                "minimum_distinct_condition_groups": 2,
                "minimum_samples_per_condition_group": 1,
            },
        }
        task_cases = [
            {
                "task_case_id": f"case-{index}",
                "conditions": {"scenario": scenario},
                "task": {
                    "schema": "task.case.v1",
                    "task_case_id": f"case-{index}",
                    "delivery": "inline_text",
                    "input": {"text": f"Suite task {index}"},
                    "expected_artifacts": ["output.html"],
                    "capability_tags": [],
                    "budget": {},
                },
            }
            for index, scenario in enumerate(("a", "b", "c"), start=1)
        ]
        execution_mapping = [
            {
                "run_id": run_id,
                "task_case_id": "case-1" if run_id != "run-3" else "case-2",
                "conditions": {
                    "scenario": "a" if run_id != "run-3" else "b"
                },
                "declared_comparable_group": condition_groups[run_id],
            }
            for run_id in execution_ids
        ]
        evaluation_suite_path = "evaluation/suite.json"
        task_condition_map_path = "evaluation/task-condition-map.json"
        _write_json(evidence / evaluation_suite_path, suite)
        _write_json(
            evidence / task_condition_map_path,
            {
                "schema": RESEARCH_TASK_CONDITION_MAP_SCHEMA,
                "suite_id": "suite-1",
                "skill_id": "test-skill",
                "task_cases": task_cases,
                "execution_mapping": execution_mapping,
                "coverage": coverage,
            },
        )
    baseline_digest = hashlib.sha256(
        (evidence / "baseline.json").read_bytes()
    ).hexdigest()
    files = []
    for path in sorted(evidence.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(evidence).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    manifest_body = {
        "purpose": "multi_trajectory_research",
        "skill_id": "test-skill",
        "revision_id": "rev-1",
        "objectives": selected_objectives,
        "execution_ids": execution_ids,
        "revision_manifest": "revision/revision.json",
        "corpus_map": "corpus-map.json",
        "navigation_index": "navigation-index.json",
        "baseline": "baseline.json",
        "readiness": "readiness.json",
        "evaluation_suite": evaluation_suite_path,
        "task_condition_map": task_condition_map_path,
        "runs": run_manifests,
        "files": files,
        "redaction": {
            "schema": RESEARCH_REDACTION_POLICY_SCHEMA,
            "policy_id": "observable-evidence-v1",
            "hidden_reasoning": "redacted",
            "credentials": "redacted",
            "environment": "redacted",
            "text_artifacts": "utf8-sanitized",
            "binary_artifacts": "excluded",
            "pi_session": "excluded",
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            manifest_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": RESEARCH_CORPUS_SCHEMA,
        "corpus_id": f"corpus-{digest[:20]}",
        "content_sha256": digest,
        **manifest_body,
    }
    readiness = dict(frozen_readiness)
    if not ready:
        readiness["status"] = "not_ready"
        readiness["issues"] = [{"code": "not_ready"}]
    _write_json(evidence / "corpus.json", manifest)
    return ResearchCorpusResult(
        directory=evidence,
        manifest=manifest,
        corpus_map=corpus_map,
        navigation_index=navigation_index,
        baseline=baseline,
        readiness=readiness,
        corpus_digest=digest,
        baseline_digest=baseline_digest,
    )


def _specs(root: Path) -> dict[AgentRole, AgentSpec]:
    root.mkdir(parents=True)
    result: dict[AgentRole, AgentSpec] = {}
    for role in ACTIVE_SPECIALIST_ROLES:
        prompt = root / f"{role.value}.md"
        prompt.write_text("Approved fake prompt.\n", encoding="utf-8")
        result[role] = AgentSpec(role=role, prompt_path=prompt)
    return result


def _benchmark(
    path: Path,
    *,
    skill_id: str = "test-skill",
    revision_id: str = "rev-1",
    execution_ids: list[str] | None = None,
    description: str = "HIDDEN_SCRIPT_PATTERN_SENTINEL",
) -> Path:
    _write_json(
        path,
        {
            "schema": VALIDATION_BENCHMARK_SCHEMA,
            "benchmark_id": "benchmark-five-trajectory-v1",
            "title": "Hidden blind-research acceptance benchmark",
            "status": "approved",
            "skill_id": skill_id,
            "revision_id": revision_id,
            "execution_ids": execution_ids
            or ["run-1", "run-2", "run-3"],
            "required_discoveries": [
                {
                    "discovery_id": "temporary-generation-flow",
                    "description": description,
                    "minimum_supporting_trajectory_count": 2,
                    "required_findings": list(BENCHMARK_REQUIRED_FINDINGS),
                }
            ],
            "owner": "project-owner",
            "approved_by": "project-owner",
            "approved_at": "2026-08-14T12:00:00+08:00",
        },
    )
    return path


def _freeze_benchmark(
    workflow: ResearchWorkflow,
    batch_id: str,
    path: Path,
) -> dict[str, object]:
    batch = workflow.load(batch_id)
    readiness = batch["readiness"]
    source = _benchmark(
        path,
        skill_id=str(readiness["skill_id"]),
        revision_id=str(readiness["revision_id"]),
        execution_ids=list(readiness["execution_ids"]),
    )
    return workflow.freeze_validation_benchmark(
        batch_id,
        benchmark_file=source,
    )


class FakeResearchRuntime:
    """Return controlled fresh AgentRuns without a model or Docker."""

    def __init__(
        self,
        root: Path,
        *,
        statuses: dict[AgentRole, list[str]] | None = None,
        preflight_error: Exception | None = None,
        session_ids: list[str] | None = None,
        write_session_identity: bool = True,
    ) -> None:
        self.root = root
        self.statuses = {
            role: list(values) for role, values in (statuses or {}).items()
        }
        self.preflight_error = preflight_error
        self.session_ids = list(session_ids or [])
        self.write_session_identity = write_session_identity
        self.calls: list[dict[str, object]] = []
        self.preflights: list[list[AgentRole]] = []
        self.capability_revision = "fixture-v1"
        self.sandbox_engine_id = "fixture-engine-a"
        fixture_root = root.parent / "runtime-fixtures"
        self.pi_command = [
            str(_write_packaged_pi_entrypoint(fixture_root))
        ]
        self.harness_context = _write_approved_harness_context(
            fixture_root
        )
        self.harness_context_sha256 = hashlib.sha256(
            self.harness_context.read_bytes()
        ).hexdigest()
        self.pi_execution_identity = attest_pi_execution_identity(
            self.pi_command,
            working_directory=_REPOSITORY_ROOT,
        )
        self.pi_execution_revision = str(
            self.pi_execution_identity["version"]
        )

    def research_capability_identity(self, spec: AgentSpec) -> dict[str, object]:
        """Return a valid identity bound to the live repository fingerprint."""

        del spec
        limits = ResearchSandboxLimits().to_dict()
        pi_execution_identity = json.loads(
            json.dumps(self.pi_execution_identity)
        )
        pi_execution_identity["version"] = self.pi_execution_revision
        return build_research_capability_identity(
            repository_root=_REPOSITORY_ROOT,
            prompt_id="analysis.behavior-pattern-research",
            prompt_version="1",
            prompt_sha256="b" * 64,
            harness_context_sha256=self.harness_context_sha256,
            harness_version="1",
            tool_schema_version="1",
            research_tools_sha256=hashlib.sha256(
                (_REPOSITORY_ROOT / "extensions/research-tools.ts").read_bytes()
            ).hexdigest(),
            research_output_sha256=hashlib.sha256(
                (_REPOSITORY_ROOT / "extensions/research-output.ts").read_bytes()
            ).hexdigest(),
            pi_execution_identity=pi_execution_identity,
            model={
                "provider": "fixture-provider",
                "model": self.capability_revision,
                "thinking": "medium",
            },
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image="python:3.11-slim",
            sandbox_image_id=_SANDBOX_IMAGE_ID,
            sandbox_limits=limits,
            sandbox_control_plane_identity=_sandbox_control_plane(
                engine_id=self.sandbox_engine_id
            ),
        )

    def preflight(self, specs) -> None:
        roles = [spec.role for spec in specs]
        self.preflights.append(roles)
        if self.preflight_error is not None:
            raise self.preflight_error

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
        index = len(self.calls) + 1
        run_id = f"agent-run-{index}-{spec.role.value}"
        directory = self.root / run_id
        directory.mkdir(parents=True)
        (directory / "pi-session.jsonl").write_text(
            json.dumps({"session": f"session-{index}"}) + "\n",
            encoding="utf-8",
        )
        if self.write_session_identity:
            session_id = (
                self.session_ids.pop(0)
                if self.session_ids
                else f"session-{index}"
            )
            _write_json(
                directory / "research/session-identity.json",
                {
                    "schema": "analysis.research_session_identity.v1",
                    "session_id": session_id,
                    "process_isolated": True,
                    "session_retained": False,
                },
            )
        queue = self.statuses.get(spec.role, [])
        status = queue.pop(0) if queue else "succeeded"
        result = (
            {
                "schema": "analysis.multi_trajectory_research.v1",
                "role": spec.role.value,
            }
            if status == "succeeded"
            else None
        )
        error = (
            None
            if status == "succeeded"
            else {"type": "FakeFailure", "message": "controlled"}
        )
        if result is not None:
            _write_json(directory / "result.json", result)
        self.calls.append(
            {
                "role": spec.role,
                "campaign_id": campaign_id,
                "round_number": round_number,
                "context": dict(context),
                "evidence_bundle": evidence_bundle,
            }
        )
        return AgentRunResult(
            agent_run_id=run_id,
            role=spec.role,
            status=status,
            result=result,
            error=error,
            run_directory=directory,
        )


class ResearchWorkflowTests(unittest.TestCase):
    """A batch cannot skip Harness, blind validation, or readiness gates."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = FakeResearchRuntime(self.root / "agent-runs")
        self.orchestrator = MultiPiOrchestrator(
            runtime=self.runtime,
            specs=_specs(self.root / "prompts"),
        )
        self.workflow = ResearchWorkflow(
            self.root / "batches",
            orchestrator=self.orchestrator,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _passing_checks() -> dict[str, bool]:
        return {name: True for name in REVIEW_CHECKS}

    def _harness(self, batch_id: str) -> dict[str, object]:
        _run_harness(self.workflow, batch_id)
        return _freeze_benchmark(
            self.workflow,
            batch_id,
            self.root / "benchmarks" / f"{batch_id}.json",
        )

    def _validated_batch(
        self,
        *,
        corpus: ResearchCorpusResult | None = None,
        batch_id: str = "batch-1",
    ) -> dict[str, object]:
        self._passed_uncertified_batch(corpus=corpus, batch_id=batch_id)
        return self.workflow.issue_capability_certification(batch_id)

    def _passed_uncertified_batch(
        self,
        *,
        corpus: ResearchCorpusResult | None = None,
        batch_id: str = "batch-1",
    ) -> dict[str, object]:
        self.workflow.prepare(
            corpus or _corpus(self.root),
            batch_id=batch_id,
        )
        self._harness(batch_id)
        running = self.workflow.run_single_agent_validation_cycle(batch_id)
        attempts = running["validation_cycles"][-1]["attempts"]
        for attempt in attempts:
            self.workflow.review_single_agent_attempt(
                batch_id,
                attempt_id=attempt["attempt_id"],
                reviewer="project-owner",
                checks=self._passing_checks(),
            )
        return self.workflow.load(batch_id)

    def test_prepare_rejects_unready_corpus_without_state_or_runtime(self) -> None:
        with self.assertRaisesRegex(ResearchWorkflowError, "not ready"):
            self.workflow.prepare(
                _corpus(self.root, ready=False),
                batch_id="rejected",
            )

        self.assertEqual(self.workflow.repository.list_manifests(), [])
        self.assertEqual(self.runtime.preflights, [])
        self.assertEqual(self.runtime.calls, [])

    def test_frozen_corpus_is_reverified_before_harness_advances(self) -> None:
        corpus = _corpus(self.root)
        self.workflow.prepare(corpus, batch_id="batch-1")
        (corpus.directory / "baseline.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "failed verification",
        ):
            self._harness("batch-1")

        self.assertEqual(self.workflow.load("batch-1")["status"], "prepared")
        self.assertEqual(self.runtime.preflights, [])
        self.assertEqual(self.runtime.calls, [])

    def test_caller_authored_harness_results_are_rejected(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        incomplete = {name: True for name in HARNESS_CHECKS[:-1]}

        with self.assertRaisesRegex(ResearchWorkflowError, "retired"):
            self.workflow.record_harness_validation(
                "batch-1",
                checks=incomplete,
                report_ref={"path": "harness.json"},
            )

        self.assertEqual(self.workflow.load("batch-1")["status"], "prepared")

    def test_smoke_cannot_start_without_a_frozen_benchmark(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        _run_harness(self.workflow, "batch-1")

        with self.assertRaisesRegex(ResearchWorkflowError, "frozen hidden"):
            self.workflow.run_single_agent_validation_cycle("batch-1")

        self.assertEqual(self.runtime.preflights, [])
        self.assertEqual(self.runtime.calls, [])

    def test_old_harness_cannot_start_a_changed_pi_capability(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-old-harness")
        self._harness("batch-old-harness")
        self.runtime.pi_execution_revision = "fixture-pi-v2"

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "differs from the batch's passed Harness",
        ):
            self.workflow.run_single_agent_validation_cycle(
                "batch-old-harness"
            )

        batch = self.workflow.load("batch-old-harness")
        self.assertEqual(batch["status"], "harness_validated")
        self.assertEqual(batch["validation_cycles"], [])
        self.assertEqual(self.runtime.calls, [])

    def test_old_harness_cannot_start_on_a_different_docker_engine(self) -> None:
        batch_id = "batch-old-docker-engine"
        self.workflow.prepare(_corpus(self.root), batch_id=batch_id)
        self._harness(batch_id)
        self.runtime.sandbox_engine_id = "fixture-engine-b"

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "differs from the batch's passed Harness",
        ):
            self.workflow.run_single_agent_validation_cycle(batch_id)

        batch = self.workflow.load(batch_id)
        self.assertEqual(batch["status"], "harness_validated")
        self.assertEqual(batch["validation_cycles"], [])
        self.assertEqual(self.runtime.calls, [])

    def test_benchmark_must_be_approved_and_match_the_full_batch(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        _run_harness(self.workflow, "batch-1")
        proposed = _benchmark(self.root / "proposed-benchmark.json")
        proposed_value = load_json_object(proposed)
        proposed_value["status"] = "proposed"
        _write_json(proposed, proposed_value)

        with self.assertRaisesRegex(ResearchWorkflowError, "not approved"):
            self.workflow.freeze_validation_benchmark(
                "batch-1",
                benchmark_file=proposed,
            )

        mismatched = _benchmark(
            self.root / "mismatched-benchmark.json",
            execution_ids=["run-1", "run-2"],
        )
        with self.assertRaisesRegex(ResearchWorkflowError, "execution_ids"):
            self.workflow.freeze_validation_benchmark(
                "batch-1",
                benchmark_file=mismatched,
            )

        batch = self.workflow.load("batch-1")
        self.assertIsNone(batch["validation_benchmark"])

    def test_frozen_benchmark_is_independent_of_later_source_edits(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        _run_harness(self.workflow, "batch-1")
        source = _benchmark(self.root / "source-benchmark.json")
        frozen = self.workflow.freeze_validation_benchmark(
            "batch-1",
            benchmark_file=source,
        )
        snapshot = (
            self.workflow.repository.object_directory("batch-1")
            / frozen["validation_benchmark"]["snapshot_path"]
        )
        original_snapshot = snapshot.read_bytes()
        _write_json(source, {"changed_after_freeze": True})

        running = self.workflow.run_single_agent_validation_cycle("batch-1")

        self.assertEqual(
            running["validation_cycles"][0]["status"],
            "awaiting_review",
        )
        self.assertEqual(snapshot.read_bytes(), original_snapshot)

    def test_tampered_benchmark_snapshot_blocks_agent_runtime(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        frozen = self._harness("batch-1")
        snapshot = (
            self.workflow.repository.object_directory("batch-1")
            / frozen["validation_benchmark"]["snapshot_path"]
        )
        _write_json(snapshot, {"tampered": True})

        with self.assertRaisesRegex(ResearchWorkflowError, "digest changed"):
            self.workflow.run_single_agent_validation_cycle("batch-1")

        self.assertEqual(self.runtime.preflights, [])
        self.assertEqual(self.runtime.calls, [])

    def test_two_independent_smokes_reviews_and_certificate_are_required(
        self,
    ) -> None:
        prepared = self.workflow.prepare(
            _corpus(self.root),
            batch_id="batch-1",
        )
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self._harness("batch-1")["status"], "harness_validated")

        running = self.workflow.run_single_agent_validation_cycle("batch-1")

        self.assertEqual(running["status"], "single_agent_validation_running")
        cycle = running["validation_cycles"][0]
        self.assertEqual(cycle["status"], "awaiting_review")
        self.assertEqual(len(cycle["attempts"]), 2)
        self.assertEqual(
            len({item["agent_run_id"] for item in cycle["attempts"]}),
            2,
        )
        self.assertEqual(
            len({item["session_ref"] for item in cycle["attempts"]}),
            2,
        )
        self.assertEqual(
            cycle["capability_identity_sha256"],
            research_capability_identity_digest(cycle["capability_identity"]),
        )
        for call in self.runtime.calls:
            self.assertEqual(call["role"], AgentRole.BEHAVIOR_PATTERN)
            self.assertNotIn("hidden_benchmark", call["context"])
            self.assertNotIn("specialist_reports", call["context"])
            context_text = json.dumps(call["context"], sort_keys=True)
            self.assertNotIn("HIDDEN_SCRIPT_PATTERN_SENTINEL", context_text)
            self.assertNotIn("required_discoveries", context_text)
            self.assertNotIn("validation_benchmark", context_text)
            evidence_root = Path(call["evidence_bundle"])
            self.assertTrue(
                all(
                    b"HIDDEN_SCRIPT_PATTERN_SENTINEL"
                    not in path.read_bytes()
                    for path in evidence_root.rglob("*")
                    if path.is_file()
                )
            )

        first = cycle["attempts"][0]
        once = self.workflow.review_single_agent_attempt(
            "batch-1",
            attempt_id=first["attempt_id"],
            reviewer="project-owner",
            checks=self._passing_checks(),
        )
        self.assertEqual(once["status"], "single_agent_validation_running")
        second = cycle["attempts"][1]
        accepted = self.workflow.review_single_agent_attempt(
            "batch-1",
            attempt_id=second["attempt_id"],
            reviewer="project-owner",
            checks=self._passing_checks(),
        )
        self.assertEqual(accepted["status"], "single_agent_validated")
        benchmark_digest = accepted["validation_benchmark"][
            "snapshot_sha256"
        ]
        self.assertTrue(
            all(
                review["benchmark_sha256"] == benchmark_digest
                for review in accepted["validation_cycles"][0]["reviews"]
            )
        )
        self.assertIsNone(accepted["capability_certification"])
        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "capability certificate",
        ):
            self.workflow.run_specialists("batch-1")

        certified = self.workflow.issue_capability_certification("batch-1")

        self.assertEqual(
            certified["capability_certification"]["mode"],
            "issued",
        )
        self.assertEqual(
            certified["capability_certification"]["identity_sha256"],
            cycle["capability_identity_sha256"],
        )

    def test_capability_certificate_tamper_is_rejected(self) -> None:
        certified = self._validated_batch(batch_id="batch-tamper")
        binding = certified["capability_certification"]
        snapshot = (
            self.workflow.repository.object_directory("batch-tamper")
            / binding["snapshot_path"]
        )
        _write_json(snapshot, {"tampered": True})

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "file digest changed",
        ):
            self.workflow.load("batch-tamper")

    def test_capability_certificate_parent_symlink_cannot_escape_batch(
        self,
    ) -> None:
        self._passed_uncertified_batch(batch_id="batch-parent-link")
        batch_root = self.workflow.repository.object_directory(
            "batch-parent-link"
        )
        outside = self.root / "outside-capability"
        outside.mkdir()
        (batch_root / "capability").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(ResearchWorkflowError, "parent is unsafe"):
            self.workflow.issue_capability_certification(
                "batch-parent-link"
            )

        self.assertEqual(list(outside.iterdir()), [])
        batch = self.workflow.load("batch-parent-link")
        self.assertIsNone(batch["capability_certification"])

    def test_capability_parent_swap_cannot_redirect_atomic_write(self) -> None:
        self._passed_uncertified_batch(batch_id="batch-parent-swap")
        batch_root = self.workflow.repository.object_directory(
            "batch-parent-swap"
        )
        outside = self.root / "outside-swap"
        outside.mkdir()
        original_replace = __import__("os").replace
        swapped = False

        def replace_after_swap(source, target, **keywords):
            nonlocal swapped
            if not swapped and target == "certificate.json":
                swapped = True
                (batch_root / "capability").rename(
                    batch_root / "capability-pinned"
                )
                (batch_root / "capability").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            return original_replace(source, target, **keywords)

        with patch(
            "skill_evolution.research_workflow.os.replace",
            side_effect=replace_after_swap,
        ):
            with self.assertRaisesRegex(
                ResearchWorkflowError,
                "parent changed during write",
            ):
                self.workflow.issue_capability_certification(
                    "batch-parent-swap"
                )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(
            (batch_root / "capability-pinned/certificate.json").is_file()
        )

    def test_missing_capability_certificate_is_rejected(self) -> None:
        certified = self._validated_batch(batch_id="batch-missing")
        binding = certified["capability_certification"]
        snapshot = (
            self.workflow.repository.object_directory("batch-missing")
            / binding["snapshot_path"]
        )
        snapshot.unlink()

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "missing or unsafe",
        ):
            self.workflow.load("batch-missing")

    def test_capability_identity_change_blocks_specialists(self) -> None:
        certified = self._validated_batch(batch_id="batch-changed")
        self.assertEqual(
            certified["capability_certification"]["mode"],
            "issued",
        )
        self.runtime.capability_revision = "fixture-v2"

        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "differs from its certificate",
        ):
            self.workflow.run_specialists("batch-changed")

        unchanged = self.workflow.load("batch-changed")
        self.assertEqual(unchanged["status"], "single_agent_validated")
        self.assertIsNone(unchanged["specialist_board_id"])

    def test_pi_command_version_and_args_changes_invalidate_certificate(
        self,
    ) -> None:
        self._validated_batch(batch_id="batch-pi-boundary")
        original = json.loads(json.dumps(self.runtime.pi_execution_identity))
        mutations = {
            "command": (
                lambda value: value["resolved_command"].append(
                    "changed-command"
                ),
                "exactly one direct executable entrypoint",
            ),
            "version": (
                lambda value: value.__setitem__(
                    "version", "fixture-pi-v2"
                ),
                "differs from its certificate",
            ),
            "extra_args": (
                lambda value: value["extra_args"].append("--offline"),
                "fixed research Pi policy",
            ),
        }
        for name, (mutate, expected) in mutations.items():
            with self.subTest(name=name):
                changed = json.loads(json.dumps(original))
                mutate(changed)
                self.runtime.pi_execution_identity = changed
                self.runtime.pi_execution_revision = str(changed["version"])
                prior_calls = len(self.runtime.calls)
                with self.assertRaisesRegex(
                    ResearchWorkflowError,
                    expected,
                ):
                    self.workflow.run_specialists("batch-pi-boundary")
                self.assertEqual(len(self.runtime.calls), prior_calls)
        self.runtime.pi_execution_identity = original
        self.runtime.pi_execution_revision = str(original["version"])

    def test_issued_capability_imports_once_into_full_readiness_batch(
        self,
    ) -> None:
        source_corpus = _corpus(
            self.root / "capability-source",
            objectives={"behavior_patterns", "recovery_success"},
        )
        source = self._validated_batch(
            corpus=source_corpus,
            batch_id="source-batch",
        )
        self.assertEqual(
            source["capability_certification"]["mode"],
            "issued",
        )

        self.workflow.prepare(
            _corpus(self.root / "capability-target"),
            batch_id="target-batch",
        )
        target = _run_harness(self.workflow, "target-batch")
        self.assertEqual(target["status"], "harness_validated")
        self.assertIsNone(target["validation_benchmark"])
        self.assertEqual(target["validation_cycles"], [])

        imported = self.workflow.import_capability_certification(
            "target-batch",
            source_batch_id="source-batch",
        )

        self.assertEqual(imported["status"], "single_agent_validated")
        self.assertEqual(imported["validation_cycles"], [])
        self.assertIsNone(imported["validation_benchmark"])
        self.assertEqual(
            imported["capability_certification"]["mode"],
            "imported",
        )
        self.assertEqual(
            imported["capability_certification"]["source_batch_id"],
            "source-batch",
        )

        completed = self.workflow.run_specialists("target-batch")

        self.assertEqual(completed["status"], "specialists_completed")
        board = self.workflow.specialist_board("target-batch")
        self.assertEqual(board["status"], "complete")

        self.workflow.prepare(
            _corpus(self.root / "capability-chain-target"),
            batch_id="chain-target",
        )
        _run_harness(self.workflow, "chain-target")
        with self.assertRaisesRegex(
            ResearchWorkflowError,
            "originally issued",
        ):
            self.workflow.import_capability_certification(
                "chain-target",
                source_batch_id="target-batch",
            )

    def test_failed_cycle_runs_both_attempts_and_repair_starts_new_cycle(
        self,
    ) -> None:
        runtime = FakeResearchRuntime(
            self.root / "failed-agent-runs",
            statuses={
                AgentRole.BEHAVIOR_PATTERN: [
                    "failed",
                    "succeeded",
                    "succeeded",
                    "succeeded",
                ]
            },
        )
        workflow = ResearchWorkflow(
            self.root / "failed-batches",
            orchestrator=MultiPiOrchestrator(
                runtime=runtime,
                specs=_specs(self.root / "failed-prompts"),
            ),
        )
        workflow.prepare(_corpus(self.root / "failed"), batch_id="batch-f")
        _run_harness(workflow, "batch-f")
        _freeze_benchmark(
            workflow,
            "batch-f",
            self.root / "failed-benchmark.json",
        )

        failed = workflow.run_single_agent_validation_cycle("batch-f")

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(len(failed["validation_cycles"][0]["attempts"]), 2)
        with self.assertRaisesRegex(ResearchWorkflowError, "repair_summary"):
            workflow.run_single_agent_validation_cycle("batch-f")

        repaired = workflow.run_single_agent_validation_cycle(
            "batch-f",
            repair_summary="Corrected the research protocol exposure.",
            repair_categories=["agent_exploration"],
        )

        self.assertEqual(len(repaired["validation_cycles"]), 2)
        self.assertEqual(repaired["validation_cycles"][0]["status"], "failed")
        self.assertEqual(
            repaired["validation_cycles"][1]["status"],
            "awaiting_review",
        )

    def test_missing_session_identity_invalidates_both_smoke_runs(self) -> None:
        runtime = FakeResearchRuntime(
            self.root / "identity-agent-runs",
            write_session_identity=False,
        )
        workflow = ResearchWorkflow(
            self.root / "identity-batches",
            orchestrator=MultiPiOrchestrator(
                runtime=runtime,
                specs=_specs(self.root / "identity-prompts"),
            ),
        )
        workflow.prepare(_corpus(self.root / "identity"), batch_id="batch-i")
        _run_harness(workflow, "batch-i")
        _freeze_benchmark(
            workflow,
            "batch-i",
            self.root / "identity-benchmark.json",
        )

        failed = workflow.run_single_agent_validation_cycle("batch-i")

        attempts = failed["validation_cycles"][0]["attempts"]
        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(
            [item["status"] for item in attempts],
            ["invalid_output", "invalid_output"],
        )
        self.assertTrue(
            all(
                item["error"]["type"]
                == "InvalidResearchSessionIdentity"
                for item in attempts
            )
        )

    def test_reused_session_identity_fails_independence_gate(self) -> None:
        runtime = FakeResearchRuntime(
            self.root / "reused-session-agent-runs",
            session_ids=["same-session", "same-session"],
        )
        workflow = ResearchWorkflow(
            self.root / "reused-session-batches",
            orchestrator=MultiPiOrchestrator(
                runtime=runtime,
                specs=_specs(self.root / "reused-session-prompts"),
            ),
        )
        workflow.prepare(
            _corpus(self.root / "reused-session"),
            batch_id="batch-r",
        )
        _run_harness(workflow, "batch-r")
        _freeze_benchmark(
            workflow,
            "batch-r",
            self.root / "reused-session-benchmark.json",
        )

        failed = workflow.run_single_agent_validation_cycle("batch-r")

        self.assertEqual(failed["status"], "failed")
        self.assertIn(
            "result_gate",
            failed["validation_cycles"][0]["failure_reasons"],
        )

    def test_any_false_human_check_fails_the_cycle(self) -> None:
        self.workflow.prepare(_corpus(self.root), batch_id="batch-1")
        self._harness("batch-1")
        running = self.workflow.run_single_agent_validation_cycle("batch-1")
        checks = self._passing_checks()
        checks["hidden_benchmark"] = False

        failed = self.workflow.review_single_agent_attempt(
            "batch-1",
            attempt_id=running["validation_cycles"][0]["attempts"][0][
                "attempt_id"
            ],
            reviewer="project-owner",
            checks=checks,
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            failed["failure"]["reasons"],
            ["agent_exploration"],
        )

    def test_specialists_keep_failures_and_retry_only_failed_role(self) -> None:
        runtime = FakeResearchRuntime(
            self.root / "specialist-agent-runs",
            statuses={
                AgentRole.BEHAVIOR_PATTERN: [
                    "succeeded",
                    "succeeded",
                    "succeeded",
                ],
                AgentRole.CONDITIONS_COVERAGE: ["failed", "succeeded"],
            },
        )
        workflow = ResearchWorkflow(
            self.root / "specialist-batches",
            orchestrator=MultiPiOrchestrator(
                runtime=runtime,
                specs=_specs(self.root / "specialist-prompts"),
            ),
        )
        workflow.prepare(
            _corpus(self.root / "specialists"),
            batch_id="batch-s",
        )
        _run_harness(workflow, "batch-s")
        _freeze_benchmark(
            workflow,
            "batch-s",
            self.root / "specialist-benchmark.json",
        )
        running = workflow.run_single_agent_validation_cycle("batch-s")
        for attempt in running["validation_cycles"][0]["attempts"]:
            workflow.review_single_agent_attempt(
                "batch-s",
                attempt_id=attempt["attempt_id"],
                reviewer="project-owner",
                checks=self._passing_checks(),
            )
        workflow.issue_capability_certification("batch-s")

        incomplete = workflow.run_specialists("batch-s")

        self.assertEqual(incomplete["status"], "specialists_incomplete")
        board = workflow.specialist_board("batch-s")
        self.assertEqual(
            [item["status"] for item in board["roles"]],
            ["succeeded", "failed", "succeeded", "succeeded"],
        )
        self.assertNotIn(
            AgentRole.SYNTHESIS,
            [call["role"] for call in runtime.calls],
        )
        with self.assertRaisesRegex(ResearchWorkflowError, "Successful"):
            workflow.retry_specialist(
                "batch-s",
                role=AgentRole.OUTCOME_CONSISTENCY,
            )

        completed = workflow.retry_specialist(
            "batch-s",
            role=AgentRole.CONDITIONS_COVERAGE,
        )

        self.assertEqual(completed["status"], "specialists_completed")
        board = workflow.specialist_board("batch-s")
        conditions = board["roles"][1]
        self.assertEqual(
            [item["status"] for item in conditions["attempts"]],
            ["failed", "succeeded"],
        )

    def test_incomplete_readiness_cannot_create_specialist_board(self) -> None:
        corpus = _corpus(
            self.root,
            objectives={"behavior_patterns", "recovery_success"},
        )
        validated = self._validated_batch(corpus=corpus)
        self.assertEqual(validated["status"], "single_agent_validated")

        with self.assertRaisesRegex(ResearchWorkflowError, "incomplete"):
            self.workflow.run_specialists("batch-1")

        unchanged = self.workflow.load("batch-1")
        self.assertEqual(unchanged["status"], "single_agent_validated")
        self.assertIsNone(unchanged["specialist_board_id"])

    def test_prompt_preflight_failure_does_not_start_agent_cycle(self) -> None:
        runtime = FakeResearchRuntime(
            self.root / "blocked-agent-runs",
            preflight_error=RuntimeError("prompt is proposed"),
        )
        workflow = ResearchWorkflow(
            self.root / "blocked-batches",
            orchestrator=MultiPiOrchestrator(
                runtime=runtime,
                specs=_specs(self.root / "blocked-prompts"),
            ),
        )
        workflow.prepare(_corpus(self.root / "blocked"), batch_id="batch-b")
        _run_harness(workflow, "batch-b")
        _freeze_benchmark(
            workflow,
            "batch-b",
            self.root / "blocked-benchmark.json",
        )

        with self.assertRaisesRegex(RuntimeError, "proposed"):
            workflow.run_single_agent_validation_cycle("batch-b")

        batch = workflow.load("batch-b")
        self.assertEqual(batch["status"], "harness_validated")
        self.assertEqual(batch["validation_cycles"], [])
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
