"""Seal and reverify immutable references to internal research results."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from skill_evolution.research_results import RESEARCH_RESULT_SCHEMA
from skill_evolution.storage import JsonObject


RESEARCH_RESULT_REFERENCE_SCHEMA = "analysis.research_result_reference.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResearchArtifactError(ValueError):
    """Raised when a saved result reference is unsafe or no longer intact."""


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchArtifactError(f"{label} must be non-empty text")
    return value.strip()


def _digest(value: object, *, label: str) -> str:
    normalized = _text(value, label=label)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ResearchArtifactError(f"{label} must be a lowercase SHA-256")
    return normalized


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0)


def _read_fd_bytes(file_fd: int) -> bytes:
    """Read one already-open result file exactly once."""

    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_absolute_directory(path: Path, *, label: str) -> int:
    """Open every component of one canonical absolute directory no-follow."""

    if not path.is_absolute() or not path.anchor:
        raise ResearchArtifactError(f"{label} must be absolute")
    flags = _directory_flags()
    current_fd: int | None = None
    try:
        current_fd = os.open(Path(path.anchor), flags)
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise ResearchArtifactError(f"{label} is unsafe")
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as error:
        if current_fd is not None:
            os.close(current_fd)
        raise ResearchArtifactError(f"{label} is missing or unsafe") from error
    except BaseException:
        if current_fd is not None:
            os.close(current_fd)
        raise


def _read_regular_file_beneath(
    root: Path,
    relative: Path,
    *,
    label: str,
) -> tuple[bytes, os.stat_result]:
    """Read one confined regular file through a complete descriptor chain."""

    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.name in {"", ".", ".."}
    ):
        raise ResearchArtifactError(f"{label} path is unsafe")
    directory_fd = _open_absolute_directory(root, label=f"{label} root")
    file_fd: int | None = None
    try:
        flags = _directory_flags()
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except OSError as error:
                raise ResearchArtifactError(
                    f"{label} parent contains a symlink or unsafe component"
                ) from error
            os.close(directory_fd)
            directory_fd = next_fd
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(
                relative.name,
                file_flags,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise ResearchArtifactError(
                f"{label} file is missing or unsafe"
            ) from error
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResearchArtifactError(f"{label} file is not regular")
        content = _read_fd_bytes(file_fd)
        try:
            current = os.stat(
                relative.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ResearchArtifactError(
                f"{label} file changed while being read"
            ) from error
        if (current.st_dev, current.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ResearchArtifactError(
                f"{label} file changed while being read"
            )
        return content, metadata
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _canonical_run_root(path: Path) -> Path:
    """Resolve one real AgentRun directory while rejecting a linked root."""

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise ResearchArtifactError(
            "Research AgentRun directory is unsafe"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino)
        != (resolved_metadata.st_dev, resolved_metadata.st_ino)
    ):
        raise ResearchArtifactError("Research AgentRun directory is unsafe")
    return resolved


def validate_research_result_reference(
    value: Mapping[str, Any],
    *,
    expected_role: str | None = None,
    expected_agent_run_id: str | None = None,
    expected_corpus_digest: str | None = None,
    expected_baseline_digest: str | None = None,
) -> JsonObject:
    """Validate a strict result identity without trusting its referenced file."""

    expected_fields = {
        "schema",
        "result_schema",
        "role",
        "agent_run_id",
        "path",
        "sha256",
        "corpus_digest",
        "baseline_digest",
    }
    if set(value) != expected_fields:
        raise ResearchArtifactError("Research result reference fields differ")
    if value.get("schema") != RESEARCH_RESULT_REFERENCE_SCHEMA:
        raise ResearchArtifactError("Unsupported research result reference")
    if value.get("result_schema") != RESEARCH_RESULT_SCHEMA:
        raise ResearchArtifactError("Unsupported referenced research result")
    role = _text(value.get("role"), label="result_ref.role")
    agent_run_id = _text(
        value.get("agent_run_id"),
        label="result_ref.agent_run_id",
    )
    corpus_digest = _digest(
        value.get("corpus_digest"),
        label="result_ref.corpus_digest",
    )
    baseline_digest = _digest(
        value.get("baseline_digest"),
        label="result_ref.baseline_digest",
    )
    if expected_role is not None and role != expected_role:
        raise ResearchArtifactError("Research result reference role changed")
    if (
        expected_agent_run_id is not None
        and agent_run_id != expected_agent_run_id
    ):
        raise ResearchArtifactError("Research result AgentRun identity changed")
    if (
        expected_corpus_digest is not None
        and corpus_digest != expected_corpus_digest
    ):
        raise ResearchArtifactError("Research result corpus binding changed")
    if (
        expected_baseline_digest is not None
        and baseline_digest != expected_baseline_digest
    ):
        raise ResearchArtifactError("Research result baseline binding changed")
    return {
        "schema": RESEARCH_RESULT_REFERENCE_SCHEMA,
        "result_schema": RESEARCH_RESULT_SCHEMA,
        "role": role,
        "agent_run_id": agent_run_id,
        "path": _text(value.get("path"), label="result_ref.path"),
        "sha256": _digest(value.get("sha256"), label="result_ref.sha256"),
        "corpus_digest": corpus_digest,
        "baseline_digest": baseline_digest,
    }


def seal_research_result_reference(
    *,
    result_file: str | os.PathLike[str],
    run_directory: str | os.PathLike[str],
    result: Mapping[str, Any],
    role: str,
    agent_run_id: str,
    corpus_digest: str,
    baseline_digest: str,
) -> JsonObject:
    """Create a digest-bound reference after checking the runtime artifact."""

    run_root = Path(run_directory)
    declared = Path(result_file)
    root = _canonical_run_root(run_root)
    absolute_declared = (
        declared if declared.is_absolute() else Path.cwd() / declared
    )
    if absolute_declared.is_relative_to(run_root):
        relative = absolute_declared.relative_to(run_root)
    elif absolute_declared.is_relative_to(root):
        relative = absolute_declared.relative_to(root)
    else:
        raise ResearchArtifactError("Research result file is missing or unsafe")
    path = root / relative
    content, _ = _read_regular_file_beneath(
        root,
        relative,
        label="Research result",
    )
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResearchArtifactError(
            "Research result file is not valid UTF-8 JSON"
        ) from error
    if not isinstance(decoded, Mapping) or dict(decoded) != dict(result):
        raise ResearchArtifactError(
            "Research result file differs from the validated runtime result"
        )
    if decoded.get("schema") != RESEARCH_RESULT_SCHEMA:
        raise ResearchArtifactError("Research result schema changed")
    if decoded.get("role") != role:
        raise ResearchArtifactError("Research result role changed")
    if decoded.get("corpus_digest") not in {None, corpus_digest}:
        raise ResearchArtifactError("Research result corpus binding changed")
    if decoded.get("baseline_digest") not in {None, baseline_digest}:
        raise ResearchArtifactError("Research result baseline binding changed")
    return validate_research_result_reference(
        {
            "schema": RESEARCH_RESULT_REFERENCE_SCHEMA,
            "result_schema": RESEARCH_RESULT_SCHEMA,
            "role": role,
            "agent_run_id": agent_run_id,
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "corpus_digest": corpus_digest,
            "baseline_digest": baseline_digest,
        },
        expected_role=role,
        expected_agent_run_id=agent_run_id,
        expected_corpus_digest=corpus_digest,
        expected_baseline_digest=baseline_digest,
    )


def verify_research_result_reference(
    value: Mapping[str, Any],
    *,
    expected_role: str,
    expected_agent_run_id: str,
    expected_corpus_digest: str,
    expected_baseline_digest: str,
) -> JsonObject:
    """Re-read a saved result and reject deletion, replacement, or rebinding."""

    reference = validate_research_result_reference(
        value,
        expected_role=expected_role,
        expected_agent_run_id=expected_agent_run_id,
        expected_corpus_digest=expected_corpus_digest,
        expected_baseline_digest=expected_baseline_digest,
    )
    declared = Path(str(reference["path"]))
    if (
        not declared.is_absolute()
        or ".." in declared.parts
        or not declared.anchor
    ):
        raise ResearchArtifactError("Research result path is unsafe")
    root = Path(declared.anchor)
    relative = declared.relative_to(root)
    content, _ = _read_regular_file_beneath(
        root,
        relative,
        label="Research result",
    )
    if hashlib.sha256(content).hexdigest() != reference["sha256"]:
        raise ResearchArtifactError("Research result digest changed")
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResearchArtifactError(
            "Research result is no longer valid JSON"
        ) from error
    if not isinstance(decoded, Mapping):
        raise ResearchArtifactError("Research result must remain an object")
    if decoded.get("schema") != reference["result_schema"]:
        raise ResearchArtifactError("Research result schema changed")
    if decoded.get("role") != reference["role"]:
        raise ResearchArtifactError("Research result role changed")
    if decoded.get("corpus_digest") not in {
        None,
        reference["corpus_digest"],
    }:
        raise ResearchArtifactError("Research result corpus binding changed")
    if decoded.get("baseline_digest") not in {
        None,
        reference["baseline_digest"],
    }:
        raise ResearchArtifactError("Research result baseline binding changed")
    return reference
