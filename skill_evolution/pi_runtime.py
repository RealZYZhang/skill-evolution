"""Pi RPC adapter for one-process-per-agent analysis runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

from scripts.pi_rpc import (
    PiRequestTimeoutError,
    PiRpcClient,
    PiRpcError,
)
from scripts.prompt_approval import load_approved_prompt
from scripts.trajectory_spike import TrajectoryJournal
from skill_evolution.agents import (
    AgentRole,
    AgentRunRepository,
    AgentRunResult,
    AgentSpec,
    ModelConfiguration,
)
from skill_evolution.analysis import validate_agent_result
from skill_evolution.comparison import validate_test_effect
from skill_evolution.evidence import EvidenceRef
from skill_evolution.storage import JsonObject, atomic_write_json
from skill_evolution.trajectory_analysis import validate_trajectory_error_report


TRAJECTORY_ERROR_SUBMISSION_TOOL = "submit_trajectory_error_analysis"


class PiAgentRuntimeError(RuntimeError):
    """Raised when a Pi analysis run cannot be prepared safely."""


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "".join(parts)


def _parse_single_json_object(text: str) -> JsonObject:
    if not text.strip():
        raise ValueError("Agent final message is empty")
    decoder = json.JSONDecoder()
    stripped = text.strip()
    value, end = decoder.raw_decode(stripped)
    if stripped[end:].strip():
        raise ValueError(
            "Agent final message must contain only one JSON object"
        )
    if not isinstance(value, dict):
        raise ValueError("Agent final message must be a JSON object")
    return value


def _select_structured_submission(
    submissions: Sequence[Mapping[str, Any]],
) -> JsonObject:
    """Require exactly one schema-validated structured tool submission."""

    if len(submissions) != 1:
        raise ValueError(
            "Trajectory error analysis must make exactly one successful "
            f"{TRAJECTORY_ERROR_SUBMISSION_TOOL} submission; received "
            f"{len(submissions)}"
        )
    return dict(submissions[0])


def _validate_candidate_proposal(value: Mapping[str, Any]) -> JsonObject:
    if value.get("schema") != "candidate.proposal.v1":
        raise ValueError("Unsupported candidate proposal schema")
    hypothesis_id = value.get("hypothesis_id")
    summary = value.get("summary")
    files_touched = value.get("files_touched")
    evidence = value.get("evidence")
    if not isinstance(hypothesis_id, str) or not hypothesis_id:
        raise ValueError("Candidate proposal requires hypothesis_id")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Candidate proposal requires summary")
    if not isinstance(files_touched, list) or not all(
        isinstance(item, str) and item for item in files_touched
    ):
        raise ValueError("files_touched must be a string list")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Candidate proposal must cite evidence")
    return {
        "schema": "candidate.proposal.v1",
        "hypothesis_id": hypothesis_id,
        "summary": summary.strip(),
        "files_touched": files_touched,
        "evidence": evidence,
        "claimed_diff_ignored": True,
    }


def _validate_role_result(
    role: AgentRole,
    value: Mapping[str, Any],
    context: Mapping[str, Any],
) -> JsonObject:
    if role is AgentRole.TRAJECTORY_ERROR_ANALYST:
        return validate_trajectory_error_report(value, context)
    if role in {
        AgentRole.OUTCOME_CONSISTENCY,
        AgentRole.CAPABILITY_COVERAGE,
        AgentRole.RESOURCE_EFFICIENCY,
        AgentRole.SYNTHESIS,
    }:
        result = validate_agent_result(value)
        if result["role"] != role.value:
            raise ValueError(
                f"Agent result role {result['role']!r} does not match "
                f"{role.value!r}"
            )
        return result
    if role is AgentRole.CANDIDATE_PROPOSER:
        proposal = _validate_candidate_proposal(value)
        hypothesis = context.get("hypothesis")
        expected_hypothesis_id = (
            hypothesis.get("id")
            if isinstance(hypothesis, Mapping)
            else context.get("hypothesis_id")
        )
        if (
            not isinstance(expected_hypothesis_id, str)
            or not expected_hypothesis_id
        ):
            raise ValueError(
                "CandidateProposer context lacks one hypothesis id"
            )
        if proposal["hypothesis_id"] != expected_hypothesis_id:
            raise ValueError(
                "Candidate proposal references a different hypothesis"
            )
        return proposal
    proposer_run_id = context.get("proposer_agent_run_id")
    if not isinstance(proposer_run_id, str) or not proposer_run_id:
        raise ValueError("ReplayJudge context lacks proposer_agent_run_id")
    effect = validate_test_effect(
        value,
        proposer_agent_run_id=proposer_run_id,
    )
    current_agent_run_id = context.get("agent_run_id")
    if (
        not isinstance(current_agent_run_id, str)
        or effect["judge_agent_run_id"] != current_agent_run_id
    ):
        raise ValueError(
            "ReplayJudge result must use the current AgentRun id"
        )
    return effect


def _validate_evidence_list(
    values: Any,
    *,
    bundle_root: Path,
    field: str,
    allow_empty: bool = False,
) -> None:
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list")
    if not values and not allow_empty:
        raise ValueError(f"{field} must cite at least one evidence reference")
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} entries must be objects")
        reference = EvidenceRef.from_dict(item)
        reference.validate(bundle_root)


def _validate_result_evidence(
    role: AgentRole,
    result: Mapping[str, Any],
    *,
    bundle_root: Path,
) -> None:
    if role is AgentRole.TRAJECTORY_ERROR_ANALYST:
        _validate_evidence_list(
            result.get("summary_evidence"),
            bundle_root=bundle_root,
            field="summary_evidence",
        )
        for index, incident in enumerate(result.get("incidents", [])):
            if not isinstance(incident, Mapping):
                continue
            _validate_evidence_list(
                incident.get("evidence"),
                bundle_root=bundle_root,
                field=f"incidents[{index}].evidence",
            )
            _validate_evidence_list(
                incident.get("counterevidence"),
                bundle_root=bundle_root,
                field=f"incidents[{index}].counterevidence",
                allow_empty=True,
            )
        for index, relation in enumerate(result.get("causal_chain", [])):
            if not isinstance(relation, Mapping):
                continue
            _validate_evidence_list(
                relation.get("evidence"),
                bundle_root=bundle_root,
                field=f"causal_chain[{index}].evidence",
            )
        return
    if role in {
        AgentRole.OUTCOME_CONSISTENCY,
        AgentRole.CAPABILITY_COVERAGE,
        AgentRole.RESOURCE_EFFICIENCY,
        AgentRole.SYNTHESIS,
    }:
        for index, finding in enumerate(result.get("findings", [])):
            if not isinstance(finding, Mapping):
                continue
            _validate_evidence_list(
                finding.get("evidence"),
                bundle_root=bundle_root,
                field=f"findings[{index}].evidence",
            )
            _validate_evidence_list(
                finding.get("counterevidence", []),
                bundle_root=bundle_root,
                field=f"findings[{index}].counterevidence",
                allow_empty=True,
            )
        for index, hypothesis in enumerate(
            result.get("optimization_hypotheses", [])
        ):
            if not isinstance(hypothesis, Mapping):
                continue
            _validate_evidence_list(
                hypothesis.get("evidence"),
                bundle_root=bundle_root,
                field=f"optimization_hypotheses[{index}].evidence",
            )
        return
    _validate_evidence_list(
        result.get("evidence"),
        bundle_root=bundle_root,
        field="evidence",
    )


def _capture_session(
    *,
    runtime_directory: Path,
    destination: Path,
    reported_session_file: str | None,
    settled: bool,
) -> str:
    candidates: list[Path] = []
    if reported_session_file:
        candidates.append(Path(reported_session_file))
    candidates.extend(sorted(runtime_directory.rglob("*.jsonl")))
    source = next((item for item in candidates if item.is_file()), None)
    if source is None:
        destination.touch()
        return "missing"
    shutil.copy2(source, destination)
    return "complete" if settled else "partial"


class PiAgentRuntime:
    """Run each role in a new Pi process, session, workspace, and trajectory."""

    def __init__(
        self,
        *,
        agent_runs_root: str | os.PathLike[str],
        extension_path: str | os.PathLike[str],
        structured_output_extension_path: (
            str | os.PathLike[str] | None
        ) = None,
        model: ModelConfiguration | None = None,
        pi_command: Sequence[str] | str | None = None,
        extra_pi_args: Sequence[str] = (),
        abort_wait_seconds: float = 3.0,
    ) -> None:
        self.runs = AgentRunRepository(agent_runs_root)
        self.extension_path = Path(extension_path).resolve()
        self.structured_output_extension_path = (
            Path(structured_output_extension_path).resolve()
            if structured_output_extension_path is not None
            else None
        )
        self.model = model or ModelConfiguration.from_project_configuration()
        self.pi_command = pi_command
        self.extra_pi_args = tuple(extra_pi_args)
        self.abort_wait_seconds = abort_wait_seconds

    def preflight(self, specs: Sequence[AgentSpec]) -> None:
        """Require every prompt approval and the extension before scheduling."""

        if not self.extension_path.is_file():
            raise PiAgentRuntimeError(
                f"Root-jail extension does not exist: {self.extension_path}"
            )
        if any(
            spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST for spec in specs
        ) and (
            self.structured_output_extension_path is None
            or not self.structured_output_extension_path.is_file()
        ):
            raise PiAgentRuntimeError(
                "Trajectory error analysis requires the structured-output "
                "extension"
            )
        for spec in specs:
            approved = load_approved_prompt(spec.prompt_path)
            if "{{" in approved.text or "}}" in approved.text:
                raise PiAgentRuntimeError(
                    f"Analysis prompt has unresolved placeholders: "
                    f"{spec.prompt_path}"
                )

    def run(
        self,
        *,
        spec: AgentSpec,
        campaign_id: str,
        round_number: int,
        context: Mapping[str, Any],
        evidence_bundle: Path,
        candidate_workspace: Path | None = None,
    ) -> AgentRunResult:
        """Execute an approved role prompt and persist every terminal outcome."""

        approved_prompt = load_approved_prompt(spec.prompt_path)
        if "{{" in approved_prompt.text or "}}" in approved_prompt.text:
            raise PiAgentRuntimeError(
                "Analysis prompts must be static and read context.json; "
                "unresolved template placeholders are not allowed."
            )
        if not self.extension_path.is_file():
            raise PiAgentRuntimeError(
                f"Root-jail extension does not exist: {self.extension_path}"
            )
        if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST and (
            self.structured_output_extension_path is None
            or not self.structured_output_extension_path.is_file()
        ):
            raise PiAgentRuntimeError(
                "Trajectory error analysis requires the structured-output "
                "extension"
            )
        if spec.tool_mode == "candidate" and candidate_workspace is None:
            raise PiAgentRuntimeError(
                "CandidateProposer requires an isolated candidate workspace"
            )

        agent_run_id, run_directory = self.runs.prepare(
            spec=spec,
            campaign_id=campaign_id,
            round_number=round_number,
            model=self.model,
            context=context,
            evidence_bundle=evidence_bundle,
        )
        self.runs.repository.update(
            agent_run_id,
            {
                "prompt": {
                    "template_snapshot": "prompt/template.md",
                    "approval_snapshot": "prompt/approval.json",
                    "prompt_id": approved_prompt.prompt_id,
                    "version": approved_prompt.version,
                    "approved_by": approved_prompt.approved_by,
                    "approved_at": approved_prompt.approved_at,
                    "content_sha256": approved_prompt.content_sha256,
                },
                "output_contract": (
                    {
                        "mode": "validated_tool_submission",
                        "tool": TRAJECTORY_ERROR_SUBMISSION_TOOL,
                    }
                    if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST
                    else {"mode": "single_json_final_message"}
                ),
            },
            expected_status="prepared",
        )
        workspace = run_directory / "workspace"
        read_root = workspace
        if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST:
            read_root = workspace / "evidence"
            shutil.copy2(
                workspace / "context.json",
                read_root / "context.json",
            )
        runtime_sessions = run_directory / "runtime/pi-session"
        runtime_sessions.mkdir(parents=True)
        journal = TrajectoryJournal(
            run_directory / "trajectory.jsonl",
            agent_run_id,
        )
        journal.append(
            source="framework",
            record_type="trajectory_started",
            payload={
                "manifest": {
                    "schema": "analysis.agent_run.trajectory.v1",
                    "agent_run_id": agent_run_id,
                    "role": spec.role.value,
                    "campaign_id": campaign_id,
                    "round": round_number,
                    "model": self.model.to_dict(),
                    "tool_mode": spec.tool_mode,
                    "prompt": {
                        "prompt_id": approved_prompt.prompt_id,
                        "version": approved_prompt.version,
                        "approval_snapshot": "prompt/approval.json",
                    },
                }
            },
        )

        pi_args = [
            "--session-dir",
            str(runtime_sessions),
            "--name",
            f"{spec.role.value}-{agent_run_id}",
            "--no-builtin-tools",
            "--extension",
            str(self.extension_path),
            "--no-prompt-templates",
            "--no-skills",
            "--no-context-files",
            "--provider",
            self.model.provider,
            "--model",
            self.model.model,
            "--thinking",
            self.model.thinking,
            *self.extra_pi_args,
        ]
        if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST:
            assert self.structured_output_extension_path is not None
            pi_args.extend(
                [
                    "--extension",
                    str(self.structured_output_extension_path),
                ]
            )
        runtime_env = {
            "SKILL_EVOLUTION_READ_ROOT": str(read_root),
            "SKILL_EVOLUTION_TOOL_MODE": spec.tool_mode,
        }
        if candidate_workspace is not None:
            runtime_env["SKILL_EVOLUTION_WRITE_ROOT"] = str(
                candidate_workspace.resolve()
            )

        client: PiRpcClient | None = None
        session_file: str | None = None
        last_assistant: Mapping[str, Any] | None = None
        structured_submissions: list[JsonObject] = []
        pending_structured_submissions: dict[str, JsonObject] = {}
        settled = False
        status = "failed"
        error_record: JsonObject | None = None
        parse_failure: JsonObject | None = None
        result: JsonObject | None = None
        timeout_uncertain = False
        try:
            client = PiRpcClient(
                cwd=workspace,
                pi_command=self.pi_command,
                pi_args=pi_args,
                no_session=False,
                approve_project=False,
                env=runtime_env,
                rpc_record_observer=journal.record_rpc,
                stderr_observer=journal.record_stderr,
            )
            journal.append(
                source="framework",
                record_type="pi_process_starting",
                payload={
                    "role": spec.role.value,
                    "built_in_tools": False,
                    "extension": self.extension_path.name,
                    "output_contract": (
                        "validated_tool_submission"
                        if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST
                        else "single_json_final_message"
                    ),
                },
            )
            client.start()
            self.runs.mark_running(
                agent_run_id,
                process_id=client.process.pid,
            )
            journal.append(
                source="framework",
                record_type="pi_process_started",
                payload={"pid": client.process.pid},
            )
            state = client.request({"type": "get_state"}, timeout=30)
            state_data = state.get("data")
            if isinstance(state_data, Mapping):
                candidate = state_data.get("sessionFile")
                if isinstance(candidate, str):
                    session_file = candidate
                journal.append(
                    source="framework",
                    record_type="runtime_observed",
                    payload={
                        "model": state_data.get("model"),
                        "thinking_level": state_data.get("thinkingLevel"),
                        "session_id": state_data.get("sessionId"),
                    },
                )
            prompt_response = client.request(
                {
                    "type": "prompt",
                    "message": approved_prompt.text,
                },
                timeout=30,
            )
            if not prompt_response.get("success"):
                raise PiAgentRuntimeError(
                    f"Pi rejected prompt: {prompt_response.get('error')}"
                )
            for event in client.events_until(
                lambda item: item.get("type") == "agent_settled",
                timeout=spec.timeout_seconds,
            ):
                if event.get("type") == "message_end":
                    message = event.get("message")
                    if (
                        isinstance(message, Mapping)
                        and message.get("role") == "assistant"
                    ):
                        last_assistant = message
                if (
                    event.get("type") == "tool_execution_start"
                    and event.get("toolName")
                    == TRAJECTORY_ERROR_SUBMISSION_TOOL
                ):
                    tool_call_id = event.get("toolCallId")
                    arguments = event.get("args")
                    if (
                        isinstance(tool_call_id, str)
                        and tool_call_id
                        and isinstance(arguments, Mapping)
                    ):
                        pending_structured_submissions[tool_call_id] = dict(
                            arguments
                        )
                if (
                    event.get("type") == "tool_execution_end"
                    and event.get("toolName")
                    == TRAJECTORY_ERROR_SUBMISSION_TOOL
                ):
                    tool_call_id = event.get("toolCallId")
                    arguments = (
                        pending_structured_submissions.pop(
                            tool_call_id,
                            None,
                        )
                        if isinstance(tool_call_id, str)
                        else None
                    )
                    if not event.get("isError") and arguments is not None:
                        structured_submissions.append(arguments)
            settled = True
            final_state = client.request({"type": "get_state"}, timeout=30)
            final_data = final_state.get("data")
            if isinstance(final_data, Mapping):
                candidate = final_data.get("sessionFile")
                if isinstance(candidate, str):
                    session_file = candidate
            raw_text = (
                _message_text(last_assistant)
                if last_assistant is not None
                else ""
            )
            try:
                if spec.role is AgentRole.TRAJECTORY_ERROR_ANALYST:
                    decoded = _select_structured_submission(
                        structured_submissions
                    )
                else:
                    if last_assistant is None:
                        raise ValueError(
                            "Pi settled without a final assistant message"
                        )
                    decoded = _parse_single_json_object(raw_text)
                validation_context = {
                    **dict(context),
                    "agent_run_id": agent_run_id,
                }
                result = _validate_role_result(
                    spec.role,
                    decoded,
                    validation_context,
                )
                _validate_result_evidence(
                    spec.role,
                    result,
                    bundle_root=workspace / "evidence",
                )
            except (ValueError, TypeError) as error:
                if structured_submissions:
                    invalid_path = "result.invalid.json"
                    atomic_write_json(
                        run_directory / invalid_path,
                        {
                            "submissions": structured_submissions,
                            "final_message": raw_text,
                        },
                    )
                else:
                    invalid_path = "result.invalid.txt"
                    (run_directory / invalid_path).write_text(
                        raw_text,
                        encoding="utf-8",
                    )
                parse_failure = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "raw_output": invalid_path,
                }
                status = "invalid_output"
            else:
                status = "succeeded"
        except PiRequestTimeoutError as error:
            error_record = {
                "type": type(error).__name__,
                "message": str(error),
            }
            status, timeout_uncertain = self._abort_after_timeout(
                client,
                journal,
            )
        except Exception as error:
            error_record = {
                "type": type(error).__name__,
                "message": str(error),
            }
            status = "failed"
        finally:
            if client is not None:
                client.close()
                try:
                    exit_code = client.process.returncode
                except PiRpcError:
                    exit_code = None
                journal.append(
                    source="framework",
                    record_type="pi_process_exited",
                    payload={
                        "exit_code": exit_code,
                        "timeout_uncertain": timeout_uncertain,
                    },
                )
            journal.capture_incomplete_state(
                "agent_settled" if settled else "agent_run_ended"
            )
            session_status = _capture_session(
                runtime_directory=runtime_sessions,
                destination=run_directory / "pi-session.jsonl",
                reported_session_file=session_file,
                settled=settled,
            )
            journal.append(
                source="framework",
                record_type="session_captured",
                payload={
                    "path": "pi-session.jsonl",
                    "status": session_status,
                },
            )
            journal.append(
                source="framework",
                record_type="trajectory_finished",
                payload={
                    "status": status,
                    "result_path": (
                        "result.json" if result is not None else None
                    ),
                    "error": error_record,
                    "parse_failure": parse_failure,
                },
            )
            journal.close()

        self.runs.finish(
            agent_run_id,
            status=status,
            result=result,
            error=error_record,
            parse_failure=parse_failure,
            session_status=session_status,
        )
        return AgentRunResult(
            agent_run_id=agent_run_id,
            role=spec.role,
            status=status,
            result=result,
            error=error_record or parse_failure,
            run_directory=run_directory,
        )

    def _abort_after_timeout(
        self,
        client: PiRpcClient | None,
        journal: TrajectoryJournal,
    ) -> tuple[str, bool]:
        if client is None:
            return "timed_out", False
        acknowledged = False
        settled = False
        try:
            response = client.request({"type": "abort"}, timeout=5)
            acknowledged = bool(response.get("success"))
        except PiRpcError:
            acknowledged = False
        journal.append(
            source="framework",
            record_type="agent_abort_requested",
            payload={"acknowledged": acknowledged},
        )
        deadline = time.monotonic() + self.abort_wait_seconds
        while time.monotonic() < deadline:
            try:
                event = client.next_event(
                    timeout=max(0.05, deadline - time.monotonic())
                )
            except PiRequestTimeoutError:
                break
            if event.get("type") == "agent_settled":
                settled = True
                break
        if acknowledged and settled:
            return "timed_out", False
        return "indeterminate", True
