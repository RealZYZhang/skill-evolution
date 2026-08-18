"""Build and validate portable single-Agent research capability certificates."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
from typing import Any

from scripts.pi_rpc import resolve_pi_command
from skill_evolution.agents import AgentRole
from skill_evolution.research_sandbox import (
    RESEARCH_SANDBOX_BACKEND,
    ResearchSandboxError,
    validate_research_sandbox_control_plane_identity,
)
from skill_evolution.storage import JsonObject


RESEARCH_IMPLEMENTATION_FINGERPRINT_SCHEMA = (
    "research.implementation_fingerprint.v1"
)
RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA = "research.pi_execution_identity.v2"
RESEARCH_EXECUTION_IDENTITY_SCHEMA = "research.execution_identity.v2"
RESEARCH_CAPABILITY_IDENTITY_SCHEMA = "research.capability_identity.v3"
RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA = "research.capability_certificate.v1"
RESEARCH_CAPABILITY_CERTIFICATE_STATUS = "valid"

RESEARCH_IMPLEMENTATION_FILES = (
    "scripts/__init__.py",
    "scripts/pi_rpc.py",
    "scripts/prompt_approval.py",
    "scripts/task_case.py",
    "scripts/trajectory_spike.py",
    "skill_evolution/__init__.py",
    "skill_evolution/agents.py",
    "skill_evolution/analysis.py",
    "skill_evolution/config.py",
    "skill_evolution/evaluation.py",
    "skill_evolution/evidence.py",
    "skill_evolution/hierarchy.py",
    "skill_evolution/research_agent_runtime.py",
    "skill_evolution/research_corpus.py",
    "skill_evolution/research_sandbox.py",
    "skill_evolution/research_results.py",
    "skill_evolution/research_artifacts.py",
    "skill_evolution/research_board.py",
    "skill_evolution/research_workflow.py",
    "skill_evolution/research_harness_acceptance.py",
    "skill_evolution/research_capability.py",
    "skill_evolution/storage.py",
    "skill_evolution/trajectory_precheck.py",
    "extensions/research-tools.ts",
    "extensions/research-output.ts",
    "extensions/research-harness-driver.ts",
)
RESEARCH_TOOLS_PATH = "extensions/research-tools.ts"
RESEARCH_OUTPUT_PATH = "extensions/research-output.ts"
SMOKE_REVIEW_CHECKS = (
    "evidence",
    "protocol",
    "safety",
    "hidden_benchmark",
)
SANDBOX_LIMIT_FIELDS = (
    "cpus",
    "memory",
    "pids",
    "open_files",
    "work_bytes",
    "temporary_bytes",
    "command_timeout_seconds",
    "max_output_bytes",
    "max_tool_calls",
    "max_concurrent_tool_calls",
    "max_total_output_bytes",
    "max_total_command_milliseconds",
)
RESEARCH_PI_TOOL_ALLOWLIST = (
    "research_list",
    "research_read",
    "research_search",
    "research_query",
    "research_trajectory_window",
    "research_work_read",
    "research_work_write",
    "research_work_edit",
    "research_exec",
    "submit_multi_trajectory_research",
    "submit_error_identification",
    "submit_error_report",
)
_RESEARCH_PI_CREDENTIAL_SOURCE_POLICY = {
    "real_provider": "selected_literal_api_key_checked_each_spawn",
    "harness_faux": "none",
}
_RESEARCH_PI_MODEL_CATALOG_POLICY = {
    "real_provider": "package_bound_builtin_only",
    "harness_faux": "attested_driver_extension_only",
}
_RESEARCH_PI_EXPLICIT_EXTENSION_POLICY = {
    "real_provider": {
        "names": ["research_tools", "research_output"],
        "binding": "execution_toolchain_hashes",
    },
    "harness_faux": {
        "names": [
            "research_tools",
            "research_output",
            "research_harness_driver",
        ],
        "binding": (
            "execution_toolchain_and_acceptance_implementation_hashes"
        ),
    },
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_PI_VERSION_MAX_BYTES = 1024
_PI_IDENTITY_TIMEOUT_SECONDS = 10.0
_PI_PACKAGE_MAX_ROOTS = 1024
_PI_PACKAGE_MAX_FILES = 250_000
_PI_PACKAGE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_PI_AUTH_MAX_BYTES = 1024 * 1024
_SECRET_ARGUMENT = re.compile(
    r"(?i)(?:api[-_]?key|authorization|bearer|password|secret|token)"
)
_FORBIDDEN_PI_EXTRA_OPTIONS = frozenset(
    {
        "-a",
        "-c",
        "-e",
        "-f",
        "-n",
        "-na",
        "-nbt",
        "-nc",
        "-ne",
        "-np",
        "-ns",
        "-nt",
        "-r",
        "-t",
        "-xt",
        "--append-system-prompt",
        "--approve",
        "--builtin-tools",
        "--continue",
        "--context-files",
        "--exclude-tools",
        "--extension",
        "--extensions",
        "--fork",
        "--mode",
        "--model",
        "--name",
        "--no-approve",
        "--no-builtin-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-prompt-templates",
        "--no-session",
        "--no-skills",
        "--no-tools",
        "--no-themes",
        "--offline",
        "--prompt-template",
        "--prompt-templates",
        "--provider",
        "--resume",
        "--session",
        "--session-dir",
        "--session-id",
        "--skill",
        "--skills",
        "--system-prompt",
        "--thinking",
        "--themes",
        "--tools",
    }
)
_ALLOWED_PI_EXTRA_ARGUMENTS = frozenset({"--verbose"})


def _research_pi_rpc_policy() -> JsonObject:
    """Return the shared, conditional real/faux Pi runtime policy."""

    return {
        "mode": "rpc",
        "no_session": True,
        "project_approval": "denied",
        "built_in_tools": False,
        "tool_allowlist": list(RESEARCH_PI_TOOL_ALLOWLIST),
        "discovered_extensions": False,
        "built_in_extension_factories": "package_bound",
        "explicit_extensions": json.loads(
            json.dumps(_RESEARCH_PI_EXPLICIT_EXTENSION_POLICY)
        ),
        "prompt_templates": False,
        "skills": False,
        "context_files": False,
        "themes": False,
        "offline": True,
        "system_prompt": "bound_pi_builtin",
        "append_system_prompt": "none_isolated",
        "environment": "replace_allowlist",
        "agent_directory": "isolated_ephemeral",
        "credential_source": dict(
            _RESEARCH_PI_CREDENTIAL_SOURCE_POLICY
        ),
        "model_catalog": dict(_RESEARCH_PI_MODEL_CATALOG_POLICY),
    }


class ResearchCapabilityError(ValueError):
    """Raised when a capability identity or certificate is not trustworthy."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResearchCapabilityError(
            "Capability data must be canonical JSON"
        ) from error


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_fields(
    value: object,
    expected: set[str],
    *,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError(f"{label} must be an object")
    if set(value) != expected:
        raise ResearchCapabilityError(f"{label} fields differ from schema")


def _text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ResearchCapabilityError(f"{label} must be non-empty text")
    return value


def _sha256(value: object, *, label: str) -> str:
    normalized = _text(value, label=label)
    if not _SHA256.fullmatch(normalized):
        raise ResearchCapabilityError(f"{label} must be a lowercase SHA-256")
    return normalized


def _timestamp(value: object, *, label: str) -> str:
    normalized = _text(value, label=label)
    if not _TIMESTAMP.fullmatch(normalized):
        raise ResearchCapabilityError(
            f"{label} must be an ISO-8601 timestamp with a timezone"
        )
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResearchCapabilityError(
            f"{label} must be a valid ISO-8601 timestamp"
        ) from error
    if parsed.utcoffset() != timedelta(0):
        raise ResearchCapabilityError(f"{label} must use UTC")
    return normalized


def file_sha256(path: str | os.PathLike[str]) -> str:
    """Hash one regular file while refusing links and special files."""

    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ResearchCapabilityError(
            f"Capability implementation file cannot be read: {candidate}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ResearchCapabilityError(
            f"Capability implementation path is not a regular file: {candidate}"
        )
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ResearchCapabilityError(
            f"Capability implementation file cannot be read: {candidate}"
        ) from error
    return digest.hexdigest()


def fingerprint_research_implementation(
    repository_root: str | os.PathLike[str],
) -> JsonObject:
    """Fingerprint the fixed code and extension boundary used by research."""

    root = Path(repository_root)
    if root.is_symlink():
        raise ResearchCapabilityError("Repository root may not be a symlink")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ResearchCapabilityError("Repository root does not exist") from error
    if not resolved_root.is_dir():
        raise ResearchCapabilityError("Repository root must be a directory")
    verify_research_implementation_dependency_closure(resolved_root)

    files: list[JsonObject] = []
    for relative in RESEARCH_IMPLEMENTATION_FILES:
        candidate = resolved_root / relative
        current = resolved_root
        for component in Path(relative).parts:
            current /= component
            if current.is_symlink():
                raise ResearchCapabilityError(
                    "Research implementation path may not contain a symlink: "
                    f"{relative}"
                )
        try:
            resolved_candidate = candidate.resolve(strict=True)
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise ResearchCapabilityError(
                f"Research implementation file is missing or escapes root: "
                f"{relative}"
            ) from error
        files.append(
            {
                "path": relative,
                "sha256": file_sha256(resolved_candidate),
            }
        )
    body = {
        "schema": RESEARCH_IMPLEMENTATION_FINGERPRINT_SCHEMA,
        "files": files,
    }
    return {**body, "content_sha256": _canonical_digest(body)}


def research_implementation_dependency_closure(
    repository_root: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Return first-party imports reached by the declared Python boundary."""

    root = Path(repository_root).resolve()
    discovered: set[str] = {
        path
        for path in RESEARCH_IMPLEMENTATION_FILES
        if path.endswith("/__init__.py")
    }
    for relative in RESEARCH_IMPLEMENTATION_FILES:
        if not relative.endswith(".py"):
            continue
        path = root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as error:
            raise ResearchCapabilityError(
                f"Research implementation cannot be parsed: {relative}"
            ) from error
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    base = node.module or ""
                else:
                    relative_path = Path(relative)
                    package_parts = list(relative_path.parent.parts)
                    parent_count = node.level - 1
                    if parent_count > len(package_parts):
                        continue
                    base_parts = (
                        package_parts[: len(package_parts) - parent_count]
                        if parent_count
                        else package_parts
                    )
                    if node.module:
                        base_parts.extend(node.module.split("."))
                    base = ".".join(base_parts)
                if base:
                    modules.append(base)
                    modules.extend(
                        f"{base}.{item.name}"
                        for item in node.names
                        if item.name != "*"
                    )
            for module in modules:
                top_level = module.split(".", 1)[0]
                if top_level not in {"scripts", "skill_evolution"}:
                    continue
                module_path = Path(*module.split("."))
                candidates = (
                    module_path.with_suffix(".py"),
                    module_path / "__init__.py",
                )
                for candidate in candidates:
                    if (root / candidate).is_file():
                        discovered.add(candidate.as_posix())
                        break
    return tuple(sorted(discovered))


def verify_research_implementation_dependency_closure(
    repository_root: str | os.PathLike[str],
) -> None:
    """Reject a fixed fingerprint list that omits a first-party dependency."""

    closure = set(research_implementation_dependency_closure(repository_root))
    missing = closure - set(RESEARCH_IMPLEMENTATION_FILES)
    if missing:
        raise ResearchCapabilityError(
            "Research implementation fingerprint omits first-party imports: "
            f"{sorted(missing)}"
        )


def _validate_implementation_fingerprint(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError(
            "Capability implementation fingerprint must be an object"
        )
    _require_fields(
        value,
        {"schema", "files", "content_sha256"},
        label="Capability implementation fingerprint",
    )
    if value.get("schema") != RESEARCH_IMPLEMENTATION_FINGERPRINT_SCHEMA:
        raise ResearchCapabilityError(
            "Unsupported research implementation fingerprint schema"
        )
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise ResearchCapabilityError(
            "Capability implementation files must be a list"
        )
    if len(raw_files) != len(RESEARCH_IMPLEMENTATION_FILES):
        raise ResearchCapabilityError(
            "Capability implementation fingerprint is incomplete"
        )
    files: list[JsonObject] = []
    for index, expected_path in enumerate(RESEARCH_IMPLEMENTATION_FILES):
        item = raw_files[index]
        if not isinstance(item, Mapping):
            raise ResearchCapabilityError(
                "Capability implementation file binding must be an object"
            )
        _require_fields(
            item,
            {"path", "sha256"},
            label=f"Capability implementation file {index}",
        )
        if item.get("path") != expected_path:
            raise ResearchCapabilityError(
                "Capability implementation files differ from the fixed boundary"
            )
        files.append(
            {
                "path": expected_path,
                "sha256": _sha256(
                    item.get("sha256"),
                    label=f"implementation.files[{index}].sha256",
                ),
            }
        )
    body = {
        "schema": RESEARCH_IMPLEMENTATION_FINGERPRINT_SCHEMA,
        "files": files,
    }
    content_sha256 = _sha256(
        value.get("content_sha256"),
        label="implementation.content_sha256",
    )
    if content_sha256 != _canonical_digest(body):
        raise ResearchCapabilityError(
            "Capability implementation fingerprint digest does not match"
        )
    return {**body, "content_sha256": content_sha256}


def _implementation_file_digest(
    implementation: Mapping[str, Any],
    relative_path: str,
) -> str:
    for item in implementation["files"]:
        if item["path"] == relative_path:
            return str(item["sha256"])
    raise ResearchCapabilityError(
        f"Capability implementation lacks required file: {relative_path}"
    )


def _command_tokens(value: object, *, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ResearchCapabilityError(f"{label} must be a non-empty list")
    tokens: list[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
        ):
            raise ResearchCapabilityError(
                f"{label}[{index}] must be non-empty safe text"
            )
        tokens.append(item)
    return tokens


def _validate_resolved_pi_command_policy(
    command: Sequence[str],
) -> None:
    """Require one direct package-bound Pi entrypoint, never a launcher."""

    if len(command) != 1:
        raise ResearchCapabilityError(
            "Pi base command must be exactly one direct executable entrypoint"
        )


def _argument_tokens(value: object, *, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ResearchCapabilityError(f"{label} must be a list")
    tokens: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ResearchCapabilityError(
                f"{label}[{index}] must be non-empty safe text"
            )
        _reject_unsafe_pi_option(item, label=f"{label}[{index}]")
        if item not in _ALLOWED_PI_EXTRA_ARGUMENTS:
            raise ResearchCapabilityError(
                f"{label}[{index}] is not an approved research Pi argument"
            )
        if item in tokens:
            raise ResearchCapabilityError(
                f"{label} must not repeat research Pi arguments"
            )
        tokens.append(item)
    return tokens


def _reject_unsafe_pi_option(value: str, *, label: str) -> None:
    if not value.startswith("-"):
        return
    option = value.split("=", 1)[0]
    if _SECRET_ARGUMENT.search(option):
        raise ResearchCapabilityError(
            f"{label} may not carry credential arguments"
        )
    if option in _FORBIDDEN_PI_EXTRA_OPTIONS:
        raise ResearchCapabilityError(
            f"{label} may not override the fixed research Pi policy"
        )


def _pi_executable_path(
    command: Sequence[str],
    *,
    working_directory: Path,
) -> Path:
    launcher = command[0]
    if os.sep in launcher or (os.altsep and os.altsep in launcher):
        candidate = Path(launcher)
        if not candidate.is_absolute():
            candidate = working_directory / candidate
    else:
        discovered = shutil.which(launcher)
        if discovered is None:
            raise ResearchCapabilityError(
                f"Pi executable cannot be resolved: {launcher}"
            )
        candidate = Path(discovered)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise ResearchCapabilityError(
            f"Pi executable cannot be inspected: {candidate}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ResearchCapabilityError(
            f"Pi executable is not a regular file: {resolved}"
        )
    return resolved


def _verify_pi_executable(value: Mapping[str, Any]) -> None:
    path = Path(str(value["path"]))
    try:
        metadata = path.stat()
    except OSError as error:
        raise ResearchCapabilityError(
            "Certified Pi executable cannot be inspected"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ResearchCapabilityError(
            "Certified Pi executable is not a regular file"
        )
    if metadata.st_size != value["bytes"] or file_sha256(path) != value["sha256"]:
        raise ResearchCapabilityError(
            "Pi executable changed after capability certification"
        )


def _pi_command_file_identities(
    command: Sequence[str],
    *,
    working_directory: Path,
    executable: Path,
) -> list[JsonObject]:
    identities: list[JsonObject] = []
    seen: set[Path] = set()
    for index, token in enumerate(command):
        if index == 0:
            candidate = executable
        else:
            raw = Path(token)
            candidate = raw if raw.is_absolute() else working_directory / raw
            if not candidate.exists():
                continue
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as error:
                raise ResearchCapabilityError(
                    f"Pi command file cannot be inspected: {token}"
                ) from error
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        identities.append(
            {
                "argument_index": index,
                "path": str(candidate),
                "bytes": candidate.stat().st_size,
                "sha256": file_sha256(candidate),
            }
        )
    return identities


def _file_identity(path: Path) -> JsonObject:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ResearchCapabilityError(
            f"Pi runtime dependency is not a regular file: {resolved}"
        )
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def _pi_interpreter_identities(
    executable: Path,
    *,
    certified_env_command: Path | None = None,
) -> list[JsonObject]:
    try:
        with executable.open("rb") as stream:
            first_line = stream.readline(4096)
    except OSError as error:
        raise ResearchCapabilityError(
            "Pi executable shebang cannot be inspected"
        ) from error
    if not first_line.startswith(b"#!"):
        return []
    try:
        shebang = first_line[2:].decode("utf-8").strip()
        tokens = shlex.split(shebang)
    except (UnicodeDecodeError, ValueError) as error:
        raise ResearchCapabilityError("Pi executable shebang is invalid") from error
    if not tokens:
        raise ResearchCapabilityError("Pi executable shebang is empty")
    paths: list[Path] = []
    launcher = (
        tokens[0]
        if Path(tokens[0]).is_absolute()
        else shutil.which(tokens[0])
    )
    if launcher is None:
        raise ResearchCapabilityError("Pi shebang interpreter is unavailable")
    paths.append(Path(launcher))
    if Path(str(launcher)).name == "env":
        command = next((item for item in tokens[1:] if not item.startswith("-")), None)
        if command is None or "=" in command:
            raise ResearchCapabilityError("Pi env shebang lacks an interpreter")
        if certified_env_command is None:
            resolved_command = shutil.which(command)
        else:
            try:
                certified = certified_env_command.resolve(strict=True)
            except OSError as error:
                raise ResearchCapabilityError(
                    "Certified Pi env interpreter is unavailable"
                ) from error
            requested = Path(command)
            if requested.is_absolute():
                try:
                    matches = requested.resolve(strict=True) == certified
                except OSError:
                    matches = False
            else:
                matches = requested.name == certified.name
            if not matches:
                raise ResearchCapabilityError(
                    "Certified Pi env interpreter differs from the shebang"
                )
            resolved_command = str(certified)
        if not resolved_command:
            raise ResearchCapabilityError(
                f"Pi shebang command is unavailable: {command}"
            )
        paths.append(Path(resolved_command))
    identities: list[JsonObject] = []
    seen: set[str] = set()
    for path in paths:
        identity = _file_identity(path)
        if identity["path"] not in seen:
            identities.append(identity)
            seen.add(str(identity["path"]))
    return identities


def _pi_process_environment(
    interpreters: Sequence[Mapping[str, Any]],
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Build a host-PATH-independent environment for one certified entrypoint."""

    result: dict[str, str] = {}
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not name
            or "\x00" in name
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ResearchCapabilityError(
                "Pi process environment must contain safe text"
            )
        if name != "PATH":
            result[name] = value
    if interpreters and Path(str(interpreters[0]["path"])).name == "env":
        if len(interpreters) != 2:
            raise ResearchCapabilityError(
                "Pi env shebang must bind exactly one target interpreter"
            )
        target = Path(str(interpreters[1]["path"]))
        result["PATH"] = str(target.parent)
    return result


def resolve_research_pi_agent_directory(
    value: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the credential/catalog source without trusting Pi env overrides."""

    candidate = Path(value) if value is not None else Path.home() / ".pi/agent"
    try:
        return candidate.expanduser().resolve(strict=False)
    except OSError as error:
        raise ResearchCapabilityError(
            "Pi agent configuration directory cannot be resolved"
        ) from error


def _read_auth_descriptor(file_descriptor: int) -> Mapping[str, Any]:
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResearchCapabilityError(
                "Pi authentication source must be a regular file"
            )
        if metadata.st_size <= 0 or metadata.st_size > _PI_AUTH_MAX_BYTES:
            raise ResearchCapabilityError(
                "Pi authentication source has an invalid size"
            )
        raw = os.pread(file_descriptor, metadata.st_size + 1, 0)
    except OSError as error:
        raise ResearchCapabilityError(
            "Pi authentication source cannot be read"
        ) from error
    if len(raw) != metadata.st_size:
        raise ResearchCapabilityError(
            "Pi authentication source changed while being read"
        )
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResearchCapabilityError(
            "Pi authentication source is not valid JSON"
        ) from error
    if not isinstance(decoded, Mapping):
        raise ResearchCapabilityError(
            "Pi authentication source must be an object"
        )
    return decoded


def _credential_identity_from_auth(
    auth: Mapping[str, Any],
    *,
    provider: str,
) -> JsonObject:
    credential = auth.get(provider)
    if not isinstance(credential, Mapping):
        raise ResearchCapabilityError(
            "Pi authentication lacks a credential for the selected provider"
        )
    if set(credential) != {"type", "key"}:
        raise ResearchCapabilityError(
            "Selected Pi credential contains unsupported metadata or env values"
        )
    if credential.get("type") != "api_key":
        raise ResearchCapabilityError(
            "Research currently supports only literal API-key credentials"
        )
    key = credential.get("key")
    if (
        not isinstance(key, str)
        or not key
        or key.startswith("!")
        or "$" in key
        or "\x00" in key
    ):
        raise ResearchCapabilityError(
            "Selected Pi credential must be a literal API key"
        )
    return {"provider": provider, "kind": "literal_api_key"}


def validate_selected_pi_credential(
    agent_directory: str | os.PathLike[str],
    *,
    provider: str,
) -> JsonObject:
    """Validate only the chosen provider's non-secret credential metadata."""

    selected_provider = _text(provider, label="credential.provider")
    auth_path = resolve_research_pi_agent_directory(agent_directory) / "auth.json"
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(auth_path, flags)
    except OSError as error:
        raise ResearchCapabilityError(
            "Pi authentication source is unavailable"
        ) from error
    try:
        return _credential_identity_from_auth(
            _read_auth_descriptor(descriptor),
            provider=selected_provider,
        )
    finally:
        os.close(descriptor)


def read_selected_pi_api_key(
    file_descriptor: int,
    *,
    provider: str,
) -> str:
    """Return only the already-certified provider's literal key in memory."""

    selected_provider = _text(provider, label="credential.provider")
    auth = _read_auth_descriptor(file_descriptor)
    observed = _credential_identity_from_auth(
        auth,
        provider=selected_provider,
    )
    credential = auth[observed["provider"]]
    assert isinstance(credential, Mapping)
    key = credential["key"]
    assert isinstance(key, str)
    return key


def _nearest_node_package_root(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    for parent in (start, *start.parents):
        package = parent / "package.json"
        if (
            package.is_file()
            and not package.is_symlink()
            and not parent.is_symlink()
        ):
            return parent
    return None


def _package_tree_identity(
    root: Path,
    *,
    bound_package_roots: Sequence[Path] = (),
) -> JsonObject:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_bound_roots = {
            candidate.resolve(strict=True)
            for candidate in bound_package_roots
        }
    except (OSError, RuntimeError) as error:
        raise ResearchCapabilityError("Pi package root is unsafe") from error
    if (
        root.is_symlink()
        or root != resolved_root
        or not resolved_root.is_dir()
    ):
        raise ResearchCapabilityError("Pi package root is unsafe")
    resolved_bound_roots.add(resolved_root)
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    try:
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory_names.sort()
            file_names.sort()
            current_path = Path(current)
            linked_directories = [
                name
                for name in directory_names
                if (current_path / name).is_symlink()
            ]
            directory_names[:] = [
                name for name in directory_names if name not in linked_directories
            ]
            for name in [*linked_directories, *file_names]:
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    try:
                        target = path.resolve(strict=True)
                    except (OSError, RuntimeError) as error:
                        raise ResearchCapabilityError(
                            "Pi package contains a broken or cyclic symlink: "
                            f"{relative}"
                        ) from error
                    if not any(
                        _is_within(target, bound_root)
                        for bound_root in resolved_bound_roots
                    ):
                        raise ResearchCapabilityError(
                            "Pi package symlink has an unbound external target: "
                            f"{relative}"
                        )
                    entry: JsonObject = {
                        "path": relative,
                        "type": "symlink",
                        "target": os.readlink(path),
                    }
                elif stat.S_ISREG(metadata.st_mode):
                    entry = {
                        "path": relative,
                        "type": "file",
                        "bytes": metadata.st_size,
                        "sha256": file_sha256(path),
                    }
                    total_bytes += metadata.st_size
                else:
                    raise ResearchCapabilityError(
                        f"Pi package contains a special file: {relative}"
                    )
                digest.update(_canonical_bytes(entry))
                digest.update(b"\n")
                file_count += 1
                if file_count > _PI_PACKAGE_MAX_FILES:
                    raise ResearchCapabilityError(
                        "Pi package tree exceeds the file limit"
                    )
                if total_bytes > _PI_PACKAGE_MAX_BYTES:
                    raise ResearchCapabilityError(
                        "Pi package tree exceeds the byte limit"
                    )
    except OSError as error:
        raise ResearchCapabilityError("Pi package tree cannot be read") from error
    if file_count <= 0:
        raise ResearchCapabilityError("Pi package tree is empty")
    package_json = resolved_root / "package.json"
    return {
        "root": str(resolved_root),
        "package_json_sha256": file_sha256(package_json),
        "files": file_count,
        "bytes": total_bytes,
        "tree_sha256": digest.hexdigest(),
    }


def _node_module_symlink_package_roots(root: Path) -> tuple[Path, ...]:
    """Resolve package roots reached through node_modules symlinks."""

    dependencies: set[Path] = set()
    try:
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory_names.sort()
            file_names.sort()
            current_path = Path(current)
            linked_directories = [
                name
                for name in directory_names
                if (current_path / name).is_symlink()
            ]
            directory_names[:] = [
                name for name in directory_names if name not in linked_directories
            ]
            linked_files = [
                name
                for name in file_names
                if (current_path / name).is_symlink()
            ]
            for name in [*linked_directories, *linked_files]:
                link = current_path / name
                relative = link.relative_to(root)
                if "node_modules" not in relative.parts:
                    continue
                try:
                    target = link.resolve(strict=True)
                except (OSError, RuntimeError) as error:
                    raise ResearchCapabilityError(
                        "Pi node_modules contains a broken dependency link: "
                        f"{relative.as_posix()}"
                    ) from error
                package_root = _nearest_node_package_root(target)
                if package_root is None:
                    raise ResearchCapabilityError(
                        "Pi node_modules dependency link escapes any npm package: "
                        f"{relative.as_posix()}"
                    )
                dependencies.add(package_root.resolve(strict=True))
    except OSError as error:
        raise ResearchCapabilityError(
            "Pi package dependency links cannot be inspected"
        ) from error
    return tuple(sorted(dependencies))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _pi_package_identities(
    command_files: Sequence[Mapping[str, Any]],
) -> list[JsonObject]:
    pending: set[Path] = set()
    for item in command_files:
        root = _nearest_node_package_root(Path(str(item["path"])))
        if root is not None:
            pending.add(root.resolve(strict=True))
    roots: set[Path] = set()
    while pending:
        root = min(pending)
        pending.remove(root)
        if any(_is_within(root, covered) for covered in roots):
            continue
        contained = [
            covered for covered in roots if _is_within(covered, root)
        ]
        for covered in contained:
            roots.remove(covered)
        if len(roots) >= _PI_PACKAGE_MAX_ROOTS:
            raise ResearchCapabilityError(
                "Pi reachable package tree exceeds the package limit"
            )
        roots.add(root)
        for dependency in _node_module_symlink_package_roots(root):
            if not any(
                _is_within(dependency, covered)
                for covered in roots
            ):
                pending.add(dependency)
    ordered_roots = tuple(sorted(roots))
    return [
        _package_tree_identity(
            root,
            bound_package_roots=ordered_roots,
        )
        for root in ordered_roots
    ]


def _validate_pi_command_files(
    value: object,
    *,
    verify_files: bool,
) -> list[JsonObject]:
    if not isinstance(value, list) or not value:
        raise ResearchCapabilityError(
            "Pi command files must include the executable"
        )
    files: list[JsonObject] = []
    prior_index = -1
    paths: set[str] = set()
    for position, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ResearchCapabilityError(
                f"Pi command file {position} must be an object"
            )
        _require_fields(
            item,
            {"argument_index", "path", "bytes", "sha256"},
            label=f"Pi command file {position}",
        )
        argument_index = item.get("argument_index")
        file_bytes = item.get("bytes")
        if (
            isinstance(argument_index, bool)
            or not isinstance(argument_index, int)
            or argument_index <= prior_index
        ):
            raise ResearchCapabilityError(
                "Pi command file argument indices must increase"
            )
        if (
            isinstance(file_bytes, bool)
            or not isinstance(file_bytes, int)
            or file_bytes <= 0
        ):
            raise ResearchCapabilityError("Pi command file size is invalid")
        path = _text(item.get("path"), label=f"pi.command_files[{position}].path")
        if not Path(path).is_absolute() or path in paths:
            raise ResearchCapabilityError(
                "Pi command file paths must be unique and absolute"
            )
        normalized: JsonObject = {
            "argument_index": argument_index,
            "path": path,
            "bytes": file_bytes,
            "sha256": _sha256(
                item.get("sha256"),
                label=f"pi.command_files[{position}].sha256",
            ),
        }
        if verify_files:
            _verify_pi_executable(normalized)
        files.append(normalized)
        paths.add(path)
        prior_index = argument_index
    if files[0]["argument_index"] != 0:
        raise ResearchCapabilityError(
            "Pi command files must start with the executable"
        )
    return files


def _validate_runtime_file_identities(
    value: object,
    *,
    label: str,
    verify_files: bool,
) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ResearchCapabilityError(f"{label} must be a list")
    identities: list[JsonObject] = []
    paths: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ResearchCapabilityError(f"{label}[{index}] must be an object")
        _require_fields(
            item,
            {"path", "bytes", "sha256"},
            label=f"{label}[{index}]",
        )
        path = _text(item.get("path"), label=f"{label}[{index}].path")
        file_bytes = item.get("bytes")
        if (
            not Path(path).is_absolute()
            or path in paths
            or isinstance(file_bytes, bool)
            or not isinstance(file_bytes, int)
            or file_bytes <= 0
        ):
            raise ResearchCapabilityError(f"{label}[{index}] is invalid")
        normalized: JsonObject = {
            "path": path,
            "bytes": file_bytes,
            "sha256": _sha256(
                item.get("sha256"),
                label=f"{label}[{index}].sha256",
            ),
        }
        if verify_files:
            _verify_pi_executable(normalized)
        identities.append(normalized)
        paths.add(path)
    return identities


def _validate_pi_packages(
    value: object,
    *,
    verify_packages: bool,
) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ResearchCapabilityError("Pi packages must be a list")
    packages: list[JsonObject] = []
    roots: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ResearchCapabilityError(
                f"Pi package {index} must be an object"
            )
        _require_fields(
            item,
            {
                "root",
                "package_json_sha256",
                "files",
                "bytes",
                "tree_sha256",
            },
            label=f"Pi package {index}",
        )
        root = _text(item.get("root"), label=f"pi.packages[{index}].root")
        files = item.get("files")
        package_bytes = item.get("bytes")
        if (
            not Path(root).is_absolute()
            or root in roots
            or isinstance(files, bool)
            or not isinstance(files, int)
            or files <= 0
            or isinstance(package_bytes, bool)
            or not isinstance(package_bytes, int)
            or package_bytes <= 0
        ):
            raise ResearchCapabilityError(f"Pi package {index} is invalid")
        normalized: JsonObject = {
            "root": root,
            "package_json_sha256": _sha256(
                item.get("package_json_sha256"),
                label=f"pi.packages[{index}].package_json_sha256",
            ),
            "files": files,
            "bytes": package_bytes,
            "tree_sha256": _sha256(
                item.get("tree_sha256"),
                label=f"pi.packages[{index}].tree_sha256",
            ),
        }
        packages.append(normalized)
        roots.add(root)
    if [item["root"] for item in packages] != sorted(roots):
        raise ResearchCapabilityError("Pi package identities are not ordered")
    if verify_packages:
        bound_roots = tuple(Path(root) for root in sorted(roots))
        for normalized in packages:
            if _package_tree_identity(
                Path(str(normalized["root"])),
                bound_package_roots=bound_roots,
            ) != normalized:
                raise ResearchCapabilityError(
                    "Pi package tree changed after capability certification"
                )
    return packages


def attest_pi_execution_identity(
    pi_command: Sequence[str] | str | None = None,
    *,
    extra_pi_args: Sequence[str] = (),
    working_directory: str | os.PathLike[str] | None = None,
    version_timeout_seconds: float = _PI_IDENTITY_TIMEOUT_SECONDS,
) -> JsonObject:
    """Resolve and attest the exact Pi launcher used by research RPC runs."""

    if (
        isinstance(version_timeout_seconds, bool)
        or not isinstance(version_timeout_seconds, (int, float))
        or not math.isfinite(version_timeout_seconds)
        or version_timeout_seconds <= 0
    ):
        raise ResearchCapabilityError(
            "Pi version timeout must be a positive finite number"
        )
    try:
        command = _command_tokens(
            resolve_pi_command(pi_command),
            label="pi.resolved_command",
        )
    except (OSError, ValueError) as error:
        raise ResearchCapabilityError(str(error)) from error
    arguments = _argument_tokens(extra_pi_args, label="pi.extra_args")
    root = Path(working_directory or Path.cwd()).resolve()
    executable = _pi_executable_path(command, working_directory=root)
    command[0] = str(executable)
    for index, token in enumerate(command[1:], start=1):
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.exists() and candidate.is_file():
            command[index] = str(candidate.resolve(strict=True))
    _validate_resolved_pi_command_policy(command)
    command_files = _pi_command_file_identities(
        command,
        working_directory=root,
        executable=executable,
    )
    interpreters = _pi_interpreter_identities(executable)
    packages = _pi_package_identities(command_files)
    probe_environment = _pi_process_environment(
        interpreters,
        {
            name: value
            for name in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if (value := os.environ.get(name)) is not None
        },
    )
    try:
        result = subprocess.run(
            [*command, "--version"],
            cwd=root,
            env=probe_environment,
            check=False,
            capture_output=True,
            timeout=float(version_timeout_seconds),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ResearchCapabilityError(
            f"Pi version probe failed: {type(error).__name__}"
        ) from error
    output = result.stdout or result.stderr
    if result.returncode != 0:
        raise ResearchCapabilityError(
            f"Pi version probe exited with code {result.returncode}"
        )
    if not output or len(output) > _PI_VERSION_MAX_BYTES:
        raise ResearchCapabilityError(
            "Pi version probe returned empty or oversized output"
        )
    try:
        version = output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ResearchCapabilityError(
            "Pi version output must be UTF-8"
        ) from error
    identity: JsonObject = {
        "schema": RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA,
        "resolved_command": command,
        "executable": {
            "path": str(executable),
            "bytes": executable.stat().st_size,
            "sha256": file_sha256(executable),
        },
        "command_files": command_files,
        "interpreters": interpreters,
        "packages": packages,
        "version": _text(version, label="pi.version"),
        "extra_args": arguments,
        "rpc_policy": _research_pi_rpc_policy(),
    }
    return _validate_pi_execution_identity(identity, verify_executable=False)


def _validate_pi_execution_identity(
    value: object,
    *,
    verify_executable: bool = False,
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError(
            "Pi execution identity must be an object"
        )
    _require_fields(
        value,
        {
            "schema",
            "resolved_command",
            "executable",
            "command_files",
            "interpreters",
            "packages",
            "version",
            "extra_args",
            "rpc_policy",
        },
        label="Pi execution identity",
    )
    if value.get("schema") != RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA:
        raise ResearchCapabilityError("Unsupported Pi execution identity schema")
    command = _command_tokens(
        value.get("resolved_command"),
        label="pi.resolved_command",
    )
    _validate_resolved_pi_command_policy(command)
    executable = value.get("executable")
    if not isinstance(executable, Mapping):
        raise ResearchCapabilityError("Pi executable identity must be an object")
    _require_fields(
        executable,
        {"path", "bytes", "sha256"},
        label="Pi executable identity",
    )
    executable_path = _text(executable.get("path"), label="pi.executable.path")
    if not Path(executable_path).is_absolute():
        raise ResearchCapabilityError("Pi executable path must be absolute")
    executable_bytes = executable.get("bytes")
    if (
        isinstance(executable_bytes, bool)
        or not isinstance(executable_bytes, int)
        or executable_bytes <= 0
    ):
        raise ResearchCapabilityError("Pi executable size is invalid")
    normalized_executable = {
        "path": executable_path,
        "bytes": executable_bytes,
        "sha256": _sha256(
            executable.get("sha256"),
            label="pi.executable.sha256",
        ),
    }
    if command[0] != executable_path:
        raise ResearchCapabilityError(
            "Pi resolved command differs from its executable identity"
        )
    policy = value.get("rpc_policy")
    expected_policy = _research_pi_rpc_policy()
    if policy != expected_policy:
        raise ResearchCapabilityError("Pi RPC policy differs from research")
    normalized: JsonObject = {
        "schema": RESEARCH_PI_EXECUTION_IDENTITY_SCHEMA,
        "resolved_command": command,
        "executable": normalized_executable,
        "command_files": _validate_pi_command_files(
            value.get("command_files"),
            verify_files=False,
        ),
        "interpreters": _validate_runtime_file_identities(
            value.get("interpreters"),
            label="pi.interpreters",
            verify_files=False,
        ),
        "packages": _validate_pi_packages(
            value.get("packages"),
            verify_packages=False,
        ),
        "version": _text(value.get("version"), label="pi.version"),
        "extra_args": _argument_tokens(
            value.get("extra_args"),
            label="pi.extra_args",
        ),
        "rpc_policy": expected_policy,
    }
    if verify_executable:
        _verify_pi_executable(normalized_executable)
    if normalized["command_files"][0] != {
        "argument_index": 0,
        **normalized_executable,
    }:
        raise ResearchCapabilityError(
            "Pi command executable differs from its command-file identity"
        )
    if len(normalized["command_files"]) != 1:
        raise ResearchCapabilityError(
            "Pi identity must bind one direct command-file entrypoint"
        )
    if any(
        item["argument_index"] >= len(command)
        for item in normalized["command_files"]
    ):
        raise ResearchCapabilityError(
            "Pi command file index exceeds the resolved command"
        )
    if any(
        command[item["argument_index"]] != item["path"]
        for item in normalized["command_files"]
    ):
        raise ResearchCapabilityError(
            "Pi command-file path differs from its resolved command token"
        )
    if not normalized["packages"] or not any(
        _is_within(
            Path(normalized_executable["path"]),
            Path(str(package["root"])),
        )
        for package in normalized["packages"]
    ):
        raise ResearchCapabilityError(
            "Pi executable entrypoint must belong to a bound npm package"
        )
    if verify_executable:
        expected_command_files = _pi_command_file_identities(
            command,
            working_directory=Path("/"),
            executable=Path(normalized_executable["path"]),
        )
        if normalized["command_files"] != expected_command_files:
            raise ResearchCapabilityError(
                "Pi command-file identities are incomplete or changed"
            )
        certified_env_command: Path | None = None
        if (
            normalized["interpreters"]
            and Path(
                str(normalized["interpreters"][0]["path"])
            ).name == "env"
        ):
            if len(normalized["interpreters"]) != 2:
                raise ResearchCapabilityError(
                    "Pi env interpreter identity is incomplete"
                )
            certified_env_command = Path(
                str(normalized["interpreters"][1]["path"])
            )
        expected_interpreters = _pi_interpreter_identities(
            Path(normalized_executable["path"]),
            certified_env_command=certified_env_command,
        )
        if normalized["interpreters"] != expected_interpreters:
            raise ResearchCapabilityError(
                "Pi interpreter identity changed after certification"
            )
        expected_packages = _pi_package_identities(expected_command_files)
        if normalized["packages"] != expected_packages:
            raise ResearchCapabilityError(
                "Pi package tree changed or its identity is incomplete"
            )
    return normalized


def verify_pi_execution_identity_current(
    value: Mapping[str, Any],
) -> JsonObject:
    """Rehash one attested Pi process boundary immediately before spawn."""

    return _validate_pi_execution_identity(
        value,
        verify_executable=True,
    )


def pi_execution_environment(
    value: Mapping[str, Any],
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Pin PATH to the interpreter already certified for this Pi entrypoint."""

    normalized = _validate_pi_execution_identity(
        value,
        verify_executable=False,
    )
    return _pi_process_environment(
        normalized["interpreters"],
        environment,
    )


def _validate_prompt(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError("Capability prompt must be an object")
    _require_fields(
        value,
        {"status", "prompt_id", "version", "content_sha256"},
        label="Capability prompt",
    )
    if value.get("status") != "approved":
        raise ResearchCapabilityError("Capability prompt must be approved")
    return {
        "status": "approved",
        "prompt_id": _text(value.get("prompt_id"), label="prompt.prompt_id"),
        "version": _text(value.get("version"), label="prompt.version"),
        "content_sha256": _sha256(
            value.get("content_sha256"),
            label="prompt.content_sha256",
        ),
    }


def _validate_harness(
    value: object,
    *,
    implementation: Mapping[str, Any],
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError("Capability Harness must be an object")
    expected = {
        "status",
        "context_sha256",
        "version",
        "tool_schema_version",
        "research_tools_sha256",
        "research_output_sha256",
    }
    _require_fields(value, expected, label="Capability Harness")
    if value.get("status") != "approved":
        raise ResearchCapabilityError("Capability Harness context must be approved")
    tools_digest = _sha256(
        value.get("research_tools_sha256"),
        label="harness.research_tools_sha256",
    )
    output_digest = _sha256(
        value.get("research_output_sha256"),
        label="harness.research_output_sha256",
    )
    if tools_digest != _implementation_file_digest(
        implementation,
        RESEARCH_TOOLS_PATH,
    ):
        raise ResearchCapabilityError(
            "Approved Harness tools differ from the implementation fingerprint"
        )
    if output_digest != _implementation_file_digest(
        implementation,
        RESEARCH_OUTPUT_PATH,
    ):
        raise ResearchCapabilityError(
            "Approved Harness output tool differs from the implementation "
            "fingerprint"
        )
    return {
        "status": "approved",
        "context_sha256": _sha256(
            value.get("context_sha256"),
            label="harness.context_sha256",
        ),
        "version": _text(value.get("version"), label="harness.version"),
        "tool_schema_version": _text(
            value.get("tool_schema_version"),
            label="harness.tool_schema_version",
        ),
        "research_tools_sha256": tools_digest,
        "research_output_sha256": output_digest,
    }


def _validate_model(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError("Capability model must be an object")
    _require_fields(
        value,
        {"provider", "model", "thinking"},
        label="Capability model",
    )
    return {
        "provider": _text(value.get("provider"), label="model.provider"),
        "model": _text(value.get("model"), label="model.model"),
        "thinking": _text(value.get("thinking"), label="model.thinking"),
    }


def _validate_limits(value: object) -> JsonObject:
    if not isinstance(value, Mapping) or set(value) != set(
        SANDBOX_LIMIT_FIELDS
    ):
        raise ResearchCapabilityError("Capability sandbox limits are invalid")
    cpus = value.get("cpus")
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, (int, float))
        or not math.isfinite(cpus)
        or cpus <= 0
    ):
        raise ResearchCapabilityError("Capability sandbox cpus is invalid")
    limits: JsonObject = {
        "cpus": cpus,
        "memory": _text(value.get("memory"), label="sandbox.limits.memory"),
    }
    for field in SANDBOX_LIMIT_FIELDS:
        if field in {"cpus", "memory"}:
            continue
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ResearchCapabilityError(
                f"Capability sandbox limit {field} is invalid"
            )
        limits[field] = item
    return {field: limits[field] for field in SANDBOX_LIMIT_FIELDS}


def _validate_sandbox(
    value: object,
    *,
    verify_control_plane_files: bool = False,
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError("Capability sandbox must be an object")
    _require_fields(
        value,
        {"backend", "image", "image_id", "limits", "control_plane"},
        label="Capability sandbox",
    )
    if value.get("backend") != RESEARCH_SANDBOX_BACKEND:
        raise ResearchCapabilityError(
            "Capability requires the Docker research sandbox"
        )
    image_id = _text(value.get("image_id"), label="sandbox.image_id")
    if not _IMAGE_ID.fullmatch(image_id):
        raise ResearchCapabilityError(
            "Capability sandbox image ID must be an immutable SHA-256 ID"
        )
    try:
        control_plane = validate_research_sandbox_control_plane_identity(
            value.get("control_plane"),
            verify_files=verify_control_plane_files,
        )
    except ResearchSandboxError as error:
        raise ResearchCapabilityError(str(error)) from error
    return {
        "backend": RESEARCH_SANDBOX_BACKEND,
        "image": _text(value.get("image"), label="sandbox.image"),
        "image_id": image_id,
        "limits": _validate_limits(value.get("limits")),
        "control_plane": control_plane,
    }


def build_research_execution_identity(
    *,
    repository_root: str | os.PathLike[str],
    pi_execution_identity: Mapping[str, Any],
    harness_context_sha256: str,
    research_tools_sha256: str,
    research_output_sha256: str,
    sandbox_backend: str,
    sandbox_image: str,
    sandbox_image_id: str,
    sandbox_limits: Mapping[str, Any],
    sandbox_control_plane_identity: Mapping[str, Any],
) -> JsonObject:
    """Build the shared runtime boundary exercised by Harness and Agents."""

    identity: JsonObject = {
        "schema": RESEARCH_EXECUTION_IDENTITY_SCHEMA,
        "implementation": fingerprint_research_implementation(repository_root),
        "toolchain": {
            "harness_context_sha256": harness_context_sha256,
            "research_tools_sha256": research_tools_sha256,
            "research_output_sha256": research_output_sha256,
        },
        "pi": dict(pi_execution_identity),
        "sandbox": {
            "backend": sandbox_backend,
            "image": sandbox_image,
            "image_id": sandbox_image_id,
            "limits": dict(sandbox_limits),
            "control_plane": dict(sandbox_control_plane_identity),
        },
    }
    return validate_research_execution_identity(
        identity,
        repository_root=repository_root,
        verify_pi_executable=True,
    )


def validate_research_execution_identity(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
    verify_pi_executable: bool = False,
) -> JsonObject:
    """Validate the exact implementation, Pi, tools, and sandbox boundary."""

    _require_fields(
        value,
        {"schema", "implementation", "toolchain", "pi", "sandbox"},
        label="Research execution identity",
    )
    if value.get("schema") != RESEARCH_EXECUTION_IDENTITY_SCHEMA:
        raise ResearchCapabilityError(
            "Unsupported research execution identity schema"
        )
    implementation = _validate_implementation_fingerprint(
        value.get("implementation")
    )
    if repository_root is not None:
        current = fingerprint_research_implementation(repository_root)
        if implementation != current:
            raise ResearchCapabilityError(
                "Research implementation changed after Harness acceptance"
            )
    toolchain = value.get("toolchain")
    if not isinstance(toolchain, Mapping):
        raise ResearchCapabilityError(
            "Research execution toolchain must be an object"
        )
    _require_fields(
        toolchain,
        {
            "harness_context_sha256",
            "research_tools_sha256",
            "research_output_sha256",
        },
        label="Research execution toolchain",
    )
    context_digest = _sha256(
        toolchain.get("harness_context_sha256"),
        label="execution.toolchain.harness_context_sha256",
    )
    tools_digest = _sha256(
        toolchain.get("research_tools_sha256"),
        label="execution.toolchain.research_tools_sha256",
    )
    output_digest = _sha256(
        toolchain.get("research_output_sha256"),
        label="execution.toolchain.research_output_sha256",
    )
    if tools_digest != _implementation_file_digest(
        implementation,
        RESEARCH_TOOLS_PATH,
    ):
        raise ResearchCapabilityError(
            "Research tools differ from the execution implementation"
        )
    if output_digest != _implementation_file_digest(
        implementation,
        RESEARCH_OUTPUT_PATH,
    ):
        raise ResearchCapabilityError(
            "Research output differs from the execution implementation"
        )
    return {
        "schema": RESEARCH_EXECUTION_IDENTITY_SCHEMA,
        "implementation": implementation,
        "toolchain": {
            "harness_context_sha256": context_digest,
            "research_tools_sha256": tools_digest,
            "research_output_sha256": output_digest,
        },
        "pi": _validate_pi_execution_identity(
            value.get("pi"),
            verify_executable=verify_pi_executable,
        ),
        "sandbox": _validate_sandbox(
            value.get("sandbox"),
            verify_control_plane_files=verify_pi_executable,
        ),
    }


def research_execution_identity_digest(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
    verify_pi_executable: bool = False,
) -> str:
    """Return a canonical digest for one Harness/Agent execution boundary."""

    normalized = validate_research_execution_identity(
        value,
        repository_root=repository_root,
        verify_pi_executable=verify_pi_executable,
    )
    return _canonical_digest(normalized)


def build_research_capability_identity(
    *,
    repository_root: str | os.PathLike[str],
    prompt_id: str,
    prompt_version: str,
    prompt_sha256: str,
    harness_context_sha256: str,
    harness_version: str,
    tool_schema_version: str,
    research_tools_sha256: str,
    research_output_sha256: str,
    pi_execution_identity: Mapping[str, Any],
    model: Mapping[str, Any],
    sandbox_backend: str,
    sandbox_image: str,
    sandbox_image_id: str,
    sandbox_limits: Mapping[str, Any],
    sandbox_control_plane_identity: Mapping[str, Any],
) -> JsonObject:
    """Build the complete identity proven by two behavior research smokes."""

    identity: JsonObject = {
        "schema": RESEARCH_CAPABILITY_IDENTITY_SCHEMA,
        "role": AgentRole.BEHAVIOR_PATTERN.value,
        "implementation": fingerprint_research_implementation(repository_root),
        "prompt": {
            "status": "approved",
            "prompt_id": prompt_id,
            "version": prompt_version,
            "content_sha256": prompt_sha256,
        },
        "harness": {
            "status": "approved",
            "context_sha256": harness_context_sha256,
            "version": harness_version,
            "tool_schema_version": tool_schema_version,
            "research_tools_sha256": research_tools_sha256,
            "research_output_sha256": research_output_sha256,
        },
        "pi": dict(pi_execution_identity),
        "model": dict(model),
        "sandbox": {
            "backend": sandbox_backend,
            "image": sandbox_image,
            "image_id": sandbox_image_id,
            "limits": dict(sandbox_limits),
            "control_plane": dict(sandbox_control_plane_identity),
        },
    }
    return validate_research_capability_identity(
        identity,
        repository_root=repository_root,
    )


def validate_research_capability_identity(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> JsonObject:
    """Validate one behavior capability identity and optional live code match."""

    _require_fields(
        value,
        {
            "schema",
            "role",
            "implementation",
            "prompt",
            "harness",
            "pi",
            "model",
            "sandbox",
        },
        label="Research capability identity",
    )
    if value.get("schema") != RESEARCH_CAPABILITY_IDENTITY_SCHEMA:
        raise ResearchCapabilityError(
            "Unsupported research capability identity schema"
        )
    if value.get("role") != AgentRole.BEHAVIOR_PATTERN.value:
        raise ResearchCapabilityError(
            "Only the behavior-pattern smoke can certify research capability"
        )
    implementation = _validate_implementation_fingerprint(
        value.get("implementation")
    )
    if repository_root is not None:
        current = fingerprint_research_implementation(repository_root)
        if implementation != current:
            raise ResearchCapabilityError(
                "Research implementation changed after capability certification"
            )
    return {
        "schema": RESEARCH_CAPABILITY_IDENTITY_SCHEMA,
        "role": AgentRole.BEHAVIOR_PATTERN.value,
        "implementation": implementation,
        "prompt": _validate_prompt(value.get("prompt")),
        "harness": _validate_harness(
            value.get("harness"),
            implementation=implementation,
        ),
        "pi": _validate_pi_execution_identity(
            value.get("pi"),
            verify_executable=repository_root is not None,
        ),
        "model": _validate_model(value.get("model")),
        "sandbox": _validate_sandbox(value.get("sandbox")),
    }


def research_capability_execution_identity(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> JsonObject:
    """Extract the exact Harness-comparable boundary from a capability."""

    capability = validate_research_capability_identity(
        value,
        repository_root=repository_root,
    )
    return validate_research_execution_identity(
        {
            "schema": RESEARCH_EXECUTION_IDENTITY_SCHEMA,
            "implementation": capability["implementation"],
            "toolchain": {
                "harness_context_sha256": capability["harness"][
                    "context_sha256"
                ],
                "research_tools_sha256": capability["harness"][
                    "research_tools_sha256"
                ],
                "research_output_sha256": capability["harness"][
                    "research_output_sha256"
                ],
            },
            "pi": capability["pi"],
            "sandbox": capability["sandbox"],
        },
        repository_root=repository_root,
        verify_pi_executable=False,
    )


def research_capability_execution_identity_digest(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> str:
    """Digest the Harness-comparable portion of a capability identity."""

    execution = research_capability_execution_identity(
        value,
        repository_root=repository_root,
    )
    return _canonical_digest(execution)


def research_capability_identity_digest(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> str:
    """Return the canonical digest of one validated capability identity."""

    normalized = validate_research_capability_identity(
        value,
        repository_root=repository_root,
    )
    return _canonical_digest(normalized)


def _validate_review(
    value: object,
    *,
    benchmark_sha256: str,
    label: str,
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ResearchCapabilityError(f"{label} must be an object")
    _require_fields(
        value,
        {
            "review_id",
            "status",
            "reviewer",
            "checks",
            "benchmark_sha256",
            "reviewed_at",
        },
        label=label,
    )
    if value.get("status") != "passed":
        raise ResearchCapabilityError(f"{label} must have passed")
    checks = value.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != set(
        SMOKE_REVIEW_CHECKS
    ):
        raise ResearchCapabilityError(f"{label} checks are incomplete")
    if not all(checks.get(name) is True for name in SMOKE_REVIEW_CHECKS):
        raise ResearchCapabilityError(f"{label} checks must all pass")
    review_benchmark = _sha256(
        value.get("benchmark_sha256"),
        label=f"{label}.benchmark_sha256",
    )
    if review_benchmark != benchmark_sha256:
        raise ResearchCapabilityError(
            f"{label} used a different hidden benchmark"
        )
    return {
        "review_id": _text(
            value.get("review_id"),
            label=f"{label}.review_id",
        ),
        "status": "passed",
        "reviewer": _text(
            value.get("reviewer"),
            label=f"{label}.reviewer",
        ),
        "checks": {name: True for name in SMOKE_REVIEW_CHECKS},
        "benchmark_sha256": review_benchmark,
        "reviewed_at": _timestamp(
            value.get("reviewed_at"),
            label=f"{label}.reviewed_at",
        ),
    }


def _validate_smoke_runs(
    value: object,
    *,
    benchmark_sha256: str,
) -> list[JsonObject]:
    if not isinstance(value, list) or len(value) != 2:
        raise ResearchCapabilityError(
            "Capability certificate requires exactly two smoke runs"
        )
    smokes: list[JsonObject] = []
    for index, item in enumerate(value):
        label = f"smoke_runs[{index}]"
        if not isinstance(item, Mapping):
            raise ResearchCapabilityError(f"{label} must be an object")
        _require_fields(
            item,
            {"run_id", "session_id", "result_sha256", "review"},
            label=label,
        )
        smokes.append(
            {
                "run_id": _text(item.get("run_id"), label=f"{label}.run_id"),
                "session_id": _text(
                    item.get("session_id"),
                    label=f"{label}.session_id",
                ),
                "result_sha256": _sha256(
                    item.get("result_sha256"),
                    label=f"{label}.result_sha256",
                ),
                "review": _validate_review(
                    item.get("review"),
                    benchmark_sha256=benchmark_sha256,
                    label=f"{label}.review",
                ),
            }
        )
    for field in ("run_id", "session_id"):
        if len({item[field] for item in smokes}) != 2:
            raise ResearchCapabilityError(
                f"Capability smoke {field}s must be independent"
            )
    if len({item["review"]["review_id"] for item in smokes}) != 2:
        raise ResearchCapabilityError(
            "Capability smoke reviews must be independent"
        )
    return smokes


def build_research_capability_certificate(
    *,
    source_batch_id: str,
    source_corpus_sha256: str,
    source_baseline_sha256: str,
    identity: Mapping[str, Any],
    hidden_benchmark_sha256: str,
    smoke_runs: Sequence[Mapping[str, Any]],
    issued_at: str,
    repository_root: str | os.PathLike[str] | None = None,
) -> JsonObject:
    """Build a portable certificate from two independently reviewed smokes."""

    normalized_identity = validate_research_capability_identity(
        identity,
        repository_root=repository_root,
    )
    certificate: JsonObject = {
        "schema": RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA,
        "status": RESEARCH_CAPABILITY_CERTIFICATE_STATUS,
        "source": {
            "batch_id": source_batch_id,
            "corpus_sha256": source_corpus_sha256,
            "baseline_sha256": source_baseline_sha256,
        },
        "identity": normalized_identity,
        "identity_sha256": _canonical_digest(normalized_identity),
        "hidden_benchmark_sha256": hidden_benchmark_sha256,
        "smoke_runs": [dict(item) for item in smoke_runs],
        "issued_at": issued_at,
    }
    return validate_research_capability_certificate(
        certificate,
        repository_root=repository_root,
    )


def validate_research_capability_certificate(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> JsonObject:
    """Validate a portable capability certificate and every bound identity."""

    _require_fields(
        value,
        {
            "schema",
            "status",
            "source",
            "identity",
            "identity_sha256",
            "hidden_benchmark_sha256",
            "smoke_runs",
            "issued_at",
        },
        label="Research capability certificate",
    )
    if value.get("schema") != RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA:
        raise ResearchCapabilityError(
            "Unsupported research capability certificate schema"
        )
    if value.get("status") != RESEARCH_CAPABILITY_CERTIFICATE_STATUS:
        raise ResearchCapabilityError(
            "Research capability certificate is not valid"
        )
    source = value.get("source")
    if not isinstance(source, Mapping):
        raise ResearchCapabilityError("Capability source must be an object")
    _require_fields(
        source,
        {"batch_id", "corpus_sha256", "baseline_sha256"},
        label="Capability source",
    )
    normalized_source = {
        "batch_id": _text(source.get("batch_id"), label="source.batch_id"),
        "corpus_sha256": _sha256(
            source.get("corpus_sha256"),
            label="source.corpus_sha256",
        ),
        "baseline_sha256": _sha256(
            source.get("baseline_sha256"),
            label="source.baseline_sha256",
        ),
    }
    identity_value = value.get("identity")
    if not isinstance(identity_value, Mapping):
        raise ResearchCapabilityError("Capability identity must be an object")
    identity = validate_research_capability_identity(
        identity_value,
        repository_root=repository_root,
    )
    identity_sha256 = _sha256(
        value.get("identity_sha256"),
        label="identity_sha256",
    )
    if identity_sha256 != _canonical_digest(identity):
        raise ResearchCapabilityError(
            "Capability identity digest does not match its content"
        )
    benchmark_sha256 = _sha256(
        value.get("hidden_benchmark_sha256"),
        label="hidden_benchmark_sha256",
    )
    smoke_runs = _validate_smoke_runs(
        value.get("smoke_runs"),
        benchmark_sha256=benchmark_sha256,
    )
    issued_at = _timestamp(value.get("issued_at"), label="issued_at")
    issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    reviewed = [
        datetime.fromisoformat(
            item["review"]["reviewed_at"].replace("Z", "+00:00")
        )
        for item in smoke_runs
    ]
    if issued < max(reviewed):
        raise ResearchCapabilityError(
            "Capability certificate was issued before smoke review completed"
        )
    return {
        "schema": RESEARCH_CAPABILITY_CERTIFICATE_SCHEMA,
        "status": RESEARCH_CAPABILITY_CERTIFICATE_STATUS,
        "source": normalized_source,
        "identity": identity,
        "identity_sha256": identity_sha256,
        "hidden_benchmark_sha256": benchmark_sha256,
        "smoke_runs": smoke_runs,
        "issued_at": issued_at,
    }


def research_capability_certificate_digest(
    value: Mapping[str, Any],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> str:
    """Return the canonical digest of one validated capability certificate."""

    normalized = validate_research_capability_certificate(
        value,
        repository_root=repository_root,
    )
    return _canonical_digest(normalized)
