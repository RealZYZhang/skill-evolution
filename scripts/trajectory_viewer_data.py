"""Read replay trajectories into stable, presentation-oriented view models."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from skill_evolution.trajectory_user_report import (
    TrajectoryUserReportError,
    validate_trajectory_user_report,
)


JsonObject = dict[str, Any]
API_SCHEMA = "viewer.api.v1"
CAMPAIGN_SCHEMA = "replay.campaign.v1"
TRAJECTORY_SCHEMA = "trajectory.actions.v1"
LEGACY_ACTION_SCHEMA = "trace.actions.v1"
_LEGACY_RECORD_TYPES = {
    "trace_started": "trajectory_started",
    "trace_finished": "trajectory_finished",
    "trace_sealed": "trajectory_sealed",
}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KNOWN_RECORD_TYPES = {
    "action_interrupted",
    "agent_end",
    "agent_settled",
    "agent_start",
    "artifact_registered",
    "message_action",
    "observer_error",
    "pi_event",
    "pi_process_exited",
    "pi_process_started",
    "pi_process_starting",
    "process_stderr",
    "rpc_protocol_error",
    "runtime_observed",
    "session_captured",
    "skill_resolved",
    "tool_action",
    "trajectory_finished",
    "trajectory_sealed",
    "trajectory_started",
    "turn_end",
    "turn_start",
}
TECHNICAL_RECORD_TYPES = {
    "agent_end",
    "agent_settled",
    "agent_start",
    "artifact_registered",
    "pi_process_exited",
    "pi_process_started",
    "pi_process_starting",
    "runtime_observed",
    "session_captured",
    "skill_resolved",
    "trajectory_finished",
    "trajectory_sealed",
    "trajectory_started",
    "turn_end",
    "turn_start",
}


class ViewerDataError(Exception):
    """A safe error that the HTTP adapter may expose to a local reader."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _normalize_record_type(value: JsonObject) -> JsonObject:
    record_type = value.get("type")
    canonical = _LEGACY_RECORD_TYPES.get(record_type)
    if canonical is None:
        return value
    return {**value, "type": canonical}


