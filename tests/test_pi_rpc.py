"""Tests for the Pi subprocess JSONL RPC client and command-line boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from scripts.pi_rpc import (
    PiProcessExitedError,
    PiRequestTimeoutError,
    PiRpcClient,
)


MOCK_SERVER = textwrap.dedent(
    r"""
    import json
    import sys

    print("mock-started", file=sys.stderr, flush=True)
    for raw_line in sys.stdin.buffer:
        command = json.loads(raw_line.decode("utf-8"))
        kind = command.get("type")
        if kind == "get_state":
            event = {"type": "notice", "text": "left\u2028right"}
            print(json.dumps(event, ensure_ascii=False), flush=True)
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": True,
                "data": {"isStreaming": False},
            }
            print(json.dumps(response), flush=True)
        elif kind == "fail":
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": False,
                "error": "expected failure",
            }
            print(json.dumps(response), flush=True)
        elif kind == "malformed":
            print("{not-json}", flush=True)
            response = {
                "id": command["id"],
                "type": "response",
                "command": kind,
                "success": True,
            }
            print(json.dumps(response), flush=True)
        elif kind == "ignore":
            continue
        elif kind == "exit":
            break
    """
)


class PiRpcClientTest(unittest.TestCase):
    def make_client(self) -> PiRpcClient:
        return PiRpcClient(
            pi_command=[sys.executable, "-u", "-c", MOCK_SERVER],
            no_session=True,
        )

    def test_build_command_adds_rpc_mode_and_no_session(self) -> None:
        client = self.make_client()

        command = client.build_command()

        self.assertIn("--mode", command)
        self.assertIn("rpc", command)
        self.assertIn("--no-session", command)

    def test_correlates_response_and_preserves_unicode_separator_event(self) -> None:
        with self.make_client() as client:
            response = client.request({"type": "get_state"}, timeout=2)
            event = client.next_event(timeout=2)

        self.assertTrue(response["success"])
        self.assertEqual(event["text"], "left\u2028right")

    def test_returns_failed_rpc_response_for_caller_policy(self) -> None:
        with self.make_client() as client:
            response = client.request({"type": "fail"}, timeout=2)

        self.assertFalse(response["success"])
        self.assertEqual(response["error"], "expected failure")

    def test_environment_can_replace_instead_of_inherit_host_values(self) -> None:
        server = textwrap.dedent(
            """
            import json
            import os
            import sys

            for line in sys.stdin:
                command = json.loads(line)
                print(json.dumps({
                    "id": command["id"],
                    "type": "response",
                    "command": command["type"],
                    "success": True,
                    "data": {
                        "hostile": os.environ.get("HOSTILE_PARENT_VALUE"),
                        "allowed": os.environ.get("ALLOWED_CHILD_VALUE"),
                    },
                }), flush=True)
            """
        )
        with patch.dict(
            os.environ,
            {"HOSTILE_PARENT_VALUE": "must-not-cross"},
            clear=False,
        ):
            replacement = PiRpcClient(
                pi_command=[sys.executable, "-u", "-c", server],
                env={"ALLOWED_CHILD_VALUE": "allowed"},
                replace_environment=True,
            )
            merged = PiRpcClient(
                pi_command=[sys.executable, "-u", "-c", server],
                env={"ALLOWED_CHILD_VALUE": "allowed"},
            )
            with replacement:
                replacement_data = replacement.request(
                    {"type": "get_state"}, timeout=2
                )["data"]
            with merged:
                merged_data = merged.request(
                    {"type": "get_state"}, timeout=2
                )["data"]

        self.assertIsNone(replacement_data["hostile"])
        self.assertEqual(replacement_data["allowed"], "allowed")
        self.assertEqual(merged_data["hostile"], "must-not-cross")

    def test_pass_fds_targets_only_the_intended_child(self) -> None:
        server = textwrap.dedent(
            """
            import json
            import os
            import sys

            descriptor = int(sys.argv[1])
            for line in sys.stdin:
                command = json.loads(line)
                try:
                    inherited = os.pread(descriptor, 64, 0) == b"fd-secret"
                except OSError:
                    inherited = False
                print(json.dumps({
                    "id": command["id"],
                    "type": "response",
                    "command": command["type"],
                    "success": True,
                    "data": {"inherited": inherited},
                }), flush=True)
            """
        )
        with tempfile.TemporaryFile() as secret:
            secret.write(b"fd-secret")
            secret.flush()
            descriptor = secret.fileno()
            self.assertFalse(os.get_inheritable(descriptor))
            command = [
                sys.executable,
                "-u",
                "-c",
                server,
                str(descriptor),
            ]
            untargeted = PiRpcClient(pi_command=command)
            targeted = PiRpcClient(
                pi_command=command,
                pass_fds=(descriptor,),
            )
            with untargeted:
                untargeted_result = untargeted.request(
                    {"type": "get_state"}, timeout=2
                )
            with targeted:
                targeted_result = targeted.request(
                    {"type": "get_state"}, timeout=2
                )

        self.assertFalse(untargeted_result["data"]["inherited"])
        self.assertTrue(targeted_result["data"]["inherited"])

    def test_captures_stderr_separately(self) -> None:
        with self.make_client() as client:
            client.request({"type": "get_state"}, timeout=2)

        self.assertIn("mock-started", client.stderr_tail)

    def test_reports_malformed_child_record_as_protocol_event(self) -> None:
        with self.make_client() as client:
            response = client.request({"type": "malformed"}, timeout=2)
            event = client.next_event(timeout=2)

        self.assertTrue(response["success"])
        self.assertEqual(event["type"], "client_protocol_error")

    def test_observer_receives_both_directions_and_parse_failures(self) -> None:
        records: list[tuple[str, str, object, str | None]] = []
        client = PiRpcClient(
            pi_command=[sys.executable, "-u", "-c", MOCK_SERVER],
            rpc_record_observer=lambda *record: records.append(record),
        )

        with client:
            client.request({"type": "malformed"}, timeout=2)
            client.next_event(timeout=2)

        self.assertEqual(records[0][0], "client_to_pi")
        self.assertEqual(records[0][2]["type"], "malformed")
        malformed = next(record for record in records if record[1] == "{not-json}")
        self.assertEqual(malformed[0], "pi_to_client")
        self.assertIsNone(malformed[2])
        self.assertIsNotNone(malformed[3])

    def test_times_out_when_child_does_not_respond(self) -> None:
        with self.make_client() as client:
            with self.assertRaises(PiRequestTimeoutError):
                client.request({"type": "ignore"}, timeout=0.05)

    def test_raises_when_child_exits_before_response(self) -> None:
        with self.make_client() as client:
            with self.assertRaises(PiProcessExitedError):
                client.request({"type": "exit"}, timeout=2)

    def test_cli_rejects_unapproved_prompt_before_starting_pi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            prompt.write_text("Unapproved prompt.\n", encoding="utf-8")
            approval = root / "prompt.md.approval.json"
            approval.write_text(
                json.dumps(
                    {
                        "schema": "prompt.approval.v1",
                        "status": "proposed",
                        "prompt_id": "test.prompt",
                        "version": "1",
                        "prompt_file": "prompt.md",
                    }
                ),
                encoding="utf-8",
            )
            script = (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "pi_rpc.py"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--pi-command",
                    str(root / "missing-pi"),
                    "prompt",
                    "--prompt-file",
                    str(prompt),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("not approved", result.stderr)
            self.assertNotIn("No Pi executable", result.stderr)

    def test_cli_raw_command_cannot_bypass_prompt_approval(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "pi_rpc.py"
        )

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--pi-command",
                "/missing-pi",
                "raw",
                '{"type":"prompt","message":"bypass"}',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("approved prompt file", result.stderr)


if __name__ == "__main__":
    unittest.main()
