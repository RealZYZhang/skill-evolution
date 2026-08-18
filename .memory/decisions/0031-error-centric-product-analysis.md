# 0031 — 以“错误”为单元的主—子 Agent 产品级分析

> Purpose: record the owner-directed rework that makes multi-Trajectory analysis
> error-centric and user-visible, replacing the four isolated dimension roles
> with a main agent that identifies errors and one subagent per error.

Status: Accepted
Date: 2026-08-15
Owners: project owner

## Context

Decision `0027` ran four dimension specialists (BehaviorPattern,
ConditionsCoverage, OutcomeConsistency, ResourceEfficiency) in isolation over the
whole corpus, with no synthesis. The owner observed that partitioning by
*dimension* scatters one error across four unrelated reports, breaking the
cross-dimension consistency of that error, and directed that the analysis unit
be the *error*. The owner further directed that this analysis is the product
itself (user-visible), not an internal research-only layer.

## Decision

- A main agent identifies all possible errors from the raw material and emits a
  structured error list.
- For each identified error the main agent spawns exactly one subagent. The
  subagent receives the structured error description plus the four dimension
  protocols merged into one prompt, re-derives the error from the raw material,
  and returns one complete report per error.
- A single error's report surfaces only the dimensions that actually show a
  problem; it does not force coverage of all four dimensions.
- The main agent's identify results are trusted as-is; there is no independent
  review of the error list.
- The deliverable is the error list plus one report per error, exposed as a
  user-visible product-layer artifact (multi-Trajectory analysis). There is no
  separate “research-only” output.
- The four former role protocols become dimension modules inside a single-error
  analysis. The submission schema becomes per-error, permitting multiple finding
  types and a subset of dimensions per error.
- Decision `0029` still applies: analysis works on the data as-is and records
  scope and covered conditions; missing data is a recorded limitation, not a
  block.

## Alternatives considered

- Keep dimension-partitioned specialists: owner rejected; it breaks per-error
  cross-dimension consistency.
- Independent review of the identify step: owner rejected; the identify list is
  trusted.

## Consequences

- Product multi-Trajectory analysis becomes a real user-visible feature; the
  “product multi-Trajectory count stays zero” constraint no longer applies.
- New pieces: a main-agent identify protocol with a structured error
  description, a merged single-error analysis prompt (four dimension modules),
  a per-error result schema, and a main/sub orchestration path.
- The relationship between this product analysis and the earlier capability
  certificate / two-session blind smoke machinery is left open here and is
  resolved separately (see Revisit).

## Revisit when

The capability-certificate and blind-smoke gates are reconciled with the
product error-centric flow; then this record is updated or a follow-on decision
records their fate.