@dataclass(frozen=True)
class LoadIssue:
    """A concrete problem found while loading preserved replay data."""

    code: str
    message: str
    severity: str = "error"
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable issue."""

        return asdict(self)


@dataclass(frozen=True)
class CampaignSummary:
    """Compact campaign information used by discovery and navigation."""

    campaign_id: str
    schema: str | None
    status: str
    started_at: str | None
    ended_at: str | None
    duration_ms: int | float | None
    run_count: int
    succeeded: int
    failed: int
    orchestration_failed: int
    load_status: str
    issues: tuple[LoadIssue, ...]

    def to_dict(self) -> JsonObject:
        """Return the public API representation."""

        value = asdict(self)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        return value


@dataclass(frozen=True)
class RunSummary:
    """Deterministic facts derived from one run and its trajectory."""

    run_id: str
    index: int | None
    status: str
    started_at: str | None
    ended_at: str | None
    duration_ms: int | float | None
    record_count: int
    turn_count: int
    message_count: int
    assistant_message_count: int
    tool_count: int
    failed_tool_count: int
    tool_statuses: JsonObject
    tool_names: JsonObject
    usage: JsonObject
    artifact: JsonObject | None
    session: JsonObject | None
    session_status: str | None
    model: JsonObject | None
    thinking_level: str | None
    tools: tuple[str, ...]
    skill_loaded: bool | None
    sequence_contiguous: bool
    sealed: bool
    load_status: str
    issues: tuple[LoadIssue, ...]

    def to_dict(self) -> JsonObject:
        """Return the public API representation."""

        value = asdict(self)
        value["tools"] = list(self.tools)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        return value


@dataclass(frozen=True)
class SetupSnapshot:
    """Campaign-level setup plus explicit per-run runtime observations."""

    prompt: JsonObject
    skill: JsonObject
    input: JsonObject
    common: JsonObject
    differences: tuple[JsonObject, ...]
    runs: tuple[JsonObject, ...]
    issues: tuple[LoadIssue, ...]

    def to_dict(self) -> JsonObject:
        """Return the public API representation."""

        value = asdict(self)
        value["differences"] = list(self.differences)
        value["runs"] = list(self.runs)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        return value


@dataclass(frozen=True)
class CampaignDetail:
    """Complete campaign view returned by the campaign endpoint."""

    summary: CampaignSummary
    manifest: JsonObject | None
    setup: SetupSnapshot
    runs: tuple[RunSummary, ...]
    issues: tuple[LoadIssue, ...]

    def to_dict(self) -> JsonObject:
        """Return the public API representation."""

        return {
            "schema": API_SCHEMA,
            "summary": self.summary.to_dict(),
            "manifest": self.manifest,
            "setup": self.setup.to_dict(),
            "runs": [run.to_dict() for run in self.runs],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class RunDetail:
    """Complete run view with normalized and original trajectory records."""

    summary: RunSummary
    timeline: tuple[JsonObject, ...]
    relations: JsonObject
    records: tuple[JsonObject, ...]
    issues: tuple[LoadIssue, ...]

    def to_dict(self) -> JsonObject:
        """Return the public API representation."""

        return {
            "schema": API_SCHEMA,
            "summary": self.summary.to_dict(),
            "timeline": list(self.timeline),
            "relations": self.relations,
            "records": list(self.records),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class _RunBundle:
    record: JsonObject
    directory: Path | None
    records: tuple[JsonObject, ...]
    issues: tuple[LoadIssue, ...]
    summary: RunSummary


class TrajectoryUserReportRepository:
    """Read the newest validated user report for one globally unique run."""

    def __init__(self, analyses_root: str | Path) -> None:
        self.analyses_root = Path(analyses_root).resolve()

    def get_for_run(self, run_id: str) -> JsonObject:
        """Return one validated report without exposing rejected model text."""

        if not IDENTIFIER_PATTERN.fullmatch(run_id):
            raise ViewerDataError(
                "invalid_run_id",
                f"Invalid run identifier: {run_id!r}",
            )
        agent_runs = self.analyses_root / "agent-runs"
        if not agent_runs.is_dir():
            raise ViewerDataError(
                "trajectory_analysis_not_found",
                "This run does not have a saved single-trajectory analysis.",
                404,
            )
        candidates: list[tuple[str, Path, Mapping[str, Any]]] = []
        for directory in agent_runs.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            report_path = directory / "user-report.json"
            if not report_path.is_file() or report_path.is_symlink():
                continue
            resolved = report_path.resolve()
            if not resolved.is_relative_to(self.analyses_root):
                continue
            try:
                raw = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, Mapping) or raw.get("run_id") != run_id:
                continue
            generated_at = raw.get("generated_at")
            sort_key = generated_at if isinstance(generated_at, str) else ""
            candidates.append((sort_key, report_path, raw))
        if not candidates:
            raise ViewerDataError(
                "trajectory_analysis_not_found",
                "This run does not have a saved single-trajectory analysis.",
                404,
            )
        _, report_path, raw = max(candidates, key=lambda item: item[0])
        try:
            report = validate_trajectory_user_report(raw)
        except TrajectoryUserReportError as error:
            raise ViewerDataError(
                "trajectory_analysis_invalid",
                f"The saved user report is invalid: {report_path.name}",
                422,
            ) from error
        return {
            "schema": API_SCHEMA,
            "report": report,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _load_status(
    issues: Sequence[LoadIssue],
    *,
    has_data: bool,
) -> str:
    if not issues:
        return "ok"
    return "partial" if has_data else "error"


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _redact_hidden_reasoning(value: Any) -> Any:
    """Preserve record shape without exposing hidden model reasoning."""

    if isinstance(value, list):
        return [_redact_hidden_reasoning(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    block_type = value.get("type")
    if (
        isinstance(block_type, str)
        and block_type.lower() in {"thinking", "reasoning"}
    ):
        return {"type": block_type, "redacted": True}
    result: JsonObject = {}
    protected_keys = {
        "chain_of_thought",
        "reasoning_content",
        "reasoningcontent",
        "thinking_content",
        "thinkingcontent",
    }
    for key, item in value.items():
        normalized = str(key).replace("-", "_").lower()
        if normalized in protected_keys:
            result[str(key)] = "[REDACTED: hidden reasoning]"
        elif normalized == "reasoning" and not isinstance(
            item,
            (bool, int, float, type(None)),
        ):
            result[str(key)] = "[REDACTED: hidden reasoning]"
        else:
            result[str(key)] = _redact_hidden_reasoning(item)
    return result


class ReplayRepository:
    """Read-only access to replay campaign files beneath one trusted root."""

    def __init__(self, replays_root: str | Path) -> None:
        self.replays_root = Path(replays_root).resolve()

    def list_campaigns(self) -> JsonObject:
        """Discover every campaign directory, including unreadable entries."""

        self._require_root()
        summaries: list[JsonObject] = []
        for path in sorted(
            self.replays_root.iterdir(),
            key=lambda item: item.name,
            reverse=True,
        ):
            if path.name.startswith(".") or not path.is_dir():
                continue
            summaries.append(self._summarize_campaign_path(path).to_dict())
        return {
            "schema": API_SCHEMA,
            "replays_root": str(self.replays_root),
            "campaigns": summaries,
        }

    def get_campaign(self, campaign_id: str) -> CampaignDetail:
        """Load one campaign, its setup, and all run summaries."""

        campaign_directory = self._campaign_directory(campaign_id)
        manifest, manifest_issues = self._read_manifest(campaign_directory)
        summary = self._campaign_summary(
            campaign_id,
            manifest,
            manifest_issues,
        )
        if manifest is None:
            empty_setup = SetupSnapshot(
                prompt={},
                skill={},
                input={},
                common={},
                differences=(),
                runs=(),
                issues=tuple(manifest_issues),
            )
            return CampaignDetail(
                summary=summary,
                manifest=None,
                setup=empty_setup,
                runs=(),
                issues=tuple(manifest_issues),
            )

        campaign_issues = list(manifest_issues)
        bundles: list[_RunBundle] = []
        run_records = manifest.get("runs")
        if not isinstance(run_records, list):
            campaign_issues.append(
                LoadIssue(
                    code="invalid_runs",
                    message="Campaign manifest field 'runs' must be a list.",
                    path="replay.json",
                )
            )
            run_records = []
        for position, value in enumerate(run_records, start=1):
            if not isinstance(value, Mapping):
                issue = LoadIssue(
                    code="invalid_run_record",
                    message=f"Run entry {position} is not an object.",
                    path="replay.json",
                )
                campaign_issues.append(issue)
                continue
            bundle = self._load_run_bundle(
                campaign_directory,
                dict(value),
            )
            bundles.append(bundle)

        setup = self._build_setup(
            campaign_directory,
            manifest,
            bundles,
        )
        campaign_issues.extend(setup.issues)
        detail_summary = self._campaign_summary(
            campaign_id,
            manifest,
            campaign_issues,
        )
        return CampaignDetail(
            summary=detail_summary,
            manifest=manifest,
            setup=setup,
            runs=tuple(bundle.summary for bundle in bundles),
            issues=tuple(campaign_issues),
        )

    def get_run(self, campaign_id: str, run_id: str) -> RunDetail:
        """Load one run with normalized groups and raw trajectory records."""

        self._validate_identifier(run_id, "run")
        campaign_directory = self._campaign_directory(campaign_id)
        manifest, issues = self._read_manifest(campaign_directory)
        if manifest is None:
            raise ViewerDataError(
                "campaign_unreadable",
                f"Campaign '{campaign_id}' has no readable manifest.",
                422,
            )
        run_record = self._find_run_record(manifest, run_id)
        if run_record is None:
            raise ViewerDataError(
                "run_not_found",
                f"Run '{run_id}' is not listed by campaign '{campaign_id}'.",
                404,
            )
        bundle = self._load_run_bundle(campaign_directory, run_record)
        relations = self._build_relations(bundle.records)
        timeline = self._build_timeline(bundle.records, relations)
        all_issues = [*issues, *bundle.issues]
        return RunDetail(
            summary=bundle.summary,
            timeline=tuple(timeline),
            relations=relations,
            records=bundle.records,
            issues=tuple(all_issues),
        )

    def get_run_file(
        self,
        campaign_id: str,
        run_id: str,
        kind: str,
    ) -> Path:
        """Resolve an allow-listed run artifact without leaving the root."""

        if kind not in {"artifact", "session"}:
            raise ViewerDataError(
                "invalid_file_kind",
                f"Unsupported run file kind: {kind}",
            )
        self._validate_identifier(run_id, "run")
        campaign_directory = self._campaign_directory(campaign_id)
        manifest, _ = self._read_manifest(campaign_directory)
        if manifest is None:
            raise ViewerDataError(
                "campaign_unreadable",
                f"Campaign '{campaign_id}' has no readable manifest.",
                422,
            )
        run_record = self._find_run_record(manifest, run_id)
        if run_record is None:
            raise ViewerDataError(
                "run_not_found",
                f"Run '{run_id}' is not listed by campaign '{campaign_id}'.",
                404,
            )
        run_directory, directory_issues = self._resolve_run_directory(
            campaign_directory,
            run_record,
        )
        if run_directory is None:
            message = (
                directory_issues[0].message
                if directory_issues
                else f"Run directory for '{run_id}' is unavailable."
            )
            raise ViewerDataError("run_unreadable", message, 422)

        if kind == "artifact":
            artifact = _mapping(run_record.get("artifact"))
            relative = artifact.get("path", "artifacts/output.html")
            base = run_directory
        else:
            relative = run_record.get("session")
            base = campaign_directory
            if not isinstance(relative, str) or not relative:
                relative = str(
                    run_directory.relative_to(campaign_directory)
                    / "pi-session.jsonl"
                )
        if not isinstance(relative, str) or not relative:
            raise ViewerDataError(
                "file_reference_missing",
                f"Run '{run_id}' does not declare a {kind} file.",
                404,
            )
        path = self._resolve_inside(base, relative, campaign_directory)
        if not path.is_file():
            raise ViewerDataError(
                "run_file_not_found",
                f"The {kind} file for run '{run_id}' does not exist.",
                404,
            )
        return path

    def _require_root(self) -> None:
        if not self.replays_root.is_dir():
            raise ViewerDataError(
                "replays_root_not_found",
                f"Replay root does not exist: {self.replays_root}",
                404,
            )

    def _validate_identifier(self, value: str, label: str) -> None:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ViewerDataError(
                f"invalid_{label}_id",
                f"Invalid {label} identifier: {value!r}",
            )

    def _campaign_directory(self, campaign_id: str) -> Path:
        self._require_root()
        self._validate_identifier(campaign_id, "campaign")
        candidate = (self.replays_root / campaign_id).resolve()
        if not candidate.is_relative_to(self.replays_root):
            raise ViewerDataError(
                "path_outside_root",
                "Campaign path leaves the configured replay root.",
            )
        if not candidate.is_dir():
            raise ViewerDataError(
                "campaign_not_found",
                f"Campaign '{campaign_id}' was not found.",
                404,
            )
        return candidate

    def _resolve_inside(
        self,
        base: Path,
        relative: str,
        boundary: Path,
    ) -> Path:
        reference = Path(relative)
        if reference.is_absolute():
            raise ViewerDataError(
                "absolute_reference_rejected",
                f"Absolute file reference is not allowed: {relative}",
            )
        candidate = (base / reference).resolve()
        resolved_boundary = boundary.resolve()
        if not candidate.is_relative_to(resolved_boundary):
            raise ViewerDataError(
                "path_outside_root",
                f"File reference leaves its campaign: {relative}",
            )
        if not candidate.is_relative_to(self.replays_root):
            raise ViewerDataError(
                "path_outside_root",
                f"File reference leaves the replay root: {relative}",
            )
        return candidate

    def _summarize_campaign_path(self, path: Path) -> CampaignSummary:
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.replays_root):
                issue = LoadIssue(
                    code="path_outside_root",
                    message="Campaign directory resolves outside replay root.",
                    path=path.name,
                )
                return self._campaign_summary(path.name, None, [issue])
            manifest, issues = self._read_manifest(resolved)
            return self._campaign_summary(path.name, manifest, issues)
        except OSError as error:
            issue = LoadIssue(
                code="campaign_unreadable",
                message=str(error),
                path=path.name,
            )
            return self._campaign_summary(path.name, None, [issue])

    def _read_manifest(
        self,
        campaign_directory: Path,
    ) -> tuple[JsonObject | None, list[LoadIssue]]:
        path = campaign_directory / "replay.json"
        if not path.is_file():
            return None, [
                LoadIssue(
                    code="manifest_missing",
                    message="Campaign has no replay.json manifest.",
                    path="replay.json",
                )
            ]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return None, [
                LoadIssue(
                    code="manifest_invalid",
                    message=f"Unable to read replay.json: {error}",
                    path="replay.json",
                )
            ]
        if not isinstance(value, dict):
            return None, [
                LoadIssue(
                    code="manifest_invalid",
                    message="Campaign manifest must contain a JSON object.",
                    path="replay.json",
                )
            ]
        issues: list[LoadIssue] = []
        if value.get("schema") != CAMPAIGN_SCHEMA:
            issues.append(
                LoadIssue(
                    code="unsupported_campaign_schema",
                    message=(
                        "Expected campaign schema "
                        f"'{CAMPAIGN_SCHEMA}', found {value.get('schema')!r}."
                    ),
                    path="replay.json",
                )
            )
        declared_id = value.get("campaign_id")
        if declared_id is not None and declared_id != campaign_directory.name:
            issues.append(
                LoadIssue(
                    code="campaign_id_mismatch",
                    message=(
                        f"Manifest declares {declared_id!r}, but directory is "
                        f"{campaign_directory.name!r}."
                    ),
                    path="replay.json",
                )
            )
        return value, issues

    def _campaign_summary(
        self,
        campaign_id: str,
        manifest: JsonObject | None,
        issues: Sequence[LoadIssue],
    ) -> CampaignSummary:
        value = manifest or {}
        runs = _list(value.get("runs"))
        summary = _mapping(value.get("summary"))
        succeeded = _number(summary.get("succeeded"))
        failed = _number(summary.get("failed"))
        orchestration_failed = _number(
            summary.get("orchestration_failed")
        )
        return CampaignSummary(
            campaign_id=campaign_id,
            schema=(
                value.get("schema")
                if isinstance(value.get("schema"), str)
                else None
            ),
            status=str(value.get("status", "unreadable")),
            started_at=(
                value.get("started_at")
                if isinstance(value.get("started_at"), str)
                else None
            ),
            ended_at=(
                value.get("ended_at")
                if isinstance(value.get("ended_at"), str)
                else None
            ),
            duration_ms=_number(value.get("duration_ms")),
            run_count=len(runs),
            succeeded=int(succeeded or 0),
            failed=int(failed or 0),
            orchestration_failed=int(orchestration_failed or 0),
            load_status=_load_status(issues, has_data=manifest is not None),
            issues=tuple(issues),
        )

    def _find_run_record(
        self,
        manifest: Mapping[str, Any],
        run_id: str,
    ) -> JsonObject | None:
        for value in _list(manifest.get("runs")):
            if isinstance(value, Mapping) and value.get("run_id") == run_id:
                return dict(value)
        return None

    def _resolve_run_directory(
        self,
        campaign_directory: Path,
        run_record: Mapping[str, Any],
    ) -> tuple[Path | None, list[LoadIssue]]:
        run_id = run_record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None, [
                LoadIssue(
                    code="run_id_missing",
                    message="Run entry has no run_id.",
                    path="replay.json",
                )
            ]
        try:
            self._validate_identifier(run_id, "run")
        except ViewerDataError as error:
            return None, [
                LoadIssue(
                    code=error.code,
                    message=error.message,
                    path="replay.json",
                )
            ]
        relative = run_record.get("path", f"runs/{run_id}")
        if not isinstance(relative, str) or not relative:
            return None, [
                LoadIssue(
                    code="run_path_invalid",
                    message=f"Run '{run_id}' has an invalid path.",
                    path="replay.json",
                )
            ]
        try:
            directory = self._resolve_inside(
                campaign_directory,
                relative,
                campaign_directory,
            )
        except ViewerDataError as error:
            return None, [
                LoadIssue(
                    code=error.code,
                    message=error.message,
                    path="replay.json",
                )
            ]
        if not directory.is_dir():
            return None, [
                LoadIssue(
                    code="run_directory_missing",
                    message=f"Run directory does not exist: {relative}",
                    path=relative,
                )
            ]
        return directory, []

    def _load_run_bundle(
        self,
        campaign_directory: Path,
        run_record: JsonObject,
    ) -> _RunBundle:
        run_directory, directory_issues = self._resolve_run_directory(
            campaign_directory,
            run_record,
        )
        run_id = (
            run_record.get("run_id")
            if isinstance(run_record.get("run_id"), str)
            else "unknown"
        )
        records: list[JsonObject] = []
        issues = list(directory_issues)
        if run_directory is not None:
            trajectory_path = run_directory / "trajectory.jsonl"
            legacy_path = run_directory / "trace.jsonl"
            if not trajectory_path.is_file() and legacy_path.is_file():
                trajectory_path = legacy_path
            records, trajectory_issues = self._read_trajectory(
                trajectory_path,
                run_id,
                run_directory,
            )
            issues.extend(trajectory_issues)
        summary = self._summarize_run(
            run_record,
            records,
            issues,
        )
        return _RunBundle(
            record=run_record,
            directory=run_directory,
            records=tuple(records),
            issues=tuple(issues),
            summary=summary,
        )

    def _read_trajectory(
        self,
        path: Path,
        expected_run_id: str,
        run_directory: Path,
    ) -> tuple[list[JsonObject], list[LoadIssue]]:
        relative = str(path.relative_to(run_directory))
        if not path.is_file():
            return [], [
                LoadIssue(
                    code="trajectory_missing",
                    message="Run has no trajectory.jsonl.",
                    path=relative,
                )
            ]
        records: list[JsonObject] = []
        issues: list[LoadIssue] = []
        unsupported_schemas: set[str] = set()
        expected_seq = 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            return [], [
                LoadIssue(
                    code="trajectory_unreadable",
                    message=f"Unable to read trajectory: {error}",
                    path=relative,
                )
            ]
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                issues.append(
                    LoadIssue(
                        code="trajectory_json_invalid",
                        message=f"Invalid JSON: {error.msg}",
                        path=relative,
                        line=line_number,
                    )
                )
                continue
            if not isinstance(value, dict):
                issues.append(
                    LoadIssue(
                        code="trajectory_record_invalid",
                        message="Trajectory line is not a JSON object.",
                        path=relative,
                        line=line_number,
                    )
                )
                continue
            value = _normalize_record_type(_redact_hidden_reasoning(value))
            schema = value.get("schema")
            if schema not in {TRAJECTORY_SCHEMA, LEGACY_ACTION_SCHEMA}:
                schema_label = repr(schema)
                if schema_label not in unsupported_schemas:
                    unsupported_schemas.add(schema_label)
                    issues.append(
                        LoadIssue(
                            code="unsupported_trajectory_schema",
                            message=(
                                f"Expected '{TRAJECTORY_SCHEMA}', found "
                                f"{schema_label}."
                            ),
                            path=relative,
                            line=line_number,
                        )
                    )
            if value.get("run_id") != expected_run_id:
                issues.append(
                    LoadIssue(
                        code="trajectory_run_id_mismatch",
                        message=(
                            f"Expected run_id {expected_run_id!r}, found "
                            f"{value.get('run_id')!r}."
                        ),
                        path=relative,
                        line=line_number,
                    )
                )
            sequence = value.get("seq")
            if sequence != expected_seq:
                issues.append(
                    LoadIssue(
                        code="sequence_gap",
                        message=(
                            f"Expected seq {expected_seq}, found "
                            f"{sequence!r}."
                        ),
                        path=relative,
                        line=line_number,
                    )
                )
            if isinstance(sequence, int) and sequence > 0:
                expected_seq = sequence + 1
            else:
                expected_seq += 1
            records.append(value)
        if not records:
            issues.append(
                LoadIssue(
                    code="trajectory_empty",
                    message="Trajectory contains no readable records.",
                    path=relative,
                )
            )
        record_types = {
            record.get("type")
            for record in records
            if isinstance(record.get("type"), str)
        }
        if records and "trajectory_sealed" not in record_types:
            issues.append(
                LoadIssue(
                    code="trajectory_not_sealed",
                    message="Trajectory has no trajectory_sealed record.",
                    path=relative,
                )
            )
        return records, issues

    def _summarize_run(
        self,
        run_record: Mapping[str, Any],
        records: Sequence[JsonObject],
        issues: Sequence[LoadIssue],
    ) -> RunSummary:
        record_types = Counter(
            str(record.get("type"))
            for record in records
            if isinstance(record.get("type"), str)
        )
        messages = [
            record
            for record in records
            if record.get("type") == "message_action"
        ]
        assistant_messages = [
            record
            for record in messages
            if _mapping(
                _mapping(record.get("payload")).get("message")
            ).get("role")
            == "assistant"
        ]
        tools = [
            record
            for record in records
            if record.get("type") == "tool_action"
        ]
        tool_statuses = Counter(
            str(_mapping(record.get("payload")).get("status", "unknown"))
            for record in tools
        )
        tool_names: dict[str, Counter[str]] = {}
        for record in tools:
            payload = _mapping(record.get("payload"))
            name = str(payload.get("tool_name", "unknown"))
            status = str(payload.get("status", "unknown"))
            tool_names.setdefault(name, Counter())[status] += 1

        outcome = self._outcome(records)
        runtime = self._first_payload(records, "runtime_observed")
        trajectory_manifest = self._trajectory_manifest(records)
        pi_args = _list(
            _mapping(trajectory_manifest.get("runtime")).get("pi_args")
        )
        declared_artifact = _mapping(run_record.get("artifact"))
        outcome_artifact = _mapping(outcome.get("artifact"))
        artifact = dict(declared_artifact or outcome_artifact) or None
        session = _mapping(outcome.get("session"))
        if not session:
            session = {
                "path": run_record.get("session"),
                "status": run_record.get("session_status"),
            }
        duration = _number(run_record.get("duration_ms"))
        if duration is None:
            duration = _number(outcome.get("duration_ms"))
        model = runtime.get("model")
        model_value = dict(model) if isinstance(model, Mapping) else None
        skill_loaded = outcome.get("skill_loaded")
        if not isinstance(skill_loaded, bool):
            skill_payload = self._first_payload(records, "skill_resolved")
            loaded = skill_payload.get("loaded")
            skill_loaded = loaded if isinstance(loaded, bool) else None
        return RunSummary(
            run_id=str(run_record.get("run_id", "unknown")),
            index=(
                run_record.get("index")
                if isinstance(run_record.get("index"), int)
                else None
            ),
            status=str(
                run_record.get(
                    "status",
                    outcome.get("status", "unknown"),
                )
            ),
            started_at=self._string_value(
                run_record.get("started_at", outcome.get("started_at"))
            ),
            ended_at=self._string_value(
                run_record.get("ended_at", outcome.get("ended_at"))
            ),
            duration_ms=duration,
            record_count=len(records),
            turn_count=record_types["turn_start"],
            message_count=len(messages),
            assistant_message_count=len(assistant_messages),
            tool_count=len(tools),
            failed_tool_count=tool_statuses["failed"],
            tool_statuses=dict(sorted(tool_statuses.items())),
            tool_names={
                name: dict(sorted(statuses.items()))
                for name, statuses in sorted(tool_names.items())
            },
            usage=self._usage_summary(assistant_messages),
            artifact=artifact,
            session=dict(session) if session else None,
            session_status=self._string_value(
                session.get("status") if session else None
            ),
            model=model_value,
            thinking_level=self._string_value(
                runtime.get("thinking_level")
            ),
            tools=tuple(self._extract_tools(pi_args)),
            skill_loaded=skill_loaded,
            sequence_contiguous=not any(
                issue.code == "sequence_gap" for issue in issues
            ),
            sealed=record_types["trajectory_sealed"] > 0,
            load_status=_load_status(issues, has_data=bool(records)),
            issues=tuple(issues),
        )

    def _usage_summary(
        self,
        assistant_messages: Sequence[JsonObject],
    ) -> JsonObject:
        token_fields = {
            "input": "input",
            "output": "output",
            "cache_read": "cacheRead",
            "cache_write": "cacheWrite",
            "total_tokens": "totalTokens",
        }
        totals: dict[str, int | float] = {
            key: 0 for key in token_fields
        }
        cost_total = 0.0
        calls_with_usage = 0
        for record in assistant_messages:
            message = _mapping(
                _mapping(record.get("payload")).get("message")
            )
            usage = _mapping(message.get("usage"))
            if not usage:
                continue
            calls_with_usage += 1
            for output_key, source_key in token_fields.items():
                value = _number(usage.get(source_key))
                if value is not None:
                    totals[output_key] += value
            cost = _mapping(usage.get("cost"))
            value = _number(cost.get("total"))
            if value is not None:
                cost_total += float(value)
        return {
            "model_calls": len(assistant_messages),
            "calls_with_usage": calls_with_usage,
            **totals,
            "reported_cost_total": cost_total,
        }

    def _build_setup(
        self,
        campaign_directory: Path,
        manifest: Mapping[str, Any],
        bundles: Sequence[_RunBundle],
    ) -> SetupSnapshot:
        issues: list[LoadIssue] = []
        prompt = self._load_prompt_setup(
            campaign_directory,
            manifest,
            issues,
        )
        run_setups: list[JsonObject] = []
        skill_values: list[tuple[str, str | None, JsonObject]] = []
        input_values: list[tuple[str, str | None, JsonObject]] = []
        for bundle in bundles:
            setup = self._run_setup(bundle, issues)
            run_setups.append(setup)
            skill_values.append(
                (
                    bundle.summary.run_id,
                    setup.pop("_skill_content", None),
                    setup.pop("_skill_metadata", {}),
                )
            )
            input_values.append(
                (
                    bundle.summary.run_id,
                    setup.pop("_input_content", None),
                    setup.pop("_input_metadata", {}),
                )
            )

        skill = self._content_snapshot(skill_values)
        input_snapshot = self._content_snapshot(input_values)
        common, differences = self._common_setup(run_setups)
        rendered = prompt.get("rendered")
        for setup in run_setups:
            run_prompt = setup.pop("_run_prompt", None)
            setup["prompt_matches_rendered"] = (
                isinstance(rendered, str)
                and isinstance(run_prompt, str)
                and rendered == run_prompt
            )
        return SetupSnapshot(
            prompt=prompt,
            skill=skill,
            input=input_snapshot,
            common=common,
            differences=tuple(differences),
            runs=tuple(run_setups),
            issues=tuple(issues),
        )

    def _load_prompt_setup(
        self,
        campaign_directory: Path,
        manifest: Mapping[str, Any],
        issues: list[LoadIssue],
    ) -> JsonObject:
        prompt_metadata = _mapping(
            _mapping(manifest.get("task")).get("prompt")
        )
        output: JsonObject = {"metadata": dict(prompt_metadata)}
        references = {
            "template": prompt_metadata.get("template_snapshot"),
            "rendered": prompt_metadata.get("rendered_snapshot"),
        }
        for label, reference in references.items():
            output[label] = self._read_text_reference(
                campaign_directory,
                reference,
                label,
                issues,
            )
        approval_reference = prompt_metadata.get("approval_snapshot")
        output["approval"] = self._read_json_reference(
            campaign_directory,
            approval_reference,
            "approval",
            issues,
        )
        return output

    def _read_text_reference(
        self,
        campaign_directory: Path,
        reference: Any,
        label: str,
        issues: list[LoadIssue],
    ) -> str | None:
        if not isinstance(reference, str) or not reference:
            issues.append(
                LoadIssue(
                    code=f"{label}_reference_missing",
                    message=f"Campaign does not declare its {label} snapshot.",
                    path="replay.json",
                )
            )
            return None
        try:
            path = self._resolve_inside(
                campaign_directory,
                reference,
                campaign_directory,
            )
            return path.read_text(encoding="utf-8")
        except (ViewerDataError, OSError, UnicodeError) as error:
            message = (
                error.message
                if isinstance(error, ViewerDataError)
                else str(error)
            )
            issues.append(
                LoadIssue(
                    code=f"{label}_unreadable",
                    message=f"Unable to read {label}: {message}",
                    path=reference,
                )
            )
            return None

    def _read_json_reference(
        self,
        campaign_directory: Path,
        reference: Any,
        label: str,
        issues: list[LoadIssue],
    ) -> JsonObject | None:
        text = self._read_text_reference(
            campaign_directory,
            reference,
            label,
            issues,
        )
        if text is None:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            issues.append(
                LoadIssue(
                    code=f"{label}_json_invalid",
                    message=f"Invalid {label} JSON: {error.msg}",
                    path=str(reference),
                    line=error.lineno,
                )
            )
            return None
        if not isinstance(value, dict):
            issues.append(
                LoadIssue(
                    code=f"{label}_json_invalid",
                    message=f"The {label} snapshot must be a JSON object.",
                    path=str(reference),
                )
            )
            return None
        return value

    def _run_setup(
        self,
        bundle: _RunBundle,
        issues: list[LoadIssue],
    ) -> JsonObject:
        manifest = self._trajectory_manifest(bundle.records)
        runtime_event = self._first_payload(
            bundle.records,
            "runtime_observed",
        )
        skill_event = self._first_payload(
            bundle.records,
            "skill_resolved",
        )
        process_event = self._first_payload(
            bundle.records,
            "pi_process_starting",
        )
        outcome = self._outcome(bundle.records)
        runtime = _mapping(manifest.get("runtime"))
        task_case = _mapping(manifest.get("task_case"))
        skill_metadata = dict(_mapping(manifest.get("skill")))
        input_metadata = dict(_mapping(manifest.get("source")))
        skill_content: str | None = None
        input_content: str | None = None
        if bundle.directory is not None:
            skill_snapshot = skill_metadata.get(
                "snapshot_path",
                "artifacts/skill",
            )
            if isinstance(skill_snapshot, str):
                skill_content = self._read_run_text(
                    bundle.directory,
                    f"{skill_snapshot}/SKILL.md",
                    "skill",
                    issues,
                )
            input_reference = task_case.get("input", "artifacts/input.md")
            if isinstance(input_reference, str):
                input_content = self._read_run_text(
                    bundle.directory,
                    input_reference,
                    "input",
                    issues,
                )
        model = runtime_event.get("model")
        pi_args = _list(runtime.get("pi_args"))
        return {
            "run_id": bundle.summary.run_id,
            "model": dict(model) if isinstance(model, Mapping) else None,
            "thinking_level": runtime_event.get("thinking_level"),
            "tools": self._extract_tools(pi_args),
            "python": runtime.get("python"),
            "platform": runtime.get("platform"),
            "working_directory": runtime.get("working_directory"),
            "pi_args": pi_args,
            "pi_command": _list(process_event.get("command")),
            "session_id": runtime_event.get("session_id"),
            "session_name": runtime_event.get("session_name"),
            "skill_loaded": skill_event.get(
                "loaded",
                outcome.get("skill_loaded"),
            ),
            "session": dict(_mapping(outcome.get("session"))),
            "artifact": dict(_mapping(outcome.get("artifact"))),
            "_run_prompt": task_case.get("prompt"),
            "_skill_content": skill_content,
            "_skill_metadata": skill_metadata,
            "_input_content": input_content,
            "_input_metadata": input_metadata,
        }

    def _read_run_text(
        self,
        run_directory: Path,
        reference: str,
        label: str,
        issues: list[LoadIssue],
    ) -> str | None:
        try:
            path = self._resolve_inside(
                run_directory,
                reference,
                run_directory,
            )
            return path.read_text(encoding="utf-8")
        except (ViewerDataError, OSError, UnicodeError) as error:
            message = (
                error.message
                if isinstance(error, ViewerDataError)
                else str(error)
            )
            issues.append(
                LoadIssue(
                    code=f"run_{label}_unreadable",
                    message=f"Unable to read run {label}: {message}",
                    path=reference,
                )
            )
            return None

    def _content_snapshot(
        self,
        values: Sequence[tuple[str, str | None, JsonObject]],
    ) -> JsonObject:
        readable = [
            (run_id, content, metadata)
            for run_id, content, metadata in values
            if content is not None
        ]
        if not readable:
            return {
                "same_across_runs": None,
                "content": None,
                "metadata": None,
                "variants": [],
            }
        unique = {_canonical(content) for _, content, _ in readable}
        all_readable = len(readable) == len(values)
        same = len(unique) == 1 and all_readable
        if same:
            run_id, content, metadata = readable[0]
            return {
                "same_across_runs": True,
                "content": content,
                "metadata": metadata,
                "source_run_id": run_id,
                "variants": [],
            }
        return {
            "same_across_runs": False,
            "content": None,
            "metadata": None,
            "variants": [
                {
                    "run_id": run_id,
                    "content": content,
                    "metadata": metadata,
                }
                for run_id, content, metadata in values
            ],
        }

    def _common_setup(
        self,
        run_setups: Sequence[JsonObject],
    ) -> tuple[JsonObject, list[JsonObject]]:
        fields = (
            "model",
            "thinking_level",
            "tools",
            "python",
            "platform",
            "skill_loaded",
        )
        common: JsonObject = {}
        differences: list[JsonObject] = []
        if not run_setups:
            return common, differences
        for field in fields:
            values = [
                {
                    "run_id": setup.get("run_id"),
                    "value": setup.get(field),
                }
                for setup in run_setups
            ]
            unique = {_canonical(item["value"]) for item in values}
            if len(unique) == 1:
                common[field] = values[0]["value"]
            else:
                differences.append({"field": field, "values": values})
        return common, differences

    def _trajectory_manifest(
        self,
        records: Sequence[JsonObject],
    ) -> JsonObject:
        for record in records:
            if record.get("type") != "trajectory_started":
                continue
            manifest = _mapping(
                _mapping(record.get("payload")).get("manifest")
            )
            return dict(manifest)
        return {}

    def _outcome(self, records: Sequence[JsonObject]) -> JsonObject:
        for record in reversed(records):
            if record.get("type") != "trajectory_finished":
                continue
            outcome = _mapping(
                _mapping(record.get("payload")).get("outcome")
            )
            return dict(outcome)
        return {}

    def _first_payload(
        self,
        records: Sequence[JsonObject],
        record_type: str,
    ) -> JsonObject:
        for record in records:
            if record.get("type") == record_type:
                return dict(_mapping(record.get("payload")))
        return {}

    def _extract_tools(self, pi_args: Sequence[Any]) -> list[str]:
        for index, value in enumerate(pi_args):
            if value != "--tools" or index + 1 >= len(pi_args):
                continue
            tools = pi_args[index + 1]
            if not isinstance(tools, str):
                return []
            return [
                item.strip()
                for item in tools.split(",")
                if item.strip()
            ]
        return []

    def _build_relations(
        self,
        records: Sequence[JsonObject],
    ) -> JsonObject:
        relations: dict[str, JsonObject] = {}
        for record in records:
            if record.get("type") != "message_action":
                continue
            sequence = record.get("seq")
            message = _mapping(
                _mapping(record.get("payload")).get("message")
            )
            if message.get("role") == "assistant":
                for block in _list(message.get("content")):
                    if not isinstance(block, Mapping):
                        continue
                    if block.get("type") != "toolCall":
                        continue
                    call_id = block.get("id")
                    if isinstance(call_id, str) and call_id:
                        relations.setdefault(call_id, {})[
                            "assistant_seq"
                        ] = sequence
            if message.get("role") == "toolResult":
                call_id = message.get("toolCallId")
                if isinstance(call_id, str) and call_id:
                    relations.setdefault(call_id, {})[
                        "tool_result_seq"
                    ] = sequence
        for record in records:
            if record.get("type") != "tool_action":
                continue
            payload = _mapping(record.get("payload"))
            call_id = payload.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                relations.setdefault(call_id, {})[
                    "tool_action_seq"
                ] = record.get("seq")
        return relations

    def _build_timeline(
        self,
        records: Sequence[JsonObject],
        relations: Mapping[str, Any],
    ) -> list[JsonObject]:
        groups: list[JsonObject] = []
        pending: list[JsonObject] = []
        current: JsonObject | None = None
        turn_number = 0
        has_seen_turn = False

        def flush_pending(label: str) -> None:
            nonlocal pending
            if not pending:
                return
            groups.append(
                {
                    "id": f"phase-{len(groups) + 1}",
                    "kind": "framework",
                    "label": label,
                    "actions": pending,
                }
            )
            pending = []

        for record in records:
            action = self._normalize_action(record, relations)
            record_type = record.get("type")
            if record_type == "turn_start":
                flush_pending(
                    "运行准备" if not has_seen_turn else "Turn 间隔"
                )
                has_seen_turn = True
                turn_number += 1
                current = {
                    "id": f"turn-{turn_number}",
                    "kind": "turn",
                    "label": f"Turn {turn_number}",
                    "actions": [action],
                }
                continue
            if current is not None:
                current["actions"].append(action)
                if record_type == "turn_end":
                    groups.append(current)
                    current = None
                continue
            pending.append(action)
        if current is not None:
            groups.append(current)
        flush_pending("运行收尾" if has_seen_turn else "运行记录")
        return groups

    def _normalize_action(
        self,
        record: Mapping[str, Any],
        relations: Mapping[str, Any],
    ) -> JsonObject:
        payload = dict(_mapping(record.get("payload")))
        record_type = str(record.get("type", "unknown"))
        action: JsonObject = {
            "seq": record.get("seq"),
            "type": record_type,
            "source": record.get("source"),
            "observed_at": record.get("observed_at"),
            "elapsed_ms": record.get("elapsed_ms"),
            "status": payload.get("status"),
            "known_type": record_type in KNOWN_RECORD_TYPES,
            "technical": record_type in TECHNICAL_RECORD_TYPES,
            "payload": payload,
        }
        if record_type == "message_action":
            message = _mapping(payload.get("message"))
            action["role"] = message.get("role")
            action["message"] = dict(message)
            action["technical"] = message.get("role") == "toolResult"
        if record_type == "tool_action":
            call_id = payload.get("tool_call_id")
            action["tool"] = payload
            if isinstance(call_id, str):
                action["related"] = dict(_mapping(relations.get(call_id)))
        return action

    def _string_value(self, value: Any) -> str | None:
        return value if isinstance(value, str) else None
