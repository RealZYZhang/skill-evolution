# 0028 — 恢复 Trajectory 为统一命名并反向兼容 Trace

> Purpose: record the owner-directed reversal of decision 0021 and the
> resulting legacy boundary for data created under the intermediate name.

Status: Accepted
Date: 2026-08-14
Owners: project owner

## Context

Decision `0021` (2026-08-07) unified the repository on the term `trace`
(trace.jsonl, trace.actions.v1, trace_started, Trace*, trace_*). The project
owner subsequently directed that all current code, commands, schemas,
directories, prompts, tests, and documentation use `trajectory` instead.
Frozen runtime evidence, the content-addressed internal corpus, manifests,
hashes, and audit chains must not be rewritten.

## Decision

- Current code, types, commands, directories, schema identifiers, prompts,
  web assets, tests, and documentation use `trajectory` (trajectory.jsonl,
  trajectory.actions.v1, trajectory.profile.v1, trajectory_started /
  finished / sealed, Trajectory*, trajectory_*, multi-trajectory).
- New writers produce only `trajectory` names; nothing is double-written.
- Readers accept both `trajectory` (canonical) and `trace` (legacy from the
  `0021` era) for file names, boundary records, action/profile schemas,
  request types, analysis kinds, corpus purposes, and the top-level
  `trace` field in frozen skill.execution.v1 records. The compatibility
  layer projects legacy inputs onto the canonical model and keeps
  source_format / source_schema markers.
- Frozen runtime evidence (`.skill-evolution/`), historical
  decision/problem records, and vendored third-party Pi documentation are
  not rewritten.
- `0021` remains on record as the historical decision it was; this record
  reverses its naming direction.

## Alternatives considered

- Keep `trace`: owner explicitly directed the reverse, so not selected.
- Rewrite frozen evidence in place: would break content-addressed digests,
  manifest paths, and audit chains; rejected for the same reason `0021`
  refused to rewrite its predecessors.
- Keep dual writers: would create two authoritative sources; rejected.

## Consequences

- All new interfaces expose one vocabulary (`trajectory`); `trace` exists
  only in the legacy reader, compatibility tests, and frozen evidence.
- Frozen executions, analyses, and the internal corpus remain readable
  without migration.
- Approved production prompts (trajectory-error-analysis-v1/v2) had their
  approval sidecars recomputed for the renamed content in the same change.

## Revisit when

All frozen evidence that still carries `trace` names has completed a
verifiable, reversible offline migration and no manifest, hash, or external
reference depends on the intermediate names; only then may the legacy reader
be narrowed.
