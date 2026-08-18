# 0001 — Pi runs behind a Python RPC process boundary

Status: Accepted
Date: 2026-07-23
Owners: project owner

## Context

The framework is implemented in Python while Pi is distributed as a Node.js
coding-agent CLI. The requested integration mode is a subprocess using Pi's
JSONL RPC protocol.

## Decision

Use a Python-owned subprocess boundary. Pi communicates only through
LF-delimited JSON objects on stdin/stdout; stderr is captured separately.
Framework domain code must not depend directly on Pi event shapes and will use
an adapter in the architecture selected for the next phase.

The low-level standalone client may default to `--no-session` for smoke tests.
The framework's trajectory capture adapter must instead enable a per-run Pi
session directory. Pi events and the resulting session entries are persisted
inside the framework trajectory; neither replaces the framework trajectory as
the canonical object.

## Alternatives considered

- Embed the TypeScript SDK: stronger in-process typing, but it would move the
  orchestration boundary into Node.js and does not meet the requested Python
  subprocess constraint.
- Parse interactive terminal output: unstable, lossy, and unnecessary because
  Pi provides a machine protocol.

## Consequences

- Process crashes, malformed records, timeouts, and event correlation must be
  explicit failure modes.
- The adapter can be replaced by another agent runtime later.
- The capture adapter must manage and archive a Pi session for each run.
- Standalone RPC smoke tests may continue without session persistence.

## Revisit when

Reconsider if Python process overhead becomes material, RPC lacks a required
extension point, or orchestration moves primarily to TypeScript.
