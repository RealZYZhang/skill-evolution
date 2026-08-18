#!/usr/bin/env python3
"""Inspect and explicitly approve versioned prompt files."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


APPROVAL_SCHEMA = "prompt.approval.v1"
SKILL_CONTENT_PLACEHOLDER = "{{SKILL_CONTENT}}"
TASK_CASE_PLACEHOLDER = "{{TASK_CASE}}"


class PromptApprovalError(ValueError):
    """Raised when a prompt lacks a valid owner approval."""


@dataclass(frozen=True)
class ApprovedPrompt:
    """An approved prompt and the metadata binding approval to its content."""

    path: Path
    approval_path: Path
    text: str
    prompt_id: str
    version: str
    approved_by: str
    approved_at: str
    content_sha256: str


@dataclass(frozen=True)
class RenderedPrompt:
    """A reviewed template after injecting one concrete skill entrypoint."""

    text: str
    skill_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def approval_path_for(prompt_path: Path) -> Path:
    """Return the approval sidecar path for a prompt file."""

    return prompt_path.with_name(prompt_path.name + ".approval.json")


def _read_approval(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PromptApprovalError(
            f"Prompt approval file not found: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise PromptApprovalError(
            f"Prompt approval is not valid JSON: {path}"
        ) from error
    if not isinstance(value, dict):
        raise PromptApprovalError("Prompt approval must be a JSON object.")
    return value


def _require_text(
    approval: Mapping[str, Any],
    field: str,
) -> str:
    value = approval.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PromptApprovalError(
            f"Prompt approval field must be non-empty: {field}"
        )
    return value


def load_approved_prompt(
    prompt_path: str | os.PathLike[str],
) -> ApprovedPrompt:
    """Load a prompt only when its owner approval matches current content."""

    resolved = Path(prompt_path).resolve()
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PromptApprovalError(
            f"Prompt file not found: {resolved}"
        ) from error
    if not text.strip():
        raise PromptApprovalError("Prompt file must not be empty.")

    sidecar = approval_path_for(resolved)
    approval = _read_approval(sidecar)
    if approval.get("schema") != APPROVAL_SCHEMA:
        raise PromptApprovalError(
            f"Unsupported prompt approval schema: {approval.get('schema')}"
        )
    if approval.get("status") != "approved":
        raise PromptApprovalError(
            "Prompt is not approved by the project owner."
        )
    if approval.get("prompt_file") != resolved.name:
        raise PromptApprovalError(
            "Prompt approval references a different prompt file."
        )

    content_sha256 = _content_sha256(text)
    if approval.get("content_sha256") != content_sha256:
        raise PromptApprovalError(
            "Prompt content changed after approval; review it again."
        )

    return ApprovedPrompt(
        path=resolved,
        approval_path=sidecar,
        text=text,
        prompt_id=_require_text(approval, "prompt_id"),
        version=_require_text(approval, "version"),
        approved_by=_require_text(approval, "approved_by"),
        approved_at=_require_text(approval, "approved_at"),
        content_sha256=content_sha256,
    )


def render_skill_prompt(
    template: ApprovedPrompt,
    skill_path: str | os.PathLike[str],
) -> RenderedPrompt:
    """Inject one complete SKILL.md into an approved execution template."""

    return render_skill_template(template.text, skill_path)


def render_execution_prompt(
    template: ApprovedPrompt,
    skill_path: str | os.PathLike[str],
    task_case: Mapping[str, Any],
) -> RenderedPrompt:
    """Inject a skill and structured task data into an execution template."""

    return render_execution_template(
        template.text,
        skill_path,
        task_case,
    )


def render_execution_template(
    template_text: str,
    skill_path: str | os.PathLike[str],
    task_case: Mapping[str, Any],
) -> RenderedPrompt:
    """Render an execution template without replacing tokens inside inputs."""

    task_placeholder_count = template_text.count(TASK_CASE_PLACEHOLDER)
    if task_placeholder_count != 1:
        raise PromptApprovalError(
            "Approved execution prompt template must contain "
            f"{TASK_CASE_PLACEHOLDER} exactly once."
        )
    skill_placeholder_count = template_text.count(SKILL_CONTENT_PLACEHOLDER)
    if skill_placeholder_count != 1:
        raise PromptApprovalError(
            "Approved skill prompt template must contain "
            f"{SKILL_CONTENT_PLACEHOLDER} exactly once."
        )

    resolved = Path(skill_path).resolve()
    entrypoint = resolved / "SKILL.md" if resolved.is_dir() else resolved
    try:
        skill_text = entrypoint.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PromptApprovalError(
            f"Skill entrypoint not found: {entrypoint}"
        ) from error
    if not skill_text.strip():
        raise PromptApprovalError("Skill entrypoint must not be empty.")
    try:
        task_text = json.dumps(
            dict(task_case),
            ensure_ascii=False,
            indent=2,
        )
    except (TypeError, ValueError) as error:
        raise PromptApprovalError(
            "Task case prompt data must be JSON-compatible."
        ) from error

    before_skill, _, after_skill = template_text.partition(
        SKILL_CONTENT_PLACEHOLDER
    )
    before_skill = before_skill.replace(TASK_CASE_PLACEHOLDER, task_text)
    after_skill = after_skill.replace(TASK_CASE_PLACEHOLDER, task_text)
    rendered = before_skill + skill_text.rstrip() + after_skill
    return RenderedPrompt(text=rendered, skill_path=entrypoint)


def render_skill_template(
    template_text: str,
    skill_path: str | os.PathLike[str],
) -> RenderedPrompt:
    """Render a skill template for approval preview or execution."""

    resolved = Path(skill_path).resolve()
    entrypoint = resolved / "SKILL.md" if resolved.is_dir() else resolved
    try:
        skill_text = entrypoint.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PromptApprovalError(
            f"Skill entrypoint not found: {entrypoint}"
        ) from error
    if not skill_text.strip():
        raise PromptApprovalError("Skill entrypoint must not be empty.")

    placeholder_count = template_text.count(SKILL_CONTENT_PLACEHOLDER)
    if placeholder_count != 1:
        raise PromptApprovalError(
            "Approved skill prompt template must contain "
            f"{SKILL_CONTENT_PLACEHOLDER} exactly once."
        )
    rendered = template_text.replace(
        SKILL_CONTENT_PLACEHOLDER,
        skill_text.rstrip(),
    )
    return RenderedPrompt(text=rendered, skill_path=entrypoint)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def approve_prompt(
    prompt_path: str | os.PathLike[str],
    *,
    approved_by: str,
) -> Path:
    """Record explicit approval for the current prompt file content."""

    resolved = Path(prompt_path).resolve()
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PromptApprovalError(
            f"Prompt file not found: {resolved}"
        ) from error
    if not text.strip():
        raise PromptApprovalError("Prompt file must not be empty.")
    if not approved_by.strip():
        raise PromptApprovalError("approved_by must not be empty.")

    sidecar = approval_path_for(resolved)
    current = _read_approval(sidecar)
    if current.get("schema") != APPROVAL_SCHEMA:
        raise PromptApprovalError("Unsupported prompt approval schema.")
    prompt_id = _require_text(current, "prompt_id")
    version = _require_text(current, "version")
    approval = {
        "schema": APPROVAL_SCHEMA,
        "status": "approved",
        "prompt_id": prompt_id,
        "version": version,
        "prompt_file": resolved.name,
        "content_sha256": _content_sha256(text),
        "approved_by": approved_by.strip(),
        "approved_at": _utc_now(),
    }
    _atomic_write_json(sidecar, approval)
    return sidecar


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Print a prompt and its review metadata",
    )
    inspect_parser.add_argument("--prompt-file", required=True)
    inspect_parser.add_argument(
        "--skill",
        help="Render and print the prompt with this skill",
    )

    approve_parser = subparsers.add_parser(
        "approve",
        help="Approve the current prompt content",
    )
    approve_parser.add_argument("--prompt-file", required=True)
    approve_parser.add_argument("--approved-by", required=True)

    options = parser.parse_args(arguments)
    prompt_path = Path(options.prompt_file).resolve()
    if options.command == "inspect":
        approval = _read_approval(approval_path_for(prompt_path))
        print(json.dumps(approval, ensure_ascii=False, indent=2))
        template_text = prompt_path.read_text(encoding="utf-8")
        print("\n--- TEMPLATE ---\n")
        print(template_text, end="")
        if options.skill:
            rendered = render_skill_template(
                template_text,
                options.skill,
            )
            print("\n--- RENDERED PROMPT ---\n")
            print(rendered.text, end="")
        return 0

    sidecar = approve_prompt(
        prompt_path,
        approved_by=options.approved_by,
    )
    print(sidecar)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (FileNotFoundError, PromptApprovalError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
