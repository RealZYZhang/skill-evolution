"""Pure/fake tests for strict Pi agent output and timeout handling."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.pi_rpc import PiRequestTimeoutError
from skill_evolution.pi_runtime import (
    PiAgentRuntime,
    PiAgentRuntimeError,
    _capture_session,
    _message_text,
    _parse_single_json_object,
    _select_structured_submission,
    _validate_result_evidence,
    _validate_role_result,
)
from skill_evolution.agents import AgentRole, AgentSpec


class RecordingJournal:
    """Small journal double for abort-sequence assertions."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append(self, *, source, record_type, payload) -> None:
        self.records.append(
            {
                "source": source,
                "record_type": record_type,
                "payload": dict(payload),
            }
        )


class AbortClient:
    """Pi RPC double for acknowledged and uncertain abort outcomes."""

    def __init__(
        self,
        *,
        acknowledged: bool,
        events: list[dict[str, object]],
    ) -> None:
        self.acknowledged = acknowledged
        self.events = list(events)
        self.requests: list[dict[str, object]] = []

    def request(self, message, *, timeout):
        self.requests.append(dict(message))
        return {"success": self.acknowledged}

    def next_event(self, *, timeout):
        if self.events:
            return self.events.pop(0)
        raise PiRequestTimeoutError("no settled event")


