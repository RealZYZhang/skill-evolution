"""Fail-closed Docker sandbox for multi-Trajectory research agents."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from skill_evolution.storage import JsonObject


RESEARCH_SANDBOX_BACKEND = "docker_research_lab"
RESEARCH_SANDBOX_CONTROL_PLANE_SCHEMA = (
    "research.docker_control_plane.v1"
)

_CONTROL_CLIENT_FIELDS = (
    "version",
    "api_version",
    "git_commit",
    "go_version",
    "os",
    "arch",
    "build_time",
    "context",
    "endpoint",
)
_CONTROL_DAEMON_FIELDS = (
    "id",
    "version",
    "api_version",
    "min_api_version",
    "git_commit",
    "go_version",
    "os",
    "arch",
    "build_time",
    "kernel_version",
    "operating_system",
    "os_version",
    "os_type",
    "architecture",
    "security_options",
    "rootless",
    "cgroup_driver",
    "cgroup_version",
    "storage_driver",
    "default_runtime",
    "isolation",
)

_RESEARCH_INIT = """\
import os
import time

while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        time.sleep(0.01)
    except InterruptedError:
        pass
"""


class ResearchSandboxError(RuntimeError):
    """Raised when isolated research cannot be started or sealed safely."""


@dataclass(frozen=True)
class ResearchSandboxLimits:
    """Resource and output limits enforced for one research container."""

    cpus: float = 1.0
    memory: str = "1g"
    pids: int = 128
    open_files: int = 1024
    work_bytes: int = 64 * 1024 * 1024
    temporary_bytes: int = 64 * 1024 * 1024
    command_timeout_seconds: int = 120
    max_output_bytes: int = 256 * 1024
    max_tool_calls: int = 256
    max_concurrent_tool_calls: int = 1
    max_total_output_bytes: int = 16 * 1024 * 1024
    max_total_command_milliseconds: int = 30 * 60 * 1000

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError("Research sandbox cpus must be positive")
        if not self.memory:
            raise ValueError("Research sandbox memory must not be empty")
        for name in (
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
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"Research sandbox {name} must be positive")
        if self.max_concurrent_tool_calls != 1:
            raise ValueError(
                "Research sandbox tool calls must be serial for cleanup isolation"
            )

    def to_dict(self) -> JsonObject:
        """Serialize the limits for sandbox attestation."""

        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "pids": self.pids,
            "open_files": self.open_files,
            "work_bytes": self.work_bytes,
            "temporary_bytes": self.temporary_bytes,
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_tool_calls": self.max_tool_calls,
            "max_concurrent_tool_calls": self.max_concurrent_tool_calls,
            "max_total_output_bytes": self.max_total_output_bytes,
            "max_total_command_milliseconds": (
                self.max_total_command_milliseconds
            ),
        }


@dataclass(frozen=True)
class ResearchSandboxPreflightResult:
    """Result of checking the mandatory local Docker research backend."""

    available: bool
    backend: str
    detail: str
    image: str
    image_id: str | None = None
    control_plane_identity: JsonObject | None = None


@dataclass(frozen=True)
class TreeDigest:
    """Deterministic identity and size facts for a confined file tree."""

    sha256: str
    file_count: int
    directory_count: int
    total_bytes: int

    def to_dict(self) -> JsonObject:
        """Serialize a file-tree digest."""

        return {
            "sha256": self.sha256,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_bytes": self.total_bytes,
        }


def _validate_directory(path: Path, *, label: str) -> Path:
    """Resolve one exact non-root directory without accepting a symlink root."""

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise ResearchSandboxError(f"{label} does not exist: {path}") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino)
        != (resolved_metadata.st_dev, resolved_metadata.st_ino)
    ):
        raise ResearchSandboxError(f"{label} may not be a symlink: {path}")
    if resolved == Path(resolved.anchor):
        raise ResearchSandboxError(f"{label} may not be a filesystem root")
    if "," in str(resolved):
        raise ResearchSandboxError(
            f"{label} path may not contain a comma for Docker --mount"
        )
    return resolved


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0)


def _open_absolute_directory(path: Path, *, label: str) -> int:
    """Open all canonical directory components without following links."""

    if not path.is_absolute() or not path.anchor:
        raise ResearchSandboxError(f"{label} must be absolute")
    flags = _directory_flags()
    current_fd: int | None = None
    try:
        current_fd = os.open(Path(path.anchor), flags)
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as error:
        if current_fd is not None:
            os.close(current_fd)
        raise ResearchSandboxError(
            f"{label} contains a symlink or unsafe directory"
        ) from error


def _read_fd_bytes(file_fd: int) -> bytes:
    """Read one pinned evidence file exactly once."""

    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _tree_digest_at(
    directory_fd: int,
    *,
    label: str,
    digest: Any,
    prefix: str = "",
) -> tuple[int, int, int]:
    """Hash one descriptor-pinned tree and reject concurrent entry changes."""

    try:
        names_before = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise ResearchSandboxError(f"{label} cannot be listed") from error
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for name in names_before:
        relative = f"{prefix}{name}"
        try:
            declared = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ResearchSandboxError(
                f"{label} changed while hashing: {relative}"
            ) from error
        if stat.S_ISDIR(declared.st_mode):
            child_fd: int | None = None
            try:
                child_fd = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=directory_fd,
                )
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (
                    declared.st_dev,
                    declared.st_ino,
                ):
                    raise ResearchSandboxError(
                        f"{label} changed while hashing: {relative}"
                    )
                directory_count += 1
                digest.update(b"directory\0")
                digest.update(
                    relative.encode("utf-8", errors="surrogateescape")
                )
                digest.update(b"\0")
                nested = _tree_digest_at(
                    child_fd,
                    label=label,
                    digest=digest,
                    prefix=f"{relative}/",
                )
                file_count += nested[0]
                directory_count += nested[1]
                total_bytes += nested[2]
                current = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise ResearchSandboxError(
                        f"{label} changed while hashing: {relative}"
                    )
            except OSError as error:
                raise ResearchSandboxError(
                    f"{label} contains a symlink or unsafe path: {relative}"
                ) from error
            finally:
                if child_fd is not None:
                    os.close(child_fd)
            continue
        if not stat.S_ISREG(declared.st_mode):
            raise ResearchSandboxError(
                f"{label} contains a symlink or special file: {relative}"
            )
        file_fd: int | None = None
        try:
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_flags |= getattr(os, "O_NONBLOCK", 0)
            file_fd = os.open(name, file_flags, dir_fd=directory_fd)
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (declared.st_dev, declared.st_ino)
            ):
                raise ResearchSandboxError(
                    f"{label} changed while hashing: {relative}"
                )
            content = _read_fd_bytes(file_fd)
            after = os.fstat(file_fd)
            current = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                or len(content) != opened.st_size
            ):
                raise ResearchSandboxError(
                    f"{label} changed while hashing: {relative}"
                )
        except OSError as error:
            raise ResearchSandboxError(
                f"{label} contains a symlink or unsafe path: {relative}"
            ) from error
        finally:
            if file_fd is not None:
                os.close(file_fd)
        file_count += 1
        total_bytes += len(content)
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    try:
        names_after = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise ResearchSandboxError(
            f"{label} changed while hashing"
        ) from error
    if names_after != names_before:
        raise ResearchSandboxError(f"{label} changed while hashing")
    return file_count, directory_count, total_bytes


def _tree_digest(root: Path, *, label: str) -> TreeDigest:
    """Hash only regular files and directories, rejecting links and devices."""

    root = _validate_directory(root, label=label)
    digest = hashlib.sha256()
    root_fd = _open_absolute_directory(root, label=label)
    try:
        file_count, directory_count, total_bytes = _tree_digest_at(
            root_fd,
            label=label,
            digest=digest,
        )
    finally:
        os.close(root_fd)
    return TreeDigest(
        sha256=digest.hexdigest(),
        file_count=file_count,
        directory_count=directory_count,
        total_bytes=total_bytes,
    )


def research_evidence_tree_digest(
    root: str | os.PathLike[str],
) -> TreeDigest:
    """Return the exact tree identity expected at sandbox mount time."""

    return _tree_digest(Path(root), label="Research evidence")


def _completed(
    command: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Docker control command without invoking a host shell."""

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _control_file_identity(path: Path) -> JsonObject:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise ResearchSandboxError(
            f"Docker control executable cannot be inspected: {path}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ResearchSandboxError(
            f"Docker control executable is not a regular file: {resolved}"
        )
    return {
        "path": str(resolved),
        "bytes": metadata.st_size,
        "sha256": _file_sha256(resolved),
    }


