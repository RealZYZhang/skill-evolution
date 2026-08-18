#!/usr/bin/env python3
"""Build persistent, deterministic trajectory profiles for replay campaigns."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, Sequence
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.trajectory_viewer_data import (
    LoadIssue,
    ReplayRepository,
    RunDetail,
    RunSummary,
    ViewerDataError,
)


JsonObject = dict[str, Any]
PROFILE_SCHEMA = "trajectory.profile.v1"
HARNESS_RUN_SCHEMA = "harness.run.v1"
PROFILE_FILENAME = "trajectory-profile.json"
HARNESS_MANIFEST_FILENAME = "harness.json"
_SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".ts", ".sh"}
_SHELL_FILENAME_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+\."
    r"(?:html?|py|md|txt|json|css|js|mjs|ts|sh|docx|pdf))",
    re.IGNORECASE,
)
_VALIDATION_COMMAND_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:grep|wc|head|tail|tidy|xmllint)(?:\s|$)",
    re.IGNORECASE,
)
_GENERATOR_NAME_PATTERN = re.compile(
    r"(?:^|[_-])(?:gen|generate|generator|render)(?:[_-]|$)",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _new_harness_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _round_float(value: float) -> float:
    return round(value, 12)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _display_path(value: str) -> str:
    """Reduce a captured path to a useful non-host-specific label."""

    normalized = value.replace("\\", "/")
    marker = "/artifacts/"
    if marker in normalized:
        return "artifacts/" + normalized.rsplit(marker, 1)[1]
    if normalized.startswith("artifacts/"):
        return normalized
    return Path(normalized).name


def _path_from_arguments(arguments: Mapping[str, Any]) -> str | None:
    value = arguments.get("path")
    if isinstance(value, str) and value:
        return _display_path(value)
    return None


def _shell_filenames(command: str) -> set[str]:
    return {
        _display_path(match.group("name"))
        for match in _SHELL_FILENAME_PATTERN.finditer(command)
    }


def _first_manifest(records: Sequence[JsonObject]) -> Mapping[str, Any]:
    for record in records:
        if record.get("type") != "trajectory_started":
            continue
        manifest = _mapping(_mapping(record.get("payload")).get("manifest"))
        if manifest:
            return manifest
    return {}


def _declared_paths(
    detail: RunDetail,
    summary: RunSummary,
) -> tuple[set[str], set[str], set[str]]:
    manifest = _first_manifest(detail.records)
    task_case = _mapping(manifest.get("task_case"))

    artifact_paths: set[str] = set()
    artifact = _mapping(summary.artifact)
    artifact_path = artifact.get("path")
    if isinstance(artifact_path, str) and artifact_path:
        artifact_paths.add(_display_path(artifact_path))

    expected = task_case.get("expected_artifacts")
    values: list[Any]
    if isinstance(expected, list):
        values = expected
    else:
        values = [task_case.get("expected_artifact")]
    for value in values:
        if isinstance(value, str) and value:
            artifact_paths.add(_display_path(value))
        elif isinstance(value, Mapping):
            path = value.get("path")
            if isinstance(path, str) and path:
                artifact_paths.add(_display_path(path))
    if not artifact_paths:
        artifact_paths.add("artifacts/output.html")

    input_paths: set[str] = set()
    source = _mapping(manifest.get("source"))
    for value in (task_case.get("input"), source.get("path")):
        if isinstance(value, str) and value:
            input_paths.add(_display_path(value))
        elif isinstance(value, Mapping):
            path = value.get("path")
            if isinstance(path, str) and path:
                input_paths.add(_display_path(path))

    skill_paths: set[str] = set()
    skill = _mapping(manifest.get("skill"))
    entrypoint = skill.get("entrypoint")
    if isinstance(entrypoint, str) and entrypoint:
        skill_paths.add(_display_path(entrypoint))
    skill_paths.add("artifacts/skill/SKILL.md")
    skill_paths.add("SKILL.md")
    return artifact_paths, input_paths, skill_paths


def _matches_declared(path: str, declared: set[str]) -> bool:
    name = Path(path).name
    return any(path == item or name == Path(item).name for item in declared)


def _evidence_ref(
    campaign_id: str,
    run_id: str,
    sequence: int,
    trajectory_path: str,
) -> JsonObject:
    return {
        "campaign_id": campaign_id,
        "run_id": run_id,
        "seq": sequence,
        "trajectory_path": trajectory_path,
    }


@dataclass
class _StrategyState:
    """Mutable state used only while classifying one ordered trajectory."""

    read_sequences: dict[str, int]
    mutation_sequences: dict[str, int]
    generator_paths: set[str]
    unmatched_failures: list[JsonObject]


def _tool_targets(
    payload: Mapping[str, Any],
    *,
    artifact_paths: set[str],
    generator_paths: set[str],
) -> tuple[set[str], str, str]:
    arguments = _mapping(payload.get("arguments"))
    tool_name = str(payload.get("tool_name", "unknown"))
    command_value = arguments.get("command")
    command = command_value if isinstance(command_value, str) else ""
    targets: set[str] = set()
    argument_path = _path_from_arguments(arguments)
    if argument_path:
        targets.add(argument_path)
    targets.update(_shell_filenames(command))

    content_value = arguments.get("content")
    content = content_value if isinstance(content_value, str) else ""
    if (
        tool_name == "write"
        and argument_path
        and Path(argument_path).suffix.lower() in _SCRIPT_SUFFIXES
        and any(Path(path).name in content for path in artifact_paths)
    ):
        targets.update(artifact_paths)
    if (
        tool_name == "bash"
        and generator_paths
        and any(Path(path).name in command for path in generator_paths)
    ):
        targets.update(artifact_paths)
    return targets, command, content


def _is_generator(
    path: str | None,
    content: str,
    artifact_paths: set[str],
) -> bool:
    if not path or Path(path).suffix.lower() not in _SCRIPT_SUFFIXES:
        return False
    stem = Path(path).stem
    return bool(
        _GENERATOR_NAME_PATTERN.search(stem)
        or any(Path(item).name in content for item in artifact_paths)
    )


def _artifact_mode(
    categories: Sequence[str],
) -> str | None:
    precedence = (
        ("artifact_merge", "merge"),
        ("partitioned_artifact_write", "partition"),
        ("chunked_artifact_write", "chunked"),
        ("generator_executed", "generator"),
        ("direct_artifact_write", "direct"),
    )
    for category, mode in precedence:
        if category in categories:
            return mode
    return None


def _classify_tool_action(
    *,
    campaign_id: str,
    run_id: str,
    record: JsonObject,
    trajectory_path: str,
    artifact_paths: set[str],
    input_paths: set[str],
    skill_paths: set[str],
    state: _StrategyState,
) -> tuple[
    JsonObject,
    list[JsonObject],
    list[JsonObject],
    JsonObject | None,
    JsonObject | None,
]:
    payload = _mapping(record.get("payload"))
    sequence = record.get("seq")
    if not isinstance(sequence, int):
        sequence = 0
    tool_name = str(payload.get("tool_name", "unknown"))
    status = str(payload.get("status", "unknown"))
    arguments = _mapping(payload.get("arguments"))
    path = _path_from_arguments(arguments)
    targets, command, content = _tool_targets(
        payload,
        artifact_paths=artifact_paths,
        generator_paths=state.generator_paths,
    )
    categories: set[str] = set()
    repeated_reads: list[JsonObject] = []
    rework: list[JsonObject] = []

    if tool_name == "read":
        if path and _matches_declared(path, skill_paths):
            categories.add("read_skill")
        elif path and _matches_declared(path, input_paths):
            categories.add("read_input")
        elif path and _matches_declared(path, artifact_paths):
            categories.update({"read_artifact", "validation"})
        else:
            categories.add("read_other")
        if path and path in state.read_sequences:
            previous_seq = state.read_sequences[path]
            categories.add("repeated_read")
            repeated_reads.append(
                {
                    "target": path,
                    "previous_seq": previous_seq,
                    "seq": sequence,
                    "evidence": [
                        _evidence_ref(
                            campaign_id,
                            run_id,
                            previous_seq,
                            trajectory_path,
                        ),
                        _evidence_ref(
                            campaign_id,
                            run_id,
                            sequence,
                            trajectory_path,
                        ),
                    ],
                }
            )
        if path:
            state.read_sequences[path] = sequence

    is_generator = _is_generator(path, content, artifact_paths)
    if tool_name in {"write", "edit"}:
        if path and _matches_declared(path, artifact_paths):
            categories.add("direct_artifact_write")
        elif path and re.search(r"(?:part|chunk)[_-]?\d*", path, re.I):
            if Path(path).suffix.lower() in {".htm", ".html"}:
                categories.add("partitioned_artifact_write")
        if is_generator:
            categories.add("temporary_generator_created")
            if path:
                state.generator_paths.add(path)

    command_lower = command.lower()
    artifact_names = {Path(item).name.lower() for item in artifact_paths}
    mentions_artifact = any(name in command_lower for name in artifact_names)
    mentions_part = bool(
        re.search(r"(?:part|chunk)[_-]?\d*\.html?", command_lower)
    )
    if tool_name == "bash":
        if ">>" in command and mentions_artifact:
            categories.add("chunked_artifact_write")
        elif re.search(r"\bcat\b", command_lower) and mentions_part:
            part_mentions = re.findall(
                r"(?:part|chunk)[_-]?\d*\.html?",
                command_lower,
            )
            if mentions_artifact and len(part_mentions) >= 2:
                categories.add("artifact_merge")
            else:
                categories.add("partitioned_artifact_write")
        elif (
            re.search(r"\bcat\b", command_lower)
            and re.search(r"(?<!>)>(?!>)", command)
            and mentions_artifact
        ):
            categories.add("direct_artifact_write")
        if state.generator_paths and any(
            Path(item).name.lower() in command_lower
            for item in state.generator_paths
        ):
            categories.add("generator_executed")
        if _VALIDATION_COMMAND_PATTERN.search(command):
            categories.add("validation")
        if re.search(r"(?:^|[\s;&|])rm(?:\s|$)", command_lower):
            categories.add("cleanup")

    mutation_targets: set[str] = set()
    if tool_name in {"write", "edit"} and path:
        mutation_targets.add(path)
    if {
        "direct_artifact_write",
        "chunked_artifact_write",
        "artifact_merge",
        "generator_executed",
    } & categories:
        mutation_targets.update(artifact_paths)

    chunk_or_partition = bool(
        {
            "chunked_artifact_write",
            "partitioned_artifact_write",
            "artifact_merge",
        }
        & categories
    )
    if status == "succeeded":
        for target in sorted(mutation_targets):
            previous_seq = state.mutation_sequences.get(target)
            if previous_seq is not None and not chunk_or_partition:
                categories.add("rework")
                rework.append(
                    {
                        "target": target,
                        "previous_seq": previous_seq,
                        "seq": sequence,
                        "evidence": [
                            _evidence_ref(
                                campaign_id,
                                run_id,
                                previous_seq,
                                trajectory_path,
                            ),
                            _evidence_ref(
                                campaign_id,
                                run_id,
                                sequence,
                                trajectory_path,
                            ),
                        ],
                    }
                )
            state.mutation_sequences[target] = sequence

    evidence = _evidence_ref(
        campaign_id,
        run_id,
        sequence,
        trajectory_path,
    )
    matched_retry: JsonObject | None = None
    for failure in reversed(state.unmatched_failures):
        failed_targets = set(failure["targets"])
        same_target = bool(targets & failed_targets)
        same_targetless_tool = (
            not targets
            and not failed_targets
            and tool_name == failure["tool_name"]
        )
        if not same_target and not same_targetless_tool:
            continue
        categories.add("retry_after_failure")
        matched_retry = {
            "failed_seq": failure["seq"],
            "retry_seq": sequence,
            "tool_changed": failure["tool_name"] != tool_name,
            "target": sorted(targets & failed_targets)[0]
            if targets & failed_targets
            else None,
            "retry_status": status,
            "evidence": [failure["evidence"], evidence],
        }
        state.unmatched_failures.remove(failure)
        break

    if status in {"failed", "interrupted"}:
        categories.add("failed_action")
        state.unmatched_failures.append(
            {
                "seq": sequence,
                "tool_name": tool_name,
                "targets": sorted(targets),
                "evidence": evidence,
            }
        )

    if not categories:
        categories.add("other_tool")
    sequence_item = {
        "seq": sequence,
        "tool_name": tool_name,
        "status": status,
        "targets": sorted(targets),
        "categories": sorted(categories),
        "evidence": evidence,
    }
    artifact_mode = _artifact_mode(sequence_item["categories"])
    artifact_write = None
    if artifact_mode is not None:
        artifact_write = {
            "seq": sequence,
            "status": status,
            "mode": artifact_mode,
            "evidence": evidence,
        }
    return sequence_item, repeated_reads, rework, matched_retry, artifact_write


def _profile_strategies(
    campaign_id: str,
    detail: RunDetail,
    trajectory_path: str,
) -> JsonObject:
    summary = detail.summary
    artifact_paths, input_paths, skill_paths = _declared_paths(
        detail,
        summary,
    )
    state = _StrategyState({}, {}, set(), [])
    sequence: list[JsonObject] = []
    repeated_reads: list[JsonObject] = []
    rework: list[JsonObject] = []
    retries: list[JsonObject] = []
    artifact_writes: list[JsonObject] = []
    counts: Counter[str] = Counter()
    failures: list[JsonObject] = []

    for record in detail.records:
        if record.get("type") != "tool_action":
            continue
        (
            item,
            repeated,
            reworked,
            retry,
            artifact_write,
        ) = _classify_tool_action(
            campaign_id=campaign_id,
            run_id=summary.run_id,
            record=record,
            trajectory_path=trajectory_path,
            artifact_paths=artifact_paths,
            input_paths=input_paths,
            skill_paths=skill_paths,
            state=state,
        )
        sequence.append(item)
        repeated_reads.extend(repeated)
        rework.extend(reworked)
        if retry is not None:
            retries.append(retry)
        if artifact_write is not None:
            artifact_writes.append(artifact_write)
        counts.update(item["categories"])
        if "failed_action" in item["categories"]:
            failures.append(
                {
                    "seq": item["seq"],
                    "tool_name": item["tool_name"],
                    "status": item["status"],
                    "targets": item["targets"],
                    "evidence": item["evidence"],
                }
            )
    return {
        "counts": dict(sorted(counts.items())),
        "sequence": sequence,
        "failed_actions": failures,
        "retries": retries,
        "repeated_reads": repeated_reads,
        "rework": rework,
        "artifact_writes": artifact_writes,
        "first_artifact_write": (
            artifact_writes[0] if artifact_writes else None
        ),
        "unmatched_failed_seqs": [
            failure["seq"] for failure in state.unmatched_failures
        ],
    }


def _resource_facts(
    summary: RunSummary,
    detail: RunDetail | None,
    strategies: Mapping[str, Any],
) -> JsonObject:
    usage = _mapping(summary.usage)
    token_usage = {
        "input": _number(usage.get("input")) or 0,
        "output": _number(usage.get("output")) or 0,
        "cache_read": _number(usage.get("cache_read")) or 0,
        "cache_write": _number(usage.get("cache_write")) or 0,
    }
    tool_duration_ms = 0
    interrupted_actions = 0
    if detail is not None:
        for record in detail.records:
            if record.get("type") == "action_interrupted":
                interrupted_actions += 1
            if record.get("type") != "tool_action":
                continue
            duration = _number(
                _mapping(record.get("payload")).get("duration_ms")
            )
            if duration is not None:
                tool_duration_ms += duration
    counts = _mapping(strategies.get("counts"))
    return {
        "duration_ms": summary.duration_ms,
        "record_count": summary.record_count,
        "turn_count": summary.turn_count,
        "message_count": summary.message_count,
        "assistant_message_count": summary.assistant_message_count,
        "model_calls": int(_number(usage.get("model_calls")) or 0),
        "calls_with_usage": int(
            _number(usage.get("calls_with_usage")) or 0
        ),
        "tokens": token_usage,
        "reported_cost_total": _number(
            usage.get("reported_cost_total")
        )
        or 0,
        "tool_actions": summary.tool_count,
        "failed_tool_actions": summary.failed_tool_count,
        "interrupted_actions": interrupted_actions,
        "tool_duration_ms": tool_duration_ms,
        "retry_count": int(_number(counts.get("retry_after_failure")) or 0),
        "repeated_read_count": int(_number(counts.get("repeated_read")) or 0),
        "rework_count": int(_number(counts.get("rework")) or 0),
    }


_AGGREGATE_METRICS = {
    "duration_ms": ("resources", "duration_ms"),
    "model_calls": ("resources", "model_calls"),
    "input_tokens": ("resources", "tokens", "input"),
    "output_tokens": ("resources", "tokens", "output"),
    "cache_read_tokens": ("resources", "tokens", "cache_read"),
    "cache_write_tokens": ("resources", "tokens", "cache_write"),
    "reported_cost_total": ("resources", "reported_cost_total"),
    "tool_actions": ("resources", "tool_actions"),
    "failed_tool_actions": ("resources", "failed_tool_actions"),
    "tool_duration_ms": ("resources", "tool_duration_ms"),
    "retry_count": ("resources", "retry_count"),
    "repeated_read_count": ("resources", "repeated_read_count"),
    "rework_count": ("resources", "rework_count"),
}


def _nested_value(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _outlier_run_ids(samples: Sequence[tuple[str, int | float]]) -> list[str]:
    if len(samples) < 4:
        return []
    values = [float(value) for _, value in samples]
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    lower_quartile, upper_quartile = quartiles[0], quartiles[2]
    interquartile_range = upper_quartile - lower_quartile
    lower_bound = lower_quartile - 1.5 * interquartile_range
    upper_bound = upper_quartile + 1.5 * interquartile_range
    return [
        run_id
        for run_id, value in samples
        if float(value) < lower_bound or float(value) > upper_bound
    ]


def _aggregate_metric(
    runs: Sequence[JsonObject],
    path: Sequence[str],
) -> JsonObject:
    samples: list[tuple[str, int | float]] = []
    missing_run_ids: list[str] = []
    for run in runs:
        run_id = str(run.get("run_id", "unknown"))
        value = _number(_nested_value(run, path))
        if value is None or not math.isfinite(float(value)):
            missing_run_ids.append(run_id)
        else:
            samples.append((run_id, value))
    if not samples:
        return {
            "sample_count": 0,
            "missing_run_ids": missing_run_ids,
            "min": None,
            "median": None,
            "max": None,
            "mean": None,
            "coefficient_of_variation": None,
            "outlier_run_ids": [],
        }
    values = [value for _, value in samples]
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)
    coefficient = deviation / mean if mean != 0 else None
    return {
        "sample_count": len(samples),
        "missing_run_ids": missing_run_ids,
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "mean": _round_float(mean),
        "coefficient_of_variation": (
            _round_float(coefficient)
            if coefficient is not None
            else None
        ),
        "outlier_run_ids": _outlier_run_ids(samples),
    }


def _profile_load_status(
    runs: Sequence[JsonObject],
    campaign_load_status: str,
) -> str:
    if not runs:
        return "error"
    statuses = {str(run.get("load_status", "error")) for run in runs}
    if statuses == {"ok"} and campaign_load_status == "ok":
        return "ok"
    if "ok" in statuses or "partial" in statuses:
        return "partial"
    return "error"


def _issue_dict(issue: LoadIssue) -> JsonObject:
    return issue.to_dict()


@dataclass(frozen=True)
class PersistedProfile:
    """Paths and data produced by one persisted profiler invocation."""

    harness_directory: Path
    harness_manifest: JsonObject
    profile: JsonObject


class TrajectoryProfiler:
    """Profile one replay campaign without modifying preserved source data."""

    def __init__(self, replays_root: str | Path) -> None:
        self.repository = ReplayRepository(replays_root)

    def _trajectory_path(self, campaign_id: str, run_id: str) -> str:
        relative_root = Path("runs") / run_id
        run_directory = (
            self.repository.replays_root / campaign_id / relative_root
        )
        filename = "trajectory.jsonl"
        if not (run_directory / filename).is_file() and (
            run_directory / "trace.jsonl"
        ).is_file():
            filename = "trace.jsonl"
        return (relative_root / filename).as_posix()

    def profile_campaign(self, campaign_id: str) -> JsonObject:
        """Return a deterministic profile for all listed campaign runs."""

        campaign = self.repository.get_campaign(campaign_id)
        run_profiles: list[JsonObject] = []
        campaign_issues = [_issue_dict(issue) for issue in campaign.issues]

        for summary in campaign.runs:
            trajectory_path = self._trajectory_path(campaign_id, summary.run_id)
            detail: RunDetail | None = None
            detail_issues: list[JsonObject] = []
            try:
                detail = self.repository.get_run(
                    campaign_id,
                    summary.run_id,
                )
            except ViewerDataError as error:
                detail_issues.append(
                    {
                        "code": error.code,
                        "message": error.message,
                        "severity": "error",
                        "path": None,
                        "line": None,
                    }
                )
            strategies = (
                _profile_strategies(campaign_id, detail, trajectory_path)
                if detail is not None
                else {
                    "counts": {},
                    "sequence": [],
                    "failed_actions": [],
                    "retries": [],
                    "repeated_reads": [],
                    "rework": [],
                    "artifact_writes": [],
                    "first_artifact_write": None,
                    "unmatched_failed_seqs": [],
                }
            )
            issues = [
                *[_issue_dict(issue) for issue in summary.issues],
                *detail_issues,
            ]
            run_profiles.append(
                {
                    "run_id": summary.run_id,
                    "index": summary.index,
                    "status": summary.status,
                    "load_status": summary.load_status,
                    "trajectory": {
                        "path": (
                            trajectory_path
                        ),
                        "record_count": summary.record_count,
                        "sequence_contiguous": (
                            summary.sequence_contiguous
                        ),
                        "sealed": summary.sealed,
                    },
                    "resources": _resource_facts(
                        summary,
                        detail,
                        strategies,
                    ),
                    "strategies": strategies,
                    "issues": issues,
                }
            )

        aggregate = {
            metric: _aggregate_metric(run_profiles, path)
            for metric, path in _AGGREGATE_METRICS.items()
        }
        return {
            "schema": PROFILE_SCHEMA,
            "profile_id": None,
            "created_at": _utc_now(),
            "source": {
                "campaign_id": campaign_id,
                "campaign_schema": campaign.summary.schema,
                "campaign_status": campaign.summary.status,
                "run_count": campaign.summary.run_count,
            },
            "load_status": _profile_load_status(
                run_profiles,
                campaign.summary.load_status,
            ),
            "runs": run_profiles,
            "aggregate": aggregate,
            "issues": campaign_issues,
        }

    def persist_campaign(
        self,
        campaign_id: str,
        output_root: str | Path = ".skill-evolution/harness-runs",
    ) -> PersistedProfile:
        """Persist one immutable profile beneath a new harness directory."""

        harness_run_id = _new_harness_run_id()
        harness_directory = Path(output_root).resolve() / harness_run_id
        harness_directory.mkdir(parents=True, exist_ok=False)
        started_at = _utc_now()
        manifest_path = harness_directory / HARNESS_MANIFEST_FILENAME
        manifest: JsonObject = {
            "schema": HARNESS_RUN_SCHEMA,
            "harness_run_id": harness_run_id,
            "kind": "trajectory_profile",
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "source": {"campaign_id": campaign_id},
            "outputs": {},
            "error": None,
        }
        _atomic_write_json(manifest_path, manifest)
        try:
            profile = self.profile_campaign(campaign_id)
            profile["profile_id"] = harness_run_id
            profile_path = harness_directory / PROFILE_FILENAME
            _atomic_write_json(profile_path, profile)
            load_status = str(profile["load_status"])
            manifest["status"] = (
                "completed"
                if load_status == "ok"
                else f"completed_{load_status}"
            )
            manifest["ended_at"] = _utc_now()
            manifest["outputs"] = {
                "trajectory_profile": PROFILE_FILENAME,
            }
            _atomic_write_json(manifest_path, manifest)
        except Exception as error:
            manifest["status"] = "failed"
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            _atomic_write_json(manifest_path, manifest)
            raise
        return PersistedProfile(
            harness_directory=harness_directory,
            harness_manifest=manifest,
            profile=profile,
        )


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replays-root",
        default=".skill-evolution/replays",
        help="Parent directory containing replay campaigns",
    )
    parser.add_argument(
        "--campaign-id",
        required=True,
        help="Replay campaign identifier to profile",
    )
    parser.add_argument(
        "--output-root",
        default=".skill-evolution/harness-runs",
        help="Parent directory for persistent harness runs",
    )
    options = parser.parse_args(arguments)
    result = TrajectoryProfiler(options.replays_root).persist_campaign(
        options.campaign_id,
        options.output_root,
    )
    print(result.harness_directory)
    return 0 if result.profile["load_status"] != "error" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (FileNotFoundError, OSError, ValueError, ViewerDataError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
