"""Versioned task-case contracts for repeatable skill execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence


TASK_CASE_SCHEMA = "task.case.v1"
DELIVERY_FILE = "file"
DELIVERY_INLINE_TEXT = "inline_text"
SUPPORTED_DELIVERIES = frozenset({DELIVERY_FILE, DELIVERY_INLINE_TEXT})
DEFAULT_EXPECTED_ARTIFACTS = ("output.html",)
RESERVED_OUTPUT_DIRECTORIES = frozenset({"input", "skill", "runtime"})


def _require_non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _safe_relative_path(value: object, field_name: str) -> str:
    text = _require_non_empty_text(value, field_name)
    if "\\" in text:
        raise ValueError(f"{field_name} must use forward slashes")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError(f"{field_name} must stay inside the task workspace")
    return path.as_posix()


def _normalize_expected_artifacts(
    values: Sequence[str],
) -> tuple[str, ...]:
    if not values:
        raise ValueError("expected_artifacts must not be empty")
    normalized = tuple(
        _safe_relative_path(value, "expected_artifacts item")
        for value in values
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError("expected_artifacts must not contain duplicates")
    for artifact in normalized:
        first_part = PurePosixPath(artifact).parts[0]
        if first_part in RESERVED_OUTPUT_DIRECTORIES:
            raise ValueError(
                "expected_artifacts must not target reserved task directories"
            )
    return normalized


def _normalize_tags(values: Sequence[str]) -> tuple[str, ...]:
    tags = tuple(
        _require_non_empty_text(value, "capability_tags item")
        for value in values
    )
    if len(set(tags)) != len(tags):
        raise ValueError("capability_tags must not contain duplicates")
    return tags


@dataclass(frozen=True)
class TaskCase:
    """One immutable input and output contract for a skill execution."""

    schema: ClassVar[str] = TASK_CASE_SCHEMA

    task_case_id: str
    delivery: str
    expected_artifacts: tuple[str, ...] = DEFAULT_EXPECTED_ARTIFACTS
    source_path: Path | None = None
    source_name: str | None = None
    inline_text: str | None = None
    capability_tags: tuple[str, ...] = ()
    budget: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        task_case_id = _require_non_empty_text(
            self.task_case_id,
            "task_case_id",
        )
        if self.delivery not in SUPPORTED_DELIVERIES:
            raise ValueError(
                "delivery must be either 'file' or 'inline_text'"
            )
        expected_artifacts = _normalize_expected_artifacts(
            self.expected_artifacts
        )
        capability_tags = _normalize_tags(self.capability_tags)
        budget = dict(self.budget)
        if not all(isinstance(key, str) and key for key in budget):
            raise ValueError("budget keys must be non-empty strings")
        try:
            json.dumps(budget)
        except (TypeError, ValueError) as error:
            raise ValueError("budget must contain JSON-compatible values") from error

        if self.delivery == DELIVERY_FILE:
            if self.source_path is None:
                raise ValueError("file delivery requires source_path")
            if self.inline_text is not None:
                raise ValueError("file delivery cannot include inline_text")
            resolved_source = self.source_path.resolve()
            if not resolved_source.is_file():
                raise FileNotFoundError(
                    f"Task fixture not found: {resolved_source}"
                )
            source_name = _require_non_empty_text(
                self.source_name or self.source_path.name,
                "source_name",
            )
            if (
                PurePosixPath(source_name).name != source_name
                or "\\" in source_name
            ):
                raise ValueError("source_name must be one file name")
            object.__setattr__(self, "source_path", resolved_source)
            object.__setattr__(self, "source_name", source_name)
        else:
            if self.source_path is not None or self.source_name is not None:
                raise ValueError(
                    "inline_text delivery cannot include a source file"
                )
            _require_non_empty_text(self.inline_text, "inline_text")

        object.__setattr__(self, "task_case_id", task_case_id)
        object.__setattr__(self, "expected_artifacts", expected_artifacts)
        object.__setattr__(self, "capability_tags", capability_tags)
        object.__setattr__(self, "budget", MappingProxyType(budget))

    @classmethod
    def for_file(
        cls,
        source_path: str | os.PathLike[str],
        *,
        task_case_id: str = "legacy-file-input",
        expected_artifacts: Sequence[str] = DEFAULT_EXPECTED_ARTIFACTS,
        capability_tags: Sequence[str] = (),
        budget: Mapping[str, Any] | None = None,
    ) -> TaskCase:
        """Build a task case from the legacy single-source interface."""

        provided_path = Path(source_path)
        return cls(
            task_case_id=task_case_id,
            delivery=DELIVERY_FILE,
            source_path=provided_path,
            source_name=provided_path.name,
            expected_artifacts=tuple(expected_artifacts),
            capability_tags=tuple(capability_tags),
            budget=budget or {},
        )

    @classmethod
    def for_inline_text(
        cls,
        text: str,
        *,
        task_case_id: str,
        expected_artifacts: Sequence[str] = DEFAULT_EXPECTED_ARTIFACTS,
        capability_tags: Sequence[str] = (),
        budget: Mapping[str, Any] | None = None,
    ) -> TaskCase:
        """Build a task case whose input is delivered only in the prompt."""

        return cls(
            task_case_id=task_case_id,
            delivery=DELIVERY_INLINE_TEXT,
            inline_text=text,
            expected_artifacts=tuple(expected_artifacts),
            capability_tags=tuple(capability_tags),
            budget=budget or {},
        )

    @property
    def workspace_input_path(self) -> str | None:
        """Return the input path as seen from the Pi working directory."""

        if self.delivery != DELIVERY_FILE:
            return None
        return f"input/{self.source_name}"

    def prompt_payload(self) -> dict[str, Any]:
        """Return only the task data required by the executing agent."""

        if self.delivery == DELIVERY_FILE:
            input_record: dict[str, Any] = {
                "type": DELIVERY_FILE,
                "path": self.workspace_input_path,
            }
        else:
            input_record = {
                "type": DELIVERY_INLINE_TEXT,
                "text": self.inline_text,
            }
        return {
            "input": input_record,
            "expected_artifacts": list(self.expected_artifacts),
        }

    def record_payload(self) -> dict[str, Any]:
        """Return the complete task definition retained by the framework."""

        if self.delivery == DELIVERY_FILE:
            input_record: dict[str, Any] = {
                "path": self.workspace_input_path,
                "original_filename": self.source_name,
            }
        else:
            input_record = {"text": self.inline_text}
        return {
            "schema": self.schema,
            "task_case_id": self.task_case_id,
            "delivery": self.delivery,
            "input": input_record,
            "expected_artifacts": list(self.expected_artifacts),
            "capability_tags": list(self.capability_tags),
            "budget": dict(self.budget),
        }

    def manifest_payload(self, *, run_relative_input: str | None) -> dict[str, Any]:
        """Return the task case as persisted in a trajectory manifest."""

        payload = self.record_payload()
        input_record = dict(payload["input"])
        if self.delivery == DELIVERY_FILE:
            input_record["workspace_path"] = run_relative_input
            input_record["source_path"] = str(self.source_path)
        payload["input"] = run_relative_input
        payload["input_spec"] = input_record
        payload["expected_artifacts"] = [
            f"artifacts/{path}" for path in self.expected_artifacts
        ]
        payload["expected_artifact"] = payload["expected_artifacts"][0]
        return payload


def task_case_from_mapping(
    value: Mapping[str, Any],
    *,
    base_directory: Path,
) -> TaskCase:
    """Parse one ``task.case.v1`` object using a known fixture base path."""

    if value.get("schema") != TASK_CASE_SCHEMA:
        raise ValueError(
            f"Unsupported task case schema: {value.get('schema')}"
        )
    task_case_id = _require_non_empty_text(
        value.get("task_case_id"),
        "task_case_id",
    )
    delivery = value.get("delivery")
    input_value = value.get("input")
    if not isinstance(input_value, Mapping):
        raise ValueError("task case input must be an object")
    expected_value = value.get(
        "expected_artifacts",
        list(DEFAULT_EXPECTED_ARTIFACTS),
    )
    tags_value = value.get("capability_tags", [])
    budget_value = value.get("budget", {})
    if not isinstance(expected_value, list) or not all(
        isinstance(item, str) for item in expected_value
    ):
        raise ValueError("expected_artifacts must be a string array")
    if not isinstance(tags_value, list) or not all(
        isinstance(item, str) for item in tags_value
    ):
        raise ValueError("capability_tags must be a string array")
    if not isinstance(budget_value, Mapping):
        raise ValueError("budget must be an object")

    if delivery == DELIVERY_FILE:
        source_value = _require_non_empty_text(
            input_value.get("path"),
            "input.path",
        )
        source_path = Path(source_value)
        if not source_path.is_absolute():
            source_path = base_directory / source_path
        return TaskCase.for_file(
            source_path,
            task_case_id=task_case_id,
            expected_artifacts=expected_value,
            capability_tags=tags_value,
            budget=budget_value,
        )
    if delivery == DELIVERY_INLINE_TEXT:
        text = _require_non_empty_text(
            input_value.get("text"),
            "input.text",
        )
        return TaskCase.for_inline_text(
            text,
            task_case_id=task_case_id,
            expected_artifacts=expected_value,
            capability_tags=tags_value,
            budget=budget_value,
        )
    raise ValueError("delivery must be either 'file' or 'inline_text'")


def load_task_case(path: str | os.PathLike[str]) -> TaskCase:
    """Load and validate one TaskCase JSON file."""

    resolved = Path(path).resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Task case file not found: {resolved}"
        ) from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Task case is not valid JSON: {resolved}") from error
    if not isinstance(value, Mapping):
        raise ValueError("Task case must be a JSON object")
    return task_case_from_mapping(value, base_directory=resolved.parent)
