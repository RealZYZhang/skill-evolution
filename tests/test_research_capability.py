"""Tests for portable single-Agent research capability certification."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from skill_evolution.agents import AgentRole
from skill_evolution.research_capability import (
    RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA,
    RESEARCH_CAPABILITY_IDENTITY_SCHEMA,
    RESEARCH_IMPLEMENTATION_FILES,
    RESEARCH_OUTPUT_PATH,
    RESEARCH_TOOLS_PATH,
    ResearchCapabilityError,
    attest_pi_execution_identity,
    build_research_capability_certificate,
    build_research_capability_identity,
    build_research_execution_identity,
    file_sha256,
    fingerprint_research_implementation,
    pi_execution_environment,
    research_capability_certificate_digest,
    research_capability_execution_identity_digest,
    research_capability_identity_digest,
    research_execution_identity_digest,
    research_implementation_dependency_closure,
    validate_selected_pi_credential,
    validate_research_capability_certificate,
    validate_research_capability_identity,
    verify_pi_execution_identity_current,
)
from skill_evolution.research_sandbox import RESEARCH_SANDBOX_BACKEND


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64


def _sandbox_control_plane(
    executable: Path | None = None,
) -> dict[str, object]:
    executable = (executable or Path(sys.executable)).resolve()
    file_identity = {
        "path": str(executable),
        "bytes": executable.stat().st_size,
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    return {
        "schema": "research.docker_control_plane.v1",
        "resolved_command": [str(executable)],
        "executable": file_identity,
        "interpreters": [],
        "client": {
            "version": "27.0.0",
            "api_version": "1.46",
            "git_commit": "fixture-client",
            "go_version": "go1.22",
            "os": "darwin",
            "arch": "arm64",
            "build_time": "2026-08-14T00:00:00Z",
            "context": "fixture",
            "endpoint": "unix:///fixture/docker.sock",
        },
        "daemon": {
            "id": "fixture-engine-a",
            "version": "27.0.0",
            "api_version": "1.46",
            "min_api_version": "1.24",
            "git_commit": "fixture-server",
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


class ResearchCapabilityTests(unittest.TestCase):
    """A certificate binds code, protocol, runtime, and two reviewed smokes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for index, relative in enumerate(RESEARCH_IMPLEMENTATION_FILES):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f'"""Purpose: capability fixture {index}."""\n',
                encoding="utf-8",
            )
        fingerprint = fingerprint_research_implementation(self.root)
        digests = {
            item["path"]: item["sha256"] for item in fingerprint["files"]
        }
        self.limits = {
            "cpus": 1.0,
            "memory": "1g",
            "pids": 128,
            "open_files": 1024,
            "work_bytes": 64 * 1024 * 1024,
            "temporary_bytes": 64 * 1024 * 1024,
            "command_timeout_seconds": 120,
            "max_output_bytes": 256 * 1024,
            "max_tool_calls": 256,
            "max_concurrent_tool_calls": 1,
            "max_total_output_bytes": 16 * 1024 * 1024,
            "max_total_command_milliseconds": 30 * 60 * 1000,
        }
        pi_package = self.root / "fake-pi-package"
        pi_package.mkdir()
        (pi_package / "package.json").write_text(
            '{"name":"fake-pi","version":"0.81.1"}\n',
            encoding="utf-8",
        )
        self.pi = pi_package / "fake-pi"
        self.pi.write_text("#!/bin/sh\nprintf '0.81.1\\n'\n", encoding="utf-8")
        self.pi.chmod(0o755)
        self.pi_identity = attest_pi_execution_identity(
            [str(self.pi)],
            working_directory=self.root,
        )
        self.identity = build_research_capability_identity(
            repository_root=self.root,
            prompt_id="analysis.behavior-pattern-research",
            prompt_version="1",
            prompt_sha256=_A,
            harness_context_sha256=_B,
            harness_version="1",
            tool_schema_version="1",
            research_tools_sha256=digests[RESEARCH_TOOLS_PATH],
            research_output_sha256=digests[RESEARCH_OUTPUT_PATH],
            pi_execution_identity=self.pi_identity,
            model={
                "provider": "anthropic",
                "model": "claude-test",
                "thinking": "high",
            },
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image="python:3.11-slim",
            sandbox_image_id="sha256:" + _C,
            sandbox_limits=self.limits,
            sandbox_control_plane_identity=_sandbox_control_plane(),
        )
        self.smokes = [
            self._smoke(1, "run-one", "session-one", _A),
            self._smoke(2, "run-two", "session-two", _B),
        ]
        self.certificate = build_research_capability_certificate(
            source_batch_id="research-batch-one",
            source_corpus_sha256=_A,
            source_baseline_sha256=_B,
            identity=self.identity,
            hidden_benchmark_sha256=_D,
            smoke_runs=self.smokes,
            issued_at="2026-08-14T08:03:00+00:00",
            repository_root=self.root,
        )

    @staticmethod
    def _smoke(
        number: int,
        run_id: str,
        session_id: str,
        result_sha256: str,
    ) -> dict[str, object]:
        return {
            "run_id": run_id,
            "session_id": session_id,
            "result_sha256": result_sha256,
            "review": {
                "review_id": f"review-{number}",
                "status": "passed",
                "reviewer": "project-owner",
                "checks": {
                    "evidence": True,
                    "protocol": True,
                    "safety": True,
                    "hidden_benchmark": True,
                },
                "benchmark_sha256": _D,
                "reviewed_at": f"2026-08-14T08:0{number}:00Z",
            },
        }

    def test_fingerprint_has_the_fixed_complete_file_boundary(self) -> None:
        first = fingerprint_research_implementation(self.root)
        second = fingerprint_research_implementation(self.root)

        self.assertEqual(first, second)
        self.assertEqual(
            [item["path"] for item in first["files"]],
            list(RESEARCH_IMPLEMENTATION_FILES),
        )
        self.assertEqual(
            first["files"][0]["sha256"],
            file_sha256(self.root / RESEARCH_IMPLEMENTATION_FILES[0]),
        )

    def test_repository_dependency_closure_is_fully_fingerprinted(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        closure = set(research_implementation_dependency_closure(repository))

        self.assertTrue(closure.issubset(RESEARCH_IMPLEMENTATION_FILES))
        self.assertIn("scripts/pi_rpc.py", closure)
        self.assertIn("scripts/trajectory_spike.py", closure)

    def test_ast_closure_rejects_an_omitted_first_party_dependency(self) -> None:
        omitted = self.root / "skill_evolution/omitted_dependency.py"
        omitted.write_text(
            '"""Purpose: dependency omitted from the fixed boundary."""\n',
            encoding="utf-8",
        )
        importer = self.root / "skill_evolution/research_agent_runtime.py"
        importer.write_text(
            "from skill_evolution import omitted_dependency\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "omits first-party imports",
        ):
            fingerprint_research_implementation(self.root)

    def test_identity_binds_the_behavior_agent_and_approved_boundaries(self) -> None:
        validated = validate_research_capability_identity(
            self.identity,
            repository_root=self.root,
        )

        self.assertEqual(validated["schema"], RESEARCH_CAPABILITY_IDENTITY_SCHEMA)
        self.assertEqual(
            validated["role"],
            AgentRole.BEHAVIOR_PATTERN.value,
        )
        self.assertEqual(validated["prompt"]["status"], "approved")
        self.assertEqual(validated["harness"]["status"], "approved")
        self.assertEqual(validated["sandbox"]["image_id"], "sha256:" + _C)
        self.assertEqual(validated["pi"]["version"], "0.81.1")

    def test_identity_digest_is_deterministic(self) -> None:
        first = research_capability_identity_digest(
            self.identity,
            repository_root=self.root,
        )
        second = research_capability_identity_digest(deepcopy(self.identity))

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_current_code_change_invalidates_identity(self) -> None:
        changed = self.root / RESEARCH_IMPLEMENTATION_FILES[2]
        changed.write_text(
            '"""Changed implementation."""\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "implementation changed",
        ):
            validate_research_capability_identity(
                self.identity,
                repository_root=self.root,
            )

    def test_transitive_rpc_dependency_change_invalidates_identity(self) -> None:
        changed = self.root / "scripts/pi_rpc.py"
        changed.write_text(
            '"""Changed Pi RPC transport."""\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "implementation changed",
        ):
            validate_research_capability_identity(
                self.identity,
                repository_root=self.root,
            )

    def test_pi_command_version_and_extra_args_change_execution_identity(
        self,
    ) -> None:
        base = research_capability_execution_identity_digest(
            self.identity,
            repository_root=self.root,
        )
        changes = []
        for field, value in (
            ("version", "0.82.0"),
            ("extra_args", ["--verbose"]),
        ):
            changed = deepcopy(self.identity)
            changed["pi"][field] = value
            changes.append(
                research_capability_execution_identity_digest(
                    changed,
                    repository_root=self.root,
                )
            )

        self.assertTrue(all(item != base for item in changes))
        self.assertEqual(len(set(changes)), len(changes))

    def test_pi_executable_change_invalidates_certified_identity(self) -> None:
        self.pi.write_text(
            "#!/bin/sh\nprintf '0.82.0\\n'\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "Pi executable changed",
        ):
            validate_research_capability_identity(
                self.identity,
                repository_root=self.root,
            )

    def test_pi_interpreter_and_package_tree_are_bound(self) -> None:
        package = self.root / "pi-package"
        executable = package / "dist/pi.js"
        executable.parent.mkdir(parents=True)
        (package / "package.json").write_text(
            '{"name":"fixture-pi","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        dependency = package / "dist/dependency.js"
        dependency.write_text("export const value = 1;\n", encoding="utf-8")
        executable.write_text(
            "#!/bin/sh\nprintf '1.0.0\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        pi_identity = attest_pi_execution_identity(
            [str(executable)],
            working_directory=self.root,
        )
        changed = deepcopy(self.identity)
        changed["pi"] = pi_identity

        validated = validate_research_capability_identity(
            changed,
            repository_root=self.root,
        )
        self.assertGreaterEqual(len(validated["pi"]["interpreters"]), 1)
        self.assertEqual(len(validated["pi"]["packages"]), 1)

        dependency.write_text("export const value = 2;\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "package tree changed",
        ):
            validate_research_capability_identity(
                changed,
                repository_root=self.root,
            )

    def test_external_node_module_link_target_is_bound_recursively(self) -> None:
        package = self.root / "linked-pi-package"
        executable = package / "dist/pi.js"
        executable.parent.mkdir(parents=True)
        (package / "package.json").write_text(
            '{"name":"linked-pi","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        executable.write_text(
            "#!/bin/sh\nprintf '1.0.0\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        dependency = self.root / "package-store/dependency"
        dependency.mkdir(parents=True)
        (dependency / "package.json").write_text(
            '{"name":"dependency","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        dependency_source = dependency / "index.js"
        dependency_source.write_text(
            "export const value = 1;\n",
            encoding="utf-8",
        )
        node_modules = package / "node_modules"
        node_modules.mkdir()
        (node_modules / "dependency").symlink_to(
            dependency,
            target_is_directory=True,
        )
        bin_directory = node_modules / ".bin"
        bin_directory.mkdir()
        (bin_directory / "dependency").symlink_to(
            "../dependency/index.js"
        )
        dependency_modules = dependency / "node_modules"
        dependency_modules.mkdir()
        (dependency_modules / "linked-pi").symlink_to(
            package,
            target_is_directory=True,
        )
        pi_identity = attest_pi_execution_identity(
            [str(executable)],
            working_directory=self.root,
        )
        changed = deepcopy(self.identity)
        changed["pi"] = pi_identity

        validated = validate_research_capability_identity(
            changed,
            repository_root=self.root,
        )
        self.assertEqual(
            [item["root"] for item in validated["pi"]["packages"]],
            sorted((str(package.resolve()), str(dependency.resolve()))),
        )

        dependency_source.write_text(
            "export const value = 2;\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "package tree changed",
        ):
            validate_research_capability_identity(
                changed,
                repository_root=self.root,
            )

    def test_unbound_external_package_symlink_is_rejected(self) -> None:
        package = self.root / "unsafe-link-package"
        executable = package / "dist/pi"
        executable.parent.mkdir(parents=True)
        (package / "package.json").write_text(
            '{"name":"unsafe-link","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        executable.write_text(
            "#!/bin/sh\nprintf '1.0.0\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        external = self.root / "external/runtime.txt"
        external.parent.mkdir()
        external.write_text("runtime-v1\n", encoding="utf-8")
        (package / "runtime.txt").symlink_to(external)

        for content in ("runtime-v1\n", "runtime-v2\n"):
            external.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                with self.assertRaisesRegex(
                    ResearchCapabilityError,
                    "unbound external target",
                ):
                    attest_pi_execution_identity(
                        [str(executable)],
                        working_directory=self.root,
                    )

    def test_python_code_and_module_launchers_are_rejected(self) -> None:
        (self.root / "fake_pi.py").write_text(
            "print('1.0.0')\n",
            encoding="utf-8",
        )
        for command in (
            [sys.executable, "-m", "fake_pi"],
            [sys.executable, "-c", "import fake_pi"],
        ):
            with self.subTest(command=command[1]):
                with self.assertRaisesRegex(
                    ResearchCapabilityError,
                    "exactly one direct executable entrypoint",
                ):
                    attest_pi_execution_identity(
                        command,
                        working_directory=self.root,
                    )

    def test_direct_interpreted_entrypoints_bind_their_interpreter(self) -> None:
        entries: list[tuple[str, str]] = [
            (
                "python",
                f"#!{sys.executable}\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('0.81.1')\n",
            )
        ]
        node = shutil.which("node")
        if node is not None:
            entries.append(
                (
                    "node",
                    f"#!{Path(node).resolve()}\n"
                    "if (process.argv.includes('--version')) {\n"
                    "  console.log('0.81.1');\n"
                    "}\n",
                )
            )
        for name, source in entries:
            with self.subTest(name=name):
                package = self.root / f"direct-{name}-package"
                package.mkdir()
                (package / "package.json").write_text(
                    json.dumps(
                        {"name": f"direct-{name}", "version": "0.81.1"}
                    ),
                    encoding="utf-8",
                )
                entrypoint = package / f"pi-{name}"
                entrypoint.write_text(source, encoding="utf-8")
                entrypoint.chmod(0o755)

                identity = attest_pi_execution_identity(
                    [str(entrypoint)],
                    working_directory=self.root,
                )

                self.assertEqual(
                    identity["resolved_command"],
                    [str(entrypoint.resolve())],
                )
                self.assertEqual(len(identity["command_files"]), 1)
                self.assertGreaterEqual(len(identity["interpreters"]), 1)

        secondary_commands = [
            ["/usr/bin/env", "PATH=/tmp", str(self.pi)],
            [sys.executable, str(self.pi)],
        ]
        if node is not None:
            secondary_commands.append([node, str(self.pi)])
        for command in secondary_commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    ResearchCapabilityError,
                    "exactly one direct executable entrypoint",
                ):
                    attest_pi_execution_identity(
                        command,
                        working_directory=self.root,
                    )

    def test_env_shebang_target_and_spawn_path_are_certified(self) -> None:
        package = self.root / "env-shebang-package"
        package.mkdir()
        (package / "package.json").write_text(
            '{"name":"env-shebang","version":"0.81.1"}\n',
            encoding="utf-8",
        )
        certified_directory = self.root / "certified-bin"
        certified_directory.mkdir()
        certified_interpreter = certified_directory / "fixture-runtime"
        certified_interpreter.write_text(
            "#!/bin/sh\nprintf '0.81.1\\n'\n",
            encoding="utf-8",
        )
        certified_interpreter.chmod(0o755)
        entrypoint = package / "pi"
        entrypoint.write_text(
            "#!/usr/bin/env fixture-runtime\n",
            encoding="utf-8",
        )
        entrypoint.chmod(0o755)

        with patch.dict(
            os.environ,
            {"PATH": str(certified_directory)},
            clear=False,
        ):
            identity = attest_pi_execution_identity(
                [str(entrypoint)],
                working_directory=self.root,
            )

        hostile_directory = self.root / "hostile-bin"
        hostile_directory.mkdir()
        hostile_marker = self.root / "hostile-runtime-ran"
        hostile_interpreter = hostile_directory / "fixture-runtime"
        hostile_interpreter.write_text(
            "#!/bin/sh\n"
            f"touch {hostile_marker}\n"
            "printf 'forged\\n'\n",
            encoding="utf-8",
        )
        hostile_interpreter.chmod(0o755)
        with patch.dict(
            os.environ,
            {"PATH": str(hostile_directory)},
            clear=False,
        ):
            verified = verify_pi_execution_identity_current(identity)
            environment = pi_execution_environment(
                verified,
                {"PATH": str(hostile_directory), "LANG": "C.UTF-8"},
            )

        self.assertEqual(
            environment["PATH"],
            str(certified_directory.resolve()),
        )
        result = subprocess.run(
            verified["resolved_command"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(result.stdout.strip(), "0.81.1")
        self.assertFalse(hostile_marker.exists())

    def test_only_selected_literal_credential_is_accepted(self) -> None:
        agent_directory = self.root / "pi-agent"
        agent_directory.mkdir()
        marker = self.root / "unrelated-command-ran"
        auth_path = agent_directory / "auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "selected": {
                        "type": "api_key",
                        "key": "literal-fixture-key",
                    },
                    "unrelated": {
                        "type": "api_key",
                        "key": f"!touch {marker}",
                    },
                }
            ),
            encoding="utf-8",
        )

        observed = validate_selected_pi_credential(
            agent_directory,
            provider="selected",
        )

        self.assertEqual(
            observed,
            {"provider": "selected", "kind": "literal_api_key"},
        )
        self.assertFalse(marker.exists())

        invalid_credentials = (
            {"type": "api_key", "key": "!printf unsafe"},
            {"type": "api_key", "key": "$SELECTED_API_KEY"},
            {"type": "oauth", "key": "literal"},
            {"type": "api_key", "key": "literal", "env": "KEY"},
        )
        for credential in invalid_credentials:
            with self.subTest(credential=credential):
                auth_path.write_text(
                    json.dumps({"selected": credential}),
                    encoding="utf-8",
                )
                with self.assertRaises(ResearchCapabilityError):
                    validate_selected_pi_credential(
                        agent_directory,
                        provider="selected",
                    )

    def test_credential_fifo_and_symlink_fail_without_blocking(self) -> None:
        agent_directory = self.root / "pi-agent-unsafe"
        agent_directory.mkdir()
        auth_path = agent_directory / "auth.json"
        os.mkfifo(auth_path)
        started = time.monotonic()

        with self.assertRaisesRegex(ResearchCapabilityError, "regular file"):
            validate_selected_pi_credential(
                agent_directory,
                provider="selected",
            )

        self.assertLess(time.monotonic() - started, 1.0)
        auth_path.unlink()
        target = self.root / "external-auth.json"
        target.write_text(
            '{"selected":{"type":"api_key","key":"literal"}}',
            encoding="utf-8",
        )
        auth_path.symlink_to(target)
        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "unavailable",
        ):
            validate_selected_pi_credential(
                agent_directory,
                provider="selected",
            )

    def test_pi_extra_args_cannot_override_research_policy_or_credentials(
        self,
    ) -> None:
        for arguments in (
            ["--provider", "other"],
            ["--model=other"],
            ["--extension", "unsafe.ts"],
            ["-e", "unsafe.ts"],
            ["--system-prompt", "override"],
            ["--skill", "unsafe"],
            ["-c"],
            ["-ns"],
            ["--session"],
            ["--api-key=secret"],
            ["--unknown-test-flag"],
            ["--offline"],
            ["--offline", "--offline"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ResearchCapabilityError):
                    attest_pi_execution_identity(
                        [str(self.pi)],
                        extra_pi_args=arguments,
                        working_directory=self.root,
                    )

        safe = attest_pi_execution_identity(
            [str(self.pi)],
            extra_pi_args=["--verbose"],
            working_directory=self.root,
        )
        self.assertEqual(safe["extra_args"], ["--verbose"])

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "exactly one direct executable entrypoint",
        ):
            attest_pi_execution_identity(
                [str(self.pi), "--provider=other"],
                working_directory=self.root,
            )

    def test_execution_identity_round_trip_matches_capability_projection(
        self,
    ) -> None:
        execution = build_research_execution_identity(
            repository_root=self.root,
            pi_execution_identity=self.pi_identity,
            harness_context_sha256=self.identity["harness"][
                "context_sha256"
            ],
            research_tools_sha256=self.identity["harness"][
                "research_tools_sha256"
            ],
            research_output_sha256=self.identity["harness"][
                "research_output_sha256"
            ],
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image="python:3.11-slim",
            sandbox_image_id="sha256:" + _C,
            sandbox_limits=self.limits,
            sandbox_control_plane_identity=_sandbox_control_plane(),
        )

        self.assertEqual(
            research_execution_identity_digest(
                execution,
                repository_root=self.root,
                verify_pi_executable=True,
            ),
            research_capability_execution_identity_digest(
                self.identity,
                repository_root=self.root,
            ),
        )

    def test_execution_digest_rejects_docker_client_file_drift(self) -> None:
        docker_client = self.root / "fixture-docker-client"
        shutil.copyfile(Path(sys.executable).resolve(), docker_client)
        execution = build_research_execution_identity(
            repository_root=self.root,
            pi_execution_identity=self.pi_identity,
            harness_context_sha256=self.identity["harness"][
                "context_sha256"
            ],
            research_tools_sha256=self.identity["harness"][
                "research_tools_sha256"
            ],
            research_output_sha256=self.identity["harness"][
                "research_output_sha256"
            ],
            sandbox_backend=RESEARCH_SANDBOX_BACKEND,
            sandbox_image="python:3.11-slim",
            sandbox_image_id="sha256:" + _C,
            sandbox_limits=self.limits,
            sandbox_control_plane_identity=_sandbox_control_plane(
                docker_client
            ),
        )
        with docker_client.open("ab") as stream:
            stream.write(b"changed-after-harness")

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "Docker client executable changed",
        ):
            research_execution_identity_digest(
                execution,
                repository_root=self.root,
                verify_pi_executable=True,
            )

    def test_extension_hash_must_match_implementation_fingerprint(self) -> None:
        changed = deepcopy(self.identity)
        changed["harness"]["research_tools_sha256"] = _A

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "tools differ",
        ):
            validate_research_capability_identity(changed)

    def test_identity_rejects_an_extra_or_changed_role_field(self) -> None:
        extra = deepcopy(self.identity)
        extra["unexpected"] = True
        with self.assertRaisesRegex(ResearchCapabilityError, "fields differ"):
            validate_research_capability_identity(extra)

        changed_role = deepcopy(self.identity)
        changed_role["role"] = AgentRole.RESOURCE_EFFICIENCY.value
        with self.assertRaisesRegex(ResearchCapabilityError, "behavior-pattern"):
            validate_research_capability_identity(changed_role)

    def test_certificate_round_trip_and_digest_are_deterministic(self) -> None:
        validated = validate_research_capability_certificate(
            self.certificate,
            repository_root=self.root,
        )
        first = research_capability_certificate_digest(
            validated,
            repository_root=self.root,
        )
        second = research_capability_certificate_digest(
            deepcopy(self.certificate)
        )

        self.assertEqual(
            validated["schema"],
            RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA,
        )
        self.assertEqual(validated["status"], "valid")
        self.assertEqual(len(validated["smoke_runs"]), 2)
        self.assertEqual(first, second)

    def test_identity_tamper_breaks_certificate_digest_binding(self) -> None:
        tampered = deepcopy(self.certificate)
        tampered["identity"]["model"]["thinking"] = "low"

        with self.assertRaisesRegex(
            ResearchCapabilityError,
            "identity digest does not match",
        ):
            validate_research_capability_certificate(tampered)

    def test_two_smokes_require_independent_run_session_and_review(self) -> None:
        for field, message in (
            ("run_id", "run_ids must be independent"),
            ("session_id", "session_ids must be independent"),
        ):
            with self.subTest(field=field):
                tampered = deepcopy(self.certificate)
                tampered["smoke_runs"][1][field] = tampered["smoke_runs"][0][
                    field
                ]
                with self.assertRaisesRegex(ResearchCapabilityError, message):
                    validate_research_capability_certificate(tampered)

        review = deepcopy(self.certificate)
        review["smoke_runs"][1]["review"]["review_id"] = "review-1"
        with self.assertRaisesRegex(ResearchCapabilityError, "reviews must"):
            validate_research_capability_certificate(review)

    def test_certificate_requires_exactly_two_smokes(self) -> None:
        for count in (1, 3):
            with self.subTest(count=count):
                tampered = deepcopy(self.certificate)
                if count == 1:
                    tampered["smoke_runs"] = tampered["smoke_runs"][:1]
                else:
                    third = deepcopy(tampered["smoke_runs"][1])
                    third["run_id"] = "run-three"
                    third["session_id"] = "session-three"
                    third["review"]["review_id"] = "review-three"
                    tampered["smoke_runs"].append(third)
                with self.assertRaisesRegex(ResearchCapabilityError, "exactly two"):
                    validate_research_capability_certificate(tampered)

    def test_failed_review_or_other_benchmark_is_rejected(self) -> None:
        failed = deepcopy(self.certificate)
        failed["smoke_runs"][0]["review"]["checks"]["evidence"] = False
        with self.assertRaisesRegex(ResearchCapabilityError, "must all pass"):
            validate_research_capability_certificate(failed)

        other_benchmark = deepcopy(self.certificate)
        other_benchmark["smoke_runs"][0]["review"][
            "benchmark_sha256"
        ] = _A
        with self.assertRaisesRegex(ResearchCapabilityError, "different hidden"):
            validate_research_capability_certificate(other_benchmark)

    def test_sha_image_and_timestamp_formats_are_strict(self) -> None:
        bad_result = deepcopy(self.certificate)
        bad_result["smoke_runs"][0]["result_sha256"] = "A" * 64
        with self.assertRaisesRegex(ResearchCapabilityError, "lowercase SHA-256"):
            validate_research_capability_certificate(bad_result)

        bad_timestamp = deepcopy(self.certificate)
        bad_timestamp["issued_at"] = "2026-08-14T08:00:00"
        with self.assertRaisesRegex(ResearchCapabilityError, "timezone"):
            validate_research_capability_certificate(bad_timestamp)

        bad_image = deepcopy(self.identity)
        bad_image["sandbox"]["image_id"] = "python:3.11-slim"
        with self.assertRaisesRegex(ResearchCapabilityError, "immutable SHA-256"):
            validate_research_capability_identity(bad_image)

    def test_symlinked_implementation_file_is_rejected(self) -> None:
        target = self.root / "target.py"
        target.write_text("target\n", encoding="utf-8")
        linked = self.root / RESEARCH_IMPLEMENTATION_FILES[0]
        linked.unlink()
        linked.symlink_to(target)

        with self.assertRaisesRegex(ResearchCapabilityError, "symlink"):
            fingerprint_research_implementation(self.root)


if __name__ == "__main__":
    unittest.main()
