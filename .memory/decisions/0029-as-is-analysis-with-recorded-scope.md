# 0029 — 分析对象为“as-is”：不因数据不完备而阻断，只记录范围与覆盖条件

> Purpose: record the owner-directed principle that multi-Trajectory analysis
> operates on whatever frozen data exists, and completeness is recorded as a
> fact rather than enforced as a precondition.

Status: Accepted
Date: 2026-08-15
Owners: project owner

## Context

Decision `0027` sequences Specialist research behind deterministic Harness
acceptance and a two-session capability certificate. The workflow additionally
called `_require_full_readiness`, which refused to start the Specialist phase
until the corpus declared every research objective and a non-null coverage
fact. The frozen five-Trajectory corpus declares only the behavior-patterns
objective (coverage null, no comparable groups, no EvaluationSuite snapshot),
so that gate would block all four Specialists even though three of them have
usable data and the fourth can only report limitations. The owner directed
that analysis must not wait for complete conditions: analyze the data as-is
and record the scope and covered conditions.

## Decision

- Multi-Trajectory analysis operates on the frozen corpus exactly as it is.
  Absence of data is not a reason to block a role; it is a fact the role
  records.
- Each Specialist records what it actually covered: `research_scope`
  (eligible and reviewed Trajectories, counterexample search), the covered
  and checked-absent conditions per finding, and overall `limitations`.
  Missing comparable groups, missing TaskCase/condition mapping, and missing
  EvaluationSuite coverage are reported as limitations or
  `insufficient_condition_evidence` / `coverage_gap`, never as
  performance claims.
- Completeness gates are removed from the Specialist startup path. A null
  coverage fact and a partial objective list in `readiness.json` are valid
  recorded facts, not errors.
- The as-is principle applies to *data completeness* only. The safety and
  certification gates are unchanged: Harness acceptance, hidden-benchmark
  review, the two-session capability certificate, evidence binding, and
  sandbox/identity checks still gate the Specialist phase.

## Alternatives considered

- Keep the full-readiness gate: blocks analysis the owner wants and rejects a
  valid partial corpus; not selected.
- Auto-reject roles whose data is absent instead of running them as-is: loses
  the recorded limitation that explains *why* coverage is incomplete; not
  selected.

## Consequences

- The Specialist phase no longer requires all objectives or a non-null
  coverage fact; roles run on the data present and their reports carry the
  scope and coverage they achieved.
- `readiness.json` continues to record objectives and coverage as facts
  (coverage may be null when no comparable condition groups exist).
- The ConditionsCoverage protocol's “if the corpus lacks an approved suite
  snapshot or stable Trajectory→TaskCase/conditions mapping, refuse to
  conclude and submit limitations” remains the correct as-is behavior for
  that role and is kept.

## Revisit when

A future requirement needs a hard “sufficient data” guarantee before research
cost is spent; if so, add an explicit opt-in sufficiency threshold rather
than restoring an implicit all-or-nothing gate.
