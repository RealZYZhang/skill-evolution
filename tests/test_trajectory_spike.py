"""Tests for action-level Pi trajectory capture and failure preservation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from scripts.trajectory_spike import run_trajectory_spike
from scripts.task_case import TaskCase


FAKE_PI = textwrap.dedent(
    r"""
    import json
    from pathlib import Path
    import sys

    arguments = sys.argv[1:]
    session_dir = Path(arguments[arguments.index("--session-dir") + 1])
    skill_dir = Path(arguments[arguments.index("--skill") + 1])
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "fake-session.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": "fake-session",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "cwd": str(Path.cwd()),
    }
    session_file.write_text(json.dumps(header) + "\n", encoding="utf-8")
    entry_number = 0
    parent_id = None

    def append_message(message):
        global entry_number, parent_id
        if "--fake-no-session-messages" in arguments:
            return
        entry_number += 1
        entry_id = f"entry-{entry_number}"
        entry = {
            "type": "message",
            "id": entry_id,
            "parentId": parent_id,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": message,
        }
        with session_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        parent_id = entry_id

    def emit_completed(message):
        print(json.dumps({"type": "message_start", "message": message}), flush=True)
        print(json.dumps({"type": "message_end", "message": message}), flush=True)
        append_message(message)

    print("fake-pi-started", file=sys.stderr, flush=True)
    for raw_line in sys.stdin:
        command = json.loads(raw_line)
        kind = command["type"]
        if kind == "get_state":
            data = {
                "sessionFile": str(session_file),
                "sessionId": "fake-session",
                "sessionName": "fake-trajectory",
                "thinkingLevel": "off",
                "isStreaming": False,
                "model": {"provider": "fake", "id": "fake-model"},
            }
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": True,
                "data": data,
            }
            print(json.dumps(response), flush=True)
        elif kind == "get_commands":
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": True,
                "data": {
                    "commands": [{
                        "name": "skill:test",
                        "source": "skill",
                        "location": "path",
                        "sourceInfo": {
                            "path": str(skill_dir / "SKILL.md"),
                        },
                    }]
                },
            }
            print(json.dumps(response), flush=True)
        elif kind == "prompt":
            if "--fake-fail" in arguments:
                print("intentional child exit", file=sys.stderr, flush=True)
                raise SystemExit(7)
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": True,
            }
            print(json.dumps(response), flush=True)
            print(json.dumps({"type": "agent_start"}), flush=True)

            user_message = {
                "role": "user",
                "content": command["message"],
                "timestamp": 1,
            }
            emit_completed(user_message)

            partial_text = ""
            assistant_base = {
                "role": "assistant",
                "api": "fake",
                "provider": "fake",
                "model": "fake-model",
                "usage": {
                    "input": 1,
                    "output": 1,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 2,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "total": 0,
                    },
                },
                "stopReason": "stop",
                "timestamp": 2,
            }
            initial = {**assistant_base, "content": []}
            print(
                json.dumps({"type": "message_start", "message": initial}),
                flush=True,
            )
            update_count = 2 if "--fake-incomplete" in arguments else 20
            for index in range(update_count):
                delta = "x" * 1000
                partial_text += delta
                partial = {
                    **assistant_base,
                    "content": [{"type": "text", "text": partial_text}],
                }
                event = {
                    "type": "message_update",
                    "message": partial,
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 0,
                        "delta": delta,
                        "partial": partial,
                    },
                }
                print(json.dumps(event), flush=True)
            if "--fake-incomplete" in arguments:
                continue

            assistant_message = {
                **assistant_base,
                "content": [{"type": "text", "text": partial_text}],
            }
            print(
                json.dumps({
                    "type": "message_end",
                    "message": assistant_message,
                }),
                flush=True,
            )
            append_message(assistant_message)

            failed_args = {"path": "output.html", "content": "first attempt"}
            print(json.dumps({
                "type": "tool_execution_start",
                "toolCallId": "write-1",
                "toolName": "write",
                "args": failed_args,
            }), flush=True)
            if "--fake-incomplete-tool" in arguments:
                continue
            failed_result = {
                "content": [{"type": "text", "text": "output limit"}],
                "details": {},
            }
            print(json.dumps({
                "type": "tool_execution_end",
                "toolCallId": "write-1",
                "toolName": "write",
                "result": failed_result,
                "isError": True,
            }), flush=True)
            first_tool_result = {
                "role": "toolResult",
                "toolCallId": "write-1",
                "toolName": "write",
                "content": failed_result["content"],
                "details": {},
                "isError": True,
                "timestamp": 3,
            }
            emit_completed(first_tool_result)

            successful_args = {
                "path": "output.html",
                "content": "<!doctype html><title>ok</title>",
            }
            print(json.dumps({
                "type": "tool_execution_start",
                "toolCallId": "write-2",
                "toolName": "write",
                "args": successful_args,
            }), flush=True)
            Path("output.html").write_text(
                successful_args["content"],
                encoding="utf-8",
            )
            if "--fake-extra-artifact" in arguments:
                Path("summary.json").write_text(
                    json.dumps({"status": "ok"}),
                    encoding="utf-8",
                )
            successful_result = {
                "content": [{"type": "text", "text": "written"}],
                "details": {},
            }
            print(json.dumps({
                "type": "tool_execution_end",
                "toolCallId": "write-2",
                "toolName": "write",
                "result": successful_result,
                "isError": False,
            }), flush=True)
            second_tool_result = {
                "role": "toolResult",
                "toolCallId": "write-2",
                "toolName": "write",
                "content": successful_result["content"],
                "details": {},
                "isError": False,
                "timestamp": 4,
            }
            emit_completed(second_tool_result)

            print(json.dumps({
                "type": "turn_end",
                "message": assistant_message,
                "toolResults": [first_tool_result, second_tool_result],
            }), flush=True)
            print(json.dumps({
                "type": "agent_end",
                "messages": [
                    assistant_message,
                    first_tool_result,
                    second_tool_result,
                ],
            }), flush=True)
            print(json.dumps({"type": "agent_settled"}), flush=True)
    """
)


def read_journal(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def write_approved_skill_contract(
    skill: Path,
    *,
    skill_id: str = "test",
) -> None:
    """Write the minimal approved contract required by execution tests."""

    (skill / "skill_contract.json").write_text(
        json.dumps(
            {
                "schema": "skill.contract.v2",
                "skill_id": skill_id,
                "version": "2.0.0",
                "status": "approved",
                "owner": "test-owner",
                "approved_by": "test-owner",
                "approved_at": "2026-08-06T00:00:00Z",
                "supersedes": None,
                "runtime": {
                    "required_tools": [
                        "filesystem.read",
                        "filesystem.write",
                    ],
                    "allowed_tools": [
                        "filesystem.read",
                        "filesystem.write",
                        "process.execute",
                    ],
                    "allowed_permissions": [
                        "workspace.input.read",
                        "workspace.artifact.write",
                        "workspace.command.execute",
                    ],
                    "network": "forbidden",
                    "credentials_in_sandbox": False,
                    "dependencies": [],
                    "assets": [],
                },
                "evaluation": {"suite_refs": ["test-suite-v1"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )


class TrajectorySpikeTest(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path]:
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: test\ndescription: test skill\n---\n",
            encoding="utf-8",
        )
        write_approved_skill_contract(skill)
        source = root / "source.md"
        source.write_text("# Input\n\nExample.", encoding="utf-8")
        return skill, source

    def run_fake(
        self,
        root: Path,
        *,
        extra_pi_args: list[str] | None = None,
        timeout: float = 2,
    ):
        skill, source = self.make_inputs(root)
        return run_trajectory_spike(
            skill_path=skill,
            source_path=source,
            prompt="Test the fake skill.",
            output_root=root / "runs",
            timeout=timeout,
            pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            extra_pi_args=extra_pi_args or [],
        )

    def assert_ordered_without_integrity_hashes(
        self,
        records: list[dict[str, object]],
    ) -> None:
        for expected_sequence, record in enumerate(records, 1):
            self.assertEqual(record["seq"], expected_sequence)
            self.assertNotIn("previous_sha256", record)
            self.assertNotIn("record_sha256", record)

    def test_success_writes_one_ordered_canonical_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(root)
            journal_path = result.run_directory / "trajectory.jsonl"
            records = read_journal(journal_path)

            self.assertEqual(result.outcome["status"], "succeeded")
            self.assertEqual(result.outcome["session"]["status"], "complete")
            self.assertEqual(records[0]["type"], "trajectory_started")
            self.assertEqual(records[-2]["type"], "trajectory_finished")
            self.assertEqual(records[-1]["type"], "trajectory_sealed")
            self.assertEqual(
                records[-1]["payload"]["record_count"],
                len(records),
            )
            self.assert_ordered_without_integrity_hashes(records)

            self.assertTrue(
                (result.run_directory / "pi-session.jsonl").is_file()
            )
            self.assertTrue(
                (
                    result.run_directory
                    / "artifacts"
                    / "input"
                    / "source.md"
                ).is_file()
            )
            self.assertTrue(
                (result.run_directory / "artifacts" / "output.html").is_file()
            )
            self.assertTrue(
                (
                    result.run_directory
                    / "artifacts"
                    / "skill"
                    / "SKILL.md"
                ).is_file()
            )
            for legacy_name in (
                "run.json",
                "outcome.json",
                "pi-rpc.jsonl",
                "stderr.log",
            ):
                self.assertFalse((result.run_directory / legacy_name).exists())

    def test_file_task_preserves_original_name_and_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, _ = self.make_inputs(root)
            source = root / "contract.source.PDF"
            source.write_bytes(b"%PDF fake")
            task_case = TaskCase.for_file(
                source,
                task_case_id="pdf-basic",
                capability_tags=["format:pdf"],
            )

            result = run_trajectory_spike(
                skill_path=skill,
                task_case=task_case,
                prompt="Test the fake skill.",
                output_root=root / "runs",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")
            manifest = records[0]["payload"]["manifest"]

            copied = (
                result.run_directory
                / "artifacts"
                / "input"
                / source.name
            )
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertFalse(
                (result.run_directory / "artifacts" / "input.md").exists()
            )
            self.assertEqual(
                manifest["task_case"]["input"],
                f"artifacts/input/{source.name}",
            )
            self.assertEqual(
                manifest["task_case"]["input_spec"]["original_filename"],
                source.name,
            )
            self.assertEqual(
                manifest["task_case"]["capability_tags"],
                ["format:pdf"],
            )

    def test_inline_text_is_not_materialized_as_an_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, _ = self.make_inputs(root)
            task_case = TaskCase.for_inline_text(
                "Pasted source text.",
                task_case_id="pasted-text",
            )

            result = run_trajectory_spike(
                skill_path=skill,
                task_case=task_case,
                prompt="Test the fake skill.",
                output_root=root / "runs",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")
            manifest = records[0]["payload"]["manifest"]

            self.assertEqual(result.outcome["status"], "succeeded")
            self.assertFalse(
                (result.run_directory / "artifacts" / "input").exists()
            )
            self.assertEqual(
                manifest["task_case"]["delivery"],
                "inline_text",
            )
            self.assertEqual(
                manifest["task_case"]["input_spec"]["text"],
                "Pasted source text.",
            )

    def test_all_expected_artifacts_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source = self.make_inputs(root)
            task_case = TaskCase.for_file(
                source,
                task_case_id="multiple-outputs",
                expected_artifacts=["output.html", "summary.json"],
            )

            result = run_trajectory_spike(
                skill_path=skill,
                task_case=task_case,
                prompt="Test the fake skill.",
                output_root=root / "runs",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
                extra_pi_args=["--fake-extra-artifact"],
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")
            output_events = [
                record
                for record in records
                if record["type"] == "artifact_registered"
                and record["payload"]["artifact_role"] == "output"
            ]

            self.assertEqual(result.outcome["status"], "succeeded")
            self.assertEqual(
                [record["path"] for record in result.outcome["artifacts"]],
                [
                    "artifacts/output.html",
                    "artifacts/summary.json",
                ],
            )
            self.assertEqual(len(output_events), 2)
            self.assertEqual(
                [event["payload"]["artifact_index"] for event in output_events],
                [0, 1],
            )

    def test_missing_expected_artifact_fails_after_agent_settles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source = self.make_inputs(root)
            task_case = TaskCase.for_file(
                source,
                task_case_id="missing-output",
                expected_artifacts=["output.html", "summary.json"],
            )

            result = run_trajectory_spike(
                skill_path=skill,
                task_case=task_case,
                prompt="Test the fake skill.",
                output_root=root / "runs",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )

            self.assertEqual(result.outcome["status"], "failed")
            self.assertEqual(result.outcome["failure_stage"], "inspect_result")
            self.assertIn(
                "artifacts/summary.json",
                result.outcome["error"]["message"],
            )

    def test_only_complete_messages_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(root)
            journal_path = result.run_directory / "trajectory.jsonl"
            records = read_journal(journal_path)
            messages = [
                record
                for record in records
                if record["type"] == "message_action"
            ]
            assistant_messages = [
                record["payload"]["message"]
                for record in messages
                if record["payload"]["message"]["role"] == "assistant"
            ]

            self.assertEqual(len(messages), 4)
            self.assertEqual(len(assistant_messages), 1)
            self.assertEqual(
                assistant_messages[0]["content"][0]["text"],
                "x" * 20_000,
            )
            self.assertFalse(
                any(
                    record["type"]
                    in {
                        "message_started",
                        "message_delta",
                        "message_completed",
                    }
                    for record in records
                )
            )
            self.assertLess(journal_path.stat().st_size, 150_000)

    def test_preserves_each_failed_and_successful_tool_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(root)
            records = read_journal(result.run_directory / "trajectory.jsonl")
            attempts = [
                record
                for record in records
                if record["type"] == "tool_action"
            ]

            self.assertEqual(
                [record["payload"]["tool_call_id"] for record in attempts],
                ["write-1", "write-2"],
            )
            self.assertEqual(
                [record["payload"]["status"] for record in attempts],
                ["failed", "succeeded"],
            )
            self.assertEqual(
                attempts[0]["payload"]["arguments"]["content"],
                "first attempt",
            )
            self.assertEqual(
                attempts[1]["payload"]["arguments"]["content"],
                "<!doctype html><title>ok</title>",
            )
            self.assertEqual(
                attempts[0]["payload"]["result"]["content"][0]["text"],
                "output limit",
            )

    def test_child_exit_still_seals_partial_session_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(root, extra_pi_args=["--fake-fail"])
            records = read_journal(result.run_directory / "trajectory.jsonl")

            self.assertEqual(result.outcome["status"], "failed")
            self.assertEqual(result.outcome["failure_stage"], "prompt")
            self.assertEqual(result.outcome["process_exit_code"], 7)
            self.assertEqual(result.outcome["session"]["status"], "partial")
            self.assertEqual(records[-1]["type"], "trajectory_sealed")
            stderr_records = [
                record
                for record in records
                if record["type"] == "process_stderr"
            ]
            self.assertTrue(
                any(
                    "intentional child exit" in record["payload"]["line"]
                    for record in stderr_records
                )
            )

    def test_incomplete_message_records_interruption_without_partial(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(
                root,
                extra_pi_args=["--fake-incomplete"],
                timeout=0.05,
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")
            interrupted = [
                record
                for record in records
                if record["type"] == "action_interrupted"
            ]

            self.assertEqual(result.outcome["status"], "failed")
            self.assertEqual(result.outcome["failure_stage"], "agent_execution")
            self.assertEqual(result.outcome["session"]["status"], "partial")
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(
                interrupted[0]["payload"]["action_type"],
                "message",
            )
            self.assertFalse(
                interrupted[0]["payload"]["content_persisted"]
            )
            self.assertNotIn("message", interrupted[0]["payload"])
            serialized = json.dumps(interrupted[0], ensure_ascii=False)
            self.assertNotIn("x" * 100, serialized)

    def test_incomplete_tool_becomes_one_interrupted_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(
                root,
                extra_pi_args=["--fake-incomplete-tool"],
                timeout=0.05,
            )
            records = read_journal(
                result.run_directory / "trajectory.jsonl"
            )
            tools = [
                record
                for record in records
                if record["type"] == "tool_action"
            ]

            self.assertEqual(result.outcome["status"], "failed")
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0]["payload"]["status"], "interrupted")
            self.assertEqual(
                tools[0]["payload"]["arguments"]["content"],
                "first attempt",
            )
            self.assertIsNone(tools[0]["payload"]["result"])

    def test_session_is_diagnostic_not_required_for_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake(
                root,
                extra_pi_args=["--fake-no-session-messages"],
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")
            messages = [
                record
                for record in records
                if record["type"] == "message_action"
            ]

            self.assertEqual(result.outcome["status"], "succeeded")
            self.assertIsNone(result.outcome["failure_stage"])
            self.assertEqual(result.outcome["session"]["status"], "complete")
            self.assertEqual(result.outcome["session"]["message_count"], 0)
            self.assertEqual(len(messages), 4)
            self.assertFalse(
                any(
                    record["type"] == "message_recovery"
                    for record in records
                )
            )

    def test_start_failure_still_seals_failure_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source = self.make_inputs(root)

            result = run_trajectory_spike(
                skill_path=skill,
                source_path=source,
                prompt="Test the fake skill.",
                output_root=root / "runs",
                pi_command=[str(root / "missing-pi")],
            )
            records = read_journal(result.run_directory / "trajectory.jsonl")

            self.assertEqual(result.outcome["status"], "failed")
            self.assertEqual(result.outcome["failure_stage"], "start")
            self.assertIsNone(result.outcome["process_exit_code"])
            self.assertEqual(result.outcome["session"]["status"], "missing")
            self.assertEqual(records[-1]["type"], "trajectory_sealed")

    def test_hierarchy_capture_binds_execution_to_immutable_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source = self.make_inputs(root)

            result = run_trajectory_spike(
                skill_path=skill,
                source_path=source,
                prompt="Test the fake skill.",
                hierarchy_root=root / "runtime",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )

            self.assertIsNotNone(result.execution_directory)
            self.assertIsNotNone(result.execution_manifest)
            assert result.execution_directory is not None
            assert result.execution_manifest is not None
            self.assertEqual(
                result.run_directory,
                result.execution_directory / "payload",
            )
            self.assertEqual(
                result.execution_manifest["schema"],
                "skill.execution.v1",
            )
            self.assertEqual(result.execution_manifest["skill_id"], "test")
            self.assertTrue(result.execution_manifest["trajectory"]["sealed"])
            self.assertEqual(len(result.execution_manifest["inputs"]), 1)
            self.assertEqual(len(result.execution_manifest["outputs"]), 1)
            self.assertTrue(
                (result.execution_directory / "execution.json").is_file()
            )

    def test_rejects_missing_skill_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("input", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                run_trajectory_spike(
                    skill_path=root / "missing",
                    source_path=source,
                    prompt="Test the fake skill.",
                    output_root=root / "runs",
                )

            self.assertFalse((root / "runs").exists())

    def test_rejects_missing_contract_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source = self.make_inputs(root)
            (skill / "skill_contract.json").unlink()

            with self.assertRaisesRegex(
                ValueError,
                "skill_contract.json",
            ):
                run_trajectory_spike(
                    skill_path=skill,
                    source_path=source,
                    prompt="Test the fake skill.",
                    output_root=root / "runs",
                )

            self.assertFalse((root / "runs").exists())

    def test_script_is_directly_invocable(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "trajectory_spike.py"
        )

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--skill", result.stdout)
        self.assertIn("--prompt-file", result.stdout)


if __name__ == "__main__":
    unittest.main()
