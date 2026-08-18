"""Tests for executable, identity-bound multi-Trajectory Harness acceptance."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.trajectory_spike import TrajectoryJournal
from skill_evolution.agents import AgentRole, AgentSpec, ModelConfiguration
from skill_evolution.research_capability import (
    RESEARCH_EXECUTION_IDENTITY_SCHEMA,
    RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA,
    RESEARCH_PI_TOOL_ALLOWLIST,
    attest_pi_execution_identity,
    build_research_execution_identity,
)
from skill_evolution.research_agent_runtime import (
    RESEARCH_HARNESS_FAUX_MODEL,
    RESEARCH_HARNESS_FAUX_PROVIDER,
    ResearchAgentRuntimeError,
    ResearchPiAgentRuntime,
)
from skill_evolution.research_corpus import (
    BEHAVIOR_PATTERNS,
    ResearchCorpusBuilder,
    verify_research_corpus as verify_corpus,
)
from skill_evolution.research_harness_acceptance import (
    HARNESS_CHECKS,
    HARNESS_SUBCHECKS,
    HARNESS_VALIDATOR_VERSION,
    HarnessAcceptanceError,
    _ProbeResult,
    _inspect_active_container,
    _run_bounded_host_command,
    _verify_disabled_container_logs,
    _read_fd_bytes,
    _write_output_json,
    load_harness_acceptance_report,
    run_harness_acceptance,
    validate_harness_acceptance_report,
    verify_harness_acceptance_report,
)
from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxLimits,
    ResearchSandboxPreflightResult,
)
from tests.test_research_corpus import _fixture


_IMAGE_ID = "sha256:" + "a" * 64
_CONTAINER_ID = "b" * 64


def _public_acceptance_schema() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[1]
        / "contracts/schemas/research-harness-acceptance-v2.schema.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_errors(
    value: object,
    schema: dict[str, object],
    *,
    root: dict[str, object] | None = None,
    path: str = "$",
) -> list[str]:
    """Validate the public schema subset used by the acceptance contract."""

    root = root or schema
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/$defs/"):
            return [f"{path}: unsupported reference {reference}"]
        target: object = root
        for token in reference[2:].split("/"):
            if not isinstance(target, dict) or token not in target:
                return [f"{path}: unresolved reference {reference}"]
            target = target[token]
        if not isinstance(target, dict):
            return [f"{path}: invalid reference target {reference}"]
        return _schema_errors(value, target, root=root, path=path)

    errors: list[str] = []
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list):
        matches = [
            candidate
            for candidate in alternatives
            if isinstance(candidate, dict)
            and not _schema_errors(value, candidate, root=root, path=path)
        ]
        if len(matches) != 1:
            errors.append(f"{path}: expected exactly one schema match")
            return errors

    combined = schema.get("allOf")
    if isinstance(combined, list):
        for candidate in combined:
            if isinstance(candidate, dict):
                errors.extend(
                    _schema_errors(value, candidate, root=root, path=path)
                )

    condition = schema.get("if")
    if isinstance(condition, dict):
        matched = not _schema_errors(
            value, condition, root=root, path=path
        )
        branch = schema.get("then" if matched else "else")
        if isinstance(branch, dict):
            errors.extend(
                _schema_errors(value, branch, root=root, path=path)
            )

    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected_type, str) and not type_matches.get(
        expected_type, False
    ):
        return [f"{path}: expected {expected_type}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: value differs from const")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: value is outside enum")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path}: string does not match pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: number is below minimum")
        exclusive = schema.get("exclusiveMinimum")
        if isinstance(exclusive, (int, float)) and value <= exclusive:
            errors.append(f"{path}: number is below exclusive minimum")

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if name not in value:
                    errors.append(f"{path}: missing {name}")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, candidate in properties.items():
                if name in value and isinstance(candidate, dict):
                    errors.extend(
                        _schema_errors(
                            value[name],
                            candidate,
                            root=root,
                            path=f"{path}.{name}",
                        )
                    )
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(properties)
                if extra:
                    errors.append(
                        f"{path}: unexpected properties {sorted(extra)}"
                    )

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path}: array is too short")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path}: array is too long")
        if schema.get("uniqueItems") is True:
            canonical = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(canonical) != len(set(canonical)):
                errors.append(f"{path}: array items are not unique")
        prefix = schema.get("prefixItems")
        prefix_length = 0
        if isinstance(prefix, list):
            prefix_length = len(prefix)
            for index, candidate in enumerate(prefix[: len(value)]):
                if isinstance(candidate, dict):
                    errors.extend(
                        _schema_errors(
                            value[index],
                            candidate,
                            root=root,
                            path=f"{path}[{index}]",
                        )
                    )
        items = schema.get("items")
        if items is False and len(value) > prefix_length:
            errors.append(f"{path}: array has forbidden trailing items")
        elif isinstance(items, dict):
            start = prefix_length if isinstance(prefix, list) else 0
            for index in range(start, len(value)):
                errors.extend(
                    _schema_errors(
                        value[index],
                        items,
                        root=root,
                        path=f"{path}[{index}]",
                    )
                )
        contains = schema.get("contains")
        if isinstance(contains, dict):
            matches = sum(
                not _schema_errors(
                    item,
                    contains,
                    root=root,
                    path=f"{path}[{index}]",
                )
                for index, item in enumerate(value)
            )
            minimum_contains = schema.get("minContains", 1)
            if isinstance(minimum_contains, int) and matches < minimum_contains:
                errors.append(f"{path}: contains constraint failed")
    return errors


def _sandbox_control_plane() -> dict[str, object]:
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
            "id": "fixture-engine",
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


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_approved_harness_context(
    root: Path,
    *,
    tools: Path,
    output: Path,
) -> Path:
    path = root / "research-harness-context.json"
    value = {
        "schema": "prompt.research_harness_context.v1",
        "title": "Approved test Harness context",
        "version": "1",
        "tool_schema_version": "1",
        "prompt_visible_extensions": [
            {
                "name": "research_tools",
                "file": tools.name,
                "sha256": _sha256(tools),
            },
            {
                "name": "research_output",
                "file": output.name,
                "sha256": _sha256(output),
            },
        ],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    path.with_name(path.name + ".approval.json").write_text(
        json.dumps(
            {
                "schema": "prompt.approval.v1",
                "status": "approved",
                "prompt_id": "analysis.research-harness-context",
                "version": "1",
                "prompt_file": path.name,
                "content_sha256": hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                "approved_by": "test-owner",
                "approved_at": "2026-08-14T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_corpus(root: Path) -> tuple[Path, str, str, tuple[str, ...]]:
    runtime, execution_ids = _fixture(root)
    built = ResearchCorpusBuilder(runtime).build(
        skill_id="test-skill",
        execution_ids=execution_ids,
        objectives=[BEHAVIOR_PATTERNS],
        destination=root / "corpus",
    )
    return (
        built.directory,
        built.content_sha256,
        built.baseline_sha256,
        tuple(execution_ids),
    )


def _tool_environment(limits: ResearchSandboxLimits) -> dict[str, str]:
    return {
        "SKILL_EVOLUTION_RESEARCH_CONTAINER": _CONTAINER_ID,
        "SKILL_EVOLUTION_DOCKER_COMMAND": str(
            _sandbox_control_plane()["resolved_command"][0]
        ),
        "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS": str(
            limits.command_timeout_seconds * 1000
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES": str(
            limits.max_output_bytes
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS": str(limits.max_tool_calls),
        "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS": str(
            limits.max_concurrent_tool_calls
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES": str(
            limits.max_total_output_bytes
        ),
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS": str(
            limits.max_total_command_milliseconds
        ),
    }


def _sandbox_context(limits: ResearchSandboxLimits) -> dict:
    digest = {
        "sha256": "b" * 64,
        "file_count": 6,
        "directory_count": 3,
        "total_bytes": 100,
    }
    return {
        "backend": RESEARCH_SANDBOX_BACKEND,
        "container_id": _CONTAINER_ID,
        "image": "python:3.11-slim",
        "image_id": _IMAGE_ID,
        "control_plane_identity": _sandbox_control_plane(),
        "host_fallback_allowed": False,
        "network": "none",
        "root_filesystem": "read_only",
        "container_user": "65534:65534",
        "credentials_in_container": False,
        "mounts": {
            "evidence": {"container_path": "/evidence", "mode": "read_only"},
            "work": {"container_path": "/work", "mode": "read_write_tmpfs"},
        },
        "limits": limits.to_dict(),
        "evidence_digest_before": digest,
        "evidence_digest_after": None,
        "work_digest": None,
        "work_archive": None,
        "tool_environment": _tool_environment(limits),
    }


def _active_config(limits: ResearchSandboxLimits) -> dict:
    return {
        "image_id": _IMAGE_ID,
        "network_mode": "none",
        "network_count": 0,
        "log_driver": "none",
        "readonly_rootfs": True,
        "user": "65534:65534",
        "pids_limit": limits.pids,
        "nano_cpus": round(limits.cpus * 1_000_000_000),
        "memory_bytes": 1024**3,
        "nofile_soft": limits.open_files,
        "nofile_hard": limits.open_files,
        "cap_drop": ["ALL"],
        "security_options": ["no-new-privileges"],
        "tmpfs": {
            "/tmp": f"rw,noexec,nosuid,nodev,size={limits.temporary_bytes}",
            "/work": f"rw,nosuid,nodev,size={limits.work_bytes}",
        },
        "tool_budgets": {
            "command_timeout_milliseconds": (
                limits.command_timeout_seconds * 1000
            ),
            "max_output_bytes": limits.max_output_bytes,
            "max_tool_calls": limits.max_tool_calls,
            "max_concurrent_tool_calls": limits.max_concurrent_tool_calls,
            "max_total_output_bytes": limits.max_total_output_bytes,
            "max_total_command_milliseconds": (
                limits.max_total_command_milliseconds
            ),
        },
    }


class _FakeDockerSandbox(DockerResearchSandbox):
    def __init__(self, *, available: bool = True) -> None:
        super().__init__(
            docker_command="/fake/docker",
            limits=ResearchSandboxLimits(
                command_timeout_seconds=2,
                max_output_bytes=16384,
            ),
        )
        self.available = available
        self.isolated_calls = 0

    def preflight(self) -> ResearchSandboxPreflightResult:
        return ResearchSandboxPreflightResult(
            available=self.available,
            backend=RESEARCH_SANDBOX_BACKEND,
            detail="ready" if self.available else "daemon unavailable",
            image=self.image,
            image_id=_IMAGE_ID if self.available else None,
            control_plane_identity=(
                _sandbox_control_plane() if self.available else None
            ),
        )

    @contextmanager
    def isolated_run(
        self,
        *,
        evidence_directory,
        work_archive_directory,
        expected_evidence_digest,
        expected_control_plane_identity=None,
    ):
        del evidence_directory
        if expected_control_plane_identity != _sandbox_control_plane():
            raise AssertionError("Harness did not pin its Docker control plane")
        self.isolated_calls += 1
        context = _sandbox_context(self.limits)
        context["evidence_digest_before"] = (
            expected_evidence_digest.to_dict()
        )
        context["work_archive"] = str(work_archive_directory)
        yield context
        archive = Path(work_archive_directory)
        archive.mkdir()
        (archive / "cross-trajectory.json").write_text("{}", encoding="utf-8")
        context["evidence_digest_after"] = context["evidence_digest_before"]
        context["work_digest"] = {
            "sha256": "c" * 64,
            "file_count": 1,
            "directory_count": 0,
            "total_bytes": 2,
        }


def _probe_result(
    *,
    stdout: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
    output_limit_exceeded: bool = False,
) -> _ProbeResult:
    encoded = stdout.encode()
    return _ProbeResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
        stdout_bytes=len(encoded),
        stderr_bytes=0,
        stdout_sha256=hashlib.sha256(encoded).hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
    )


def _valid_submission(
    corpus_digest: str,
    baseline_digest: str,
    execution_ids: tuple[str, ...],
) -> dict:
    evidence = [
        {"schema": "evidence.ref.v1", "run_id": run_id, "seq": 1}
        for run_id in execution_ids
    ]
    return {
        "schema": "analysis.multi_trajectory_research.v1",
        "role": "outcome_consistency_analyst",
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
        "research_scope": {
            "eligible_trajectory_ids": list(execution_ids),
            "reviewed_trajectory_ids": list(execution_ids),
            "counterexample_search": "Every eligible Trajectory was inspected.",
        },
        "findings": [
            {
                "id": "deterministic-harness-finding",
                "subject": "Harness research loop",
                "pattern_type": "consistent_behavior",
                "claim": "Every Trajectory was reachable.",
                "eligible_trajectory_ids": list(execution_ids),
                "observed_trajectory_ids": list(execution_ids),
                "checked_absent_trajectory_ids": [],
                "logical_phase": "navigation",
                "shared_purpose": "prove evidence reachability",
                "observable_effect": "all locators resolved",
                "confidence": 1.0,
                "evidence": evidence,
                "counterevidence": [],
                "derivation_ids": ["harness-exec"],
                "limitations": ["Deterministic Harness-only finding."],
            }
        ],
        "limitations": [],
    }


def _journal_action(
    journal: TrajectoryJournal,
    call_id: str,
    tool_name: str,
    *,
    status: str = "succeeded",
    result=None,
    arguments=None,
) -> None:
    journal.append(
        source="pi_rpc",
        record_type="tool_action",
        payload={
            "tool_call_id": call_id,
            "tool_name": tool_name,
            "arguments": {} if arguments is None else arguments,
            "status": status,
            "result": {} if result is None else result,
            "started_at": "2026-08-14T04:00:00+00:00",
            "ended_at": "2026-08-14T04:00:00+00:00",
            "duration_ms": 1,
        },
    )


class HarnessAcceptanceTests(unittest.TestCase):
    """Only executed observations can create a passing acceptance report."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (
            self.corpus,
            self.corpus_digest,
            self.baseline_digest,
            self.execution_ids,
        ) = _write_corpus(self.root)
        self.bound_pi_identities: list[dict[str, object]] = []
        pi_package = self.root / "fixture-pi-package"
        pi_package.mkdir()
        (pi_package / "package.json").write_text(
            '{"name":"fixture-pi","version":"0.81.1"}\n',
            encoding="utf-8",
        )
        self.pi_entry = pi_package / "pi"
        self.pi_entry.write_text(
            "#!/bin/sh\nprintf '0.81.1\\n'\n",
            encoding="utf-8",
        )
        self.pi_entry.chmod(0o755)
        project_root = Path(__file__).resolve().parents[1]
        self.harness_context = _write_approved_harness_context(
            self.root,
            tools=project_root / "extensions/research-tools.ts",
            output=project_root / "extensions/research-output.ts",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _probe(self, calls: list[str]):
        def probe(_context, source, **_bounds):
            calls.append(source)
            if "sandbox-isolation" in source:
                facts = {
                    "evidence_readable": True,
                    "evidence_write_denied": True,
                    "root_write_denied": True,
                    "symlink_escape_denied": True,
                    "loopback_only": True,
                    "credential_environment_absent": True,
                    "secret_mount_absent": True,
                    "non_root": True,
                }
                return _probe_result(stdout=json.dumps(facts))
            if "acceptance-probe:timeout" in source:
                return _probe_result(exit_code=124, timed_out=True)
            if "acceptance-probe:output-limit" in source:
                return _probe_result(
                    stdout="x" * 4096,
                    exit_code=-15,
                    output_limit_exceeded=True,
                )
            raise AssertionError("Unexpected isolation probe")

        return probe

    def _drive(self, modes: list[str], *, corrupt_call: str | None = None):
        submission = _valid_submission(
            self.corpus_digest, self.baseline_digest, self.execution_ids
        )

        def drive(**arguments):
            mode = arguments["mode"]
            modes.append(mode)
            self.bound_pi_identities.append(
                arguments["research_execution_identity"]["pi"]
            )
            workspace = arguments["workspace"]
            workspace.mkdir()
            journal = arguments["journal"]
            journal.append(
                source="framework",
                record_type="pi_process_starting",
                payload={
                    "extensions": [
                        "research-tools.ts",
                        "research-output.ts",
                        "research-harness-driver.ts",
                    ]
                },
            )
            if mode == "positive":
                navigation = json.loads(
                    (self.corpus / "navigation-index.json").read_text(
                        encoding="utf-8"
                    )
                )
                first_run = self.execution_ids[0]
                trajectory_path = f"runs/{first_run}/trajectory.jsonl"
                raw_lines = (self.corpus / trajectory_path).read_text(
                    encoding="utf-8"
                ).splitlines()
                trajectory_records = [json.loads(line) for line in raw_lines]

                def tool_result(payload, operation):
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            }
                        ],
                        "details": {
                            "operation": operation,
                            "cleanupVerified": True,
                            "cleanupSnapshot": ["pid1", "cleanup"],
                            "cleanupObservedProcessCount": 2,
                            "cleanupResidualProcessCount": 0,
                            "cleanupRounds": 0,
                        },
                    }

                def observed(call_id, result):
                    if call_id == corrupt_call:
                        return {
                            "content": [{"type": "text", "text": "{}"}],
                            "details": result["details"],
                        }
                    return result

                search = {
                    "query": first_run,
                    "path": trajectory_path,
                    "matches": [
                        {
                            "path": trajectory_path,
                            "line": index,
                            "text": line[:4000],
                            "text_truncated": len(line) > 4000,
                        }
                        for index, line in enumerate(raw_lines[:2], start=1)
                    ],
                    "total_matches": len(raw_lines),
                    "cursor": 0,
                    "next_cursor": 2 if len(raw_lines) > 2 else None,
                    "truncated": len(raw_lines) > 2,
                    "skipped_binary_count": 0,
                    "skipped_binary_paths": [],
                    "skipped_binary_paths_truncated": False,
                }
                _journal_action(
                    journal,
                    "harness-search",
                    "research_search",
                    result=observed(
                        "harness-search", tool_result(search, "search")
                    ),
                )
                read = {
                    "path": trajectory_path,
                    "offset": 1,
                    "lines": [
                        {
                            "line": index,
                            "text": line[:20000],
                            "text_truncated": len(line) > 20000,
                        }
                        for index, line in enumerate(raw_lines[:1], start=1)
                    ],
                    "total_lines": len(raw_lines),
                    "next_offset": 2 if len(raw_lines) >= 2 else None,
                    "truncated": len(raw_lines) >= 2,
                }
                _journal_action(
                    journal,
                    "harness-read",
                    "research_read",
                    result=observed(
                        "harness-read", tool_result(read, "read_evidence")
                    ),
                )
                selected = [
                    (index, item)
                    for index, item in enumerate(
                        navigation["entries"], start=1
                    )
                    if item["run_id"] == first_run
                ]
                filtered = {
                    "path": "navigation-index.json",
                    "collection": "entries",
                    "records": [
                        {
                            "index_position": index,
                            "record": {
                                field: item[field]
                                for field in ("run_id", "seq", "flags")
                            },
                        }
                        for index, item in selected[:2]
                    ],
                    "total_matches": len(selected),
                    "cursor": 0,
                    "next_cursor": 2 if len(selected) > 2 else None,
                    "truncated": len(selected) > 2,
                }
                _journal_action(
                    journal,
                    "harness-filter",
                    "research_query",
                    result=observed(
                        "harness-filter", tool_result(filtered, "query")
                    ),
                )
                script_rows = navigation["scripts"]
                scripts = {
                    "path": "navigation-index.json",
                    "collection": "scripts",
                    "records": [
                        {
                            "index_position": index,
                            "record": {
                                field: item[field]
                                for field in (
                                    "run_id",
                                    "seq",
                                    "event",
                                    "path",
                                    "content_sha256",
                                )
                                if field in item
                            },
                        }
                        for index, item in enumerate(script_rows[:2], start=1)
                    ],
                    "total_matches": len(script_rows),
                    "cursor": 0,
                    "next_cursor": 2 if len(script_rows) > 2 else None,
                    "truncated": len(script_rows) > 2,
                }
                _journal_action(
                    journal,
                    "harness-scripts",
                    "research_query",
                    result=observed(
                        "harness-scripts", tool_result(scripts, "query")
                    ),
                )
                window_records = [
                    {"record": item, "truncated": False}
                    for item in trajectory_records
                    if item["seq"] == 1
                ]
                window = {
                    "run_id": first_run,
                    "target_seq": 1,
                    "before": 0,
                    "after": 0,
                    "records": window_records,
                }
                _journal_action(
                    journal,
                    "harness-window",
                    "research_trajectory_window",
                    result=observed(
                        "harness-window", tool_result(window, "trajectory_window")
                    ),
                )
                program = "# cross-trajectory.json\nprint('fixture')\n"
                encoded_program = program.encode()
                written = {
                    "path": "cross-trajectory.py",
                    "bytes": len(encoded_program),
                    "sha256": hashlib.sha256(encoded_program).hexdigest(),
                }
                _journal_action(
                    journal,
                    "harness-write",
                    "research_work_write",
                    result=observed(
                        "harness-write", tool_result(written, "write_work")
                    ),
                    arguments={"path": "cross-trajectory.py", "content": program},
                )
                derived = {"runs": sorted(self.execution_ids)}
                execution_payload = {
                    "stdout": json.dumps(derived) + "\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "aborted": False,
                    "output_limit_exceeded": False,
                }
                execution_result = tool_result(execution_payload, "exec")
                execution_result["details"].update(
                    {
                        "exitCode": 0,
                        "timedOut": False,
                        "aborted": False,
                        "outputLimitExceeded": False,
                    }
                )
                _journal_action(
                    journal,
                    "harness-exec",
                    "research_exec",
                    result=observed("harness-exec", execution_result),
                )
                work_read = {
                    "path": "cross-trajectory.json",
                    "offset": 1,
                    "lines": [
                        {
                            "line": 1,
                            "text": json.dumps(derived),
                            "text_truncated": False,
                        }
                    ],
                    "total_lines": 1,
                    "next_offset": None,
                    "truncated": False,
                }
                _journal_action(
                    journal,
                    "harness-work-read",
                    "research_work_read",
                    result=observed(
                        "harness-work-read",
                        tool_result(work_read, "read_work"),
                    ),
                )
                _journal_action(
                    journal,
                    "harness-submit",
                    "submit_multi_trajectory_research",
                )
                journal.append(
                    source="harness_driver",
                    record_type="harness_driver_attestation",
                    payload={
                        "schema": "research.harness_driver_attestation.v1",
                        "mode": "positive",
                        "callCount": 9,
                        "pendingResponses": 2,
                    },
                )
                return SimpleNamespace(
                    status="succeeded",
                    result=submission,
                    parse_failure=None,
                    error=None,
                )
            if mode == "budget":
                _journal_action(journal, "budget-search", "research_search")
                _journal_action(journal, "budget-read", "research_read")
                _journal_action(
                    journal,
                    "budget-must-fail",
                    "research_query",
                    status="failed",
                    result={"error": "Research tool-call budget exhausted"},
                )
                _journal_action(
                    journal,
                    "budget-submit",
                    "submit_multi_trajectory_research",
                )
                return SimpleNamespace(
                    status="succeeded",
                    result=submission,
                    parse_failure=None,
                    error=None,
                )
            if mode == "cleanup":
                def cleanup_result(**details):
                    return {
                        "content": [{"type": "text", "text": "{}"}],
                        "details": {
                            "exitCode": 0,
                            "timedOut": False,
                            "outputLimitExceeded": False,
                            "cleanupVerified": True,
                            "cleanupSnapshot": ["pid1", "cleanup"],
                            "cleanupObservedProcessCount": 2,
                            "cleanupResidualProcessCount": 0,
                            "cleanupRounds": 1,
                            **details,
                        },
                    }

                for index in range(1, 4):
                    _journal_action(
                        journal,
                        f"cleanup-timeout-{index}",
                        "research_exec",
                        status="failed",
                        result=cleanup_result(exitCode=124, timedOut=True),
                    )
                    _journal_action(
                        journal,
                        f"cleanup-timeout-verify-{index}",
                        "research_exec",
                        result=cleanup_result(cleanupRounds=0),
                    )
                _journal_action(
                    journal,
                    "cleanup-output",
                    "research_exec",
                    status="failed",
                    result=cleanup_result(outputLimitExceeded=True),
                )
                _journal_action(
                    journal,
                    "cleanup-output-verify",
                    "research_exec",
                    result=cleanup_result(),
                )
                _journal_action(
                    journal,
                    "cleanup-submit",
                    "submit_multi_trajectory_research",
                )
                return SimpleNamespace(
                    status="succeeded",
                    result=submission,
                    parse_failure=None,
                    error=None,
                )
            message = (
                "Research must make exactly one submission attempt"
                if mode == "duplicate_submission"
                else "The research submission must be the sole tool"
            )
            return SimpleNamespace(
                status="invalid_output",
                result=None,
                parse_failure={"message": message},
                error=None,
            )

        return drive

    def _run_passing(self, report: Path):
        sandbox = _FakeDockerSandbox()
        calls: list[str] = []
        modes: list[str] = []
        with (
            patch(
                "skill_evolution.research_harness_acceptance._probe_python",
                side_effect=self._probe(calls),
            ),
            patch(
                "skill_evolution.research_harness_acceptance."
                "_inspect_active_container",
                return_value=_active_config(sandbox.limits),
            ),
            patch(
                "skill_evolution.research_harness_acceptance."
                "_verify_disabled_container_logs",
                return_value={
                    "pid1_write_outcome": "pid1_fd_inaccessible",
                    "docker_logs_exit_code": 1,
                    "marker_absent": True,
                },
            ),
            patch.object(
                ResearchPiAgentRuntime,
                "drive_deterministic_harness",
                side_effect=self._drive(modes),
            ),
        ):
            verification = run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=report,
                trusted_output_root=self.root,
                expected_corpus_digest=self.corpus_digest,
                expected_baseline_digest=self.baseline_digest,
                pi_command=[str(self.pi_entry)],
                research_harness_context_path=self.harness_context,
            )
        return verification, sandbox, calls, modes

    def test_executes_all_fixed_checks_and_seals_a_passing_report(self) -> None:
        report = self.root / "acceptance.json"
        verification, sandbox, calls, modes = self._run_passing(report)

        self.assertTrue(verification.passed)
        self.assertEqual(
            _schema_errors(
                verification.report,
                _public_acceptance_schema(),
            ),
            [],
        )
        self.assertEqual(sandbox.isolated_calls, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            modes,
            [
                "positive",
                "budget",
                "cleanup",
                "duplicate_submission",
                "post_submission",
            ],
        )
        self.assertEqual(len(self.bound_pi_identities), 5)
        self.assertTrue(
            all(
                identity == verification.report["execution_identity"]["pi"]
                for identity in self.bound_pi_identities
            )
        )
        self.assertEqual(
            [item["name"] for item in verification.report["checks"]],
            list(HARNESS_CHECKS),
        )
        for check in verification.report["checks"]:
            self.assertEqual(
                [item["name"] for item in check["subchecks"]],
                list(HARNESS_SUBCHECKS[check["name"]]),
            )
            self.assertTrue(
                all(item["status"] == "passed" for item in check["subchecks"])
            )
        self.assertEqual(
            verification.report["validator_version"], HARNESS_VALIDATOR_VERSION
        )
        audit = self.root / verification.report["audit"]["directory"]
        self.assertTrue((audit / "manifest.json").is_file())
        limits_sha256 = hashlib.sha256(
            _canonical_bytes(sandbox.limits.to_dict())
        ).hexdigest()
        reloaded = verify_harness_acceptance_report(
            report,
            expected_file_sha256=verification.file_sha256,
            trusted_output_root=self.root,
            expected_corpus_digest=self.corpus_digest,
            expected_baseline_digest=self.baseline_digest,
            expected_image_id=_IMAGE_ID,
            expected_limits_sha256=limits_sha256,
            corpus_directory=self.corpus,
        )
        self.assertEqual(reloaded.file_sha256, verification.file_sha256)

    def test_empty_success_cannot_satisfy_any_research_tool_semantics(self) -> None:
        for call_id in (
            "harness-search",
            "harness-read",
            "harness-filter",
            "harness-scripts",
            "harness-window",
            "harness-write",
            "harness-exec",
            "harness-work-read",
        ):
            with self.subTest(call_id=call_id):
                report = self.root / f"{call_id}.json"
                sandbox = _FakeDockerSandbox()
                with (
                    patch(
                        "skill_evolution.research_harness_acceptance."
                        "_probe_python",
                        side_effect=self._probe([]),
                    ),
                    patch(
                        "skill_evolution.research_harness_acceptance."
                        "_inspect_active_container",
                        return_value=_active_config(sandbox.limits),
                    ),
                    patch(
                        "skill_evolution.research_harness_acceptance."
                        "_verify_disabled_container_logs",
                        return_value={
                            "pid1_write_outcome": "pid1_fd_inaccessible",
                            "docker_logs_exit_code": 1,
                            "marker_absent": True,
                        },
                    ),
                    patch.object(
                        ResearchPiAgentRuntime,
                        "drive_deterministic_harness",
                        side_effect=self._drive(
                            [], corrupt_call=call_id
                        ),
                    ),
                ):
                    verification = run_harness_acceptance(
                        corpus_directory=self.corpus,
                        sandbox=sandbox,
                        report_path=report,
                        trusted_output_root=self.root,
                        pi_command=[str(self.pi_entry)],
                        research_harness_context_path=self.harness_context,
                    )
                self.assertFalse(verification.passed)
                check = next(
                    item
                    for item in verification.report["checks"]
                    if item["name"] == "fake_agent_research_loop"
                )
                self.assertEqual(check["status"], "failed")

    def test_unavailable_docker_fails_closed_without_agent_or_probes(self) -> None:
        report = self.root / "unavailable.json"
        sandbox = _FakeDockerSandbox(available=False)
        with (
            patch(
                "skill_evolution.research_harness_acceptance._probe_python"
            ) as probe,
            patch.object(
                ResearchPiAgentRuntime, "drive_deterministic_harness"
            ) as drive,
        ):
            verification = run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=report,
                trusted_output_root=self.root,
            )

        self.assertFalse(verification.passed)
        self.assertEqual(
            _schema_errors(
                verification.report,
                _public_acceptance_schema(),
            ),
            [],
        )
        self.assertEqual(sandbox.isolated_calls, 0)
        probe.assert_not_called()
        drive.assert_not_called()
        with self.assertRaisesRegex(HarnessAcceptanceError, "did not pass"):
            load_harness_acceptance_report(report, require_passed=True)

    def test_report_and_audit_tampering_are_rejected(self) -> None:
        report = self.root / "acceptance.json"
        verification, _sandbox, _calls, _modes = self._run_passing(report)
        value = json.loads(report.read_text(encoding="utf-8"))
        value["checks"][0]["status"] = "failed"
        report.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(HarnessAcceptanceError, "digest"):
            load_harness_acceptance_report(report)

        report.write_text(
            json.dumps(verification.report, ensure_ascii=False), encoding="utf-8"
        )
        audit = self.root / verification.report["audit"]["directory"]
        snapshot = audit / "implementation/research-tools.ts"
        with snapshot.open("a", encoding="utf-8") as stream:
            stream.write("\n// tampered\n")
        with self.assertRaisesRegex(HarnessAcceptanceError, "audit|snapshot"):
            load_harness_acceptance_report(report)

    def test_passed_report_requires_corpus_baseline_and_active_config(self) -> None:
        report = self.root / "acceptance.json"
        verification, _sandbox, _calls, _modes = self._run_passing(report)
        for path, field in (
            (("corpus",), "content_sha256"),
            (("corpus",), "baseline_sha256"),
            (("sandbox",), "active_config"),
        ):
            with self.subTest(field=field):
                forged = json.loads(json.dumps(verification.report))
                forged[path[0]][field] = None
                if field == "active_config":
                    forged["sandbox"]["active_config_sha256"] = None
                body = {
                    key: item
                    for key, item in forged.items()
                    if key not in {"schema", "content_sha256"}
                }
                forged["content_sha256"] = hashlib.sha256(
                    _canonical_bytes(body)
                ).hexdigest()
                with self.assertRaises(HarnessAcceptanceError):
                    validate_harness_acceptance_report(forged)

    def test_implementation_summary_must_match_execution_identity(self) -> None:
        report = self.root / "acceptance.json"
        verification, _sandbox, _calls, _modes = self._run_passing(report)

        for field in (
            "validator_sha256",
            "runtime_sha256",
            "research_tools_sha256",
            "research_output_sha256",
            "driver_sha256",
        ):
            with self.subTest(field=field):
                forged = json.loads(json.dumps(verification.report))
                forged["implementation"][field] = "0" * 64
                body = {
                    key: item
                    for key, item in forged.items()
                    if key not in {"schema", "content_sha256"}
                }
                forged["content_sha256"] = hashlib.sha256(
                    _canonical_bytes(body)
                ).hexdigest()
                with self.assertRaisesRegex(
                    HarnessAcceptanceError,
                    "summary differs|toolchain differs",
                ):
                    validate_harness_acceptance_report(forged)

    def test_v2_schema_and_python_validator_share_fixed_subchecks(self) -> None:
        schema = _public_acceptance_schema()
        definition_names = (
            "corpusPreflight",
            "navigationIndex",
            "evidenceRoundtrip",
            "fakeAgentResearchLoop",
            "sandboxIsolation",
            "resourceLimits",
            "structuredSubmission",
        )
        observed_checks: list[str] = []
        observed_subchecks: dict[str, tuple[str, ...]] = {}
        for definition_name in definition_names:
            properties = schema["$defs"][definition_name]["properties"]
            check_name = properties["name"]["const"]
            observed_checks.append(check_name)
            observed_subchecks[check_name] = tuple(
                item["properties"]["name"]["const"]
                for item in properties["subchecks"]["prefixItems"]
            )
            self.assertEqual(
                properties["evidence"]["minItems"],
                len(HARNESS_SUBCHECKS[check_name]),
            )
            self.assertEqual(
                properties["evidence"]["maxItems"],
                len(HARNESS_SUBCHECKS[check_name]),
            )
        self.assertEqual(observed_checks, list(HARNESS_CHECKS))
        self.assertEqual(observed_subchecks, HARNESS_SUBCHECKS)
        pi_identity = attest_pi_execution_identity(
            [str(self.pi_entry)], working_directory=self.root
        )
        self.assertEqual(
            set(schema["$defs"]["piExecutionIdentity"]["required"]),
            set(pi_identity),
        )
        self.assertEqual(
            set(schema["$defs"]["piPackage"]["required"]),
            {"root", "package_json_sha256", "files", "bytes", "tree_sha256"},
        )
        extra_args = schema["$defs"]["piExecutionIdentity"]["properties"][
            "extra_args"
        ]
        self.assertTrue(extra_args["uniqueItems"])
        self.assertEqual(extra_args["items"], {"const": "--verbose"})
        self.assertEqual(
            schema["$defs"]["piExecutionIdentity"]["properties"][
                "schema"
            ]["const"],
            RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA,
        )
        self.assertEqual(
            schema["$defs"]["executionIdentity"]["properties"][
                "schema"
            ]["const"],
            RESEARCH_EXECUTION_IDENTITY_SCHEMA,
        )
        rpc_policy = schema["$defs"]["piExecutionIdentity"][
            "properties"
        ]["rpc_policy"]
        self.assertEqual(
            set(rpc_policy["required"]),
            set(pi_identity["rpc_policy"]),
        )
        self.assertEqual(
            [
                item["const"]
                for item in rpc_policy["properties"]["tool_allowlist"][
                    "prefixItems"
                ]
            ],
            list(RESEARCH_PI_TOOL_ALLOWLIST),
        )
        for name, value in pi_identity["rpc_policy"].items():
            if name != "tool_allowlist":
                self.assertEqual(
                    rpc_policy["properties"][name]["const"],
                    value,
                )
        pi_properties = schema["$defs"]["piExecutionIdentity"][
            "properties"
        ]
        for name in ("resolved_command", "command_files"):
            self.assertEqual(pi_properties[name]["minItems"], 1)
            self.assertEqual(pi_properties[name]["maxItems"], 1)
        self.assertEqual(pi_properties["packages"]["minItems"], 1)
        self.assertEqual(
            set(
                schema["$defs"]["executionIdentity"]["properties"][
                    "toolchain"
                ]["required"]
            ),
            {
                "harness_context_sha256",
                "research_tools_sha256",
                "research_output_sha256",
            },
        )
        control_plane = _sandbox_control_plane()
        control_schema = schema["$defs"]["dockerControlPlane"]
        self.assertEqual(
            set(control_schema["required"]),
            set(control_plane),
        )
        self.assertEqual(
            set(control_schema["properties"]["client"]["required"]),
            set(control_plane["client"]),
        )
        self.assertEqual(
            set(control_schema["properties"]["daemon"]["required"]),
            set(control_plane["daemon"]),
        )
        execution_sandbox = schema["$defs"]["executionIdentity"][
            "properties"
        ]["sandbox"]
        self.assertEqual(
            set(execution_sandbox["required"]),
            {"backend", "image", "image_id", "limits", "control_plane"},
        )
        self.assertEqual(
            schema["$defs"]["limits"]["properties"][
                "max_concurrent_tool_calls"
            ],
            {"const": 1},
        )
        security = schema["$defs"]["activeConfig"]["properties"][
            "security_options"
        ]
        self.assertEqual(
            security["contains"]["pattern"],
            "^no-new-privileges(?::true)?$",
        )
        self.assertEqual(
            schema["$defs"]["activeConfig"]["properties"]["log_driver"],
            {"const": "none"},
        )

    def test_validator_rejects_subcheck_and_privilege_forgery(self) -> None:
        report = self.root / "acceptance.json"
        verification, _sandbox, _calls, _modes = self._run_passing(report)

        for label, mutate in (
            (
                "subcheck order",
                lambda value: value["checks"][3]["subchecks"].reverse(),
            ),
            (
                "subcheck evidence",
                lambda value: value["checks"][3]["subchecks"][1].update(
                    {"evidence_sha256": "0" * 64}
                ),
            ),
            (
                "privilege spelling",
                lambda value: value["sandbox"]["active_config"].update(
                    {"security_options": ["no-new-privileges-bogus"]}
                ),
            ),
        ):
            with self.subTest(label=label):
                forged = json.loads(json.dumps(verification.report))
                mutate(forged)
                if label == "privilege spelling":
                    forged["sandbox"]["active_config_sha256"] = hashlib.sha256(
                        _canonical_bytes(
                            forged["sandbox"]["active_config"]
                        )
                    ).hexdigest()
                body = {
                    key: item
                    for key, item in forged.items()
                    if key not in {"schema", "content_sha256"}
                }
                forged["content_sha256"] = hashlib.sha256(
                    _canonical_bytes(body)
                ).hexdigest()
                with self.assertRaises(HarnessAcceptanceError):
                    validate_harness_acceptance_report(forged)

    def test_caller_cannot_supply_check_booleans(self) -> None:
        parameters = inspect.signature(run_harness_acceptance).parameters
        self.assertNotIn("checks", parameters)
        self.assertNotIn("passed", parameters)

    def test_report_parent_symlink_escape_is_rejected_before_acceptance(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        sandbox = _FakeDockerSandbox()

        with self.assertRaisesRegex(HarnessAcceptanceError, "symlink"):
            run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=linked / "acceptance.json",
                trusted_output_root=self.root,
            )

        self.assertFalse((outside / "acceptance.json").exists())
        self.assertEqual(sandbox.isolated_calls, 0)

    def test_trusted_output_root_symlink_is_rejected_without_writes(self) -> None:
        outside = self.root / "outside-root"
        outside.mkdir()
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        sandbox = _FakeDockerSandbox()

        with self.assertRaisesRegex(
            HarnessAcceptanceError,
            "trust root.*symlink|trust root.*real directory",
        ):
            run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=linked_root / "acceptance.json",
                trusted_output_root=linked_root,
            )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(sandbox.isolated_calls, 0)

    def test_replaced_deep_ancestor_cannot_redirect_report_write(self) -> None:
        trusted = self.root / "trusted/deep"
        trusted.mkdir(parents=True)
        displaced = self.root / "displaced-trusted"
        report = trusted / "reports/acceptance.json"

        def replace_ancestor(boundary, value):
            (self.root / "trusted").rename(displaced)
            (self.root / "trusted").symlink_to(
                displaced,
                target_is_directory=True,
            )
            return _write_output_json(boundary, value)

        with (
            patch(
                "skill_evolution.research_harness_acceptance."
                "_write_output_json",
                side_effect=replace_ancestor,
            ),
            self.assertRaisesRegex(HarnessAcceptanceError, "symlink|unsafe"),
        ):
            self._run_passing(report)

        self.assertFalse(
            (displaced / "deep/reports/acceptance.json").exists()
        )

    def test_existing_directory_below_symlink_ancestor_is_rejected(self) -> None:
        outside = self.root / "outside"
        nested = outside / "existing"
        nested.mkdir(parents=True)
        linked = self.root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        sandbox = _FakeDockerSandbox()

        with self.assertRaisesRegex(HarnessAcceptanceError, "symlink"):
            run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=linked / "existing/acceptance.json",
                trusted_output_root=self.root,
            )

        self.assertFalse((nested / "acceptance.json").exists())
        self.assertEqual(sandbox.isolated_calls, 0)

    def test_preexisting_audit_symlink_is_rejected_before_acceptance(self) -> None:
        outside = self.root / "outside-audit"
        outside.mkdir()
        (self.root / "acceptance.audit").symlink_to(
            outside, target_is_directory=True
        )
        sandbox = _FakeDockerSandbox()

        with self.assertRaisesRegex(HarnessAcceptanceError, "audit path"):
            run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=self.root / "acceptance.json",
                trusted_output_root=self.root,
            )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(sandbox.isolated_calls, 0)

    def test_replaced_audit_staging_directory_cannot_redirect_writes(self) -> None:
        outside = self.root / "outside-race"
        outside.mkdir()
        displaced = self.root / "displaced-audit"
        sandbox = _FakeDockerSandbox()

        def replace_staging(*arguments, **keywords):
            verified = verify_corpus(*arguments, **keywords)
            staging = next(self.root.glob(".acceptance.audit.*.staging"))
            staging.rename(displaced)
            staging.symlink_to(outside, target_is_directory=True)
            return verified

        with (
            patch(
                "skill_evolution.research_harness_acceptance."
                "verify_research_corpus",
                side_effect=replace_staging,
            ),
            self.assertRaisesRegex(HarnessAcceptanceError, "audit boundary"),
        ):
            run_harness_acceptance(
                corpus_directory=self.corpus,
                sandbox=sandbox,
                report_path=self.root / "acceptance.json",
                trusted_output_root=self.root,
                pi_command=[str(self.pi_entry)],
                research_harness_context_path=self.harness_context,
            )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(sandbox.isolated_calls, 0)

    def test_report_replacement_during_single_read_is_rejected(self) -> None:
        report = self.root / "acceptance.json"
        verification, *_ = self._run_passing(report)
        replacement = self.root / "replacement.json"
        replacement.write_bytes(report.read_bytes())
        replaced = False

        def replace_after_read(file_fd):
            nonlocal replaced
            content = _read_fd_bytes(file_fd)
            if not replaced:
                os.replace(replacement, report)
                replaced = True
            return content

        with (
            patch(
                "skill_evolution.research_harness_acceptance._read_fd_bytes",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(HarnessAcceptanceError, "changed while"),
        ):
            verify_harness_acceptance_report(
                report,
                expected_file_sha256=verification.file_sha256,
                trusted_output_root=self.root,
                corpus_directory=self.corpus,
                expected_corpus_digest=self.corpus_digest,
                expected_baseline_digest=self.baseline_digest,
            )
        self.assertTrue(replaced)

    def test_audit_manifest_replacement_during_read_is_rejected(self) -> None:
        report = self.root / "acceptance.json"
        verification, *_ = self._run_passing(report)
        audit = self.root / verification.report["audit"]["directory"]
        manifest = audit / "manifest.json"
        replacement = self.root / "replacement-manifest.json"
        replacement.write_bytes(manifest.read_bytes())
        read_count = 0

        def replace_second_file(file_fd):
            nonlocal read_count
            content = _read_fd_bytes(file_fd)
            read_count += 1
            if read_count == 2:
                os.replace(replacement, manifest)
            return content

        with (
            patch(
                "skill_evolution.research_harness_acceptance._read_fd_bytes",
                side_effect=replace_second_file,
            ),
            self.assertRaisesRegex(HarnessAcceptanceError, "changed while"),
        ):
            verify_harness_acceptance_report(
                report,
                expected_file_sha256=verification.file_sha256,
                trusted_output_root=self.root,
                corpus_directory=self.corpus,
                expected_corpus_digest=self.corpus_digest,
                expected_baseline_digest=self.baseline_digest,
            )
        self.assertEqual(read_count, 2)

    def test_audit_inventory_replacement_during_read_is_rejected(self) -> None:
        report = self.root / "acceptance.json"
        verification, *_ = self._run_passing(report)
        audit = self.root / verification.report["audit"]["directory"]
        command_plan = audit / "command-plan.json"
        replacement = self.root / "replacement-command-plan.json"
        replacement.write_bytes(command_plan.read_bytes())
        read_count = 0

        def replace_third_file(file_fd):
            nonlocal read_count
            content = _read_fd_bytes(file_fd)
            read_count += 1
            if read_count == 3:
                os.replace(replacement, command_plan)
            return content

        with (
            patch(
                "skill_evolution.research_harness_acceptance._read_fd_bytes",
                side_effect=replace_third_file,
            ),
            self.assertRaisesRegex(HarnessAcceptanceError, "changed while"),
        ):
            verify_harness_acceptance_report(
                report,
                expected_file_sha256=verification.file_sha256,
                trusted_output_root=self.root,
                corpus_directory=self.corpus,
                expected_corpus_digest=self.corpus_digest,
                expected_baseline_digest=self.baseline_digest,
            )
        self.assertEqual(read_count, 3)

    def test_active_docker_inspection_enforces_network_and_limits(self) -> None:
        limits = ResearchSandboxLimits()
        context = _sandbox_context(limits)

        def inspection() -> dict:
            return {
                "Image": _IMAGE_ID,
                "HostConfig": {
                    "NetworkMode": "none",
                    "LogConfig": {"Type": "none", "Config": {}},
                    "ReadonlyRootfs": True,
                    "PidsLimit": limits.pids,
                    "NanoCpus": round(limits.cpus * 1_000_000_000),
                    "Memory": 1024**3,
                    "Ulimits": [
                        {
                            "Name": "nofile",
                            "Soft": limits.open_files,
                            "Hard": limits.open_files,
                        }
                    ],
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "Tmpfs": {
                        "/tmp": (
                            "rw,noexec,nosuid,nodev,size="
                            f"{limits.temporary_bytes}"
                        ),
                        "/work": f"rw,nosuid,nodev,size={limits.work_bytes}",
                    },
                },
                "Config": {"User": "65534:65534"},
                "NetworkSettings": {"Networks": {}},
            }

        good = inspection()
        completed = subprocess.CompletedProcess(
            ["docker", "inspect"], 0, stdout=json.dumps([good]), stderr=""
        )
        with patch("subprocess.run", return_value=completed) as run:
            observed = _inspect_active_container(
                context,
                expected_image_id=_IMAGE_ID,
                expected_limits=limits.to_dict(),
            )
        self.assertEqual(observed["network_mode"], "none")
        self.assertFalse(run.call_args.kwargs.get("shell", False))

        for label, mutate in (
            (
                "network",
                lambda value: value["HostConfig"].update(
                    {"NetworkMode": "bridge"}
                ),
            ),
            (
                "memory",
                lambda value: value["HostConfig"].update({"Memory": 1}),
            ),
            (
                "log driver",
                lambda value: value["HostConfig"].update(
                    {"LogConfig": {"Type": "json-file", "Config": {}}}
                ),
            ),
            (
                "privileges",
                lambda value: value["HostConfig"].update({"CapDrop": []}),
            ),
        ):
            with self.subTest(label=label):
                bad = inspection()
                mutate(bad)
                completed = subprocess.CompletedProcess(
                    ["docker", "inspect"],
                    0,
                    stdout=json.dumps([bad]),
                    stderr="",
                )
                with (
                    patch("subprocess.run", return_value=completed),
                    self.assertRaises(HarnessAcceptanceError),
                ):
                    _inspect_active_container(
                        context,
                        expected_image_id=_IMAGE_ID,
                        expected_limits=limits.to_dict(),
                    )

    def test_pid1_output_has_no_readable_docker_log_sink(self) -> None:
        context = _sandbox_context(ResearchSandboxLimits())
        write_probe = _ProbeResult(
            exit_code=0,
            stdout=json.dumps(
                {"outcome": "write_discarded_by_disabled_log_driver"}
            ),
            stderr="",
            timed_out=False,
            output_limit_exceeded=False,
            stdout_bytes=32,
            stderr_bytes=0,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        )
        logs = _ProbeResult(
            exit_code=1,
            stdout="",
            stderr="logging driver does not support reading",
            timed_out=False,
            output_limit_exceeded=False,
            stdout_bytes=0,
            stderr_bytes=40,
            stdout_sha256="c" * 64,
            stderr_sha256="d" * 64,
        )

        with (
            patch(
                "skill_evolution.research_harness_acceptance._probe_python",
                return_value=write_probe,
            ),
            patch(
                "skill_evolution.research_harness_acceptance."
                "_run_bounded_host_command",
                return_value=logs,
            ) as run,
        ):
            observed = _verify_disabled_container_logs(context)

        self.assertTrue(observed["marker_absent"])
        self.assertEqual(run.call_args.args[0][1:4], ["logs", "--tail", "1"])

    def test_host_log_probe_enforces_output_while_process_is_running(self) -> None:
        observed = _run_bounded_host_command(
            [sys.executable, "-c", "print('x' * 1000000)"],
            timeout_seconds=5,
            max_output_bytes=4096,
        )

        self.assertTrue(observed.output_limit_exceeded)
        self.assertLessEqual(len(observed.stdout.encode("utf-8")), 4096)

    def test_deterministic_runtime_bridge_uses_production_extensions(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        sandbox = _FakeDockerSandbox()
        tools = repository / "extensions/research-tools.ts"
        output = repository / "extensions/research-output.ts"
        driver = repository / "extensions/research-harness-driver.ts"
        runtime = ResearchPiAgentRuntime(
            agent_runs_root=self.root / "runs",
            research_extension_path=tools,
            research_output_extension_path=output,
            research_harness_context_path=(
                repository
                / "prompts/analysis/research-harness-context-v1.json"
            ),
            sandbox=sandbox,
            model=ModelConfiguration(
                provider=RESEARCH_HARNESS_FAUX_PROVIDER,
                model=RESEARCH_HARNESS_FAUX_MODEL,
                thinking="off",
            ),
            repository_root=repository,
        )
        journal = TrajectoryJournal(self.root / "bridge.jsonl", "bridge")
        result = SimpleNamespace(status="succeeded")
        pi_identity = attest_pi_execution_identity(
            [str(self.pi_entry)],
            working_directory=self.root,
        )
        execution_identity = build_research_execution_identity(
            repository_root=repository,
            pi_execution_identity=pi_identity,
            harness_context_sha256="d" * 64,
            research_tools_sha256=hashlib.sha256(
                tools.read_bytes()
            ).hexdigest(),
            research_output_sha256=hashlib.sha256(
                output.read_bytes()
            ).hexdigest(),
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image=sandbox.image,
            sandbox_image_id=_IMAGE_ID,
            sandbox_limits=sandbox.limits.to_dict(),
            sandbox_control_plane_identity=_sandbox_control_plane(),
        )
        try:
            with patch.object(runtime, "_drive_pi", return_value=result) as drive:
                observed = runtime.drive_deterministic_harness(
                    workspace=self.root / "workspace",
                    evidence=self.corpus,
                    sandbox_context=_sandbox_context(sandbox.limits),
                    validation_context={
                        "corpus_digest": self.corpus_digest,
                        "baseline_digest": self.baseline_digest,
                        "eligible_trajectory_ids": list(self.execution_ids),
                    },
                    journal=journal,
                    driver_extension_path=driver,
                    mode="positive",
                    research_execution_identity=execution_identity,
                )
        finally:
            journal.close()
        self.assertIs(observed, result)
        arguments = drive.call_args.kwargs
        self.assertEqual(
            arguments["additional_extension_paths"],
            (driver,),
        )
        self.assertEqual(arguments["pi_execution_identity"], pi_identity)
        self.assertEqual(
            arguments["additional_pi_args"],
            (),
        )
        self.assertFalse(arguments["record_process_start"])

    def test_deterministic_bridge_rejects_unbound_sandbox_context(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        tools = repository / "extensions/research-tools.ts"
        output = repository / "extensions/research-output.ts"
        driver = repository / "extensions/research-harness-driver.ts"
        sandbox = _FakeDockerSandbox()
        runtime = ResearchPiAgentRuntime(
            agent_runs_root=self.root / "sandbox-runs",
            research_extension_path=tools,
            research_output_extension_path=output,
            research_harness_context_path=(
                repository
                / "prompts/analysis/research-harness-context-v1.json"
            ),
            sandbox=sandbox,
            model=ModelConfiguration(
                provider=RESEARCH_HARNESS_FAUX_PROVIDER,
                model=RESEARCH_HARNESS_FAUX_MODEL,
                thinking="off",
            ),
            repository_root=repository,
        )
        pi_identity = attest_pi_execution_identity(
            [str(self.pi_entry)],
            working_directory=self.root,
        )
        execution_identity = build_research_execution_identity(
            repository_root=repository,
            pi_execution_identity=pi_identity,
            harness_context_sha256="d" * 64,
            research_tools_sha256=_sha256(tools),
            research_output_sha256=_sha256(output),
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image=sandbox.image,
            sandbox_image_id=_IMAGE_ID,
            sandbox_limits=sandbox.limits.to_dict(),
            sandbox_control_plane_identity=_sandbox_control_plane(),
        )
        cases = []
        changed_command = _sandbox_context(sandbox.limits)
        changed_command["tool_environment"][
            "SKILL_EVOLUTION_DOCKER_COMMAND"
        ] = "/tmp/unbound-host-executable"
        cases.append(("docker-command", changed_command))
        changed_container = _sandbox_context(sandbox.limits)
        changed_container["container_id"] = "c" * 64
        cases.append(("container-id", changed_container))
        changed_limits = _sandbox_context(sandbox.limits)
        changed_limits["limits"]["max_tool_calls"] += 1
        cases.append(("limits", changed_limits))
        changed_budget = _sandbox_context(sandbox.limits)
        changed_budget["tool_environment"][
            "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES"
        ] = "999999"
        cases.append(("budget", changed_budget))

        for label, sandbox_context in cases:
            with self.subTest(label=label):
                workspace = self.root / f"unbound-sandbox-{label}"
                journal = TrajectoryJournal(
                    self.root / f"unbound-sandbox-{label}.jsonl",
                    f"unbound-sandbox-{label}",
                )
                try:
                    with (
                        patch.object(runtime, "_drive_pi") as drive,
                        patch(
                            "skill_evolution.research_agent_runtime."
                            "PiRpcClient.start"
                        ) as start,
                    ):
                        with self.assertRaises(ResearchAgentRuntimeError):
                            runtime.drive_deterministic_harness(
                                workspace=workspace,
                                evidence=self.corpus,
                                sandbox_context=sandbox_context,
                                validation_context={
                                    "corpus_digest": self.corpus_digest,
                                    "baseline_digest": self.baseline_digest,
                                    "eligible_trajectory_ids": list(
                                        self.execution_ids
                                    ),
                                },
                                journal=journal,
                                driver_extension_path=driver,
                                mode="positive",
                                research_execution_identity=(
                                    execution_identity
                                ),
                            )
                        drive.assert_not_called()
                        start.assert_not_called()
                finally:
                    journal.close()
                self.assertFalse(workspace.exists())

    def test_deterministic_bridge_rejects_unbound_host_extensions(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        tools = repository / "extensions/research-tools.ts"
        output = repository / "extensions/research-output.ts"
        driver = repository / "extensions/research-harness-driver.ts"
        sandbox = _FakeDockerSandbox()
        pi_identity = attest_pi_execution_identity(
            [str(self.pi_entry)],
            working_directory=self.root,
        )
        execution_identity = build_research_execution_identity(
            repository_root=repository,
            pi_execution_identity=pi_identity,
            harness_context_sha256="d" * 64,
            research_tools_sha256=hashlib.sha256(
                tools.read_bytes()
            ).hexdigest(),
            research_output_sha256=hashlib.sha256(
                output.read_bytes()
            ).hexdigest(),
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image=sandbox.image,
            sandbox_image_id=_IMAGE_ID,
            sandbox_limits=sandbox.limits.to_dict(),
            sandbox_control_plane_identity=_sandbox_control_plane(),
        )
        marker = self.root / "unbound-extension-ran"
        external = self.root / "external/research-harness-driver.ts"
        external.parent.mkdir()
        external.write_text(
            "import { writeFileSync } from 'node:fs';\n"
            f"writeFileSync({json.dumps(str(marker))}, 'ran');\n",
            encoding="utf-8",
        )
        linked = self.root / "linked-driver.ts"
        linked.symlink_to(driver)
        cases = (
            ("external-driver", tools, output, external),
            ("symlink-driver", tools, output, linked),
            (
                "relative-driver",
                tools,
                output,
                Path("extensions/research-harness-driver.ts"),
            ),
            ("external-tools", external, output, driver),
            ("external-output", tools, external, driver),
        )
        for name, selected_tools, selected_output, selected_driver in cases:
            with self.subTest(name=name):
                runtime = ResearchPiAgentRuntime(
                    agent_runs_root=self.root / f"runs-{name}",
                    research_extension_path=selected_tools,
                    research_output_extension_path=selected_output,
                    research_harness_context_path=(
                        repository
                        / "prompts/analysis/research-harness-context-v1.json"
                    ),
                    sandbox=sandbox,
                    model=ModelConfiguration(
                        provider=RESEARCH_HARNESS_FAUX_PROVIDER,
                        model=RESEARCH_HARNESS_FAUX_MODEL,
                        thinking="off",
                    ),
                    repository_root=repository,
                )
                workspace = self.root / f"workspace-{name}"
                journal = TrajectoryJournal(
                    self.root / f"unbound-{name}.jsonl",
                    f"unbound-{name}",
                )
                try:
                    with (
                        patch.object(runtime, "_drive_pi") as drive,
                        patch(
                            "skill_evolution.research_agent_runtime."
                            "PiRpcClient.start"
                        ) as start,
                    ):
                        with self.assertRaisesRegex(
                            ResearchAgentRuntimeError,
                            "fixed repository extensions",
                        ):
                            runtime.drive_deterministic_harness(
                                workspace=workspace,
                                evidence=self.corpus,
                                sandbox_context=_sandbox_context(
                                    sandbox.limits
                                ),
                                validation_context={
                                    "corpus_digest": self.corpus_digest,
                                    "baseline_digest": self.baseline_digest,
                                    "eligible_trajectory_ids": list(
                                        self.execution_ids
                                    ),
                                },
                                journal=journal,
                                driver_extension_path=selected_driver,
                                mode="positive",
                                research_execution_identity=(
                                    execution_identity
                                ),
                            )
                        drive.assert_not_called()
                        start.assert_not_called()
                finally:
                    journal.close()
                self.assertFalse(workspace.exists())
        self.assertFalse(marker.exists())

        runtime = ResearchPiAgentRuntime(
            agent_runs_root=self.root / "runs-private-bypass",
            research_extension_path=tools,
            research_output_extension_path=output,
            research_harness_context_path=(
                repository
                / "prompts/analysis/research-harness-context-v1.json"
            ),
            sandbox=sandbox,
            model=ModelConfiguration(
                provider=RESEARCH_HARNESS_FAUX_PROVIDER,
                model=RESEARCH_HARNESS_FAUX_MODEL,
                thinking="off",
            ),
            repository_root=repository,
        )
        extension_sha256 = {
            "research_tools": hashlib.sha256(tools.read_bytes()).hexdigest(),
            "research_output": hashlib.sha256(
                output.read_bytes()
            ).hexdigest(),
            "research_harness_driver": hashlib.sha256(
                driver.read_bytes()
            ).hexdigest(),
        }
        journal = TrajectoryJournal(
            self.root / "private-extension-bypass.jsonl",
            "private-extension-bypass",
        )
        try:
            with patch(
                "skill_evolution.research_agent_runtime.PiRpcClient.start"
            ) as start:
                with self.assertRaisesRegex(
                    ResearchAgentRuntimeError,
                    "fixed repository extensions",
                ):
                    runtime._drive_pi(
                        spec=AgentSpec(
                            role=AgentRole.OUTCOME_CONSISTENCY,
                            prompt_path=driver,
                        ),
                        agent_run_id="private-extension-bypass",
                        workspace=self.root / "private-workspace",
                        prompt_text="must not run",
                        runtime_environment={},
                        validation_context={},
                        evidence=self.corpus,
                        journal=journal,
                        research_extension_path=tools,
                        research_output_extension_path=output,
                        additional_extension_paths=(external,),
                        pi_execution_identity=pi_identity,
                        deterministic_extension_sha256=extension_sha256,
                        record_process_start=False,
                    )
                start.assert_not_called()
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()
