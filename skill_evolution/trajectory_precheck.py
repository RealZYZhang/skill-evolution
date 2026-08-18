"""Deterministic integrity and explicit-signal checks for one trajectory."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from skill_evolution.storage import JsonObject, utc_now


TRAJECTORY_PRECHECK_SCHEMA = "trajectory.precheck.v1"
TRAJECTORY_SCHEMA = "trajectory.actions.v1"
LEGACY_ACTION_SCHEMA = "trace.actions.v1"
_LEGACY_RECORD_TYPES = {
    "trace_started": "trajectory_started",
    "trace_finished": "trajectory_finished",
    "trace_sealed": "trajectory_sealed",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _record_type(record: Mapping[str, Any]) -> str:
    value = str(record.get("type", ""))
    return _LEGACY_RECORD_TYPES.get(value, value)


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _display_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    marker = "/artifacts/"
    if marker in normalized:
        return "artifacts/" + normalized.rsplit(marker, 1)[1]
    if Path(normalized).is_absolute():
        return Path(normalized).name
    return normalized


def _evidence_ref(run_id: str | None, sequence: int | None) -> JsonObject | None:
    if not run_id or sequence is None:
        return None
    return {
        "schema": "evidence.ref.v1",
        "run_id": run_id,
        "seq": sequence,
    }


def _issue(
    code: str,
    message: str,
    *,
    line: int | None = None,
    sequence: int | None = None,
) -> JsonObject:
    value: JsonObject = {"code": code, "message": message}
    if line is not None:
        value["line"] = line
    if sequence is not None:
        value["seq"] = sequence
    return value


def _gap_ranges(sequences: Sequence[int]) -> list[JsonObject]:
    unique = sorted(set(sequences))
    ranges: list[JsonObject] = []
    for previous, current in zip(unique, unique[1:]):
        if current <= previous + 1:
            continue
        ranges.append({"start": previous + 1, "end": current - 1})
    return ranges


def _safe_artifact_path(root: Path, value: str) -> tuple[Path | None, bool]:
    candidate_path = Path(value)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        return None, False
    candidate = (root / candidate_path).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None, False
    return candidate, True


def _record_target(payload: Mapping[str, Any]) -> str | None:
    arguments = _mapping(payload.get("arguments"))
    path = arguments.get("path")
    if isinstance(path, str) and path:
        return _display_path(path)
    return None


def _validate_envelope(
    record: Mapping[str, Any],
    *,
    line: int,
) -> list[JsonObject]:
    issues: list[JsonObject] = []
    sequence = _positive_integer(record.get("seq"))
    required_text = ("schema", "run_id", "observed_at", "source", "type")
    for field in required_text:
        if not isinstance(record.get(field), str) or not record.get(field):
            issues.append(
                _issue(
                    "invalid_envelope",
                    f"Record field {field!r} must be a non-empty string.",
                    line=line,
                    sequence=sequence,
                )
            )
    if sequence is None:
        issues.append(
            _issue(
                "invalid_sequence",
                "Record seq must be a positive integer.",
                line=line,
            )
        )
    if _non_negative_integer(record.get("elapsed_ms")) is None:
        issues.append(
            _issue(
                "invalid_elapsed_ms",
                "Record elapsed_ms must be a non-negative integer.",
                line=line,
                sequence=sequence,
            )
        )
    if not isinstance(record.get("payload"), Mapping):
        issues.append(
            _issue(
                "invalid_payload",
                "Record payload must be an object.",
                line=line,
                sequence=sequence,
            )
        )
    observed_at = record.get("observed_at")
    if isinstance(observed_at, str) and observed_at:
        try:
            datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            issues.append(
                _issue(
                    "invalid_observed_at",
                    "Record observed_at is not an ISO-8601 timestamp.",
                    line=line,
                    sequence=sequence,
                )
            )
    return issues


def _load_records(
    path: Path,
) -> tuple[list[JsonObject], list[JsonObject], int]:
    records: list[JsonObject] = []
    issues: list[JsonObject] = []
    line_count = 0
    with path.open("r", encoding="utf-8") as stream:
        for line_count, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                issues.append(
                    _issue(
                        "blank_line",
                        "Trajectory contains a blank JSONL line.",
                        line=line_count,
                    )
                )
                continue
            try:
                decoded = json.loads(raw_line)
            except json.JSONDecodeError:
                issues.append(
                    _issue(
                        "invalid_json",
                        "Trajectory line is not valid JSON.",
                        line=line_count,
                    )
                )
                continue
            if not isinstance(decoded, dict):
                issues.append(
                    _issue(
                        "non_object_record",
                        "Trajectory line must be a JSON object.",
                        line=line_count,
                    )
                )
                continue
            records.append(decoded)
            issues.extend(_validate_envelope(decoded, line=line_count))
    return records, issues, line_count


def _boundary_summary(
    records: Sequence[JsonObject],
    issues: list[JsonObject],
) -> tuple[JsonObject, Mapping[str, Any], Mapping[str, Any]]:
    positions: dict[str, list[int]] = {
        name: [
            index
            for index, record in enumerate(records)
            if _record_type(record) == name
        ]
        for name in (
            "trajectory_started",
            "trajectory_finished",
            "trajectory_sealed",
        )
    }
    labels = {
        "trajectory_started": "started",
        "trajectory_finished": "finished",
        "trajectory_sealed": "sealed",
    }
    for record_type, indices in positions.items():
        if not indices:
            issues.append(
                _issue(
                    f"missing_{labels[record_type]}",
                    f"Trajectory is missing {record_type}.",
                )
            )
        elif len(indices) > 1:
            issues.append(
                _issue(
                    f"multiple_{labels[record_type]}",
                    f"Trajectory contains multiple {record_type} records.",
                )
            )

    started_first = positions["trajectory_started"] == [0]
    sealed_last = positions["trajectory_sealed"] == [len(records) - 1]
    finished_before_seal = (
        len(records) >= 2
        and positions["trajectory_finished"] == [len(records) - 2]
        and positions["trajectory_sealed"] == [len(records) - 1]
    )
    if positions["trajectory_started"] and not started_first:
        issues.append(
            _issue(
                "started_not_first",
                "trajectory_started is not the first parsed record.",
            )
        )
    if positions["trajectory_sealed"] and not sealed_last:
        issues.append(
            _issue(
                "sealed_not_last",
                "trajectory_sealed is not the last parsed record.",
            )
        )
    if positions["trajectory_finished"] and not finished_before_seal:
        issues.append(
            _issue(
                "finished_boundary_order",
                "trajectory_finished is not immediately before the final seal.",
            )
        )

    finished = (
        records[positions["trajectory_finished"][0]]
        if len(positions["trajectory_finished"]) == 1
        else {}
    )
    sealed = (
        records[positions["trajectory_sealed"][0]]
        if len(positions["trajectory_sealed"]) == 1
        else {}
    )
    outcome = _mapping(_mapping(finished.get("payload")).get("outcome"))
    seal_payload = _mapping(sealed.get("payload"))
    declared_count = _positive_integer(seal_payload.get("record_count"))
    count_matches = declared_count == len(records)
    if sealed and declared_count is None:
        issues.append(
            _issue(
                "invalid_seal_record_count",
                "Seal record_count must be a positive integer.",
                sequence=_positive_integer(sealed.get("seq")),
            )
        )
    elif sealed and not count_matches:
        issues.append(
            _issue(
                "seal_record_count_mismatch",
                "Seal record_count does not match parsed record count.",
                sequence=_positive_integer(sealed.get("seq")),
            )
        )
    outcome_status = outcome.get("status")
    seal_status = seal_payload.get("status")
    statuses_match = bool(outcome_status) and outcome_status == seal_status
    if finished and not outcome:
        issues.append(
            _issue(
                "missing_outcome",
                "trajectory_finished does not contain an outcome object.",
                sequence=_positive_integer(finished.get("seq")),
            )
        )
    if finished and sealed and not statuses_match:
        issues.append(
            _issue(
                "outcome_seal_status_mismatch",
                "Outcome status and seal status do not match.",
            )
        )
    summary: JsonObject = {
        "started_count": len(positions["trajectory_started"]),
        "finished_count": len(positions["trajectory_finished"]),
        "sealed_count": len(positions["trajectory_sealed"]),
        "started_first": started_first,
        "finished_immediately_before_seal": finished_before_seal,
        "sealed_last": sealed_last,
        "seal_record_count": declared_count,
        "seal_record_count_matches": count_matches,
        "outcome_status_matches_seal": statuses_match,
    }
    return summary, outcome, seal_payload


def _artifact_facts(
    records: Sequence[JsonObject],
    root: Path,
) -> tuple[list[JsonObject], list[JsonObject]]:
    artifacts: list[JsonObject] = []
    signals: list[JsonObject] = []
    for record in records:
        if _record_type(record) != "artifact_registered":
            continue
        payload = _mapping(record.get("payload"))
        artifact = _mapping(payload.get("artifact"))
        path_value = artifact.get("path")
        sequence = _positive_integer(record.get("seq"))
        role = payload.get("artifact_role")
        artifact_id = f"artifact-{len(artifacts) + 1}"
        path = path_value if isinstance(path_value, str) else None
        candidate: Path | None = None
        path_safe: bool | None = None
        if path:
            candidate, path_safe = _safe_artifact_path(root, path)
        observed_exists = candidate.is_file() if candidate is not None else None
        observed_bytes = (
            candidate.stat().st_size if observed_exists and candidate else None
        )
        declared_exists = artifact.get("exists")
        declared_bytes = artifact.get("bytes")
        facts_consistent = True
        if isinstance(declared_exists, bool) and observed_exists is not None:
            facts_consistent = declared_exists == observed_exists
        if (
            facts_consistent
            and isinstance(declared_bytes, int)
            and observed_bytes is not None
        ):
            facts_consistent = declared_bytes == observed_bytes
        value: JsonObject = {
            "id": artifact_id,
            "role": role,
            "index": payload.get("artifact_index"),
            "path": _display_path(path) if path else None,
            "path_safe": path_safe,
            "declared_exists": declared_exists,
            "observed_exists": observed_exists,
            "declared_bytes": declared_bytes,
            "observed_bytes": observed_bytes,
            "facts_consistent": facts_consistent,
            "seq": sequence,
        }
        artifacts.append(value)
        problem: str | None = None
        if path is not None and path_safe is False:
            problem = "unsafe_path"
        elif role == "output" and declared_exists is False:
            problem = "declared_missing"
        elif role == "output" and observed_exists is False:
            problem = "observed_missing"
        elif role == "output" and observed_bytes == 0:
            problem = "empty_output"
        elif not facts_consistent:
            problem = "recorded_facts_mismatch_filesystem"
        if problem:
            signals.append(
                {
                    "kind": "artifact_issue",
                    "record_type": "artifact_registered",
                    "seq": sequence,
                    "facts": {
                        "artifact_id": artifact_id,
                        "problem": problem,
                    },
                }
            )
    return artifacts, signals


def _explicit_signals(
    records: Sequence[JsonObject],
    outcome: Mapping[str, Any],
) -> list[JsonObject]:
    signals: list[JsonObject] = []
    counts = Counter(_record_type(record) for record in records)
    for record in records:
        record_type = _record_type(record)
        payload = _mapping(record.get("payload"))
        sequence = _positive_integer(record.get("seq"))
        signal: JsonObject | None = None
        if record_type == "tool_action" and payload.get("status") in {
            "failed",
            "interrupted",
        }:
            facts: JsonObject = {
                "tool_name": payload.get("tool_name"),
                "status": payload.get("status"),
            }
            target = _record_target(payload)
            if target:
                facts["target_path"] = target
            signal = {
                "kind": "tool_non_success",
                "record_type": record_type,
                "seq": sequence,
                "facts": facts,
            }
        elif record_type == "action_interrupted":
            signal = {
                "kind": "interrupted_action",
                "record_type": record_type,
                "seq": sequence,
                "facts": {
                    "action_type": payload.get("action_type"),
                    "reason": payload.get("reason"),
                },
            }
        elif record_type == "rpc_protocol_error":
            signal = {
                "kind": "rpc_protocol_error",
                "record_type": record_type,
                "seq": sequence,
                "facts": {"direction": payload.get("direction")},
            }
        elif record_type == "process_stderr":
            line = payload.get("line")
            signal = {
                "kind": "process_stderr",
                "record_type": record_type,
                "seq": sequence,
                "facts": {
                    "line_present": isinstance(line, str) and bool(line),
                    "line_length": len(line) if isinstance(line, str) else None,
                },
            }
        elif record_type == "pi_process_exited":
            exit_code = payload.get("exit_code")
            if exit_code not in {None, 0}:
                signal = {
                    "kind": "nonzero_process_exit",
                    "record_type": record_type,
                    "seq": sequence,
                    "facts": {"exit_code": exit_code},
                }
        elif record_type == "skill_resolved" and payload.get("loaded") is False:
            signal = {
                "kind": "skill_not_loaded",
                "record_type": record_type,
                "seq": sequence,
                "facts": {"loaded": False, "delivery": payload.get("delivery")},
            }
        elif record_type == "session_captured":
            status = payload.get("status")
            invalid_lines = payload.get("invalid_line_count")
            if status in {"missing", "partial"} or (
                isinstance(invalid_lines, int) and invalid_lines > 0
            ):
                signal = {
                    "kind": "session_diagnostic_issue",
                    "record_type": record_type,
                    "seq": sequence,
                    "facts": {
                        "status": status,
                        "invalid_line_count": invalid_lines,
                    },
                }
        if signal is not None:
            signals.append(signal)

    if outcome.get("status") == "failed":
        error = _mapping(outcome.get("error"))
        finished = next(
            (
                record
                for record in records
                if _record_type(record) == "trajectory_finished"
            ),
            {},
        )
        signals.append(
            {
                "kind": "failed_outcome",
                "record_type": "trajectory_finished",
                "seq": _positive_integer(finished.get("seq")),
                "facts": {
                    "failure_stage": outcome.get("failure_stage"),
                    "error_type": error.get("type"),
                },
            }
        )
    observer_errors = outcome.get("observer_errors")
    if isinstance(observer_errors, list) and observer_errors:
        finished = next(
            (
                record
                for record in records
                if _record_type(record) == "trajectory_finished"
            ),
            {},
        )
        signals.append(
            {
                "kind": "observer_errors",
                "record_type": "trajectory_finished",
                "seq": _positive_integer(finished.get("seq")),
                "facts": {"count": len(observer_errors)},
            }
        )
    if outcome.get("status") == "succeeded" and not outcome.get("agent_settled"):
        signals.append(
            {
                "kind": "lifecycle_mismatch",
                "record_type": "trajectory_finished",
                "seq": None,
                "facts": {"problem": "success_without_agent_settled"},
            }
        )
    if counts["agent_start"] and not counts["agent_end"]:
        signals.append(
            {
                "kind": "lifecycle_incomplete",
                "record_type": "agent_end",
                "seq": None,
                "facts": {"problem": "agent_started_without_agent_end"},
            }
        )
    return signals


def _candidate_recoveries(
    records: Sequence[JsonObject],
    signals: Sequence[JsonObject],
) -> list[JsonObject]:
    succeeded_tools: list[tuple[int, str, str | None]] = []
    for record in records:
        payload = _mapping(record.get("payload"))
        sequence = _positive_integer(record.get("seq"))
        tool_name = payload.get("tool_name")
        if (
            _record_type(record) == "tool_action"
            and payload.get("status") == "succeeded"
            and sequence is not None
            and isinstance(tool_name, str)
        ):
            succeeded_tools.append((sequence, tool_name, _record_target(payload)))

    links: list[JsonObject] = []
    for signal in signals:
        if signal.get("kind") != "tool_non_success":
            continue
        failure_sequence = _positive_integer(signal.get("seq"))
        facts = _mapping(signal.get("facts"))
        tool_name = facts.get("tool_name")
        target = facts.get("target_path")
        if failure_sequence is None or not isinstance(tool_name, str):
            continue
        for later_sequence, later_tool, later_target in succeeded_tools:
            if later_sequence <= failure_sequence or later_tool != tool_name:
                continue
            basis = "same_tool_name"
            if target is not None and target == later_target:
                basis = "same_tool_and_target_path"
            links.append(
                {
                    "id": f"recovery-candidate-{len(links) + 1}",
                    "failed_signal_id": signal["id"],
                    "later_succeeded_seq": later_sequence,
                    "basis": basis,
                    "proves_recovery": False,
                }
            )
            break
    return links


def _required_judgments(
    signals: Sequence[JsonObject],
    recoveries: Sequence[JsonObject],
    artifacts: Sequence[JsonObject],
) -> list[JsonObject]:
    judgments: list[JsonObject] = []
    semantic_signal_ids = [
        signal["id"]
        for signal in signals
        if signal.get("kind")
        not in {"session_diagnostic_issue", "lifecycle_incomplete"}
    ]
    if semantic_signal_ids:
        judgments.extend(
            [
                {
                    "kind": "signal_disposition",
                    "signal_ids": semantic_signal_ids,
                    "question": (
                        "Which signals are real errors versus expected control flow, "
                        "symptoms, or unrelated observations?"
                    ),
                },
                {
                    "kind": "causal_attribution",
                    "signal_ids": semantic_signal_ids,
                    "question": (
                        "What causal role and responsibility boundary does the "
                        "evidence support?"
                    ),
                },
            ]
        )
    if recoveries:
        judgments.append(
            {
                "kind": "recovery_effect",
                "recovery_candidate_ids": [item["id"] for item in recoveries],
                "question": (
                    "Did later actions repair the failed effect and still satisfy "
                    "the task requirement?"
                ),
            }
        )
    output_ids = [
        artifact["id"]
        for artifact in artifacts
        if artifact.get("role") == "output"
        and artifact.get("observed_exists") is True
    ]
    if output_ids:
        judgments.append(
            {
                "kind": "artifact_semantic_correctness",
                "artifact_ids": output_ids,
                "question": (
                    "Do available task, validator, and artifact evidence show that "
                    "the outputs are semantically correct and complete?"
                ),
            }
        )
    return judgments


def precheck_trajectory(path: str | Path) -> JsonObject:
    """Return a no-model report for one action-level trajectory file."""

    trajectory_path = Path(path).resolve()
    source: JsonObject = {
        "trajectory_file": trajectory_path.name,
        "run_directory": trajectory_path.parent.name,
        "exists": trajectory_path.is_file(),
    }
    report: JsonObject = {
        "schema": TRAJECTORY_PRECHECK_SCHEMA,
        "checked_at": utc_now(),
        "source": source,
        "run_id": None,
        "deterministic_status": "invalid",
        "integrity": {
            "status": "invalid",
            "line_count": 0,
            "record_count": 0,
            "issues": [],
        },
        "outcome": {},
        "lifecycle": {"record_type_counts": {}},
        "signals": [],
        "artifacts": [],
        "candidate_recoveries": [],
        "llm_required_judgments": [],
    }
    if not trajectory_path.is_file():
        report["integrity"]["issues"] = [
            _issue("trajectory_missing", "Trajectory file does not exist.")
        ]
        return report

    try:
        records, issues, line_count = _load_records(trajectory_path)
    except (OSError, UnicodeError) as error:
        report["integrity"]["issues"] = [
            _issue(
                "trajectory_unreadable",
                f"Trajectory cannot be read as UTF-8: {type(error).__name__}.",
            )
        ]
        return report

    if not records:
        issues.append(_issue("empty_trajectory", "Trajectory has no JSON records."))

    schemas = sorted(
        {
            str(record.get("schema"))
            for record in records
            if isinstance(record.get("schema"), str)
        }
    )
    run_ids = sorted(
        {
            str(record.get("run_id"))
            for record in records
            if isinstance(record.get("run_id"), str) and record.get("run_id")
        }
    )
    supported_schemas = {TRAJECTORY_SCHEMA, LEGACY_ACTION_SCHEMA}
    source_format = (
        "legacy"
        if schemas == [LEGACY_ACTION_SCHEMA]
        else "current"
        if schemas == [TRAJECTORY_SCHEMA]
        else "unsupported_or_mixed"
    )
    if len(schemas) != 1 or schemas[0] not in supported_schemas:
        issues.append(
            _issue(
                "unsupported_or_mixed_schema",
                "Trajectory must contain only trajectory.actions.v1 records.",
            )
        )
    if len(run_ids) != 1:
        issues.append(
            _issue(
                "missing_or_mixed_run_id",
                "Trajectory must contain exactly one non-empty run_id.",
            )
        )
    run_id = run_ids[0] if len(run_ids) == 1 else None
    report["run_id"] = run_id

    sequences = [
        sequence
        for record in records
        if (sequence := _positive_integer(record.get("seq"))) is not None
    ]
    sequence_counter = Counter(sequences)
    duplicate_sequences = sorted(
        sequence for sequence, count in sequence_counter.items() if count > 1
    )
    continuous = sequences == list(range(1, len(records) + 1))
    if duplicate_sequences:
        issues.append(
            _issue("duplicate_sequence", "Trajectory contains duplicate seq values.")
        )
    if sequences and not continuous:
        issues.append(
            _issue(
                "non_continuous_sequence",
                "Trajectory seq values are not continuous in file order from one.",
            )
        )

    elapsed_values = [
        value
        for record in records
        if (value := _non_negative_integer(record.get("elapsed_ms"))) is not None
    ]
    elapsed_monotonic = all(
        current >= previous
        for previous, current in zip(elapsed_values, elapsed_values[1:])
    )
    if elapsed_values and not elapsed_monotonic:
        issues.append(
            _issue(
                "elapsed_time_regression",
                "Trajectory elapsed_ms values regress in file order.",
            )
        )

    boundaries, outcome, seal = _boundary_summary(records, issues)
    integrity_status = "valid"
    issue_codes = {str(issue["code"]) for issue in issues}
    incomplete_codes = {
        "empty_trajectory",
        "missing_started",
        "missing_finished",
        "missing_sealed",
        "missing_outcome",
    }
    if issues:
        integrity_status = (
            "incomplete"
            if issue_codes and issue_codes.issubset(incomplete_codes)
            else "invalid"
        )

    artifacts, artifact_signals = _artifact_facts(records, trajectory_path.parent)
    signals = _explicit_signals(records, outcome) + artifact_signals
    for index, signal in enumerate(signals, start=1):
        signal["id"] = f"signal-{index}"
        reference = _evidence_ref(run_id, _positive_integer(signal.get("seq")))
        if reference is not None:
            signal["evidence"] = reference
    recoveries = _candidate_recoveries(records, signals)
    judgments = _required_judgments(signals, recoveries, artifacts)

    outcome_summary: JsonObject = {
        "status": outcome.get("status"),
        "failure_stage": outcome.get("failure_stage"),
        "error_type": _mapping(outcome.get("error")).get("type"),
        "skill_loaded": outcome.get("skill_loaded"),
        "agent_settled": outcome.get("agent_settled"),
        "process_exit_code": outcome.get("process_exit_code"),
        "observer_error_count": (
            len(outcome["observer_errors"])
            if isinstance(outcome.get("observer_errors"), list)
            else None
        ),
        "seal_status": seal.get("status"),
    }
    session = _mapping(outcome.get("session"))
    if session:
        outcome_summary["session"] = {
            "status": session.get("status"),
            "line_count": session.get("line_count"),
            "message_count": session.get("message_count"),
            "invalid_line_count": session.get("invalid_line_count"),
        }

    if integrity_status == "invalid":
        deterministic_status = "invalid"
    elif integrity_status == "incomplete" or not outcome:
        deterministic_status = "incomplete"
    elif outcome.get("status") == "failed":
        deterministic_status = "failed"
    elif outcome.get("status") == "succeeded" and signals:
        deterministic_status = "completed_with_signals"
    elif outcome.get("status") == "succeeded":
        deterministic_status = "completed_clean"
    else:
        deterministic_status = "indeterminate"

    report.update(
        {
            "deterministic_status": deterministic_status,
            "integrity": {
                "status": integrity_status,
                "line_count": line_count,
                "record_count": len(records),
                "schemas": schemas,
                "source_format": source_format,
                "run_ids": run_ids,
                "sequence": {
                    "continuous_from_one_in_file_order": continuous,
                    "duplicate_values": duplicate_sequences,
                    "gap_ranges": _gap_ranges(sequences),
                },
                "elapsed_ms_monotonic": elapsed_monotonic,
                "boundaries": boundaries,
                "issues": issues,
            },
            "outcome": outcome_summary,
            "lifecycle": {
                "record_type_counts": dict(
                    sorted(Counter(_record_type(item) for item in records).items())
                )
            },
            "signals": signals,
            "artifacts": artifacts,
            "candidate_recoveries": recoveries,
            "llm_required_judgments": judgments,
        }
    )
    return report
