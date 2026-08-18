"""Atomic file storage and recoverable in-process queues."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
from typing import Any
import uuid


JsonObject = dict[str, Any]


class StorageError(RuntimeError):
    """Raised when a file-backed object cannot be read or updated safely."""


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for manifests."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_object_id(prefix: str) -> str:
    """Return a sortable, collision-resistant object identifier."""

    if not prefix or not prefix.replace("-", "").replace("_", "").isalnum():
        raise ValueError("prefix must contain letters, numbers, '-' or '_'")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}"


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace one JSON object and durably flush its directory entry."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                dict(value),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_json_object(path: Path) -> JsonObject:
    """Load a JSON object, rejecting arrays and scalar values."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StorageError(f"Manifest does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise StorageError(f"Manifest is invalid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise StorageError(f"Manifest must be a JSON object: {path}")
    return value


class ManifestRepository:
    """Store one manifest per object directory using atomic replacements."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        manifest_name: str = "manifest.json",
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest_name = manifest_name
        self._lock = threading.RLock()

    def object_directory(self, object_id: str) -> Path:
        """Return the directory for an object after validating its identifier."""

        if (
            not object_id
            or Path(object_id).name != object_id
            or object_id in {".", ".."}
        ):
            raise ValueError("object_id must be one safe path component")
        return self.root / object_id

    def manifest_path(self, object_id: str) -> Path:
        """Return the manifest path for an object."""

        return self.object_directory(object_id) / self.manifest_name

    def create(
        self,
        object_id: str,
        manifest: Mapping[str, Any],
    ) -> Path:
        """Create a new object without overwriting an existing manifest."""

        path = self.manifest_path(object_id)
        with self._lock:
            if path.exists():
                raise StorageError(f"Object already exists: {object_id}")
            value = dict(manifest)
            if value.get("id") not in {None, object_id}:
                raise StorageError("Manifest id does not match object directory")
            value["id"] = object_id
            value.setdefault("created_at", utc_now())
            value["updated_at"] = utc_now()
            atomic_write_json(path, value)
        return path

    def load(self, object_id: str) -> JsonObject:
        """Load one object's current manifest."""

        return load_json_object(self.manifest_path(object_id))

    def replace(
        self,
        object_id: str,
        manifest: Mapping[str, Any],
    ) -> JsonObject:
        """Replace an existing manifest while preserving its identity."""

        path = self.manifest_path(object_id)
        with self._lock:
            if not path.is_file():
                raise StorageError(f"Object does not exist: {object_id}")
            value = dict(manifest)
            if value.get("id") != object_id:
                raise StorageError("Replacement manifest id must match object id")
            value["updated_at"] = utc_now()
            atomic_write_json(path, value)
        return value

    def update(
        self,
        object_id: str,
        changes: Mapping[str, Any],
        *,
        expected_status: str | Iterable[str] | None = None,
    ) -> JsonObject:
        """Merge manifest fields with an optional compare-and-swap status check."""

        with self._lock:
            current = self.load(object_id)
            if expected_status is not None:
                expected = (
                    {expected_status}
                    if isinstance(expected_status, str)
                    else set(expected_status)
                )
                if current.get("status") not in expected:
                    raise StorageError(
                        "Manifest status changed: expected "
                        f"{sorted(expected)}, found {current.get('status')!r}"
                    )
            updated = {**current, **dict(changes)}
            return self.replace(object_id, updated)

    def list_manifests(self) -> list[JsonObject]:
        """Load every readable manifest in deterministic object-id order."""

        if not self.root.is_dir():
            return []
        manifests: list[JsonObject] = []
        for directory in sorted(self.root.iterdir()):
            path = directory / self.manifest_name
            if directory.is_dir() and path.is_file():
                manifests.append(load_json_object(path))
        return manifests

    def recover_non_terminal(
        self,
        terminal_statuses: Iterable[str],
    ) -> list[str]:
        """Return object IDs whose current status is not terminal."""

        terminal = set(terminal_statuses)
        recovered: list[str] = []
        for manifest in self.list_manifests():
            object_id = manifest.get("id")
            if (
                isinstance(object_id, str)
                and manifest.get("status") not in terminal
            ):
                recovered.append(object_id)
        return recovered


class ObjectIdQueue:
    """An in-memory ID queue that can be rebuilt from file manifests."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def put(self, object_id: str) -> bool:
        """Enqueue an ID once; return whether it was newly added."""

        if not object_id:
            raise ValueError("object_id must not be empty")
        with self._lock:
            if object_id in self._known:
                return False
            self._known.add(object_id)
            self._queue.put(object_id)
            return True

    def get(self, *, timeout: float | None = None) -> str:
        """Remove and return the next queued object ID."""

        object_id = self._queue.get(timeout=timeout)
        with self._lock:
            self._known.discard(object_id)
        return object_id

    def recover(
        self,
        repository: ManifestRepository,
        *,
        terminal_statuses: Iterable[str],
    ) -> list[str]:
        """Enqueue all non-terminal objects discovered after a restart."""

        recovered = repository.recover_non_terminal(terminal_statuses)
        for object_id in recovered:
            self.put(object_id)
        return recovered
