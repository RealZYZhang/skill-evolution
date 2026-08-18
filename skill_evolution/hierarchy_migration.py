"""Plan and execute an auditable legacy-to-Skill hierarchy migration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from skill_evolution.hierarchy import (
    ANALYSIS_RECORD_SCHEMA,
    HierarchyError,
    SkillHierarchyRepository,
    execution_manifest_from_payload,
    package_digest,
    validate_analysis_record,
)
from skill_evolution.storage import (
    JsonObject,
    atomic_write_json,
    load_json_object,
    new_object_id,
    utc_now,
)


MIGRATION_SCHEMA = "skill.hierarchy_migration.v1"
MAPPING_SCHEMA = "skill.hierarchy_mapping.v1"
LEGACY_ROOTS = (
    "replays",
    "harness-runs",
    "analyses",
    "candidates",
    "comparisons",
    "reviews",
    "experiment-requests",
    "spikes",
    "trajectories",
)


class HierarchyMigrationError(RuntimeError):
    """Raised when a migration cannot be planned or completed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HierarchyMigrationError(f"{label} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise HierarchyMigrationError(f"{label} is unsafe")
    return path.as_posix()


def _inventory(root: Path) -> list[JsonObject]:
    result: list[JsonObject] = []
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise HierarchyMigrationError(
                f"Legacy data contains a symlink: {path}"
            )
        if path.is_file() and path.name != ".DS_Store":
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return result


def _directory_digest(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        encoded = str(record["path"]).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(str(record["sha256"])))
    return digest.hexdigest()


def load_identity_mappings(path: str | os.PathLike[str]) -> JsonObject:
    """Load strict owner-approved legacy identity mappings."""

    value = load_json_object(Path(path))
    if set(value) != {"schema", "title", "mappings"}:
        raise HierarchyMigrationError("Identity mapping fields are invalid")
    if value.get("schema") != MAPPING_SCHEMA:
        raise HierarchyMigrationError("Unsupported identity mapping schema")
    if not isinstance(value.get("title"), str) or not value["title"]:
        raise HierarchyMigrationError("Identity mapping title is required")
    mappings = value.get("mappings")
    if not isinstance(mappings, list):
        raise HierarchyMigrationError("Identity mappings must be a list")
    normalized: list[JsonObject] = []
    skill_ids: set[str] = set()
    suffixes: set[str] = set()
    for item in mappings:
        if not isinstance(item, Mapping) or set(item) != {
            "skill_id",
            "source_suffixes",
        }:
            raise HierarchyMigrationError("Identity mapping entry is invalid")
        skill_id = item.get("skill_id")
        sources = item.get("source_suffixes")
        if not isinstance(skill_id, str) or not skill_id:
            raise HierarchyMigrationError("Mapped skill_id is invalid")
        if skill_id in skill_ids:
            raise HierarchyMigrationError("Mapped skill_id is duplicated")
        if not isinstance(sources, list) or not sources:
            raise HierarchyMigrationError("source_suffixes cannot be empty")
        safe_sources: list[str] = []
        for source in sources:
            safe = _safe_relative(source, label="source suffix")
            if safe in suffixes:
                raise HierarchyMigrationError("Source suffix is ambiguous")
            suffixes.add(safe)
            safe_sources.append(safe)
        skill_ids.add(skill_id)
        normalized.append(
            {"skill_id": skill_id, "source_suffixes": safe_sources}
        )
    return {
        "schema": MAPPING_SCHEMA,
        "title": value["title"],
        "mappings": normalized,
    }


class SkillHierarchyMigration:
    """Create a complete plan, verify it, and perform a reversible cutover."""

    def __init__(
        self,
        runtime_root: str | os.PathLike[str],
        identity_mappings: Mapping[str, Any],
    ) -> None:
        self.root = Path(runtime_root).resolve()
        self.mappings = deepcopy(dict(identity_mappings))
        if self.mappings.get("schema") != MAPPING_SCHEMA:
            raise HierarchyMigrationError("Unsupported identity mapping schema")

    def plan(
        self,
        *,
        migration_id: str | None = None,
        write_manifest: bool = True,
    ) -> JsonObject:
        """Scan legacy roots and produce a byte-addressed dry-run plan."""

        identifier = migration_id or new_object_id("hierarchy-migration")
        objects: JsonObject = {
            "revisions": [],
            "execution_sets": [],
            "executions": [],
            "single_analyses": [],
            "multi_analyses": [],
            "improvements": [],
        }
        unresolved: list[JsonObject] = []
        campaign_map: dict[str, JsonObject] = {}
        run_map: dict[str, JsonObject] = {}
        revision_map: dict[tuple[str, str], JsonObject] = {}

        replays_root = self.root / "replays"
        if replays_root.is_dir():
            for directory in sorted(replays_root.iterdir()):
                if not directory.is_dir() or directory.is_symlink():
                    continue
                manifest_path = directory / "replay.json"
                if not manifest_path.is_file():
                    unresolved.append(
                        self._unresolved(directory, "missing replay.json")
                    )
                    continue
                manifest = load_json_object(manifest_path)
                campaign_id = manifest.get("campaign_id")
                if (
                    not isinstance(campaign_id, str)
                    or campaign_id != directory.name
                ):
                    unresolved.append(
                        self._unresolved(directory, "invalid Campaign identity")
                    )
                    continue
                skill = manifest.get("skill")
                source = (
                    skill.get("source_path")
                    if isinstance(skill, Mapping)
                    else None
                )
                skill_id = self._resolve_skill_id(source)
                if skill_id is None:
                    unresolved.append(
                        self._unresolved(directory, "Skill identity is unmapped")
                    )
                    continue
                run_records = manifest.get("runs")
                if not isinstance(run_records, list) or not run_records:
                    unresolved.append(
                        self._unresolved(directory, "Campaign has no runs")
                    )
                    continue
                planned_runs: list[JsonObject] = []
                revision_ids: set[str] = set()
                for run in run_records:
                    planned = self._plan_execution(
                        directory,
                        campaign_id,
                        skill_id,
                        run,
                    )
                    if "error" in planned:
                        unresolved.append(planned)
                        continue
                    revision = planned.pop("revision")
                    key = (skill_id, str(revision["revision_id"]))
                    revision_map.setdefault(key, revision)
                    revision_ids.add(str(revision["revision_id"]))
                    planned_runs.append(planned)
                if len(planned_runs) != len(run_records):
                    continue
                if len(revision_ids) != 1:
                    unresolved.append(
                        self._unresolved(
                            directory,
                            "One legacy Campaign contains multiple Skill revisions",
                        )
                    )
                    continue
                revision_id = next(iter(revision_ids))
                set_id = f"set-legacy-{campaign_id}"
                execution_set = {
                    "set_id": set_id,
                    "skill_id": skill_id,
                    "revision_id": revision_id,
                    "source": self._relative(directory),
                    "destination": (
                        f"skills/{skill_id}/execution-sets/{set_id}"
                    ),
                    "legacy_campaign_id": campaign_id,
                    "execution_ids": [item["execution_id"] for item in planned_runs],
                    "source_manifest": deepcopy(manifest),
                }
                objects["execution_sets"].append(execution_set)
                campaign_map[campaign_id] = execution_set
                for execution in planned_runs:
                    execution["execution_set_id"] = set_id
                    objects["executions"].append(execution)
                    run_map[str(execution["execution_id"])] = execution

        self._plan_standalone_trajectories(
            objects,
            unresolved,
            run_map,
            revision_map,
        )
        objects["revisions"] = list(revision_map.values())
        self._plan_prechecks(objects, unresolved, run_map)
        self._plan_single_trajectory_agent_runs(objects, unresolved, run_map)
        self._plan_multi_analyses(objects, unresolved, campaign_map)
        self._scan_unclaimed_roots(objects, unresolved)

        source_inventory: list[JsonObject] = []
        for root_name in LEGACY_ROOTS:
            root = self.root / root_name
            for record in _inventory(root):
                source_inventory.append(
                    {**record, "path": f"{root_name}/{record['path']}"}
                )
        status = "ready" if not unresolved else "blocked"
        counts = {
            key: len(value)
            for key, value in objects.items()
            if isinstance(value, list)
        }
        manifest: JsonObject = {
            "schema": MIGRATION_SCHEMA,
            "migration_id": identifier,
            "mode": "dry_run",
            "status": status,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "runtime_root": ".",
            "identity_mapping": deepcopy(self.mappings),
            "objects": objects,
            "counts": counts,
            "unresolved": unresolved,
            "source_inventory": source_inventory,
            "source_digest": _directory_digest(source_inventory),
            "operations": [],
            "verification": {
                "source_files": len(source_inventory),
                "source_bytes": sum(item["bytes"] for item in source_inventory),
                "preserved_payload_hashes": None,
                "orphan_references": None,
                "rollback_available": True,
            },
        }
        if write_manifest:
            target = self.root / "migrations" / identifier / "manifest.json"
            if target.exists():
                raise HierarchyMigrationError(
                    f"Migration manifest already exists: {identifier}"
                )
            atomic_write_json(target, manifest)
        return manifest

    def apply(self, manifest: Mapping[str, Any], *, confirmation: str) -> JsonObject:
        """Apply a ready plan after an exact migration-ID confirmation."""

        plan = deepcopy(dict(manifest))
        if plan.get("schema") != MIGRATION_SCHEMA:
            raise HierarchyMigrationError("Unsupported migration manifest")
        migration_id = plan.get("migration_id")
        if not isinstance(migration_id, str) or confirmation != migration_id:
            raise HierarchyMigrationError("Migration confirmation does not match")
        if plan.get("status") != "ready" or plan.get("unresolved"):
            raise HierarchyMigrationError("Blocked migration cannot be applied")
        current = self.plan(write_manifest=False, migration_id=migration_id)
        if current["source_digest"] != plan.get("source_digest"):
            raise HierarchyMigrationError("Legacy source changed after dry-run")
        final_skills = self.root / "skills"
        if final_skills.exists() and any(final_skills.iterdir()):
            raise HierarchyMigrationError("Target Skill hierarchy is not empty")
        if final_skills.exists() and final_skills.is_symlink():
            raise HierarchyMigrationError("Target Skill hierarchy is unsafe")

        staging = self.root / f".hierarchy-stage-{migration_id}"
        if staging.exists():
            raise HierarchyMigrationError("Migration staging directory exists")
        journal_path = self.root / "migrations" / migration_id / "manifest.json"
        plan["mode"] = "apply"
        plan["status"] = "applying"
        plan["updated_at"] = utc_now()
        atomic_write_json(journal_path, plan)
        moves: list[JsonObject] = []
        committed = False
        try:
            repository = SkillHierarchyRepository(staging)
            repository.ensure()
            self._apply_revisions(repository, plan)
            self._apply_execution_sets(repository, plan)
            self._apply_executions(repository, plan, moves)
            self._apply_single_analyses(repository, plan, moves)
            self._apply_multi_analyses(repository, plan, moves)
            repository.rebuild_indexes()
            self._verify_staged(repository, plan)
            if final_skills.exists():
                final_skills.rmdir()
            os.replace(staging / "skills", final_skills)
            catalog = staging / "catalog.json"
            if catalog.is_file():
                os.replace(catalog, self.root / "catalog.json")
            staging_migrations = staging / "migrations"
            if staging_migrations.is_dir() and not any(staging_migrations.iterdir()):
                staging_migrations.rmdir()
            staging.rmdir()
            committed = True
            self._remove_empty_legacy_roots()
            SkillHierarchyRepository(self.root).mark_cutover_complete(
                migration_id=migration_id,
                disposition={
                    "execution_count": plan["counts"]["executions"],
                    "single_analysis_count": plan["counts"]["single_analyses"],
                    "multi_analysis_count": plan["counts"]["multi_analyses"],
                    "unresolved_count": 0,
                },
            )
            plan["operations"] = moves
            plan["status"] = "completed"
            plan["updated_at"] = utc_now()
            plan["verification"] = {
                **dict(plan["verification"]),
                "preserved_payload_hashes": True,
                "orphan_references": False,
                "rollback_available": False,
            }
            atomic_write_json(journal_path, plan)
            return plan
        except Exception as error:
            if not committed:
                for move in reversed(moves):
                    source = self.root / str(move["source"])
                    destination = staging / str(move["destination"])
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(source))
                shutil.rmtree(staging, ignore_errors=True)
            plan["operations"] = moves
            plan["status"] = "cutover_failed" if committed else "rolled_back"
            plan["updated_at"] = utc_now()
            plan["error"] = {"type": type(error).__name__, "message": str(error)}
            atomic_write_json(journal_path, plan)
            raise HierarchyMigrationError(
                (
                    f"Migration committed but cleanup failed: {error}"
                    if committed
                    else f"Migration failed and was rolled back: {error}"
                )
            ) from error

    def _plan_execution(
        self,
        campaign_directory: Path,
        campaign_id: str,
        skill_id: str,
        run: object,
    ) -> JsonObject:
        if not isinstance(run, Mapping):
            return self._unresolved(campaign_directory, "Run entry is invalid")
        run_id = run.get("run_id")
        relative = run.get("path")
        if not isinstance(run_id, str) or not isinstance(relative, str):
            return self._unresolved(campaign_directory, "Run identity is invalid")
        source = (campaign_directory / relative).resolve()
        if not source.is_relative_to(campaign_directory.resolve()):
            return self._unresolved(campaign_directory, "Run path escapes Campaign")
        package = source / "artifacts" / "skill"
        if not package.is_dir() or (package / "skill_contract.json").exists():
            return self._unresolved(
                source,
                "Historical Skill snapshot is missing or unexpectedly has a contract",
            )
        try:
            digest, inventory = package_digest(package)
        except HierarchyError as error:
            return self._unresolved(source, str(error))
        revision_id = f"rev-{digest[:16]}"
        return {
            "execution_id": run_id,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "source": self._relative(source),
            "destination": f"skills/{skill_id}/executions/{run_id}/payload",
            "legacy_campaign_id": campaign_id,
            "legacy_run": deepcopy(dict(run)),
            "source_inventory": _inventory(source),
            "revision": {
                "skill_id": skill_id,
                "revision_id": revision_id,
                "package_sha256": digest,
                "source": self._relative(package),
                "destination": f"skills/{skill_id}/revisions/{revision_id}",
                "inventory": inventory,
                "contract_status": "missing_at_execution",
            },
        }

    def _plan_standalone_trajectories(
        self,
        objects: JsonObject,
        unresolved: list[JsonObject],
        run_map: dict[str, JsonObject],
        revision_map: dict[tuple[str, str], JsonObject],
    ) -> None:
        """Map complete pre-Campaign trajectories that retained a frozen Skill."""

        root = self.root / "trajectories"
        if not root.is_dir():
            return
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            trajectory_path = next(
                (
                    candidate
                    for candidate in (
                        directory / "trajectory.jsonl",
                        directory / "trace.jsonl",
                    )
                    if candidate.is_file()
                ),
                None,
            )
            package = directory / "artifacts" / "skill"
            if trajectory_path is None or not package.is_dir():
                unresolved.append(
                    self._unresolved(
                        directory,
                        "Standalone run lacks a complete Trajectory or frozen Skill",
                    )
                )
                continue
            try:
                with trajectory_path.open("r", encoding="utf-8") as stream:
                    first_line = stream.readline()
                first = json.loads(first_line)
            except (OSError, json.JSONDecodeError) as error:
                unresolved.append(
                    self._unresolved(directory, f"Standalone Trajectory is invalid: {error}")
                )
                continue
            payload = first.get("payload") if isinstance(first, Mapping) else None
            trajectory_manifest = (
                payload.get("manifest") if isinstance(payload, Mapping) else None
            )
            run_id = first.get("run_id") if isinstance(first, Mapping) else None
            skill = (
                trajectory_manifest.get("skill")
                if isinstance(trajectory_manifest, Mapping)
                else None
            )
            source = skill.get("source_path") if isinstance(skill, Mapping) else None
            skill_id = self._resolve_skill_id(source)
            if run_id != directory.name or skill_id is None:
                unresolved.append(
                    self._unresolved(
                        directory,
                        "Standalone run identity or Skill mapping is invalid",
                    )
                )
                continue
            if run_id in run_map:
                unresolved.append(
                    self._unresolved(directory, "Standalone Execution is duplicated")
                )
                continue
            try:
                digest, inventory = package_digest(package)
            except HierarchyError as error:
                unresolved.append(self._unresolved(directory, str(error)))
                continue
            revision_id = f"rev-{digest[:16]}"
            revision = {
                "skill_id": skill_id,
                "revision_id": revision_id,
                "package_sha256": digest,
                "source": self._relative(package),
                "destination": f"skills/{skill_id}/revisions/{revision_id}",
                "inventory": inventory,
                "contract_status": (
                    "present"
                    if (package / "skill_contract.json").is_file()
                    else "missing_at_execution"
                ),
            }
            revision_map.setdefault((skill_id, revision_id), revision)
            execution: JsonObject = {
                "execution_id": run_id,
                "skill_id": skill_id,
                "revision_id": revision_id,
                "source": self._relative(directory),
                "destination": f"skills/{skill_id}/executions/{run_id}/payload",
                "origin": "direct",
                "execution_set_id": None,
                "legacy_campaign_id": None,
                "legacy_run": {"run_id": run_id, "path": "."},
                "source_inventory": _inventory(directory),
            }
            objects["executions"].append(execution)
            run_map[run_id] = execution

    def _plan_prechecks(
        self,
        objects: JsonObject,
        unresolved: list[JsonObject],
        run_map: Mapping[str, JsonObject],
    ) -> None:
        root = self.root / "analyses" / "trajectory-prechecks"
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*.json")):
            run_id = path.stem
            execution = run_map.get(run_id)
            if execution is None:
                unresolved.append(self._unresolved(path, "Precheck has no Execution"))
                continue
            analysis_id = f"precheck-legacy-{run_id}"
            objects["single_analyses"].append(
                {
                    "analysis_id": analysis_id,
                    "kind": "precheck",
                    "status": "accepted",
                    "skill_id": execution["skill_id"],
                    "revision_id": execution["revision_id"],
                    "execution_id": run_id,
                    "source": self._relative(path),
                    "destination": (
                        f"skills/{execution['skill_id']}/executions/{run_id}/"
                        f"analyses/single/{analysis_id}/result.json"
                    ),
                    "source_inventory": _inventory(path.parent),
                }
            )

    def _plan_single_trajectory_agent_runs(
        self,
        objects: JsonObject,
        unresolved: list[JsonObject],
        run_map: Mapping[str, JsonObject],
    ) -> None:
        root = self.root / "analyses" / "agent-runs"
        if not root.is_dir():
            return
        claimed_evidence: set[str] = set()
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            context_path = directory / "workspace" / "context.json"
            manifest_path = directory / "manifest.json"
            if not context_path.is_file() or not manifest_path.is_file():
                unresolved.append(
                    self._unresolved(directory, "AgentRun context missing")
                )
                continue
            context = load_json_object(context_path)
            run_id = context.get("run_id")
            analysis_id = context.get("analysis_id")
            execution = run_map.get(str(run_id))
            if execution is None or not isinstance(analysis_id, str):
                unresolved.append(
                    self._unresolved(directory, "AgentRun subject unresolved")
                )
                continue
            agent_manifest = load_json_object(manifest_path)
            user_report = directory / "user-report.json"
            old_status = agent_manifest.get("status")
            status = (
                old_status
                if old_status in {"invalid_output", "failed", "timed_out"}
                else "accepted"
            )
            evidence_id = context.get("evidence_bundle_id")
            evidence_source = None
            if isinstance(evidence_id, str):
                candidate = self.root / "analyses" / "evidence-bundles" / evidence_id
                if candidate.is_dir():
                    evidence_source = self._relative(candidate)
                    claimed_evidence.add(evidence_id)
            objects["single_analyses"].append(
                {
                    "analysis_id": analysis_id,
                    "kind": "trajectory_error",
                    "status": status,
                    "skill_id": execution["skill_id"],
                    "revision_id": execution["revision_id"],
                    "execution_id": run_id,
                    "source": self._relative(directory),
                    "destination": (
                        f"skills/{execution['skill_id']}/executions/{run_id}/"
                        f"analyses/single/{analysis_id}/attempts/{directory.name}"
                    ),
                    "evidence_source": evidence_source,
                    "has_user_report": user_report.is_file(),
                    "source_inventory": _inventory(directory),
                }
            )
        evidence_root = self.root / "analyses" / "evidence-bundles"
        if evidence_root.is_dir():
            for directory in sorted(evidence_root.iterdir()):
                if directory.is_dir() and directory.name not in claimed_evidence:
                    if self._evidence_used_by_multi_analysis(directory):
                        continue
                    unresolved.append(
                        self._unresolved(
                            directory,
                            "Evidence bundle has no analysis owner",
                        )
                    )

    def _plan_multi_analyses(
        self,
        objects: JsonObject,
        unresolved: list[JsonObject],
        campaign_map: Mapping[str, JsonObject],
    ) -> None:
        harness_root = self.root / "harness-runs"
        if harness_root.is_dir():
            for directory in sorted(harness_root.iterdir()):
                manifest_path = directory / "harness.json"
                if not directory.is_dir() or not manifest_path.is_file():
                    continue
                manifest = load_json_object(manifest_path)
                source = manifest.get("source")
                campaign_id = (
                    source.get("campaign_id") if isinstance(source, Mapping) else None
                )
                execution_set = campaign_map.get(str(campaign_id))
                if execution_set is None:
                    unresolved.append(
                        self._unresolved(
                            directory, "Harness Campaign unresolved"
                        )
                    )
                    continue
                analysis_id = f"harness-legacy-{directory.name}"
                objects["multi_analyses"].append(
                    self._multi_plan(
                        analysis_id,
                        "harness",
                        directory,
                        execution_set,
                        manifest.get("status"),
                    )
                )
        campaigns_root = self.root / "analyses" / "campaigns"
        if campaigns_root.is_dir():
            for directory in sorted(campaigns_root.iterdir()):
                manifest_path = directory / "manifest.json"
                if not directory.is_dir() or not manifest_path.is_file():
                    continue
                manifest = load_json_object(manifest_path)
                campaign_id = manifest.get("replay_campaign_id")
                execution_set = campaign_map.get(str(campaign_id))
                if execution_set is None:
                    unresolved.append(
                        self._unresolved(directory, "Analysis Campaign unresolved")
                    )
                    continue
                analysis_id = str(manifest.get("id", directory.name))
                plan = self._multi_plan(
                    analysis_id,
                    "multi_role",
                    directory,
                    execution_set,
                    manifest.get("status"),
                )
                evidence = manifest.get("evidence_bundle")
                if isinstance(evidence, str):
                    evidence_path = Path(evidence).resolve()
                    if (
                        evidence_path.is_dir()
                        and evidence_path.is_relative_to(self.root)
                    ):
                        plan["evidence_source"] = self._relative(evidence_path)
                objects["multi_analyses"].append(plan)

    def _multi_plan(
        self,
        analysis_id: str,
        kind: str,
        source: Path,
        execution_set: Mapping[str, Any],
        old_status: object,
    ) -> JsonObject:
        accepted = old_status in {"completed", "accepted"}
        return {
            "analysis_id": analysis_id,
            "kind": kind,
            "status": "accepted" if accepted else "inconclusive",
            "skill_id": execution_set["skill_id"],
            "revision_id": execution_set["revision_id"],
            "execution_set_id": execution_set["set_id"],
            "source": self._relative(source),
            "destination": (
                f"skills/{execution_set['skill_id']}/execution-sets/"
                f"{execution_set['set_id']}/analyses/{analysis_id}/payload"
            ),
            "source_inventory": _inventory(source),
        }

    def _scan_unclaimed_roots(
        self,
        objects: Mapping[str, Any],
        unresolved: list[JsonObject],
    ) -> None:
        claimed = {
            str(item.get("source"))
            for values in objects.values()
            if isinstance(values, list)
            for item in values
            if isinstance(item, Mapping) and item.get("source")
        }
        claimed.update(
            str(item.get("evidence_source"))
            for values in objects.values()
            if isinstance(values, list)
            for item in values
            if isinstance(item, Mapping) and item.get("evidence_source")
        )
        for root_name in ("spikes", "trajectories"):
            root = self.root / root_name
            if not root.is_dir():
                continue
            for directory in root.iterdir():
                if directory.name == ".DS_Store":
                    continue
                relative = self._relative(directory)
                if relative not in claimed:
                    unresolved.append(
                        self._unresolved(
                            directory,
                            "Legacy exploratory data is not a complete new Execution",
                        )
                    )
        for root_name in (
            "candidates",
            "comparisons",
            "reviews",
            "experiment-requests",
        ):
            root = self.root / root_name
            if root.is_dir():
                for directory in root.iterdir():
                    relative = self._relative(directory)
                    if directory.name != ".DS_Store" and relative not in claimed:
                        unresolved.append(
                            self._unresolved(
                                directory,
                                "Improvement object is not mapped",
                            )
                        )

    def _resolve_skill_id(self, source: object) -> str | None:
        if not isinstance(source, str) or not source:
            return None
        normalized = source.replace("\\", "/").rstrip("/")
        matches = [
            str(mapping["skill_id"])
            for mapping in self.mappings.get("mappings", [])
            if isinstance(mapping, Mapping)
            for suffix in mapping.get("source_suffixes", [])
            if normalized.endswith(str(suffix).rstrip("/"))
        ]
        return matches[0] if len(set(matches)) == 1 else None

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise HierarchyMigrationError(f"Path leaves runtime root: {path}")
        return resolved.relative_to(self.root).as_posix()

    def _unresolved(self, path: Path, reason: str) -> JsonObject:
        return {"source": self._relative(path), "error": reason}

    def _evidence_used_by_multi_analysis(self, directory: Path) -> bool:
        campaigns = self.root / "analyses" / "campaigns"
        if not campaigns.is_dir():
            return False
        for path in campaigns.glob("*/manifest.json"):
            try:
                manifest = load_json_object(path)
            except Exception:
                continue
            evidence = manifest.get("evidence_bundle")
            if (
                isinstance(evidence, str)
                and Path(evidence).resolve() == directory.resolve()
            ):
                return True
        return False

    def _apply_revisions(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
    ) -> None:
        for item in plan["objects"]["revisions"]:
            revision = repository.register_revision(
                self.root / item["source"],
                lifecycle="historical",
                legacy_skill_id=item["skill_id"],
                legacy_identity={
                    "method": "approved_migration_mapping",
                    "source": item["source"],
                    "migration_id": plan["migration_id"],
                },
            )
            if revision.manifest["revision_id"] != item["revision_id"]:
                raise HierarchyMigrationError("Revision digest changed")

    def _apply_execution_sets(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
    ) -> None:
        for item in plan["objects"]["execution_sets"]:
            source_manifest = item["source_manifest"]
            repository.create_execution_set(
                skill_id=item["skill_id"],
                revision_id=item["revision_id"],
                purpose="replay",
                task=dict(source_manifest.get("task", {})),
                runtime=dict(source_manifest.get("execution", {})),
                provenance={
                    "legacy_campaign_id": item["legacy_campaign_id"],
                    "migration_id": plan["migration_id"],
                },
                set_id=item["set_id"],
                status="completed",
            )

    def _apply_executions(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
        moves: list[JsonObject],
    ) -> None:
        for item in plan["objects"]["executions"]:
            execution_directory = repository.execution_directory(
                item["skill_id"], item["execution_id"]
            )
            execution_directory.mkdir(parents=True)
            destination = execution_directory / "payload"
            self._move_into_stage(item["source"], destination, repository.root, moves)
            manifest = execution_manifest_from_payload(
                execution_directory=execution_directory,
                skill_id=item["skill_id"],
                revision_id=item["revision_id"],
                execution_id=item["execution_id"],
                origin=item.get("origin", "replay"),
                execution_set_id=item["execution_set_id"],
                legacy={
                    **(
                        {"campaign_id": item["legacy_campaign_id"]}
                        if item.get("legacy_campaign_id")
                        else {}
                    ),
                    "run": item["legacy_run"],
                    "migration_id": plan["migration_id"],
                },
            )
            atomic_write_json(execution_directory / "execution.json", manifest)
        for item in plan["objects"]["execution_sets"]:
            value = repository.load_execution_set(item["skill_id"], item["set_id"])
            value["execution_ids"] = item["execution_ids"]
            value["ended_at"] = item["source_manifest"].get("ended_at")
            statuses = [
                repository.load_execution(item["skill_id"], execution_id)["status"]
                for execution_id in item["execution_ids"]
            ]
            value["status"] = (
                "completed" if all(status == "succeeded" for status in statuses)
                else "completed_with_failures"
            )
            repository.replace_execution_set(item["skill_id"], item["set_id"], value)
            set_directory = repository.execution_set_directory(
                item["skill_id"], item["set_id"]
            )
            legacy = set_directory / "legacy"
            legacy.mkdir()
            campaign_source = self.root / item["source"]
            for name in ("replay.json", "prompt"):
                source = campaign_source / name
                if source.exists():
                    self._move_into_stage(
                        self._relative(source), legacy / name, repository.root, moves
                    )

    def _apply_single_analyses(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
        moves: list[JsonObject],
    ) -> None:
        for item in plan["objects"]["single_analyses"]:
            manifest = self._analysis_manifest(item, plan, scope="single_execution")
            directory, _ = repository.create_analysis(manifest)
            if item["kind"] == "precheck":
                self._move_into_stage(
                    item["source"], directory / "result.json", repository.root, moves
                )
                result_refs = [{"path": "result.json", "schema": "trajectory.precheck.v1"}]
                attempts: list[JsonObject] = []
            else:
                attempt_name = Path(item["source"]).name
                self._move_into_stage(
                    item["source"],
                    directory / "attempts" / attempt_name,
                    repository.root,
                    moves,
                )
                evidence_source = item.get("evidence_source")
                if evidence_source:
                    self._move_into_stage(
                        evidence_source,
                        directory / "evidence" / Path(evidence_source).name,
                        repository.root,
                        moves,
                    )
                report = f"attempts/{attempt_name}/user-report.json"
                result_refs = (
                    [{"path": report, "schema": "analysis.single_trajectory_view.v1"}]
                    if item.get("has_user_report")
                    else []
                )
                attempts = [
                    {
                        "agent_run_id": attempt_name,
                        "path": f"attempts/{attempt_name}",
                        "status": item["status"],
                    }
                ]
            manifest["result_refs"] = result_refs
            manifest["attempts"] = attempts
            repository.replace_analysis(manifest)

    def _apply_multi_analyses(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
        moves: list[JsonObject],
    ) -> None:
        for item in plan["objects"]["multi_analyses"]:
            manifest = self._analysis_manifest(item, plan, scope="execution_set")
            directory, _ = repository.create_analysis(manifest)
            self._move_into_stage(
                item["source"], directory / "payload", repository.root, moves
            )
            evidence_source = item.get("evidence_source")
            if evidence_source and (self.root / evidence_source).exists():
                self._move_into_stage(
                    evidence_source,
                    directory / "evidence" / Path(evidence_source).name,
                    repository.root,
                    moves,
                )
            manifest["result_refs"] = [
                {"path": "payload", "schema": "legacy.execution_set_check"}
            ]
            repository.replace_analysis(manifest)

    def _analysis_manifest(
        self,
        item: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        scope: str,
    ) -> JsonObject:
        value: JsonObject = {
            "schema": ANALYSIS_RECORD_SCHEMA,
            "analysis_id": item["analysis_id"],
            "skill_id": item["skill_id"],
            "revision_id": item["revision_id"],
            "scope": scope,
            "execution_id": item.get("execution_id"),
            "execution_set_id": item.get("execution_set_id"),
            "kind": item["kind"],
            "producer": "deterministic" if item["kind"] == "precheck" else "composite",
            "status": item["status"],
            "input_refs": [],
            "result_refs": [],
            "attempts": [],
            "created_at": utc_now(),
            "ended_at": utc_now(),
            "provenance": {
                "legacy_source": item["source"],
                "migration_id": plan["migration_id"],
            },
        }
        return validate_analysis_record(value)

    def _move_into_stage(
        self,
        source_relative: str,
        destination: Path,
        staging_root: Path,
        moves: list[JsonObject],
    ) -> None:
        source = self.root / source_relative
        if not source.exists():
            raise HierarchyMigrationError(
                f"Migration source is missing: {source_relative}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise HierarchyMigrationError(f"Migration target exists: {destination}")
        shutil.move(str(source), str(destination))
        moves.append(
            {
                "source": source_relative,
                "destination": destination.relative_to(staging_root).as_posix(),
                "reverse": True,
            }
        )

    def _verify_staged(
        self,
        repository: SkillHierarchyRepository,
        plan: Mapping[str, Any],
    ) -> None:
        for item in plan["objects"]["executions"]:
            execution = repository.load_execution(
                item["skill_id"], item["execution_id"]
            )
            directory = repository.execution_directory(
                item["skill_id"], item["execution_id"]
            )
            actual = _inventory(directory / "payload")
            expected = item["source_inventory"]
            if actual != expected:
                raise HierarchyMigrationError(
                    f"Payload hash mismatch for {execution['execution_id']}"
                )
        repository.rebuild_indexes()

    def _remove_empty_legacy_roots(self) -> None:
        for root_name in LEGACY_ROOTS:
            root = self.root / root_name
            if not root.is_dir():
                continue
            metadata = root / ".DS_Store"
            if metadata.is_file() and not metadata.is_symlink():
                metadata.unlink()
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            if root.is_dir() and not any(root.iterdir()):
                root.rmdir()