def _control_interpreters(executable: Path) -> list[JsonObject]:
    try:
        with executable.open("rb") as stream:
            first_line = stream.readline(4096)
    except OSError as error:
        raise ResearchSandboxError(
            "Docker control executable shebang cannot be inspected"
        ) from error
    if not first_line.startswith(b"#!"):
        return []
    try:
        tokens = shlex.split(first_line[2:].decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as error:
        raise ResearchSandboxError(
            "Docker control executable shebang is invalid"
        ) from error
    if not tokens:
        raise ResearchSandboxError(
            "Docker control executable shebang is empty"
        )
    launcher = (
        str(Path(tokens[0]).resolve())
        if Path(tokens[0]).is_absolute()
        else shutil.which(tokens[0])
    )
    if not launcher:
        raise ResearchSandboxError(
            "Docker control executable interpreter is unavailable"
        )
    paths = [Path(launcher)]
    if paths[0].name == "env":
        command = next(
            (item for item in tokens[1:] if not item.startswith("-")),
            None,
        )
        resolved_command = shutil.which(command) if command else None
        if not resolved_command:
            raise ResearchSandboxError(
                "Docker control env shebang lacks an available interpreter"
            )
        paths.append(Path(resolved_command))
    identities: list[JsonObject] = []
    seen: set[str] = set()
    for path in paths:
        identity = _control_file_identity(path)
        if identity["path"] not in seen:
            identities.append(identity)
            seen.add(str(identity["path"]))
    return identities


def _control_text(
    value: object,
    *,
    label: str,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
    ):
        if nullable:
            return None
        raise ResearchSandboxError(f"{label} must be non-empty text")
    return value.strip()


def _validate_control_file_identity(
    value: object,
    *,
    label: str,
    verify_file: bool,
) -> JsonObject:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "bytes",
        "sha256",
    }:
        raise ResearchSandboxError(f"{label} is invalid")
    path = _control_text(value.get("path"), label=f"{label}.path")
    file_bytes = value.get("bytes")
    sha256 = value.get("sha256")
    if (
        not isinstance(path, str)
        or "\x00" in path
        or not Path(path).is_absolute()
        or isinstance(file_bytes, bool)
        or not isinstance(file_bytes, int)
        or file_bytes <= 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(item not in "0123456789abcdef" for item in sha256)
    ):
        raise ResearchSandboxError(f"{label} is invalid")
    normalized = {"path": path, "bytes": file_bytes, "sha256": sha256}
    if verify_file and _control_file_identity(Path(path)) != normalized:
        raise ResearchSandboxError(
            f"{label} changed after control-plane attestation"
        )
    return normalized


