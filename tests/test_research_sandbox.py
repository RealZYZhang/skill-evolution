"""Tests for the fail-closed multi-Trajectory Docker research laboratory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import ANY, patch

from skill_evolution.research_sandbox import (
    DockerResearchSandbox,
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxError,
    ResearchSandboxLimits,
    ResearchSandboxPreflightResult,
    _read_fd_bytes,
    research_evidence_tree_digest,
    validate_research_sandbox_context,
)


def _result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _docker_version(*, engine_version: str = "27.0.0") -> str:
    return json.dumps(
        {
            "Client": {
                "Version": "27.0.0",
                "ApiVersion": "1.46",
                "GitCommit": "client",
                "GoVersion": "go1.22",
                "Os": "darwin",
                "Arch": "arm64",
                "BuildTime": "2026-08-14T00:00:00Z",
                "Context": "fixture",
            },
            "Server": {
                "Version": engine_version,
                "ApiVersion": "1.46",
                "MinAPIVersion": "1.24",
                "GitCommit": "server",
                "GoVersion": "go1.22",
                "Os": "linux",
                "Arch": "arm64",
                "BuildTime": "2026-08-14T00:00:00Z",
            },
        }
    )


def _docker_info(*, engine_id: str = "fixture-engine") -> str:
    return json.dumps(
        {
            "ID": engine_id,
            "ServerVersion": "27.0.0",
            "KernelVersion": "6.6.0",
            "OperatingSystem": "Fixture Linux",
            "OSVersion": "1",
            "OSType": "linux",
            "Architecture": "arm64",
            "SecurityOptions": ["name=seccomp"],
            "CgroupDriver": "cgroupfs",
            "CgroupVersion": "2",
            "Driver": "overlay2",
            "DefaultRuntime": "runc",
            "Isolation": "",
        }
    )


def _docker_context(name: str = "fixture") -> str:
    return name + "\n"


def _ready(image: str = "python:3.11-slim") -> ResearchSandboxPreflightResult:
    return ResearchSandboxPreflightResult(
        available=True,
        backend=RESEARCH_SANDBOX_BACKEND,
        detail="ready",
        image=image,
        image_id="sha256:" + "a" * 64,
    )


class ResearchSandboxPreflightTests(unittest.TestCase):
    """Preflight requires a daemon and one already-present immutable image."""

    @patch("skill_evolution.research_sandbox.shutil.which", return_value=None)
    def test_missing_docker_is_unavailable(self, _which) -> None:
        sandbox = DockerResearchSandbox()

        result = sandbox.preflight()

        self.assertFalse(result.available)
        self.assertIn("not installed", result.detail)

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_missing_local_image_fails_without_pull(self, run) -> None:
        run.side_effect = [
            _result(stdout=_docker_context()),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info()),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context()),
            _result(returncode=1, stderr="No such image"),
        ]
        sandbox = DockerResearchSandbox(docker_command=sys.executable)

        result = sandbox.preflight()

        self.assertFalse(result.available)
        self.assertIn("will not pull", result.detail)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[5][1:3], ["image", "inspect"])
        self.assertNotIn("pull", [item for command in commands for item in command])

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_success_records_immutable_image_id(self, run) -> None:
        run.side_effect = [
            _result(stdout=_docker_context()),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info()),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context()),
            _result(stdout='"sha256:' + "b" * 64 + '"\n'),
        ]
        sandbox = DockerResearchSandbox(docker_command=sys.executable)

        result = sandbox.preflight()

        self.assertTrue(result.available)
        self.assertEqual(result.image_id, "sha256:" + "b" * 64)
        self.assertEqual(
            result.control_plane_identity["daemon"]["id"],
            "fixture-engine",
        )
        self.assertEqual(
            result.control_plane_identity["client"]["context"],
            "fixture",
        )

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_preflight_rejects_a_daemon_without_a_stable_engine_id(self, run) -> None:
        run.side_effect = [
            _result(stdout=_docker_context()),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info(engine_id="")),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context()),
        ]
        sandbox = DockerResearchSandbox(docker_command=sys.executable)

        result = sandbox.preflight()

        self.assertFalse(result.available)
        self.assertIn("daemon id", result.detail.lower())

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_same_version_and_endpoint_cannot_hide_engine_drift(self, run) -> None:
        run.side_effect = [
            _result(stdout=_docker_context()),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info(engine_id="engine-a")),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context()),
            _result(stdout=_docker_context()),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info(engine_id="engine-b")),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context()),
        ]
        sandbox = DockerResearchSandbox(docker_command=sys.executable)
        accepted = sandbox.control_plane_identity()

        with self.assertRaisesRegex(
            ResearchSandboxError,
            "changed after Harness acceptance",
        ):
            sandbox.verify_control_plane_identity_current(accepted)

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_context_must_stay_stable_during_attestation(self, run) -> None:
        run.side_effect = [
            _result(stdout=_docker_context("context-a")),
            _result(stdout=_docker_version()),
            _result(stdout=_docker_info()),
            _result(stdout='"unix:///fixture/docker.sock"'),
            _result(stdout=_docker_context("context-b")),
        ]
        sandbox = DockerResearchSandbox(docker_command=sys.executable)

        with self.assertRaisesRegex(
            ResearchSandboxError,
            "context changed during",
        ):
            sandbox.control_plane_identity()

    def test_limits_reject_concurrent_container_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be serial"):
            ResearchSandboxLimits(max_concurrent_tool_calls=2)


class DockerResearchSandboxTests(unittest.TestCase):
    """A run exposes only frozen evidence and quota-bound temporary work."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        (self.evidence / "bundle.json").write_text(
            json.dumps({"schema": "evidence.bundle.v1"}),
            encoding="utf-8",
        )
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.work = self.attempt / "work"
        self.limits = ResearchSandboxLimits(
            cpus=0.5,
            memory="512m",
            pids=64,
            open_files=256,
            work_bytes=1024 * 1024,
            temporary_bytes=512 * 1024,
            command_timeout_seconds=15,
            max_output_bytes=32768,
        )
        self.sandbox = DockerResearchSandbox(
            docker_command="/usr/bin/docker",
            limits=self.limits,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _control_plane(self, exported: str = "derived\n"):
        calls: list[list[str]] = []

        def run(command, **_kwargs):
            calls.append(list(command))
            action = command[1]
            if action == "run":
                return _result(stdout="container-123\n")
            if action == "pause":
                return _result()
            if action == "cp":
                destination = Path(command[-1])
                (destination / "program.py").write_text(
                    "print('ok')\n", encoding="utf-8"
                )
                (destination / "derived.txt").write_text(
                    exported, encoding="utf-8"
                )
                return _result()
            if action == "rm":
                return _result()
            raise AssertionError(f"Unexpected Docker command: {command}")

        return calls, run

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_mounts_read_only_evidence_and_quota_bound_work(self, run) -> None:
        calls, control = self._control_plane()
        run.side_effect = control
        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.sandbox.isolated_run(
                evidence_directory=self.evidence,
                work_archive_directory=self.work,
                expected_evidence_digest=research_evidence_tree_digest(
                    self.evidence
                ),
            ) as context:
                self.assertFalse(self.work.exists())
                environment = validate_research_sandbox_context(context)
                self.assertEqual(
                    environment["SKILL_EVOLUTION_RESEARCH_CONTAINER"],
                    "container-123",
                )
                self.assertEqual(
                    environment[
                        "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS"
                    ],
                    "15000",
                )

        self.assertEqual(
            (self.work / "program.py").read_text(encoding="utf-8"),
            "print('ok')\n",
        )
        self.assertIsNotNone(context["evidence_digest_after"])
        self.assertEqual(
            context["evidence_digest_before"],
            context["evidence_digest_after"],
        )
        self.assertEqual(context["work_digest"]["file_count"], 2)

        start = calls[0]
        self.assertEqual(start[0:2], ["/usr/bin/docker", "run"])
        self.assertNotIn("--rm", start)
        self.assertEqual(start[start.index("--pull") + 1], "never")
        self.assertIn("sha256:" + "a" * 64, start)
        self.assertEqual(start[start.index("--network") + 1], "none")
        self.assertEqual(start[start.index("--log-driver") + 1], "none")
        self.assertIn("--read-only", start)
        self.assertEqual(start[start.index("--user") + 1], "65534:65534")
        self.assertEqual(start[start.index("--cpus") + 1], "0.5")
        self.assertEqual(start[start.index("--memory") + 1], "512m")
        self.assertEqual(start[start.index("--pids-limit") + 1], "64")
        self.assertIn("no-new-privileges", start)
        self.assertEqual(
            start[-3:],
            [
                "python3",
                "-c",
                ANY,
            ],
        )
        mount = start[start.index("--mount") + 1]
        self.assertEqual(
            mount,
            f"type=bind,src={self.evidence.resolve()},dst=/evidence,readonly",
        )
        tmpfs_values = [
            start[index + 1]
            for index, item in enumerate(start)
            if item == "--tmpfs"
        ]
        self.assertTrue(
            any(
                value.startswith("/work:rw,nosuid,nodev")
                and "size=1048576" in value
                for value in tmpfs_values
            )
        )
        self.assertEqual(calls[-1][1:3], ["rm", "--force"])

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_rejects_symlink_in_exported_work(self, run) -> None:
        calls: list[list[str]] = []

        def control(command, **_kwargs):
            calls.append(list(command))
            if command[1] == "run":
                return _result(stdout="container-123\n")
            if command[1] == "pause":
                return _result()
            if command[1] == "cp":
                destination = Path(command[-1])
                (destination / "target.txt").write_text(
                    "target", encoding="utf-8"
                )
                (destination / "linked.txt").symlink_to("target.txt")
                return _result()
            if command[1] == "rm":
                return _result()
            raise AssertionError(command)

        run.side_effect = control
        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.assertRaisesRegex(
                ResearchSandboxError, "symlink or special file"
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=research_evidence_tree_digest(
                        self.evidence
                    ),
                ):
                    pass

        self.assertFalse(self.work.exists())
        self.assertEqual(calls[-1][1:3], ["rm", "--force"])

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_detects_any_evidence_change_before_accepting_work(self, run) -> None:
        _calls, control = self._control_plane()
        run.side_effect = control
        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.assertRaisesRegex(
                ResearchSandboxError, "evidence changed"
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=research_evidence_tree_digest(
                        self.evidence
                    ),
                ):
                    (self.evidence / "bundle.json").write_text(
                        json.dumps({"changed": True}), encoding="utf-8"
                    )

        self.assertFalse(self.work.exists())

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_changed_evidence_is_rejected_before_docker_start(self, run) -> None:
        expected = research_evidence_tree_digest(self.evidence)
        (self.evidence / "bundle.json").write_text(
            json.dumps({"changed": True}),
            encoding="utf-8",
        )

        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.assertRaisesRegex(
                ResearchSandboxError,
                "changed after corpus verification",
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=expected,
                ):
                    pass

        run.assert_not_called()

    def test_digest_rejects_file_replacement_during_single_read(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        source = self.evidence / "bundle.json"
        replaced = False

        def replace_after_read(file_fd):
            nonlocal replaced
            content = _read_fd_bytes(file_fd)
            if not replaced:
                source.unlink()
                source.symlink_to(outside)
                replaced = True
            return content

        with (
            patch(
                "skill_evolution.research_sandbox._read_fd_bytes",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(
                ResearchSandboxError,
                "changed while hashing",
            ),
        ):
            research_evidence_tree_digest(self.evidence)
        self.assertTrue(replaced)

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_rejects_symlink_in_source_evidence_before_docker(self, run) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.evidence / "linked.txt").symlink_to(outside)
        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.assertRaisesRegex(
                ResearchSandboxError, "symlink or special file"
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=research_evidence_tree_digest(
                        self.evidence
                    ),
                ):
                    pass

        run.assert_not_called()

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_unavailable_backend_never_starts_or_falls_back(self, run) -> None:
        unavailable = ResearchSandboxPreflightResult(
            available=False,
            backend=RESEARCH_SANDBOX_BACKEND,
            detail="daemon unavailable",
            image="python:3.11-slim",
        )
        with patch.object(
            self.sandbox, "preflight", return_value=unavailable
        ):
            with self.assertRaisesRegex(
                ResearchSandboxError, "daemon unavailable"
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=research_evidence_tree_digest(
                        self.evidence
                    ),
                ):
                    pass

        run.assert_not_called()

    @patch("skill_evolution.research_sandbox.subprocess.run")
    def test_container_removal_failure_is_not_hidden(self, run) -> None:
        _calls, control = self._control_plane()

        def fail_remove(command, **kwargs):
            if command[1] == "rm":
                return _result(returncode=1, stderr="remove failed")
            return control(command, **kwargs)

        run.side_effect = fail_remove
        with patch.object(self.sandbox, "preflight", return_value=_ready()):
            with self.assertRaisesRegex(
                ResearchSandboxError, "remove failed"
            ):
                with self.sandbox.isolated_run(
                    evidence_directory=self.evidence,
                    work_archive_directory=self.work,
                    expected_evidence_digest=research_evidence_tree_digest(
                        self.evidence
                    ),
                ):
                    pass


class ResearchSandboxContextTests(unittest.TestCase):
    """Only a fully attested context can configure the trusted router."""

    def test_context_validator_rejects_host_fallback(self) -> None:
        context = {
            "backend": RESEARCH_SANDBOX_BACKEND,
            "network": "none",
            "root_filesystem": "read_only",
            "credentials_in_container": False,
            "host_fallback_allowed": True,
            "mounts": {
                "evidence": {"mode": "read_only"},
                "work": {"mode": "read_write_tmpfs"},
            },
            "tool_environment": {},
        }

        with self.assertRaisesRegex(ResearchSandboxError, "host fallback"):
            validate_research_sandbox_context(context)


class ResearchToolHelperTests(unittest.TestCase):
    """The exact container helper pages, queries, and confines local data."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.work = self.root / "work"
        (self.evidence / "runs/run-1").mkdir(parents=True)
        self.work.mkdir()
        trajectory_records = [
            {"run_id": "run-1", "seq": 1, "type": "message_action"},
            {
                "run_id": "run-1",
                "seq": 2,
                "type": "tool_action",
                "payload": {"tool_name": "write", "status": "succeeded"},
            },
            {"run_id": "run-1", "seq": 3, "type": "message_action"},
        ]
        (self.evidence / "runs/run-1/trajectory.jsonl").write_text(
            "".join(
                json.dumps(record, separators=(",", ":")) + "\n"
                for record in trajectory_records
            ),
            encoding="utf-8",
        )
        index_records = [
            {
                "run_id": "run-1",
                "seq": 2,
                "category": "temporary_script",
                "duration_ms": 10,
            },
            {
                "run_id": "run-2",
                "seq": 7,
                "category": "temporary_script",
                "duration_ms": 20,
            },
            {
                "run_id": "run-3",
                "seq": 4,
                "category": "validation",
                "duration_ms": 5,
            },
        ]
        (self.evidence / "navigation-index.json").write_text(
            json.dumps(
                {
                    "schema": "research.navigation_index.v1",
                    "entries": index_records,
                    "scripts": [],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.evidence / "large-trajectory.txt").write_text(
            "x" * 2_100_000
            + " UNIQUE_LARGE_TRAJECTORY_MARKER\n"
            + "unique_large_trajectory_marker second\n"
            + "unique_large_trajectory_marker third\n",
            encoding="utf-8",
        )
        source = (
            Path(__file__).resolve().parents[1]
            / "extensions/research-tools.ts"
        ).read_text(encoding="utf-8")
        marker = "const PYTHON_HELPER = String.raw`\n"
        start = source.index(marker) + len(marker)
        end = source.index("\n`;", start)
        self.helper = source[start:end].replace(
            'EVIDENCE = os.path.realpath("/evidence")',
            f"EVIDENCE = os.path.realpath({str(self.evidence)!r})",
        ).replace(
            'WORK = os.path.realpath("/work")',
            f"WORK = os.path.realpath({str(self.work)!r})",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, operation: str, params: dict[str, object]) -> dict:
        result = subprocess.run(
            [sys.executable, "-c", self.helper, operation],
            input=json.dumps(params),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_list_and_read_are_paginated(self) -> None:
        first = self._run("list", {"path": ".", "cursor": 0, "limit": 1})
        second = self._run(
            "list", {"path": ".", "cursor": first["next_cursor"], "limit": 1}
        )
        read = self._run(
            "read_evidence",
            {"path": "runs/run-1/trajectory.jsonl", "offset": 2, "limit": 1},
        )

        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["truncated"])
        self.assertNotEqual(first["items"], second["items"])
        self.assertEqual(read["lines"][0]["line"], 2)
        self.assertEqual(read["next_offset"], 3)

    def test_search_does_not_skip_a_large_text_trajectory(self) -> None:
        first = self._run(
            "search",
            {
                "query": "unique_large_trajectory_marker",
                "path": ".",
                "cursor": 0,
                "limit": 2,
            },
        )
        second = self._run(
            "search",
            {
                "query": "unique_large_trajectory_marker",
                "path": ".",
                "cursor": first["next_cursor"],
                "limit": 2,
            },
        )

        self.assertEqual(first["total_matches"], 3)
        self.assertEqual(len(first["matches"]), 2)
        self.assertEqual(first["next_cursor"], 2)
        self.assertEqual(first["matches"][0]["path"], "large-trajectory.txt")
        self.assertTrue(first["matches"][0]["text_truncated"])
        self.assertEqual(second["total_matches"], 3)
        self.assertEqual(len(second["matches"]), 1)
        self.assertIsNone(second["next_cursor"])

    def test_structured_query_and_trajectory_window_return_source_locators(self) -> None:
        query = self._run(
            "query",
            {
                "path": "navigation-index.json",
                "collection": "entries",
                "where": [
                    {
                        "field": "category",
                        "op": "eq",
                        "value": "temporary_script",
                    }
                ],
                "select": ["run_id", "seq"],
                "cursor": 0,
                "limit": 1,
            },
        )
        window = self._run(
            "trajectory_window",
            {"run_id": "run-1", "seq": 2, "before": 1, "after": 1},
        )

        self.assertEqual(query["total_matches"], 2)
        self.assertEqual(query["records"][0]["record"]["run_id"], "run-1")
        self.assertIsNotNone(query["next_cursor"])
        self.assertEqual(len(window["records"]), 3)
        self.assertEqual(window["target_seq"], 2)

    def test_work_write_edit_read_and_traversal_rejection(self) -> None:
        written = self._run(
            "write_work", {"path": "program.py", "content": "print('one')\n"}
        )
        edited = self._run(
            "edit_work",
            {
                "path": "program.py",
                "old_text": "one",
                "new_text": "two",
            },
        )
        read = self._run(
            "read_work", {"path": "program.py", "offset": 1, "limit": 10}
        )
        escaped = subprocess.run(
            [sys.executable, "-c", self.helper, "read_work"],
            input=json.dumps({"path": "safe/../program.py"}),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(written["path"], "program.py")
        self.assertEqual(edited["path"], "program.py")
        self.assertEqual(read["lines"][0]["text"], "print('two')")
        self.assertNotEqual(escaped.returncode, 0)
        self.assertIn("Parent traversal", escaped.stderr)


class ResearchToolProcessCleanupContractTests(unittest.TestCase):
    """The production router must synchronously reap detached command trees."""

    def test_extension_serializes_and_verifies_container_process_cleanup(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "extensions/research-tools.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("maxConcurrentToolCalls !== 1", source)
        self.assertIn("await cleanupContainerProcesses", source)
        self.assertIn("os.kill(pid, signal.SIGKILL)", source)
        self.assertNotIn('state" = "Z"', source)
        self.assertIn("cleanupResidualProcessCount", source)
        self.assertIn("cleanupVerified: true", source)
        self.assertIn("research-session-poisoned", source)
        self.assertIn("if (sessionPoisoned)", source)

        output_source = (
            Path(__file__).resolve().parents[1]
            / "extensions/research-output.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("SESSION_POISON_ENV", output_source)
        self.assertIn("submission is forbidden", output_source)

        sandbox_source = (
            Path(__file__).resolve().parents[1]
            / "skill_evolution/research_sandbox.py"
        ).read_text(encoding="utf-8")
        self.assertIn("os.waitpid(-1, 0)", sandbox_source)
        self.assertIn('"--log-driver",\n            "none"', sandbox_source)

    def test_harness_checks_timeout_and_output_delayed_writes(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "extensions/research-harness-driver.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("timeout-residual-1.txt", source)
        self.assertIn("timeout-residual-2.txt", source)
        self.assertIn("timeout-residual-3a.txt", source)
        self.assertIn("output-residual.txt", source)
        self.assertIn("test ! -e timeout-residual-3b.txt", source)
        self.assertIn("test ! -e output-residual.txt", source)


if __name__ == "__main__":
    unittest.main()
