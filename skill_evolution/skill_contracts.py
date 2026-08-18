"""Deterministic validation reports for Skill packages and contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
from typing import Any

from scripts.task_case import TaskCase, load_task_case
from skill_evolution.analysis import (
    AnalysisContractError,
    SKILL_CONTRACT_SCHEMA,
    validate_capability_contract,
    validate_skill_contract_document,
)
from skill_evolution.storage import (
    JsonObject,
    StorageError,
    load_json_object,
    utc_now,
)


SKILL_VALIDATION_REPORT_SCHEMA = "skill.validation_report.v1"
_FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")


def _issue(
    code: str,
    message: str,
    location: str,
    **details: Any,
) -> JsonObject:
    value: JsonObject = {
        "code": code,
        "message": message,
        "location": location,
    }
    value.update(details)
    return value


def _contract_summary(
    contract_path: Path,
    contract: Mapping[str, Any] | None,
) -> JsonObject:
    value = contract or {}
    return {
        "path": str(contract_path),
        "schema": value.get("schema"),
        "skill_id": value.get("skill_id"),
        "version": value.get("version"),
        "status": value.get("status"),
    }


def _parse_front_matter(
    text: str,
    *,
    errors: list[JsonObject],
) -> JsonObject:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        errors.append(
            _issue(
                "missing_front_matter",
                "SKILL.md must start with Markdown front matter.",
                "skill:SKILL.md:1",
            )
        )
        return {"name": None, "description": None}
    try:
        closing_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        errors.append(
            _issue(
                "unclosed_front_matter",
                "SKILL.md front matter has no closing delimiter.",
                "skill:SKILL.md:1",
            )
        )
        return {"name": None, "description": None}

    fields: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:closing_index], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            errors.append(
                _issue(
                    "invalid_front_matter_line",
                    "Front matter entries must use 'key: value'.",
                    f"skill:SKILL.md:{line_number}",
                )
            )
            continue
        key, raw_value = line.split(":", 1)
        normalized_key = key.strip()
        normalized_value = raw_value.strip().strip('"').strip("'")
        if normalized_key:
            fields[normalized_key] = normalized_value

    for required in ("name", "description"):
        if not fields.get(required):
            errors.append(
                _issue(
                    f"missing_front_matter_{required}",
                    f"SKILL.md front matter requires a non-empty {required}.",
                    "skill:SKILL.md:1",
                )
            )
    return {
        "name": fields.get("name"),
        "description": fields.get("description"),
    }


def _inspect_markdown(
    text: str,
    *,
    errors: list[JsonObject],
    warnings: list[JsonObject],
) -> JsonObject:
    open_fence: tuple[str, int, int] | None = None
    headings: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence = _FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group(1)
            character = marker[0]
            length = len(marker)
            if open_fence is None:
                open_fence = (character, length, line_number)
            elif character == open_fence[0] and length >= open_fence[1]:
                open_fence = None
            continue
        if open_fence is None and line.startswith("# "):
            headings.append((line_number, line[2:].strip()))

    if open_fence is not None:
        errors.append(
            _issue(
                "unclosed_fenced_code_block",
                "SKILL.md contains an unclosed fenced code block.",
                f"skill:SKILL.md:{open_fence[2]}",
            )
        )
    if not headings:
        errors.append(
            _issue(
                "missing_primary_heading",
                "SKILL.md requires one top-level Markdown heading.",
                "skill:SKILL.md",
            )
        )
    elif len(headings) > 1:
        warnings.append(
            _issue(
                "multiple_primary_headings",
                "SKILL.md contains more than one top-level heading.",
                f"skill:SKILL.md:{headings[1][0]}",
                heading_count=len(headings),
            )
        )
    return {
        "primary_heading": headings[0][1] if headings else None,
        "primary_heading_count": len(headings),
        "fenced_code_blocks_closed": open_fence is None,
    }


def _inspect_skill_package(
    skill_directory: str | os.PathLike[str],
    *,
    errors: list[JsonObject],
    warnings: list[JsonObject],
) -> JsonObject:
    provided = Path(skill_directory).absolute()
    summary: JsonObject = {
        "path": str(provided),
        "entrypoint": "SKILL.md",
        "exists": False,
        "file_count": 0,
        "total_bytes": 0,
        "skill_md_bytes": 0,
        "skill_md_lines": 0,
        "unicode_codepoints": 0,
        "front_matter": {"name": None, "description": None},
        "markdown": {},
    }
    if provided.is_symlink():
        errors.append(
            _issue(
                "skill_root_is_symlink",
                "Skill package root must not be a symlink.",
                "skill:.",
            )
        )
        return summary
    if not provided.is_dir():
        errors.append(
            _issue(
                "skill_directory_missing",
                "Skill package directory does not exist.",
                "skill:.",
            )
        )
        return summary
    summary["exists"] = True

    try:
        package_paths = sorted(provided.rglob("*"))
    except OSError as error:
        errors.append(
            _issue(
                "skill_inventory_failed",
                f"Could not enumerate the Skill package: {error}",
                "skill:.",
            )
        )
        package_paths = []

    files: list[Path] = []
    for path in package_paths:
        relative = path.relative_to(provided).as_posix()
        if path.is_symlink():
            errors.append(
                _issue(
                    "skill_package_symlink",
                    "Skill packages must not contain symlinks.",
                    f"skill:{relative}",
                )
            )
        elif path.is_file() and path.name != ".DS_Store":
            files.append(path)
    summary["file_count"] = len(files)
    try:
        summary["total_bytes"] = sum(path.stat().st_size for path in files)
    except OSError as error:
        errors.append(
            _issue(
                "skill_inventory_failed",
                f"Could not inspect all Skill files: {error}",
                "skill:.",
            )
        )

    entrypoint = provided / "SKILL.md"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        errors.append(
            _issue(
                "skill_entrypoint_missing",
                "Skill package must contain a regular SKILL.md file.",
                "skill:SKILL.md",
            )
        )
        return summary
    try:
        text = entrypoint.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(
            _issue(
                "skill_entrypoint_unreadable",
                f"SKILL.md must be readable UTF-8 text: {error}",
                "skill:SKILL.md",
            )
        )
        return summary

    encoded = text.encode("utf-8")
    summary.update(
        {
            "skill_md_bytes": len(encoded),
            "skill_md_lines": len(text.splitlines()),
            "unicode_codepoints": len(text),
            "front_matter": _parse_front_matter(text, errors=errors),
            "markdown": _inspect_markdown(
                text,
                errors=errors,
                warnings=warnings,
            ),
        }
    )
    return summary


def _task_case_format(task_case: TaskCase) -> str:
    if task_case.delivery == "inline_text":
        return "inline_text"
    if task_case.source_name is None:
        return "unknown"
    return Path(task_case.source_name).suffix.lower().lstrip(".") or "unknown"


def _load_task_cases(
    task_case_paths: Sequence[str | os.PathLike[str]],
    *,
    errors: list[JsonObject],
    warnings: list[JsonObject],
) -> tuple[list[TaskCase], list[JsonObject]]:
    cases: list[TaskCase] = []
    records: list[JsonObject] = []
    identifiers: set[str] = set()
    for raw_path in sorted(task_case_paths, key=lambda item: str(item)):
        path = Path(raw_path).absolute()
        try:
            task_case = load_task_case(path)
        except (FileNotFoundError, OSError, ValueError) as error:
            errors.append(
                _issue(
                    "task_case_invalid",
                    f"TaskCase could not be loaded: {error}",
                    f"task_case:{path}",
                )
            )
            continue
        if task_case.task_case_id in identifiers:
            errors.append(
                _issue(
                    "duplicate_task_case_id",
                    "TaskCase identifiers must be unique in one validation.",
                    f"task_case:{path}",
                    task_case_id=task_case.task_case_id,
                )
            )
            continue
        identifiers.add(task_case.task_case_id)
        cases.append(task_case)
        records.append(
            {
                "path": str(path),
                "task_case_id": task_case.task_case_id,
                "delivery": task_case.delivery,
                "format": _task_case_format(task_case),
                "expected_artifacts": list(task_case.expected_artifacts),
                "capability_tags": list(task_case.capability_tags),
            }
        )
    if not task_case_paths:
        warnings.append(
            _issue(
                "no_task_cases",
                "No TaskCases were supplied, so capability coverage is empty.",
                "task_cases",
            )
        )
    return cases, records


def _normalized_format(value: str) -> str:
    return value.lower().lstrip(".")


def _build_coverage(
    contract: Mapping[str, Any] | None,
    task_cases: Sequence[TaskCase],
    *,
    coverage_gaps: list[JsonObject],
    suggestions: list[JsonObject],
) -> JsonObject:
    if contract is None:
        return {
            "basis": "delivery_and_format",
            "capabilities": [],
            "uncovered_capability_ids": [],
            "unclaimed_task_case_ids": [],
        }
    if contract["schema"] == SKILL_CONTRACT_SCHEMA:
        return {
            "basis": "evaluation_suite_reference",
            "suite_refs": list(contract["evaluation"]["suite_refs"]),
            "task_case_ids": [case.task_case_id for case in task_cases],
            "capabilities": [],
            "uncovered_capability_ids": [],
            "unclaimed_task_case_ids": [],
        }
    capability_records: list[JsonObject] = []
    claimed_task_cases: set[str] = set()
    uncovered: list[str] = []
    for capability in contract["capabilities"]:
        deliveries = set(capability["delivery_modes"])
        formats = {
            _normalized_format(item) for item in capability["formats"]
        }
        covered_by = [
            task_case.task_case_id
            for task_case in task_cases
            if task_case.delivery in deliveries
            and _task_case_format(task_case) in formats
        ]
        claimed_task_cases.update(covered_by)
        capability_id = str(capability["id"])
        capability_records.append(
            {
                "capability_id": capability_id,
                "covered_by_task_case_ids": covered_by,
            }
        )
        if not covered_by:
            uncovered.append(capability_id)
            coverage_gaps.append(
                _issue(
                    "capability_without_task_case",
                    "No supplied TaskCase matches this capability's delivery "
                    "mode and format.",
                    f"contract:/capabilities/{capability_id}",
                    capability_id=capability_id,
                )
            )

    unclaimed = sorted(
        task_case.task_case_id
        for task_case in task_cases
        if task_case.task_case_id not in claimed_task_cases
    )
    for task_case_id in unclaimed:
        coverage_gaps.append(
            _issue(
                "task_case_outside_contract",
                "No capability claims this TaskCase delivery and format.",
                f"task_case:{task_case_id}",
                task_case_id=task_case_id,
            )
        )
    if contract["capabilities"]:
        suggestions.append(
            _issue(
                "natural_language_evidence_requirements",
                "Version 1 evidence requirements are natural language; this "
                "report checks only delivery and format coverage.",
                "contract:/capabilities",
            )
        )
    return {
        "basis": "delivery_and_format",
        "capabilities": capability_records,
        "uncovered_capability_ids": uncovered,
        "unclaimed_task_case_ids": unclaimed,
    }


class SkillContractValidator:
    """Build one non-mutating validation report for a Skill and its tests."""

    def validate(
        self,
        *,
        contract_path: str | os.PathLike[str] | None = None,
        skill_directory: str | os.PathLike[str],
        task_case_paths: Sequence[str | os.PathLike[str]] = (),
    ) -> JsonObject:
        """Validate structure and coverage without executing a model or tools."""

        errors: list[JsonObject] = []
        warnings: list[JsonObject] = []
        suggestions: list[JsonObject] = []
        coverage_gaps: list[JsonObject] = []
        resolved_skill = Path(skill_directory).absolute()
        resolved_contract = (
            Path(contract_path).absolute()
            if contract_path is not None
            else resolved_skill / "skill_contract.json"
        )
        raw_contract: JsonObject | None = None
        normalized_contract: JsonObject | None = None
        try:
            raw_contract = load_json_object(resolved_contract)
            if raw_contract.get("schema") == SKILL_CONTRACT_SCHEMA:
                if resolved_contract.name != "skill_contract.json":
                    raise AnalysisContractError(
                        "The active Skill Contract must be named "
                        "skill_contract.json"
                    )
                if resolved_contract.parent != resolved_skill:
                    raise AnalysisContractError(
                        "skill_contract.json must be inside the Skill package"
                    )
                normalized_contract = validate_skill_contract_document(
                    raw_contract
                )
            else:
                normalized_contract = validate_capability_contract(
                    raw_contract
                )
        except (
            AnalysisContractError,
            OSError,
            StorageError,
            UnicodeError,
        ) as error:
            errors.append(
                _issue(
                    "skill_contract_invalid",
                    str(error),
                    "contract:.",
                )
            )

        if (
            normalized_contract is not None
            and normalized_contract["status"] != "approved"
        ):
            warnings.append(
                _issue(
                    "skill_contract_not_approved",
                    "The contract is structurally valid but not approved for "
                    "dynamic model work.",
                    "contract:/status",
                )
            )

        skill = _inspect_skill_package(
            resolved_skill,
            errors=errors,
            warnings=warnings,
        )
        task_cases, task_case_records = _load_task_cases(
            task_case_paths,
            errors=errors,
            warnings=warnings,
        )
        coverage = _build_coverage(
            normalized_contract,
            task_cases,
            coverage_gaps=coverage_gaps,
            suggestions=suggestions,
        )
        valid = not errors
        approved = bool(
            normalized_contract is not None
            and normalized_contract["status"] == "approved"
        )
        if errors:
            status = "error"
        elif warnings or coverage_gaps:
            status = "warning"
        else:
            status = "valid"
        return {
            "schema": SKILL_VALIDATION_REPORT_SCHEMA,
            "checked_at": utc_now(),
            "status": status,
            "valid": valid,
            "dynamic_test_ready": valid and approved,
            "contract": _contract_summary(
                resolved_contract,
                normalized_contract or raw_contract,
            ),
            "skill": skill,
            "task_cases": task_case_records,
            "coverage": coverage,
            "errors": errors,
            "warnings": warnings,
            "suggestions": suggestions,
            "coverage_gaps": coverage_gaps,
        }


def validate_skill_contract(
    *,
    contract_path: str | os.PathLike[str] | None = None,
    skill_directory: str | os.PathLike[str],
    task_case_paths: Sequence[str | os.PathLike[str]] = (),
) -> JsonObject:
    """Convenience API for the default deterministic Skill validator."""

    return SkillContractValidator().validate(
        contract_path=contract_path,
        skill_directory=skill_directory,
        task_case_paths=task_case_paths,
    )