def validate_research_sandbox_control_plane_identity(
    value: object,
    *,
    verify_files: bool = False,
) -> JsonObject:
    """Validate the stable Docker client and daemon boundary."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "resolved_command",
        "executable",
        "interpreters",
        "client",
        "daemon",
    }:
        raise ResearchSandboxError(
            "Docker control-plane identity fields differ from schema"
        )
    if value.get("schema") != RESEARCH_SANDBOX_CONTROL_PLANE_SCHEMA:
        raise ResearchSandboxError(
            "Unsupported Docker control-plane identity schema"
        )
    command = value.get("resolved_command")
    if (
        not isinstance(command, list)
        or len(command) != 1
        or not isinstance(command[0], str)
        or not command[0]
        or "\x00" in command[0]
        or not Path(command[0]).is_absolute()
    ):
        raise ResearchSandboxError(
            "Docker control-plane command must be one absolute executable"
        )
    executable = _validate_control_file_identity(
        value.get("executable"),
        label="Docker client executable",
        verify_file=verify_files,
    )
    if command[0] != executable["path"]:
        raise ResearchSandboxError(
            "Docker command differs from its executable identity"
        )
    raw_interpreters = value.get("interpreters")
    if not isinstance(raw_interpreters, list):
        raise ResearchSandboxError("Docker interpreters must be a list")
    interpreters = [
        _validate_control_file_identity(
            item,
            label=f"Docker interpreter {index}",
            verify_file=verify_files,
        )
        for index, item in enumerate(raw_interpreters)
    ]
    if len({item["path"] for item in interpreters}) != len(interpreters):
        raise ResearchSandboxError("Docker interpreter paths must be unique")
    raw_client = value.get("client")
    if not isinstance(raw_client, Mapping) or set(raw_client) != set(
        _CONTROL_CLIENT_FIELDS
    ):
        raise ResearchSandboxError("Docker client identity is invalid")
    client: JsonObject = {}
    for field in _CONTROL_CLIENT_FIELDS:
        client[field] = _control_text(
            raw_client.get(field),
            label=f"Docker client {field}",
            nullable=field in {"git_commit", "go_version", "build_time"},
        )
    raw_daemon = value.get("daemon")
    if not isinstance(raw_daemon, Mapping) or set(raw_daemon) != set(
        _CONTROL_DAEMON_FIELDS
    ):
        raise ResearchSandboxError("Docker daemon identity is invalid")
    daemon: JsonObject = {}
    nullable_daemon = {
        "min_api_version",
        "git_commit",
        "go_version",
        "build_time",
        "os_version",
        "cgroup_driver",
        "cgroup_version",
        "storage_driver",
        "default_runtime",
        "isolation",
    }
    for field in _CONTROL_DAEMON_FIELDS:
        item = raw_daemon.get(field)
        if field == "security_options":
            if (
                not isinstance(item, list)
                or not all(isinstance(option, str) and option for option in item)
                or item != sorted(set(item))
            ):
                raise ResearchSandboxError(
                    "Docker daemon security options are invalid"
                )
            daemon[field] = list(item)
        elif field == "rootless":
            if not isinstance(item, bool):
                raise ResearchSandboxError(
                    "Docker daemon rootless identity is invalid"
                )
            daemon[field] = item
        else:
            daemon[field] = _control_text(
                item,
                label=f"Docker daemon {field}",
                nullable=field in nullable_daemon,
            )
    expected_rootless = any(
        "rootless" in option.lower()
        for option in daemon["security_options"]
    )
    if daemon["rootless"] != expected_rootless:
        raise ResearchSandboxError(
            "Docker daemon rootless flag differs from security options"
        )
    if verify_files:
        expected_interpreters = _control_interpreters(
            Path(executable["path"])
        )
        if interpreters != expected_interpreters:
            raise ResearchSandboxError(
                "Docker interpreter identity changed after attestation"
            )
    return {
        "schema": RESEARCH_SANDBOX_CONTROL_PLANE_SCHEMA,
        "resolved_command": list(command),
        "executable": executable,
        "interpreters": interpreters,
        "client": client,
        "daemon": daemon,
    }


class DockerResearchSandbox:
    """Provide read-only evidence and an isolated, quota-bound work area."""

    name = RESEARCH_SANDBOX_BACKEND
    host_fallback_allowed = False

    def __init__(
        self,
        *,
        docker_command: str | None = None,
        image: str = "python:3.11-slim",
        preflight_timeout_seconds: float = 10.0,
        control_timeout_seconds: float = 30.0,
        limits: ResearchSandboxLimits | None = None,
    ) -> None:
        if preflight_timeout_seconds <= 0 or control_timeout_seconds <= 0:
            raise ValueError("Research sandbox timeouts must be positive")
        self.docker_command = docker_command or shutil.which("docker")
        self.image = image
        self.preflight_timeout_seconds = preflight_timeout_seconds
        self.control_timeout_seconds = control_timeout_seconds
        self.limits = limits or ResearchSandboxLimits()

    def _resolved_docker_command(self) -> Path:
        command = self.docker_command
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ResearchSandboxError("Docker CLI is not installed")
        if os.sep in command or (os.altsep and os.altsep in command):
            candidate = Path(command)
        else:
            discovered = shutil.which(command)
            if discovered is None:
                raise ResearchSandboxError("Docker CLI is not installed")
            candidate = Path(discovered)
        identity = _control_file_identity(candidate)
        return Path(str(identity["path"]))

    def _control_json(
        self,
        executable: Path,
        arguments: list[str],
        *,
        label: str,
    ) -> JsonObject:
        try:
            completed = _completed(
                [str(executable), *arguments],
                timeout=self.preflight_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ResearchSandboxError(
                f"Docker {label} query failed: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ResearchSandboxError(
                f"Docker {label} query failed: {detail}"
            )
        if not completed.stdout or len(completed.stdout.encode("utf-8")) > 1_048_576:
            raise ResearchSandboxError(
                f"Docker {label} query returned empty or oversized output"
            )
        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ResearchSandboxError(
                f"Docker {label} query returned invalid JSON"
            ) from error
        if not isinstance(decoded, Mapping):
            raise ResearchSandboxError(
                f"Docker {label} query must return an object"
            )
        return dict(decoded)

    def _control_string(
        self,
        executable: Path,
        arguments: list[str],
        *,
        label: str,
    ) -> str:
        try:
            completed = _completed(
                [str(executable), *arguments],
                timeout=self.preflight_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ResearchSandboxError(
                f"Docker {label} query failed: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ResearchSandboxError(
                f"Docker {label} query failed: {detail}"
            )
        if not completed.stdout or len(completed.stdout.encode("utf-8")) > 4096:
            raise ResearchSandboxError(
                f"Docker {label} query returned empty or oversized output"
            )
        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ResearchSandboxError(
                f"Docker {label} query returned invalid JSON"
            ) from error
        result = _control_text(decoded, label=f"Docker {label}")
        assert isinstance(result, str)
        return result

    def _control_plain_text(
        self,
        executable: Path,
        arguments: list[str],
        *,
        label: str,
    ) -> str:
        """Read one bounded non-JSON Docker control-plane fact."""

        try:
            completed = _completed(
                [str(executable), *arguments],
                timeout=self.preflight_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ResearchSandboxError(
                f"Docker {label} query failed: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ResearchSandboxError(
                f"Docker {label} query failed: {detail}"
            )
        if not completed.stdout or len(completed.stdout.encode("utf-8")) > 4096:
            raise ResearchSandboxError(
                f"Docker {label} query returned empty or oversized output"
            )
        result = _control_text(
            completed.stdout,
            label=f"Docker {label}",
        )
        assert isinstance(result, str)
        return result

    def control_plane_identity(self) -> JsonObject:
        """Attest the exact Docker CLI and stable daemon security boundary."""

        executable = self._resolved_docker_command()
        context = self._control_plain_text(
            executable,
            ["context", "show"],
            label="context",
        )
        version = self._control_json(
            executable,
            ["version", "--format", "{{json .}}"],
            label="version",
        )
        info = self._control_json(
            executable,
            ["info", "--format", "{{json .}}"],
            label="daemon identity",
        )
        endpoint = self._control_string(
            executable,
            [
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            label="context endpoint",
        )
        final_context = self._control_plain_text(
            executable,
            ["context", "show"],
            label="context",
        )
        if final_context != context:
            raise ResearchSandboxError(
                "Docker context changed during control-plane attestation"
            )
        raw_client = version.get("Client")
        raw_server = version.get("Server")
        if not isinstance(raw_client, Mapping) or not isinstance(
            raw_server, Mapping
        ):
            raise ResearchSandboxError(
                "Docker version lacks client or server identity"
            )
        version_context = raw_client.get("Context")
        if version_context is not None and _control_text(
            version_context,
            label="Docker version context",
        ) != context:
            raise ResearchSandboxError(
                "Docker context differs between context and version queries"
            )
        server_version = _control_text(
            raw_server.get("Version"),
            label="Docker server version",
        )
        info_version = _control_text(
            info.get("ServerVersion"),
            label="Docker info server version",
        )
        if server_version != info_version:
            raise ResearchSandboxError(
                "Docker daemon changed between version and info queries"
            )
        security = info.get("SecurityOptions", [])
        if not isinstance(security, list) or not all(
            isinstance(item, str) and item for item in security
        ):
            raise ResearchSandboxError(
                "Docker daemon security options are invalid"
            )
        security_options = sorted(set(security))
        identity: JsonObject = {
            "schema": RESEARCH_SANDBOX_CONTROL_PLANE_SCHEMA,
            "resolved_command": [str(executable)],
            "executable": _control_file_identity(executable),
            "interpreters": _control_interpreters(executable),
            "client": {
                "version": raw_client.get("Version"),
                "api_version": raw_client.get("ApiVersion"),
                "git_commit": raw_client.get("GitCommit"),
                "go_version": raw_client.get("GoVersion"),
                "os": raw_client.get("Os"),
                "arch": raw_client.get("Arch"),
                "build_time": raw_client.get("BuildTime"),
                "context": context,
                "endpoint": endpoint,
            },
            "daemon": {
                "id": info.get("ID"),
                "version": server_version,
                "api_version": raw_server.get("ApiVersion"),
                "min_api_version": raw_server.get("MinAPIVersion"),
                "git_commit": raw_server.get("GitCommit"),
                "go_version": raw_server.get("GoVersion"),
                "os": raw_server.get("Os"),
                "arch": raw_server.get("Arch"),
                "build_time": raw_server.get("BuildTime"),
                "kernel_version": info.get("KernelVersion"),
                "operating_system": info.get("OperatingSystem"),
                "os_version": info.get("OSVersion"),
                "os_type": info.get("OSType"),
                "architecture": info.get("Architecture"),
                "security_options": security_options,
                "rootless": any(
                    "rootless" in item.lower()
                    for item in security_options
                ),
                "cgroup_driver": info.get("CgroupDriver"),
                "cgroup_version": info.get("CgroupVersion"),
                "storage_driver": info.get("Driver"),
                "default_runtime": info.get("DefaultRuntime"),
                "isolation": info.get("Isolation"),
            },
        }
        return validate_research_sandbox_control_plane_identity(
            identity,
            verify_files=True,
        )

    def verify_control_plane_identity_current(
        self,
        expected: Mapping[str, Any],
    ) -> JsonObject:
        """Require the current Docker client and daemon to match a pin."""

        normalized = validate_research_sandbox_control_plane_identity(
            expected,
            verify_files=True,
        )
        current = self.control_plane_identity()
        if current != normalized:
            raise ResearchSandboxError(
                "Docker control plane changed after Harness acceptance"
            )
        return normalized

    def preflight(self) -> ResearchSandboxPreflightResult:
        """Require a daemon and an already-present image; never pull an image."""

        if not self.docker_command:
            return ResearchSandboxPreflightResult(
                available=False,
                backend=self.name,
                detail="Docker CLI is not installed.",
                image=self.image,
            )
        try:
            control_plane = self.control_plane_identity()
        except (ResearchSandboxError, OSError, subprocess.SubprocessError) as error:
            return ResearchSandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=f"Docker preflight failed: {error}",
                image=self.image,
            )
        docker_command = str(control_plane["resolved_command"][0])
        try:
            inspected = _completed(
                [
                    docker_command,
                    "image",
                    "inspect",
                    self.image,
                    "--format",
                    "{{json .Id}}",
                ],
                timeout=self.preflight_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return ResearchSandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=f"Research image preflight failed: {error}",
                image=self.image,
            )
        if inspected.returncode != 0:
            return ResearchSandboxPreflightResult(
                available=False,
                backend=self.name,
                detail=(
                    f"Research image {self.image!r} is not present locally; "
                    "the framework will not pull it implicitly."
                ),
                image=self.image,
            )
        raw_image_id = inspected.stdout.strip()
        try:
            decoded_image_id = json.loads(raw_image_id)
        except json.JSONDecodeError:
            decoded_image_id = raw_image_id
        image_id = (
            decoded_image_id.strip()
            if isinstance(decoded_image_id, str)
            else ""
        )
        if (
            len(image_id) != 71
            or not image_id.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in image_id[7:]
            )
        ):
            return ResearchSandboxPreflightResult(
                available=False,
                backend=self.name,
                detail="Docker returned an invalid immutable image id.",
                image=self.image,
            )
        return ResearchSandboxPreflightResult(
            available=True,
            backend=self.name,
            detail=(
                "Docker client "
                f"{control_plane['client']['version']} and server "
                f"{control_plane['daemon']['version']} with local research "
                f"image {self.image!r} are available."
            ),
            image=self.image,
            image_id=image_id,
            control_plane_identity=control_plane,
        )

    @contextmanager
    def isolated_run(
        self,
        *,
        evidence_directory: str | os.PathLike[str],
        work_archive_directory: str | os.PathLike[str],
        expected_evidence_digest: TreeDigest,
        expected_control_plane_identity: Mapping[str, Any] | None = None,
    ) -> Iterator[JsonObject]:
        """Start one research container and safely seal its temporary work tree."""

        preflight = self.preflight()
        if (
            not preflight.available
            or not preflight.image_id
            or not self.docker_command
        ):
            raise ResearchSandboxError(preflight.detail)
        if expected_control_plane_identity is not None:
            control_plane = self.verify_control_plane_identity_current(
                expected_control_plane_identity
            )
        elif preflight.control_plane_identity is not None:
            control_plane = validate_research_sandbox_control_plane_identity(
                preflight.control_plane_identity,
                verify_files=True,
            )
        else:
            control_plane = None
        docker_command = (
            str(control_plane["resolved_command"][0])
            if control_plane is not None
            else self.docker_command
        )
        evidence = _validate_directory(
            Path(evidence_directory), label="Research evidence"
        )
        evidence_before = _tree_digest(evidence, label="Research evidence")
        if evidence_before != expected_evidence_digest:
            raise ResearchSandboxError(
                "Research evidence changed after corpus verification"
            )
        work_archive = Path(work_archive_directory).resolve(strict=False)
        if work_archive.exists():
            raise ResearchSandboxError(
                f"Research work archive already exists: {work_archive}"
            )
        archive_parent = _validate_directory(
            work_archive.parent, label="Research work archive parent"
        )
        try:
            work_archive.relative_to(archive_parent)
        except ValueError as error:
            raise ResearchSandboxError(
                "Research work archive escapes its parent"
            ) from error

        evidence_mount = (
            f"type=bind,src={evidence},dst=/evidence,readonly"
        )
        start_command = [
            docker_command,
            "run",
            "--detach",
            "--pull",
            "never",
            "--network",
            "none",
            "--log-driver",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            str(self.limits.pids),
            "--memory",
            self.limits.memory,
            "--cpus",
            str(self.limits.cpus),
            "--ulimit",
            f"nofile={self.limits.open_files}:{self.limits.open_files}",
            "--stop-timeout",
            "2",
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,mode=1777,size="
                f"{self.limits.temporary_bytes}"
            ),
            "--tmpfs",
            (
                "/work:rw,nosuid,nodev,mode=1777,size="
                f"{self.limits.work_bytes}"
            ),
            "--mount",
            evidence_mount,
            "--workdir",
            "/work",
            preflight.image_id,
            "python3",
            "-c",
            _RESEARCH_INIT,
        ]
        try:
            started = _completed(
                start_command,
                timeout=self.control_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ResearchSandboxError(
                f"Could not start research sandbox: {error}"
            ) from error
        container_id = started.stdout.strip()
        if started.returncode != 0 or not container_id:
            raise ResearchSandboxError(
                "Could not start research sandbox: "
                f"{started.stderr.strip()}"
            )

        mounted_evidence = _tree_digest(evidence, label="Research evidence")
        if mounted_evidence != expected_evidence_digest:
            removal_error = self._remove_container(
                container_id,
                docker_command=docker_command,
            )
            changed = ResearchSandboxError(
                "Research evidence changed while mounting"
            )
            if removal_error is not None:
                raise BaseExceptionGroup(
                    "Research evidence changed and sandbox cleanup failed",
                    [changed, removal_error],
                )
            raise changed

        context: JsonObject = {
            "backend": self.name,
            "container_id": container_id,
            "image": self.image,
            "image_id": preflight.image_id,
            "control_plane_identity": control_plane,
            "host_fallback_allowed": False,
            "network": "none",
            "root_filesystem": "read_only",
            "container_user": "65534:65534",
            "credentials_in_container": False,
            "mounts": {
                "evidence": {
                    "container_path": "/evidence",
                    "mode": "read_only",
                },
                "work": {
                    "container_path": "/work",
                    "mode": "read_write_tmpfs",
                },
            },
            "limits": self.limits.to_dict(),
            "evidence_digest_before": evidence_before.to_dict(),
            "evidence_digest_after": None,
            "work_digest": None,
            "work_archive": str(work_archive),
            "tool_environment": {
                "SKILL_EVOLUTION_RESEARCH_CONTAINER": container_id,
                "SKILL_EVOLUTION_DOCKER_COMMAND": docker_command,
                "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS": str(
                    self.limits.command_timeout_seconds * 1000
                ),
                "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES": str(
                    self.limits.max_output_bytes
                ),
                "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS": str(
                    self.limits.max_tool_calls
                ),
                "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS": str(
                    self.limits.max_concurrent_tool_calls
                ),
                "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES": str(
                    self.limits.max_total_output_bytes
                ),
                "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS": str(
                    self.limits.max_total_command_milliseconds
                ),
            },
        }
        primary_error: BaseException | None = None
        primary_traceback: Any = None
        cleanup_error: BaseException | None = None
        try:
            yield context
        except BaseException as error:
            primary_error = error
            primary_traceback = error.__traceback__
        finally:
            try:
                work_digest = self._seal_work(
                    container_id=container_id,
                    evidence=evidence,
                    expected_evidence=evidence_before,
                    work_archive=work_archive,
                    archive_parent=archive_parent,
                    docker_command=docker_command,
                )
                context["evidence_digest_after"] = evidence_before.to_dict()
                context["work_digest"] = work_digest.to_dict()
            except BaseException as error:
                cleanup_error = error
            finally:
                removal_error = self._remove_container(
                    container_id,
                    docker_command=docker_command,
                )
                if cleanup_error is None and removal_error is not None:
                    cleanup_error = removal_error

        if primary_error is not None and cleanup_error is not None:
            raise BaseExceptionGroup(
                "Research run and sandbox sealing both failed",
                [primary_error, cleanup_error],
            )
        if cleanup_error is not None:
            raise cleanup_error
        if primary_error is not None:
            raise primary_error.with_traceback(primary_traceback)

    def _seal_work(
        self,
        *,
        container_id: str,
        evidence: Path,
        expected_evidence: TreeDigest,
        work_archive: Path,
        archive_parent: Path,
        docker_command: str,
    ) -> TreeDigest:
        """Pause a container, copy one stable work snapshot, and verify both trees."""

        paused = _completed(
            [docker_command, "pause", container_id],
            timeout=self.control_timeout_seconds,
        )
        if paused.returncode != 0:
            raise ResearchSandboxError(
                "Could not freeze research work for export: "
                f"{paused.stderr.strip()}"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".research-work-", dir=archive_parent)
        )
        try:
            copied = _completed(
                [
                    docker_command,
                    "cp",
                    f"{container_id}:/work/.",
                    str(staging),
                ],
                timeout=self.control_timeout_seconds,
            )
            if copied.returncode != 0:
                raise ResearchSandboxError(
                    "Could not export research work: "
                    f"{copied.stderr.strip()}"
                )
            work_digest = _tree_digest(
                staging, label="Exported research work"
            )
            if work_digest.total_bytes > self.limits.work_bytes:
                raise ResearchSandboxError(
                    "Exported research work exceeds its declared quota"
                )
            evidence_after = _tree_digest(
                evidence, label="Research evidence"
            )
            if evidence_after != expected_evidence:
                raise ResearchSandboxError(
                    "Research evidence changed while mounted read-only"
                )
            os.replace(staging, work_archive)
            return work_digest
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _remove_container(
        self,
        container_id: str,
        *,
        docker_command: str | None = None,
    ) -> ResearchSandboxError | None:
        """Remove exactly the disposable container, including a paused one."""

        active_command = docker_command or self.docker_command
        if not active_command:
            return ResearchSandboxError(
                "Docker command disappeared before container cleanup"
            )
        try:
            removed = _completed(
                [active_command, "rm", "--force", container_id],
                timeout=self.control_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return ResearchSandboxError(
                f"Could not remove research container: {error}"
            )
        if removed.returncode != 0:
            detail = removed.stderr.strip() or removed.stdout.strip()
            return ResearchSandboxError(
                f"Could not remove research container: {detail}"
            )
        return None


def validate_research_sandbox_context(
    context: Mapping[str, Any],
) -> dict[str, str]:
    """Extract the exact environment accepted by the trusted tool router."""

    if context.get("backend") != RESEARCH_SANDBOX_BACKEND:
        raise ResearchSandboxError("Unexpected research sandbox backend")
    if context.get("network") != "none":
        raise ResearchSandboxError("Research sandbox must have no network")
    if context.get("root_filesystem") != "read_only":
        raise ResearchSandboxError("Research sandbox root must be read-only")
    if context.get("credentials_in_container") is not False:
        raise ResearchSandboxError("Research sandbox cannot receive credentials")
    if context.get("host_fallback_allowed") is not False:
        raise ResearchSandboxError("Research sandbox cannot allow host fallback")
    mounts = context.get("mounts")
    if not isinstance(mounts, Mapping):
        raise ResearchSandboxError("Research sandbox mounts are missing")
    evidence = mounts.get("evidence")
    work = mounts.get("work")
    if not isinstance(evidence, Mapping) or evidence.get("mode") != "read_only":
        raise ResearchSandboxError("Research evidence is not read-only")
    if (
        not isinstance(work, Mapping)
        or work.get("mode") != "read_write_tmpfs"
    ):
        raise ResearchSandboxError("Research work is not quota-bound tmpfs")
    environment = context.get("tool_environment")
    if not isinstance(environment, Mapping):
        raise ResearchSandboxError("Research tool environment is missing")
    normalized: dict[str, str] = {}
    for key in (
        "SKILL_EVOLUTION_RESEARCH_CONTAINER",
        "SKILL_EVOLUTION_DOCKER_COMMAND",
        "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS",
        "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS",
        "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES",
        "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS",
    ):
        value = environment.get(key)
        if not isinstance(value, str) or not value:
            raise ResearchSandboxError(
                f"Research tool environment is missing {key}"
            )
        normalized[key] = value
    return normalized