class StrictAgentOutputTests(unittest.TestCase):
    """An agent result is exactly one JSON object, never repaired ad hoc."""

    def test_parses_exactly_one_object(self) -> None:
        self.assertEqual(
            _parse_single_json_object('{"schema":"example.v1","ok":true}'),
            {"schema": "example.v1", "ok": True},
        )

    def test_rejects_markdown_extra_objects_arrays_and_invalid_json(
        self,
    ) -> None:
        invalid_values = [
            '```json\n{"ok":true}\n```',
            '{"ok":true}\n{"second":true}',
            '["not", "an", "object"]',
            '{"unterminated":',
            "",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises((ValueError, TypeError)):
                    _parse_single_json_object(value)

    def test_message_text_joins_only_text_blocks(self) -> None:
        text = _message_text(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": '{"first":'},
                    {
                        "type": "toolCall",
                        "name": "harness_read",
                        "arguments": {},
                    },
                    {"type": "text", "text": "true}"},
                ],
            }
        )
        self.assertEqual(text, '{"first":true}')

    def test_trajectory_analysis_uses_exactly_one_structured_submission(
        self,
    ) -> None:
        submission = {"schema": "analysis.trajectory_error_report.v1"}

        self.assertEqual(
            _select_structured_submission([submission]),
            submission,
        )
        for invalid in ([], [submission, submission]):
            with self.subTest(count=len(invalid)):
                with self.assertRaisesRegex(
                    ValueError,
                    "exactly one successful",
                ):
                    _select_structured_submission(invalid)

    def test_candidate_must_reference_the_assigned_hypothesis(self) -> None:
        value = {
            "schema": "candidate.proposal.v1",
            "hypothesis_id": "other",
            "summary": "One atomic edit.",
            "files_touched": ["SKILL.md"],
            "evidence": [
                {
                    "schema": "evidence.ref.v1",
                    "run_id": "run-1",
                    "seq": 1,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "different hypothesis"):
            _validate_role_result(
                AgentRole.CANDIDATE_PROPOSER,
                value,
                {"hypothesis_id": "assigned"},
            )

    def test_judge_must_report_the_current_agent_run_id(self) -> None:
        value = {
            "schema": "test.effect.v1",
            "judge_agent_run_id": "wrong-judge",
            "runnable": True,
            "complete": False,
            "dimensions": {"correctness": "inconclusive"},
            "protected_dimensions": [],
            "classification": "inconclusive",
            "evidence": [
                {
                    "schema": "evidence.ref.v1",
                    "run_id": "run-1",
                    "seq": 1,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "current AgentRun"):
            _validate_role_result(
                AgentRole.REPLAY_JUDGE,
                value,
                {
                    "proposer_agent_run_id": "proposer",
                    "agent_run_id": "actual-judge",
                },
            )

    def test_empty_judge_evidence_is_rejected_against_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "must cite"):
                _validate_result_evidence(
                    AgentRole.REPLAY_JUDGE,
                    {"evidence": []},
                    bundle_root=Path(temporary),
                )


class TimeoutHandlingTests(unittest.TestCase):
    """Timeout handling aborts first and exposes uncertain completion."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runtime = PiAgentRuntime(
            agent_runs_root=root / "agent-runs",
            extension_path=root / "root-jail.ts",
            structured_output_extension_path=(
                root / "trajectory-error-output.ts"
            ),
            abort_wait_seconds=0.01,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_acknowledged_abort_followed_by_settled_is_timed_out(self) -> None:
        client = AbortClient(
            acknowledged=True,
            events=[{"type": "agent_settled"}],
        )
        journal = RecordingJournal()

        status, uncertain = self.runtime._abort_after_timeout(
            client,
            journal,
        )

        self.assertEqual(status, "timed_out")
        self.assertFalse(uncertain)
        self.assertEqual(client.requests, [{"type": "abort"}])
        self.assertEqual(
            journal.records,
            [
                {
                    "source": "framework",
                    "record_type": "agent_abort_requested",
                    "payload": {"acknowledged": True},
                }
            ],
        )

    def test_missing_settled_event_is_indeterminate(self) -> None:
        client = AbortClient(acknowledged=True, events=[])
        journal = RecordingJournal()

        status, uncertain = self.runtime._abort_after_timeout(
            client,
            journal,
        )

        self.assertEqual(status, "indeterminate")
        self.assertTrue(uncertain)
        self.assertEqual(
            journal.records[0]["payload"],
            {"acknowledged": True},
        )


class StructuredSubmissionRuntimeTests(unittest.TestCase):
    """Trajectory analysis accepts validated tool data, not surrounding prose."""

    def test_preflight_requires_structured_output_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_jail = root / "root-jail.ts"
            root_jail.write_text(
                "export default () => {};\n",
                encoding="utf-8",
            )
            runtime = PiAgentRuntime(
                agent_runs_root=root / "agent-runs",
                extension_path=root_jail,
            )
            prompt = (
                Path(__file__).resolve().parents[1]
                / "prompts/analysis/trajectory-error-analysis-v2.md"
            )

            with self.assertRaisesRegex(
                PiAgentRuntimeError,
                "structured-output extension",
            ):
                runtime.preflight(
                    [
                        AgentSpec(
                            role=AgentRole.TRAJECTORY_ERROR_ANALYST,
                            prompt_path=prompt,
                        )
                    ]
                )

    def test_structured_submission_is_used_even_with_prose_message(
        self,
    ) -> None:
        report = {
            "schema": "analysis.trajectory_error_report.v1",
            "role": "trajectory_error_analyst",
            "run_id": "run-1",
            "precheck": {
                "report_path": "reports/trajectory-precheck.json",
                "deterministic_status": "completed_with_signals",
                "integrity_status": "valid",
                "interpreted_signal_ids": ["signal-1"],
                "uninterpreted_signal_ids": [],
            },
            "trajectory_assessment": "errors_recovered",
            "primary_incident_id": "incident-1",
            "summary": "The later action repaired the failed write.",
            "summary_evidence": [
                {
                    "schema": "evidence.ref.v1",
                    "report_path": "reports/trajectory-precheck.json",
                    "json_pointer": "/deterministic_status",
                }
            ],
            "incidents": [
                {
                    "id": "incident-1",
                    "source_signal_ids": ["signal-1"],
                    "disposition": "recovered",
                    "causal_role": "root_cause",
                    "attributed_to": "tool_or_dependency",
                    "phase": "artifact_write",
                    "claim": "A later action repaired the failed write.",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "schema": "evidence.ref.v1",
                            "run_id": "run-1",
                            "seq": 2,
                        }
                    ],
                    "counterevidence": [],
                }
            ],
            "causal_chain": [],
            "skill_fix_applicability": "no",
            "repair_target": None,
            "additional_evidence_needed": [],
            "limitations": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "reports").mkdir(parents=True)
            (evidence / "runs/run-1").mkdir(parents=True)
            (evidence / "reports/trajectory-precheck.json").write_text(
                json.dumps(
                    {
                        "deterministic_status": "completed_with_signals",
                    }
                ),
                encoding="utf-8",
            )
            (evidence / "runs/run-1/trajectory.jsonl").write_text(
                json.dumps({"run_id": "run-1", "seq": 2}) + "\n",
                encoding="utf-8",
            )
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            fake_pi = root / "fake_pi.py"
            fake_pi.write_text(
                """import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
for line in sys.stdin:
    command = json.loads(line)
    response = {
        "id": command.get("id"),
        "type": "response",
        "command": command["type"],
        "success": True,
    }
    if command["type"] == "get_state":
        response["data"] = {"sessionId": "fake-session"}
        print(json.dumps(response), flush=True)
    elif command["type"] == "prompt":
        print(json.dumps(response), flush=True)
        print(json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here is the result."}],
            },
        }), flush=True)
        print(json.dumps({
            "type": "tool_execution_start",
            "toolCallId": "submit-1",
            "toolName": "submit_trajectory_error_analysis",
            "args": report,
        }), flush=True)
        print(json.dumps({
            "type": "tool_execution_end",
            "toolCallId": "submit-1",
            "toolName": "submit_trajectory_error_analysis",
            "result": {"accepted": True},
            "isError": False,
        }), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
""",
                encoding="utf-8",
            )
            root_jail = root / "root-jail.ts"
            output_extension = root / "trajectory-error-output.ts"
            root_jail.write_text("export default () => {};\n", encoding="utf-8")
            output_extension.write_text(
                "export default () => {};\n",
                encoding="utf-8",
            )
            prompt = (
                Path(__file__).resolve().parents[1]
                / "prompts/analysis/trajectory-error-analysis-v2.md"
            )
            runtime = PiAgentRuntime(
                agent_runs_root=root / "agent-runs",
                extension_path=root_jail,
                structured_output_extension_path=output_extension,
                pi_command=[
                    sys.executable,
                    str(fake_pi),
                    str(report_path),
                ],
            )

            result = runtime.run(
                spec=AgentSpec(
                    role=AgentRole.TRAJECTORY_ERROR_ANALYST,
                    prompt_path=prompt,
                ),
                campaign_id="analysis-1",
                round_number=1,
                context={
                    "run_id": "run-1",
                    "trajectory_precheck_path": (
                        "reports/trajectory-precheck.json"
                    ),
                    "precheck_deterministic_status": (
                        "completed_with_signals"
                    ),
                    "precheck_integrity_status": "valid",
                    "precheck_signal_ids": ["signal-1"],
                },
                evidence_bundle=evidence,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.result, report)
            manifest = json.loads(
                (result.run_directory / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["output_contract"]["mode"],
                "validated_tool_submission",
            )


class SessionCaptureTests(unittest.TestCase):
    """Interrupted runs preserve partial sessions instead of hiding them."""

    def test_unsettled_session_is_copied_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            source = runtime / "session.jsonl"
            source.write_text(
                '{"type":"message","partial":true}\n',
                encoding="utf-8",
            )
            destination = root / "captured.jsonl"

            status = _capture_session(
                runtime_directory=runtime,
                destination=destination,
                reported_session_file=str(source),
                settled=False,
            )

            self.assertEqual(status, "partial")
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                source.read_text(encoding="utf-8"),
            )

    def test_missing_session_creates_visible_empty_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            destination = root / "captured.jsonl"

            status = _capture_session(
                runtime_directory=runtime,
                destination=destination,
                reported_session_file=None,
                settled=False,
            )

            self.assertEqual(status, "missing")
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
