"""Publish a reviewed locale projection without changing its source report."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any

from skill_evolution.storage import JsonObject
from skill_evolution.trajectory_user_report import (
    TrajectoryUserReportError,
    validate_trajectory_user_report,
)


TRAJECTORY_USER_REPORT_LOCALIZATION_SCHEMA = (
    "analysis.single_trajectory_localization_input.v1"
)
SUPPORTED_LOCALE = "zh-CN"


class TrajectoryUserReportLocalizationError(ValueError):
    """Raised when a reviewed locale projection is incomplete or inconsistent."""


def _reviewed_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrajectoryUserReportLocalizationError(
            f"{label} must be a non-empty string"
        )
    text = value.strip()
    if not any("\u4e00" <= character <= "\u9fff" for character in text):
        raise TrajectoryUserReportLocalizationError(
            f"{label} must contain reviewed Simplified Chinese text"
        )
    return text


def localize_trajectory_user_report(
    source: Mapping[str, Any],
    localization: Mapping[str, Any],
) -> JsonObject:
    """Apply complete reviewed Chinese semantic text to one valid report."""

    try:
        normalized = validate_trajectory_user_report(source)
    except TrajectoryUserReportError as error:
        raise TrajectoryUserReportLocalizationError(
            f"Source report is invalid: {error}"
        ) from error
    if normalized["analysis"]["status"] != "accepted":
        raise TrajectoryUserReportLocalizationError(
            "Only accepted semantic reports can be localized"
        )
    expected_fields = {
        "schema",
        "locale",
        "run_id",
        "summary",
        "skill_recommendation_detail",
        "incidents",
    }
    if set(localization) != expected_fields:
        raise TrajectoryUserReportLocalizationError(
            "Localization fields do not match the reviewed input contract"
        )
    if localization.get("schema") != TRAJECTORY_USER_REPORT_LOCALIZATION_SCHEMA:
        raise TrajectoryUserReportLocalizationError(
            "Unsupported localization input schema"
        )
    if localization.get("locale") != SUPPORTED_LOCALE:
        raise TrajectoryUserReportLocalizationError("Unsupported locale")
    if localization.get("run_id") != normalized["run_id"]:
        raise TrajectoryUserReportLocalizationError(
            "Localization run_id differs from the source report"
        )

    summary = _reviewed_text(localization.get("summary"), label="summary")
    raw_skill_detail = localization.get("skill_recommendation_detail")
    skill_detail = (
        None
        if raw_skill_detail is None
        else _reviewed_text(
            raw_skill_detail,
            label="skill_recommendation_detail",
        )
    )
    raw_incidents = localization.get("incidents")
    if not isinstance(raw_incidents, list):
        raise TrajectoryUserReportLocalizationError("incidents must be a list")
    translations: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(raw_incidents):
        if not isinstance(item, Mapping) or set(item) != {
            "id",
            "title",
            "impact",
        }:
            raise TrajectoryUserReportLocalizationError(
                f"incidents[{index}] has invalid fields"
            )
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise TrajectoryUserReportLocalizationError(
                f"incidents[{index}].id must be a non-empty string"
            )
        if identifier in translations:
            raise TrajectoryUserReportLocalizationError(
                f"Duplicate incident localization: {identifier}"
            )
        translations[identifier] = (
            _reviewed_text(item.get("title"), label=f"{identifier}.title"),
            _reviewed_text(item.get("impact"), label=f"{identifier}.impact"),
        )
    expected_incident_ids = {
        str(incident["id"]) for incident in normalized["incidents"]
    }
    if set(translations) != expected_incident_ids:
        raise TrajectoryUserReportLocalizationError(
            "Localization must cover every source incident exactly once"
        )

    localized = copy.deepcopy(normalized)
    localized["narrative"]["summary"] = summary
    if skill_detail is not None:
        localized["overview"]["skill_recommendation"]["detail"] = skill_detail
    for index, incident in enumerate(localized["incidents"]):
        title, impact = translations[str(incident["id"])]
        incident["title"] = title
        incident["impact"] = impact
        timeline_id = f"timeline-incident-{index + 1}"
        for event in localized["narrative"]["timeline"]:
            if event["id"] == timeline_id:
                event["detail"] = impact
                break
    for event in localized["narrative"]["timeline"]:
        if event["id"] == "timeline-conclusion":
            event["detail"] = summary
    return validate_trajectory_user_report(localized)
