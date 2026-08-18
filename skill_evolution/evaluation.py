"""Strict EvaluationSuite contracts and project-local reference resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from scripts.task_case import TaskCase, load_task_case
from skill_evolution.storage import JsonObject, StorageError, load_json_object


EVALUATION_SUITE_SCHEMA = "evaluation.suite.v1"
EVALUATION_SUITE_STATUSES = frozenset({"proposed", "approved"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")


class EvaluationSuiteError(ValueError):
    """Raised when a suite or one of its TaskCase references is invalid."""


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise EvaluationSuiteError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _text(value: Mapping[str, Any], field: str, *, label: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise EvaluationSuiteError(f"{label}.{field} must be non-empty text")
    return item.strip()


def _optional_text(
    value: Mapping[str, Any], field: str, *, label: str
) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise EvaluationSuiteError(f"{label}.{field} must be text or null")
    return item.strip()


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvaluationSuiteError(f"{label} must be a registry identifier")
    return value


def _safe_project_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationSuiteError(f"{label} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise EvaluationSuiteError(f"{label} must stay inside the project root")
    return path.as_posix()


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvaluationSuiteError(f"{label} must be a positive integer")
    return value


def validate_evaluation_suite(value: Mapping[str, Any]) -> JsonObject:
    """Validate and normalize one strict ``evaluation.suite.v1`` document."""

    _exact_fields(
        value,
        {
            "schema",
            "suite_id",
            "skill_id",
            "version",
            "status",
            "owner",
            "approved_by",
            "approved_at",
            "task_cases",
            "coverage_dimensions",
            "readiness",
        },
        label="EvaluationSuite",
    )
    if value.get("schema") != EVALUATION_SUITE_SCHEMA:
        raise EvaluationSuiteError("Unsupported EvaluationSuite schema")
    suite_id = _identifier(value.get("suite_id"), label="suite_id")
    skill_id = _identifier(value.get("skill_id"), label="skill_id")
    version = _text(value, "version", label="EvaluationSuite")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise EvaluationSuiteError("EvaluationSuite.version must use MAJOR.MINOR.PATCH")
    status = value.get("status")
    if status not in EVALUATION_SUITE_STATUSES:
        raise EvaluationSuiteError("EvaluationSuite.status is unsupported")
    owner = _text(value, "owner", label="EvaluationSuite")
    approved_by = _optional_text(value, "approved_by", label="EvaluationSuite")
    approved_at = _optional_text(value, "approved_at", label="EvaluationSuite")
    if status == "approved" and (approved_by is None or approved_at is None):
        raise EvaluationSuiteError(
            "Approved EvaluationSuite requires approved_by and approved_at"
        )
    if status == "proposed" and (approved_by is not None or approved_at is not None):
        raise EvaluationSuiteError(
            "Proposed EvaluationSuite approval fields must be null"
        )

    raw_cases = value.get("task_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationSuiteError("EvaluationSuite.task_cases must not be empty")
    task_cases: list[JsonObject] = []
    task_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        label = f"EvaluationSuite.task_cases[{index}]"
        if not isinstance(raw_case, Mapping):
            raise EvaluationSuiteError(f"{label} must be an object")
        _exact_fields(
            raw_case,
            {"task_case_id", "path", "conditions"},
            label=label,
        )
        task_case_id = _identifier(
            raw_case.get("task_case_id"), label=f"{label}.task_case_id"
        )
        if task_case_id in task_case_ids:
            raise EvaluationSuiteError(
                f"Duplicate EvaluationSuite task_case_id: {task_case_id}"
            )
        task_case_ids.add(task_case_id)
        raw_conditions = raw_case.get("conditions")
        if not isinstance(raw_conditions, Mapping) or not raw_conditions:
            raise EvaluationSuiteError(f"{label}.conditions must not be empty")
        conditions: JsonObject = {}
        for raw_key, raw_condition in raw_conditions.items():
            key = _identifier(raw_key, label=f"{label}.conditions key")
            if not isinstance(raw_condition, str) or not raw_condition.strip():
                raise EvaluationSuiteError(
                    f"{label}.conditions.{key} must be non-empty text"
                )
            conditions[key] = raw_condition.strip()
        task_cases.append(
            {
                "task_case_id": task_case_id,
                "path": _safe_project_path(raw_case.get("path"), label=f"{label}.path"),
                "conditions": dict(sorted(conditions.items())),
            }
        )

    raw_dimensions = value.get("coverage_dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise EvaluationSuiteError(
            "EvaluationSuite.coverage_dimensions must not be empty"
        )
    dimensions: list[JsonObject] = []
    dimension_ids: set[str] = set()
    allowed_values: dict[str, set[str]] = {}
    for index, raw_dimension in enumerate(raw_dimensions):
        label = f"EvaluationSuite.coverage_dimensions[{index}]"
        if not isinstance(raw_dimension, Mapping):
            raise EvaluationSuiteError(f"{label} must be an object")
        _exact_fields(raw_dimension, {"id", "required_values"}, label=label)
        dimension_id = _identifier(raw_dimension.get("id"), label=f"{label}.id")
        if dimension_id in dimension_ids:
            raise EvaluationSuiteError(
                f"Duplicate coverage dimension: {dimension_id}"
            )
        dimension_ids.add(dimension_id)
        raw_values = raw_dimension.get("required_values")
        if (
            not isinstance(raw_values, list)
            or not raw_values
            or not all(isinstance(item, str) and item.strip() for item in raw_values)
        ):
            raise EvaluationSuiteError(f"{label}.required_values is invalid")
        required_values = [str(item).strip() for item in raw_values]
        if len(required_values) != len(set(required_values)):
            raise EvaluationSuiteError(f"{label}.required_values must be unique")
        allowed_values[dimension_id] = set(required_values)
        dimensions.append(
            {"id": dimension_id, "required_values": required_values}
        )

    for task_case in task_cases:
        actual_dimensions = set(task_case["conditions"])
        if actual_dimensions != dimension_ids:
            raise EvaluationSuiteError(
                "TaskCase conditions must cover exactly the suite dimensions: "
                f"missing={sorted(dimension_ids - actual_dimensions)}, "
                f"unexpected={sorted(actual_dimensions - dimension_ids)}"
            )
        for dimension_id, condition in task_case["conditions"].items():
            if condition not in allowed_values[dimension_id]:
                raise EvaluationSuiteError(
                    "TaskCase condition is outside required_values: "
                    f"{dimension_id}={condition}"
                )
    for dimension_id, required_values in allowed_values.items():
        represented = {
            str(task_case["conditions"][dimension_id])
            for task_case in task_cases
        }
        missing_values = required_values - represented
        if missing_values:
            raise EvaluationSuiteError(
                "EvaluationSuite has required values with no TaskCase: "
                f"{dimension_id}={sorted(missing_values)}"
            )
    raw_readiness = value.get("readiness")
    if not isinstance(raw_readiness, Mapping):
        raise EvaluationSuiteError("EvaluationSuite.readiness must be an object")
    _exact_fields(
        raw_readiness,
        {
            "minimum_distinct_condition_groups",
            "minimum_samples_per_condition_group",
        },
        label="EvaluationSuite.readiness",
    )
    readiness = {
        "minimum_distinct_condition_groups": _positive_integer(
            raw_readiness.get("minimum_distinct_condition_groups"),
            label="readiness.minimum_distinct_condition_groups",
        ),
        "minimum_samples_per_condition_group": _positive_integer(
            raw_readiness.get("minimum_samples_per_condition_group"),
            label="readiness.minimum_samples_per_condition_group",
        ),
    }
    return {
        "schema": EVALUATION_SUITE_SCHEMA,
        "suite_id": suite_id,
        "skill_id": skill_id,
        "version": version,
        "status": status,
        "owner": owner,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "task_cases": task_cases,
        "coverage_dimensions": dimensions,
        "readiness": readiness,
    }


@dataclass(frozen=True)
class ResolvedEvaluationSuite:
    """One validated suite together with its validated TaskCase objects."""

    path: Path
    document: JsonObject
    task_cases: Mapping[str, TaskCase]


class EvaluationSuiteResolver:
    """Resolve registry IDs to project-local suites and TaskCases."""

    def __init__(
        self,
        suites_root: str | os.PathLike[str],
        *,
        project_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.suites_root = Path(suites_root).resolve()
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else self.suites_root.parent
        )

    def resolve(
        self,
        suite_id: str,
        *,
        require_approved: bool = True,
    ) -> ResolvedEvaluationSuite:
        """Load one suite and fail closed on approval or reference drift."""

        identifier = _identifier(suite_id, label="suite_id")
        path = self.suites_root / f"{identifier}.json"
        try:
            document = validate_evaluation_suite(load_json_object(path))
        except StorageError as error:
            raise EvaluationSuiteError(str(error)) from error
        if document["suite_id"] != identifier:
            raise EvaluationSuiteError("EvaluationSuite identity differs from its path")
        if require_approved and document["status"] != "approved":
            raise EvaluationSuiteError("EvaluationSuite is not approved")

        resolved_cases: dict[str, TaskCase] = {}
        for reference in document["task_cases"]:
            relative = PurePosixPath(str(reference["path"]))
            unresolved = self.project_root / Path(*relative.parts)
            current = self.project_root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise EvaluationSuiteError(
                        f"TaskCase reference is unsafe: {reference['path']}"
                    )
            candidate = unresolved.resolve()
            if not candidate.is_relative_to(self.project_root):
                raise EvaluationSuiteError("TaskCase reference escapes project root")
            if candidate.is_symlink() or not candidate.is_file():
                raise EvaluationSuiteError(
                    f"TaskCase reference is missing or unsafe: {reference['path']}"
                )
            try:
                task_case = load_task_case(candidate)
            except (FileNotFoundError, OSError, ValueError) as error:
                raise EvaluationSuiteError(
                    f"TaskCase could not be loaded: {reference['path']}: {error}"
                ) from error
            if task_case.task_case_id != reference["task_case_id"]:
                raise EvaluationSuiteError(
                    "EvaluationSuite TaskCase identity differs from referenced file"
                )
            resolved_cases[task_case.task_case_id] = task_case
        return ResolvedEvaluationSuite(path, document, resolved_cases)
