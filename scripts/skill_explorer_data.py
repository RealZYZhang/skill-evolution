"""Read Skill-first runtime objects into safe presentation view models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
from typing import Any

from skill_evolution.hierarchy import (
    HierarchyError,
    SkillHierarchyRepository,
    validate_multi_trajectory_errors_view,
    validate_multi_trajectory_view,
)
from skill_evolution.storage import StorageError
from skill_evolution.hierarchy_improvements import HierarchyImprovementService
from skill_evolution.trajectory_user_report import (
    TrajectoryUserReportError,
    validate_trajectory_user_report,
)
from scripts.trajectory_viewer_data import ViewerDataError


JsonObject = dict[str, Any]
API_SCHEMA = "skill.explorer.api.v1"
_LEGACY_RECORD_TYPES = {
    "trace_started": "trajectory_started",
    "trace_finished": "trajectory_finished",
    "trace_sealed": "trajectory_sealed",
}
_PREFERRED_REPORT_LOCALE = "zh-CN"


def _redact_hidden_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_hidden_reasoning(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    block_type = value.get("type")
    if isinstance(block_type, str) and block_type.lower() in {
        "thinking",
        "reasoning",
    }:
        return {"type": block_type, "redacted": True}
    protected = {
        "chain_of_thought",
        "reasoning_content",
        "reasoningcontent",
        "thinking_content",
        "thinkingcontent",
    }
    result: JsonObject = {}
    for key, item in value.items():
        normalized = str(key).replace("-", "_").lower()
        if normalized in protected or (
            normalized == "reasoning"
            and not isinstance(item, (bool, int, float, type(None)))
        ):
            result[str(key)] = "[REDACTED: hidden reasoning]"
        else:
            result[str(key)] = _redact_hidden_reasoning(item)
    return result


def _read_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ViewerDataError(
            "preserved_json_invalid",
            f"A preserved JSON result is unreadable: {path.name}",
            422,
        ) from error
    if not isinstance(value, dict):
        raise ViewerDataError(
            "preserved_json_invalid",
            f"A preserved JSON result is not an object: {path.name}",
            422,
        )
    return value


class SkillExplorerRepository:
    """Expose only validated objects and allow-listed files to the Viewer."""

    def __init__(self, runtime_root: str | Path) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.repository = SkillHierarchyRepository(self.runtime_root)

    def list_skills(self) -> JsonObject:
        """Return compact Skill cards from the rebuildable catalog."""

        try:
            catalog = self.repository.load_catalog()
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error) from error
        return {
            "schema": API_SCHEMA,
            "generated_at": catalog["generated_at"],
            "skills": catalog["skills"],
        }

    def has_hierarchy_data(self) -> bool:
        """Return whether the completed cutover makes hierarchy authoritative."""

        return self.repository.is_cutover_complete()

    def get_skill(self, skill_id: str) -> JsonObject:
        """Return the Skill home view with current package boundaries."""

        try:
            index = self.repository.load_skill_index(skill_id)
            revisions = self.repository.list_revisions(skill_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        selected = self._preferred_revision(revisions)
        package: JsonObject | None = None
        if selected is not None:
            revision_directory = self.repository.revision_directory(
                skill_id, str(selected["revision_id"])
            )
            skill_path = revision_directory / "package" / "SKILL.md"
            contract_path = revision_directory / "package" / "skill_contract.json"
            package = {
                "revision_id": selected["revision_id"],
                "entrypoint": (
                    skill_path.read_text(encoding="utf-8")
                    if skill_path.is_file()
                    else None
                ),
                "contract": (
                    _read_json(contract_path) if contract_path.is_file() else None
                ),
                "contract_status": selected["contract"]["status"],
            }
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "index": index,
            "package": package,
        }

    def list_revisions(self, skill_id: str) -> JsonObject:
        """Return all immutable revisions for one Skill."""

        try:
            revisions = self.repository.list_revisions(skill_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        if not revisions and not self.repository.skill_directory(skill_id).is_dir():
            raise ViewerDataError("skill_not_found", "Skill was not found.", 404)
        return {"schema": API_SCHEMA, "skill_id": skill_id, "revisions": revisions}

    def list_executions(self, skill_id: str) -> JsonObject:
        """Return direct Execution children with analysis counts."""

        try:
            executions = self.repository.list_executions(skill_id)
            execution_sets = self.repository.list_execution_sets(skill_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        set_labels = {
            item["set_id"]: {
                "purpose": item["purpose"],
                "status": item["status"],
                "legacy_campaign_id": item["provenance"].get(
                    "legacy_campaign_id"
                ),
            }
            for item in execution_sets
        }
        summaries = []
        for execution in sorted(
            executions,
            key=lambda item: str(item.get("started_at") or ""),
            reverse=True,
        ):
            analyses = self.repository.list_analyses(
                skill_id, execution_id=str(execution["execution_id"])
            )
            summaries.append(
                {
                    **execution,
                    "analysis_count": len(analyses),
                    "execution_set": set_labels.get(
                        execution.get("execution_set_id")
                    ),
                }
            )
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "executions": summaries,
        }

    def get_execution(self, skill_id: str, execution_id: str) -> JsonObject:
        """Return Input, Output, Trajectory, analysis, and Setup for one Execution."""

        try:
            manifest = self.repository.load_execution(skill_id, execution_id)
            records, issues = self._read_trajectory(skill_id, execution_id, manifest)
            analyses = self.get_execution_analyses(skill_id, execution_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        return {
            "schema": API_SCHEMA,
            "execution": manifest,
            "input": {
                "task": manifest["task"],
                "artifacts": manifest["inputs"],
            },
            "output": {
                "status": manifest["status"],
                "artifacts": manifest["outputs"],
                "supporting_artifacts": manifest["supporting_artifacts"],
            },
            "trajectory": {
                "metadata": manifest["trajectory"],
                "records": records,
                "timeline": self._timeline(records),
                "issues": issues,
            },
            "analyses": analyses,
            "setup": {
                **manifest["setup"],
                "session": manifest["session"],
                "origin": manifest["origin"],
                "execution_set_id": manifest["execution_set_id"],
                "comparison_id": manifest["comparison_id"],
                "legacy": manifest["legacy"],
            },
        }

    def get_execution_analyses(
        self,
        skill_id: str,
        execution_id: str,
    ) -> JsonObject:
        """Return all envelopes and the newest schema-valid user report."""

        try:
            self.repository.load_execution(skill_id, execution_id)
            records = self.repository.list_analyses(
                skill_id, execution_id=execution_id
            )
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        enriched: list[JsonObject] = []
        valid_reports: list[tuple[str, int, JsonObject]] = []
        for record in records:
            result: JsonObject = {"record": record, "report": None}
            directory = self.repository.analysis_directory(record)
            record_reports: list[tuple[int, JsonObject]] = []
            for reference in record["result_refs"]:
                if not isinstance(reference, Mapping):
                    continue
                if reference.get("schema") != "analysis.single_trajectory_view.v1":
                    continue
                path = reference.get("path")
                if not isinstance(path, str):
                    continue
                try:
                    candidate_path = self.repository.resolve_object_file(
                        directory,
                        path,
                    )
                    if not self._localized_report_source_matches(
                        directory,
                        reference,
                    ):
                        continue
                    candidate = _read_json(candidate_path)
                    report = validate_trajectory_user_report(candidate)
                except (
                    HierarchyError,
                    OSError,
                    TrajectoryUserReportError,
                    ViewerDataError,
                ):
                    continue
                locale_priority = (
                    1
                    if reference.get("locale") == _PREFERRED_REPORT_LOCALE
                    else 0
                )
                record_reports.append((locale_priority, report))
            if record_reports:
                priority, selected_report = max(
                    record_reports,
                    key=lambda item: item[0],
                )
                result["report"] = selected_report
                valid_reports.append(
                    (
                        str(record.get("ended_at") or record["created_at"]),
                        priority,
                        selected_report,
                    )
                )
            enriched.append(result)
        latest = (
            max(valid_reports, key=lambda item: (item[0], item[1]))[2]
            if valid_reports
            else None
        )
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "execution_id": execution_id,
            "analyses": enriched,
            "latest_valid_report": latest,
        }

    def _localized_report_source_matches(
        self,
        directory: Path,
        reference: Mapping[str, Any],
    ) -> bool:
        """Accept locale projections only while their source report is unchanged."""

        locale = reference.get("locale")
        if locale is None:
            return True
        if locale != _PREFERRED_REPORT_LOCALE:
            return False
        source_path = reference.get("localized_from")
        source_sha256 = reference.get("localized_from_sha256")
        if not isinstance(source_path, str) or not isinstance(
            source_sha256,
            str,
        ):
            return False
        try:
            source = self.repository.resolve_object_file(
                directory,
                source_path,
            )
        except HierarchyError:
            return False
        return hashlib.sha256(source.read_bytes()).hexdigest() == source_sha256

    def list_multi_analyses(self, skill_id: str) -> JsonObject:
        """Return Skill-owned multi-Trajectory analysis envelopes."""

        try:
            records = self.repository.list_multi_trajectory_analyses(skill_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "analyses": records,
        }

    def list_improvements(self, skill_id: str) -> JsonObject:
        """Return Candidate summaries owned by one Skill."""

        try:
            candidates = HierarchyImprovementService(
                self.runtime_root
            ).list_improvements(skill_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "candidates": candidates,
        }

    def get_multi_analysis(self, skill_id: str, analysis_id: str) -> JsonObject:
        """Return one multi analysis and its validated user projection if present."""

        try:
            record = self.repository.load_analysis(skill_id, analysis_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        if record["kind"] != "multi_trajectory":
            raise ViewerDataError(
                "multi_trajectory_analysis_not_found",
                "The requested record is not a multi-trajectory analysis.",
                404,
            )
        report = None
        directory = self.repository.analysis_directory(record)
        for reference in record["result_refs"]:
            if not isinstance(reference, Mapping):
                continue
            path = reference.get("path")
            if not isinstance(path, str):
                continue
            schema = reference.get("schema")
            try:
                raw = _read_json(
                    self.repository.resolve_object_file(directory, path)
                )
            except (HierarchyError, ViewerDataError):
                continue
            if schema == "analysis.multi_trajectory_errors.v1":
                try:
                    report = validate_multi_trajectory_errors_view(raw)
                    break
                except HierarchyError:
                    continue
            elif schema == "analysis.multi_trajectory_view.v1":
                try:
                    report = validate_multi_trajectory_view(raw)
                except HierarchyError:
                    continue
        return {
            "schema": API_SCHEMA,
            "skill_id": skill_id,
            "record": record,
            "report": report,
        }

    def get_execution_file(
        self,
        skill_id: str,
        execution_id: str,
        file_id: str,
    ) -> tuple[Path, str | None]:
        """Resolve a declared artifact, trajectory, or session by stable role ID."""

        try:
            manifest = self.repository.load_execution(skill_id, execution_id)
        except (HierarchyError, StorageError, OSError) as error:
            raise self._safe_error(error, not_found=True) from error
        reference: Mapping[str, Any] | None = None
        if file_id == "trajectory" and manifest["trajectory"]["path"]:
            reference = manifest["trajectory"]
        elif file_id == "session" and manifest["session"]["path"]:
            reference = manifest["session"]
        else:
            artifacts: Sequence[Mapping[str, Any]] = [
                *manifest["inputs"],
                *manifest["outputs"],
                *manifest["supporting_artifacts"],
            ]
            reference = next(
                (
                    item
                    for item in artifacts
                    if item.get("artifact_id") == file_id
                ),
                None,
            )
        if reference is None or not isinstance(reference.get("path"), str):
            raise ViewerDataError(
                "execution_file_not_found",
                "The requested Execution file is not declared.",
                404,
            )
        directory = self.repository.execution_directory(skill_id, execution_id)
        try:
            path = self.repository.resolve_object_file(
                directory, str(reference["path"])
            )
        except HierarchyError as error:
            raise self._safe_error(error, not_found=True) from error
        media_type = reference.get("media_type")
        return path, media_type if isinstance(media_type, str) else None

    def list_campaign_projections(self) -> JsonObject:
        """Project Execution Sets into the deprecated Campaign list shape."""

        campaigns: list[JsonObject] = []
        for skill in self.list_skills()["skills"]:
            skill_id = str(skill["skill_id"])
            for execution_set in self.repository.list_execution_sets(skill_id):
                legacy_id = execution_set["provenance"].get("legacy_campaign_id")
                campaign_id = legacy_id or execution_set["set_id"]
                executions = [
                    self.repository.load_execution(skill_id, execution_id)
                    for execution_id in execution_set["execution_ids"]
                ]
                campaigns.append(
                    {
                        "campaign_id": campaign_id,
                        "schema": "execution.set.v1",
                        "status": execution_set["status"],
                        "started_at": execution_set["created_at"],
                        "ended_at": execution_set["ended_at"],
                        "duration_ms": None,
                        "run_count": len(executions),
                        "succeeded": sum(
                            item["status"] == "succeeded" for item in executions
                        ),
                        "failed": sum(
                            item["status"] in {"failed", "interrupted"}
                            for item in executions
                        ),
                        "orchestration_failed": sum(
                            item["status"] == "orchestration_failed"
                            for item in executions
                        ),
                        "load_status": "ok",
                        "issues": [],
                        "skill_id": skill_id,
                        "execution_set_id": execution_set["set_id"],
                    }
                )
        return {"schema": "viewer.api.v1", "campaigns": campaigns}

    def get_campaign_projection(self, campaign_id: str) -> JsonObject:
        """Project one deprecated Campaign detail from an Execution Set."""

        skill_id, execution_set = self._find_campaign_set(campaign_id)
        runs = [
            self.repository.load_execution(skill_id, execution_id)
            for execution_id in execution_set["execution_ids"]
        ]
        summaries = [self._legacy_run_summary(item) for item in runs]
        return {
            "schema": "viewer.api.v1",
            "summary": next(
                item
                for item in self.list_campaign_projections()["campaigns"]
                if item["campaign_id"] == campaign_id
            ),
            "manifest": execution_set,
            "setup": {
                "prompt": {},
                "skill": {
                    "skill_id": skill_id,
                    "revision_id": execution_set["revision_id"],
                },
                "input": execution_set["task"],
                "common": execution_set["runtime"],
                "differences": [],
                "runs": [],
                "issues": [],
            },
            "runs": summaries,
            "issues": [],
        }

    def get_campaign_run_projection(
        self,
        campaign_id: str,
        execution_id: str,
    ) -> JsonObject:
        """Project one deprecated Campaign run from an Execution."""

        skill_id, execution_set = self._find_campaign_set(campaign_id)
        if execution_id not in execution_set["execution_ids"]:
            raise ViewerDataError("run_not_found", "Run was not found.", 404)
        detail = self.get_execution(skill_id, execution_id)
        execution = detail["execution"]
        return {
            "schema": "viewer.api.v1",
            "summary": self._legacy_run_summary(execution),
            "timeline": detail["trajectory"]["timeline"],
            "relations": {},
            "records": detail["trajectory"]["records"],
            "issues": detail["trajectory"]["issues"],
        }

    def get_campaign_analysis_projection(
        self,
        campaign_id: str,
        execution_id: str,
    ) -> JsonObject:
        """Project the latest valid single-Trajectory report for a legacy route."""

        skill_id, execution_set = self._find_campaign_set(campaign_id)
        if execution_id not in execution_set["execution_ids"]:
            raise ViewerDataError("run_not_found", "Run was not found.", 404)
        detail = self.get_execution_analyses(skill_id, execution_id)
        report = detail["latest_valid_report"]
        if report is None:
            raise ViewerDataError(
                "trajectory_analysis_not_found",
                "This run does not have a saved single-trajectory analysis.",
                404,
            )
        return {"schema": "viewer.api.v1", "report": report}

    def get_campaign_file_projection(
        self,
        campaign_id: str,
        execution_id: str,
        kind: str,
    ) -> tuple[Path, str | None]:
        """Resolve an artifact/session through the deprecated Campaign route."""

        skill_id, execution_set = self._find_campaign_set(campaign_id)
        if execution_id not in execution_set["execution_ids"]:
            raise ViewerDataError("run_not_found", "Run was not found.", 404)
        manifest = self.repository.load_execution(skill_id, execution_id)
        if kind == "session":
            return self.get_execution_file(skill_id, execution_id, "session")
        if kind == "artifact" and manifest["outputs"]:
            artifact_id = str(manifest["outputs"][0]["artifact_id"])
            return self.get_execution_file(skill_id, execution_id, artifact_id)
        raise ViewerDataError(
            "run_file_not_found", "The requested run file does not exist.", 404
        )

    def get_campaign_profile_projection(self, campaign_id: str) -> JsonObject:
        """Return the latest migrated deterministic profile for a batch."""

        skill_id, execution_set = self._find_campaign_set(campaign_id)
        candidates = [
            item
            for item in self.repository.list_execution_set_analyses(
                skill_id,
                set_id=str(execution_set["set_id"]),
            )
            if item["kind"] == "harness"
        ]
        for record in sorted(
            candidates,
            key=lambda item: str(item.get("ended_at") or item["created_at"]),
            reverse=True,
        ):
            directory = self.repository.analysis_directory(record)
            path = directory / "payload" / "trajectory-profile.json"
            if path.is_file() and path.resolve().is_relative_to(directory.resolve()):
                return _read_json(path)
        raise ViewerDataError(
            "campaign_profile_not_found",
            "This Execution Set does not have a migrated profile.",
            404,
        )

    def _find_campaign_set(self, campaign_id: str) -> tuple[str, JsonObject]:
        matches: list[tuple[str, JsonObject]] = []
        for skill in self.list_skills()["skills"]:
            skill_id = str(skill["skill_id"])
            for execution_set in self.repository.list_execution_sets(skill_id):
                legacy_id = execution_set["provenance"].get("legacy_campaign_id")
                if campaign_id in {legacy_id, execution_set["set_id"]}:
                    matches.append((skill_id, execution_set))
        if len(matches) != 1:
            raise ViewerDataError(
                "campaign_not_found", "Campaign projection was not found.", 404
            )
        return matches[0]

    def _legacy_run_summary(self, execution: Mapping[str, Any]) -> JsonObject:
        return {
            "run_id": execution["execution_id"],
            "index": None,
            "status": execution["status"],
            "started_at": execution["started_at"],
            "ended_at": execution["ended_at"],
            "duration_ms": execution["duration_ms"],
            "record_count": None,
            "turn_count": None,
            "message_count": None,
            "assistant_message_count": None,
            "tool_count": None,
            "failed_tool_count": None,
            "tool_statuses": {},
            "tool_names": {},
            "usage": {},
            "artifact": execution["outputs"][0] if execution["outputs"] else None,
            "session": execution["session"],
            "session_status": execution["session"]["status"],
            "model": execution["setup"].get("runtime", {}).get("model"),
            "thinking_level": execution["setup"].get("runtime", {}).get(
                "thinking_level"
            ),
            "tools": [],
            "skill_loaded": True,
            "sequence_contiguous": True,
            "sealed": execution["trajectory"]["sealed"],
            "load_status": "ok",
            "issues": [],
        }

    def _read_trajectory(
        self,
        skill_id: str,
        execution_id: str,
        manifest: Mapping[str, Any],
    ) -> tuple[list[JsonObject], list[JsonObject]]:
        relative = manifest["trajectory"].get("path")
        if not isinstance(relative, str):
            return [], [
                {"code": "trajectory_missing", "message": "Execution has no Trajectory."}
            ]
        directory = self.repository.execution_directory(skill_id, execution_id)
        path = self.repository.resolve_object_file(directory, relative)
        records: list[JsonObject] = []
        issues: list[JsonObject] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    issues.append(
                        {
                            "code": "trajectory_line_invalid",
                            "message": f"Trajectory line {line_number} is invalid.",
                            "line": line_number,
                        }
                    )
                    continue
                if not isinstance(value, Mapping):
                    continue
                record = _redact_hidden_reasoning(dict(value))
                record_type = record.get("type")
                if record_type in _LEGACY_RECORD_TYPES:
                    record["type"] = _LEGACY_RECORD_TYPES[str(record_type)]
                records.append(record)
        return records, issues

    def _timeline(self, records: Sequence[Mapping[str, Any]]) -> list[JsonObject]:
        timeline: list[JsonObject] = []
        for record in records:
            record_type = record.get("type")
            if record_type not in {
                "message_action",
                "tool_action",
                "action_interrupted",
                "observer_error",
                "trajectory_started",
                "trajectory_finished",
                "trajectory_sealed",
            }:
                continue
            payload = record.get("payload")
            timeline.append(
                {
                    "seq": record.get("seq"),
                    "type": record_type,
                    "observed_at": record.get("observed_at"),
                    "source": record.get("source"),
                    "status": (
                        payload.get("status")
                        if isinstance(payload, Mapping)
                        else None
                    ),
                    "payload": payload if isinstance(payload, Mapping) else {},
                }
            )
        return timeline

    def _preferred_revision(
        self, revisions: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any] | None:
        active = [
            item for item in revisions if item.get("lifecycle") == "active"
        ]
        candidates = active or list(revisions)
        return (
            max(
                candidates,
                key=lambda item: str(item.get("captured_at", "")),
            )
            if candidates
            else None
        )

    def _safe_error(
        self,
        error: Exception,
        *,
        not_found: bool = False,
    ) -> ViewerDataError:
        code = "hierarchy_object_not_found" if not_found else "hierarchy_invalid"
        status = 404 if not_found else 422
        return ViewerDataError(
            code,
            f"Skill hierarchy data is unavailable: {error}",
            status,
        )
