"""Evidence references, path confinement, and frozen analysis bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from skill_evolution.storage import (
    JsonObject,
    atomic_write_json,
    load_json_object,
    utc_now,
)


EVIDENCE_REF_SCHEMA = "evidence.ref.v1"
EVIDENCE_BUNDLE_SCHEMA = "evidence.bundle.v1"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
)
_HIDDEN_CONTENT_TYPES = {"analysis", "reasoning", "thinking"}
_SENSITIVE_EXACT_KEYS = {
    "access_token",
    "bearer_token",
    "refresh_token",
    "token",
}


class EvidenceError(ValueError):
    """Raised when evidence is missing, malformed, or escapes its root."""


def _trajectory_file(run_directory: Path) -> Path | None:
    for name in ("trajectory.jsonl", "trace.jsonl"):
        candidate = run_directory / name
        if candidate.is_file():
            return candidate
    return None


def resolve_inside(
    root: str | os.PathLike[str],
    relative_path: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a relative path while rejecting traversal and symlink escape."""

    if not relative_path:
        raise EvidenceError("Evidence path must not be empty")
    candidate_path = Path(relative_path)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise EvidenceError(f"Evidence path escapes its root: {relative_path}")
    root_path = Path(root).resolve()
    candidate = (root_path / candidate_path).resolve(strict=False)
    try:
        candidate.relative_to(root_path)
    except ValueError as error:
        raise EvidenceError(
            f"Evidence path escapes its root: {relative_path}"
        ) from error
    if must_exist and not candidate.exists():
        raise EvidenceError(f"Evidence path does not exist: {relative_path}")
    return candidate


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise EvidenceError("JSON pointer must be empty or start with '/'")
    current = document
    for encoded_part in pointer[1:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as error:
                raise EvidenceError(
                    f"JSON pointer list index does not exist: {pointer}"
                ) from error
        elif isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise EvidenceError(f"JSON pointer does not exist: {pointer}")
    return current


@dataclass(frozen=True)
class EvidenceRef:
    """A stable reference to trajectory, report, or artifact evidence."""

    campaign_id: str | None = None
    run_id: str | None = None
    seq: int | None = None
    report_path: str | None = None
    json_pointer: str | None = None
    artifact_path: str | None = None
    line: int | None = None
    selector: str | None = None

    def to_dict(self) -> JsonObject:
        """Serialize the reference using the public schema."""

        value: JsonObject = {"schema": EVIDENCE_REF_SCHEMA}
        for name in (
            "campaign_id",
            "run_id",
            "seq",
            "report_path",
            "json_pointer",
            "artifact_path",
            "line",
            "selector",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = item
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceRef:
        """Parse and validate the shape of a serialized reference."""

        if value.get("schema") != EVIDENCE_REF_SCHEMA:
            raise EvidenceError("Unsupported evidence reference schema")
        seq = value.get("seq")
        line = value.get("line")
        if seq is not None and (not isinstance(seq, int) or seq <= 0):
            raise EvidenceError("Evidence seq must be a positive integer")
        if line is not None and (not isinstance(line, int) or line <= 0):
            raise EvidenceError("Artifact line must be a positive integer")
        text_fields: dict[str, str | None] = {}
        for field in (
            "campaign_id",
            "run_id",
            "report_path",
            "json_pointer",
            "artifact_path",
            "selector",
        ):
            item = value.get(field)
            if item is not None and not isinstance(item, str):
                raise EvidenceError(f"{field} must be a string")
            text_fields[field] = item
        reference = cls(seq=seq, line=line, **text_fields)
        if not any(
            (
                reference.seq is not None,
                reference.report_path,
                reference.artifact_path,
            )
        ):
            raise EvidenceError("Evidence reference has no locator")
        if reference.seq is not None and not reference.run_id:
            raise EvidenceError("Trajectory seq requires run_id")
        if reference.json_pointer is not None and not reference.report_path:
            raise EvidenceError("JSON pointer requires report_path")
        if (
            reference.line is not None or reference.selector is not None
        ) and not reference.artifact_path:
            raise EvidenceError("Artifact location requires artifact_path")
        return reference

    def validate(self, bundle_root: str | os.PathLike[str]) -> None:
        """Verify that every referenced location exists in a frozen bundle."""

        root = Path(bundle_root).resolve()
        if self.seq is not None:
            trajectory_path = self._find_trajectory(root)
            found = False
            with trajectory_path.open("r", encoding="utf-8") as stream:
                for raw_line in stream:
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(record, Mapping)
                        and record.get("seq") == self.seq
                    ):
                        found = True
                        break
            if not found:
                raise EvidenceError(
                    f"Trajectory seq {self.seq} does not exist for "
                    f"run {self.run_id}"
                )
        if self.report_path:
            report = load_json_object(
                resolve_inside(root, self.report_path)
            )
            if self.json_pointer is not None:
                _json_pointer(report, self.json_pointer)
        if self.artifact_path:
            artifact = resolve_inside(root, self.artifact_path)
            if not artifact.is_file():
                raise EvidenceError("Artifact reference is not a file")
            if self.line is not None:
                with artifact.open(
                    "r", encoding="utf-8", errors="replace"
                ) as stream:
                    if sum(1 for _ in stream) < self.line:
                        raise EvidenceError(
                            f"Artifact line does not exist: {self.line}"
                        )

    def _find_trajectory(self, root: Path) -> Path:
        run_id = self.run_id
        assert run_id is not None
        candidates = [
            root / "runs" / run_id / "trajectory.jsonl",
            root / "trajectories" / run_id / "trajectory.jsonl",
            root / "runs" / run_id / "trace.jsonl",
            root / "traces" / run_id / "trace.jsonl",
        ]
        for candidate in candidates:
            try:
                candidate = resolve_inside(
                    root,
                    str(candidate.relative_to(root)),
                )
            except EvidenceError:
                continue
            if candidate.is_file():
                return candidate
        raise EvidenceError(f"Trajectory does not exist for run {run_id}")


def sanitize_for_evidence(value: Any) -> Any:
    """Remove credentials and hidden reasoning while retaining observable facts."""

    if isinstance(value, list):
        return [sanitize_for_evidence(item) for item in value]
    if not isinstance(value, Mapping):
        return value

    content_type = str(value.get("type", "")).lower()
    if content_type in _HIDDEN_CONTENT_TYPES:
        return {
            "type": value.get("type"),
            "redacted": "[HIDDEN_MODEL_REASONING]",
        }

    sanitized: JsonObject = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        lowered = key.lower().replace("-", "_")
        if (
            lowered in _SENSITIVE_EXACT_KEYS
            or any(part in lowered for part in _SENSITIVE_KEY_PARTS)
        ):
            sanitized[key] = "[REDACTED]"
        elif lowered in {"env", "environment"} and isinstance(item, Mapping):
            sanitized[key] = "[REDACTED_ENVIRONMENT]"
        else:
            sanitized[key] = sanitize_for_evidence(item)
    return sanitized


def _copy_sanitized_jsonl(source: Path, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    record_count = 0
    with source.open("r", encoding="utf-8") as input_stream:
        with destination.open("w", encoding="utf-8") as output_stream:
            for raw_line in input_stream:
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                output_stream.write(
                    json.dumps(
                        sanitize_for_evidence(record),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                record_count += 1
    return record_count


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise EvidenceError(f"Evidence source may not be a symlink: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_screenshot_segment(value: object) -> str:
    """Return a filename-safe identifier for a screenshot viewport."""

    normalized = "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else "-"
        for character in str(value)
    ).strip("-")
    return normalized or "viewport"


def _archive_comparison_screenshots(
    *,
    comparison_path: Path,
    destination_root: Path,
) -> tuple[JsonObject, list[str]]:
    """Copy captured screenshots and rewrite their report paths."""

    if comparison_path.is_symlink():
        raise EvidenceError(
            f"Evidence source may not be a symlink: {comparison_path}"
        )
    comparison = load_json_object(comparison_path)
    artifacts = comparison.get("artifacts")
    if not isinstance(artifacts, list):
        return comparison, []

    copied_paths: list[str] = []
    source_root = comparison_path.parent
    for artifact_index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        screenshots = artifact.get("screenshots")
        if not isinstance(screenshots, Mapping):
            continue
        rewritten: JsonObject = {}
        for raw_viewport, raw_record in screenshots.items():
            viewport = str(raw_viewport)
            if not isinstance(raw_record, Mapping):
                rewritten[viewport] = raw_record
                continue
            record: JsonObject = dict(raw_record)
            raw_path = record.get("path")
            if raw_path is None:
                rewritten[viewport] = record
                continue
            if not isinstance(raw_path, str):
                raise EvidenceError("Screenshot path must be a string")

            status = record.get("status")
            source = resolve_inside(
                source_root,
                raw_path,
                must_exist=status == "captured",
            )
            if status != "captured":
                record.pop("path", None)
                record["attempted_path"] = raw_path
                rewritten[viewport] = record
                continue
            if not source.is_file():
                raise EvidenceError(
                    f"Captured screenshot is not a file: {raw_path}"
                )

            destination_relative = (
                Path("screenshots")
                / (
                    f"{artifact_index:03d}-"
                    f"{_safe_screenshot_segment(viewport)}.png"
                )
            )
            _copy_regular_file(
                source,
                destination_root / destination_relative,
            )
            record["path"] = destination_relative.as_posix()
            record["bytes"] = source.stat().st_size
            rewritten[viewport] = record
            copied_paths.append(destination_relative.as_posix())
        artifact["screenshots"] = rewritten
    return comparison, copied_paths


class EvidenceBundleBuilder:
    """Freeze sanitized trajectories, reports, and artifacts for one analysis."""

    def build(
        self,
        *,
        campaign_directory: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        profile_path: str | os.PathLike[str],
        comparison_path: str | os.PathLike[str],
        skill_contract_path: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Create a self-contained read-only analysis evidence directory."""

        campaign = Path(campaign_directory).resolve()
        destination_path = Path(destination).resolve()
        if destination_path.exists() and any(destination_path.iterdir()):
            raise EvidenceError(
                f"Evidence destination is not empty: {destination_path}"
            )
        destination_path.mkdir(parents=True, exist_ok=True)

        campaign_manifest = self._find_campaign_manifest(campaign)
        _copy_regular_file(
            campaign_manifest,
            destination_path / "campaign.json",
        )
        profile = Path(profile_path).resolve()
        comparison = Path(comparison_path).resolve()
        _copy_regular_file(profile, destination_path / "reports/profile.json")
        archived_comparison, screenshots = _archive_comparison_screenshots(
            comparison_path=comparison,
            destination_root=destination_path,
        )
        atomic_write_json(
            destination_path / "reports/artifact-comparison.json",
            archived_comparison,
        )
        if skill_contract_path is not None:
            _copy_regular_file(
                Path(skill_contract_path).resolve(),
                destination_path / "skill_contract.json",
            )

        run_records: list[JsonObject] = []
        for run_directory in self._run_directories(campaign):
            trajectory = _trajectory_file(run_directory)
            if trajectory is None:
                continue
            run_id = run_directory.name
            output_run = destination_path / "runs" / run_id
            records = _copy_sanitized_jsonl(
                trajectory,
                output_run / "trajectory.jsonl",
            )
            artifacts: list[str] = []
            source_artifacts = run_directory / "artifacts"
            if source_artifacts.is_dir():
                for source in sorted(source_artifacts.rglob("*")):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(source_artifacts)
                    _copy_regular_file(
                        source,
                        output_run / "artifacts" / relative,
                    )
                    artifacts.append(
                        str(
                            (
                                Path("runs")
                                / run_id
                                / "artifacts"
                                / relative
                            )
                        )
                    )
            run_records.append(
                {
                    "run_id": run_id,
                    "trajectory": f"runs/{run_id}/trajectory.jsonl",
                    "trajectory_records": records,
                    "artifacts": artifacts,
                }
            )

        manifest: JsonObject = {
            "schema": EVIDENCE_BUNDLE_SCHEMA,
            "created_at": utc_now(),
            "source_campaign": str(campaign),
            "campaign_manifest": "campaign.json",
            "profile": "reports/profile.json",
            "artifact_comparison": "reports/artifact-comparison.json",
            "screenshots": screenshots,
            "skill_contract": (
                "skill_contract.json"
                if skill_contract_path is not None
                else None
            ),
            "runs": run_records,
            "redaction": {
                "hidden_reasoning": True,
                "credentials": True,
                "pi_session_included": False,
            },
        }
        atomic_write_json(destination_path / "bundle.json", manifest)
        return destination_path

    @staticmethod
    def _find_campaign_manifest(campaign: Path) -> Path:
        for name in ("replay.json", "manifest.json"):
            candidate = campaign / name
            if candidate.is_file():
                return candidate
        raise EvidenceError(f"Campaign manifest not found: {campaign}")

    @staticmethod
    def _run_directories(campaign: Path) -> list[Path]:
        runs = campaign / "runs"
        root = runs if runs.is_dir() else campaign
        return [
            path
            for path in sorted(root.iterdir())
            if path.is_dir() and _trajectory_file(path) is not None
        ]


class SingleTrajectoryEvidenceBundleBuilder:
    """Freeze exactly one trajectory and its deterministic precheck for analysis."""

    def build(
        self,
        *,
        trajectory_path: str | os.PathLike[str],
        precheck_path: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        analyzer_contract_path: str | os.PathLike[str],
        subject_contract_path: str | os.PathLike[str] | None = None,
        task_context_path: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Create a single-run evidence bundle without Pi session data."""

        trajectory = Path(trajectory_path).resolve()
        precheck = Path(precheck_path).resolve()
        analyzer_contract = Path(analyzer_contract_path).resolve()
        destination_path = Path(destination).resolve()
        sources = (trajectory, precheck, analyzer_contract)
        for source in sources:
            if not source.is_file() or source.is_symlink():
                raise EvidenceError(
                    f"Single-trajectory evidence source is invalid: {source}"
                )
        if destination_path.exists() and any(destination_path.iterdir()):
            raise EvidenceError(
                f"Evidence destination is not empty: {destination_path}"
            )
        destination_path.mkdir(parents=True, exist_ok=True)

        precheck_value = load_json_object(precheck)
        if precheck_value.get("schema") != "trajectory.precheck.v1":
            raise EvidenceError("Unsupported single-trajectory precheck schema")
        run_id = precheck_value.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise EvidenceError("Single-trajectory precheck requires run_id")
        signal_values = precheck_value.get("signals")
        if not isinstance(signal_values, list):
            raise EvidenceError("Single-trajectory precheck signals must be a list")
        signal_ids = [
            item.get("id")
            for item in signal_values
            if isinstance(item, Mapping)
        ]
        if not all(isinstance(item, str) and item for item in signal_ids):
            raise EvidenceError("Single-trajectory precheck has invalid signal ids")
        if len(signal_ids) != len(set(signal_ids)):
            raise EvidenceError("Single-trajectory precheck signal ids repeat")

        trajectory_run_ids: set[str] = set()
        source_schemas: set[str] = set()
        with trajectory.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise EvidenceError(
                        "Frozen trajectory contains invalid JSON"
                    ) from error
                if not isinstance(record, Mapping):
                    raise EvidenceError(
                        "Frozen trajectory records must be objects"
                    )
                raw_run_id = record.get("run_id")
                if isinstance(raw_run_id, str):
                    trajectory_run_ids.add(raw_run_id)
                raw_schema = record.get("schema")
                if isinstance(raw_schema, str):
                    source_schemas.add(raw_schema)
        if trajectory_run_ids != {run_id}:
            raise EvidenceError(
                "Trajectory run identity does not match the frozen precheck"
            )

        output_run = destination_path / "runs" / run_id
        trajectory_records = _copy_sanitized_jsonl(
            trajectory,
            output_run / "trajectory.jsonl",
        )
        _copy_regular_file(
            precheck,
            destination_path / "reports/trajectory-precheck.json",
        )
        _copy_regular_file(
            analyzer_contract,
            destination_path / "analyzer/skill_contract.json",
        )

        artifacts: list[str] = []
        artifact_root = trajectory.parent / "artifacts"
        if artifact_root.is_dir():
            for source in sorted(artifact_root.rglob("*")):
                if source.is_symlink():
                    raise EvidenceError(
                        f"Artifact evidence may not be a symlink: {source}"
                    )
                if not source.is_file():
                    continue
                relative = source.relative_to(artifact_root)
                destination_file = output_run / "artifacts" / relative
                _copy_regular_file(source, destination_file)
                artifacts.append(
                    (
                        Path("runs")
                        / run_id
                        / "artifacts"
                        / relative
                    ).as_posix()
                )

        subject_contract_relative: str | None = None
        if subject_contract_path is not None:
            subject_contract = Path(subject_contract_path).resolve()
            if not subject_contract.is_file():
                raise EvidenceError(
                    f"Subject Skill Contract is missing: {subject_contract}"
                )
            subject_contract_relative = "subject/skill_contract.json"
            _copy_regular_file(
                subject_contract,
                destination_path / subject_contract_relative,
            )

        task_context_relative: str | None = None
        if task_context_path is not None:
            task_context = Path(task_context_path).resolve()
            if not task_context.is_file():
                raise EvidenceError(
                    f"Task context is missing: {task_context}"
                )
            task_context_relative = "task/rendered-prompt.md"
            _copy_regular_file(
                task_context,
                destination_path / task_context_relative,
            )

        integrity = precheck_value.get("integrity")
        source_format = (
            integrity.get("source_format")
            if isinstance(integrity, Mapping)
            else None
        )
        manifest: JsonObject = {
            "schema": EVIDENCE_BUNDLE_SCHEMA,
            "purpose": "single_trajectory_error_analysis",
            "created_at": utc_now(),
            "precheck": "reports/trajectory-precheck.json",
            "analyzer_contract": "analyzer/skill_contract.json",
            "subject_contract": subject_contract_relative,
            "task_context": task_context_relative,
            "runs": [
                {
                    "run_id": run_id,
                    "trajectory": f"runs/{run_id}/trajectory.jsonl",
                    "trajectory_records": trajectory_records,
                    "source_schemas": sorted(source_schemas),
                    "source_format": source_format,
                    "artifacts": artifacts,
                }
            ],
            "source_hashes": {
                "trajectory_sha256": _sha256(trajectory),
                "precheck_sha256": _sha256(precheck),
                "analyzer_contract_sha256": _sha256(analyzer_contract),
            },
            "precheck_signal_ids": signal_ids,
            "redaction": {
                "hidden_reasoning": True,
                "credentials": True,
                "pi_session_included": False,
            },
        }
        atomic_write_json(destination_path / "bundle.json", manifest)
        return destination_path


class ComparisonEvidenceBundleBuilder:
    """Freeze full comparison facts for an independent ReplayJudge."""

    def build(
        self,
        *,
        comparison_directory: str | os.PathLike[str],
        batch_campaign_directory: str | os.PathLike[str],
        harness_directory: str | os.PathLike[str],
        candidate_directory: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        skill_contract_path: str | os.PathLike[str],
    ) -> Path:
        """Create a judge bundle from a frozen full comparison Harness."""

        comparison = Path(comparison_directory).resolve()
        candidate = Path(candidate_directory).resolve()
        harness = Path(harness_directory).resolve()
        sources = (
            (comparison / "manifest.json", "comparison.json"),
            (candidate / "manifest.json", "candidate/manifest.json"),
            (candidate / "diff.patch", "candidate/diff.patch"),
        )
        for source, _ in sources:
            if not source.is_file() or source.is_symlink():
                raise EvidenceError(
                    f"Comparison judge source does not exist: {source}"
                )
        destination_path = EvidenceBundleBuilder().build(
            campaign_directory=batch_campaign_directory,
            destination=destination,
            profile_path=harness / "trajectory-profile.json",
            comparison_path=harness / "artifact-comparison.json",
            skill_contract_path=skill_contract_path,
        )
        for source, relative in sources:
            _copy_regular_file(source, destination_path / relative)

        manifest_path = destination_path / "bundle.json"
        manifest = load_json_object(manifest_path)
        manifest.update(
            {
                "purpose": "replay_judge",
                "comparison": "comparison.json",
                "candidate_manifest": "candidate/manifest.json",
                "candidate_diff": "candidate/diff.patch",
            }
        )
        atomic_write_json(manifest_path, manifest)
        return destination_path
