"""Resolve the public trajectory profile consumed by the read-only viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from scripts.trajectory_profiler import PROFILE_SCHEMA, TrajectoryProfiler


JsonObject = dict[str, Any]
HARNESS_SCHEMA = "harness.run.v1"
LEGACY_PROFILE_SCHEMA = "trace.profile.v1"
_TERMINAL_HARNESS_STATUSES = {
    "completed",
    "completed_ok",
    "completed_partial",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class TrajectoryProfileViewRepository:
    """Load a persisted profile or derive the same public projection read-only.

    The replay repository remains the source for action-level inspection. All
    efficiency facts exposed to the viewer come from ``trajectory.profile.v1``
    so the UI and offline analysis cannot silently use different token or
    retry accounting.
    """

    def __init__(
        self,
        replays_root: str | Path,
        harness_root: str | Path | None = None,
    ) -> None:
        self.replays_root = Path(replays_root).resolve()
        self.harness_root = (
            Path(harness_root).resolve()
            if harness_root is not None
            else self.replays_root.parent / "harness-runs"
        )
        self.profiler = TrajectoryProfiler(self.replays_root)

    def get_campaign_profile(self, campaign_id: str) -> JsonObject:
        """Return the newest valid persisted profile, else profile in memory."""

        persisted = self._latest_persisted_profile(campaign_id)
        if persisted is not None:
            return persisted
        return self.profiler.profile_campaign(campaign_id)

    def _latest_persisted_profile(
        self,
        campaign_id: str,
    ) -> JsonObject | None:
        if not self.harness_root.is_dir():
            return None
        candidates: list[tuple[str, str, JsonObject]] = []
        for entry in self.harness_root.iterdir():
            directory = entry.resolve()
            if (
                entry.name.startswith(".")
                or not directory.is_dir()
                or not directory.is_relative_to(self.harness_root)
            ):
                continue
            manifest = self._read_json_inside(
                directory / "harness.json",
                directory,
            )
            if manifest is None:
                continue
            if manifest.get("schema") != HARNESS_SCHEMA:
                continue
            if manifest.get("status") not in _TERMINAL_HARNESS_STATUSES:
                continue
            source = _mapping(manifest.get("source"))
            if source.get("campaign_id") != campaign_id:
                continue
            outputs = _mapping(manifest.get("outputs"))
            relative = outputs.get("trajectory_profile")
            if relative is None:
                relative = outputs.get("trace_profile")
            profile_path = self._resolve_output(directory, relative)
            if profile_path is None:
                continue
            profile = self._read_json_inside(profile_path, directory)
            if not self._profile_matches(profile, campaign_id):
                continue
            if profile.get("schema") == LEGACY_PROFILE_SCHEMA:
                profile = {
                    **profile,
                    "schema": PROFILE_SCHEMA,
                    "source_schema": LEGACY_PROFILE_SCHEMA,
                }
            ended_at = manifest.get("ended_at")
            sort_time = ended_at if isinstance(ended_at, str) else ""
            candidates.append((sort_time, entry.name, profile))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _resolve_output(
        self,
        directory: Path,
        relative: Any,
    ) -> Path | None:
        if not isinstance(relative, str) or not relative:
            return None
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return None
        resolved = (directory / path).resolve()
        if not resolved.is_relative_to(directory):
            return None
        return resolved

    def _read_json_inside(
        self,
        path: Path,
        boundary: Path,
    ) -> JsonObject | None:
        try:
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(boundary)
                or not resolved.is_file()
            ):
                return None
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _profile_matches(
        self,
        profile: JsonObject | None,
        campaign_id: str,
    ) -> bool:
        if profile is None or profile.get("schema") not in {
            PROFILE_SCHEMA,
            LEGACY_PROFILE_SCHEMA,
        }:
            return False
        source = _mapping(profile.get("source"))
        return source.get("campaign_id") == campaign_id
