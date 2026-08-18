# 0033 — 形式完备性缺口记录为警告而非拒收

> Purpose: record the owner decision that formal completeness gaps (such as a
> missing run_id+seq evidence for some observed Trajectories) must not discard a
> content-coherent report; they are recorded and saved with the analysis.

Status: Accepted
Date: 2026-08-17
Owners: project owner

## Context

The second real E1 subagent report was rejected as `invalid_output` because its
resource dimension declared `observed = 5` Trajectories but cited `run_id + seq`
evidence for only two of them, relying on `baseline.json` (a report path) for the
other three. The report's content was coherent and the per-run data was present in
the evidence (baseline.json was read 19 times), but the inline schema had omitted
the rule "every observed Trajectory needs at least one run_id+seq evidence ref;
report_path does not count". The owner directed that such formal gaps should be
recorded and saved, not rejected: user display is unaffected by the missing
citations.

## Decision

- The evidence-coverage rule is documented in the protocol and demoted from a
  hard rejection to a recorded warning: when a submission is otherwise
  content-coherent but some `observed` Trajectories lack a run_id+seq evidence
  ref in a dimension, the runtime accepts the submission, marks it succeeded,
  and records the gap in `validation_warnings`.
- The product publish carries those warnings into the saved multi-Trajectory
  analysis, so the analysis still appears to users with the warning retained in
  the backend.
- Hard rejections remain for content/integrity violations: wrong schema or role,
  wrong corpus/baseline digest, unknown dimension names, `observed`/`checked_absent`
  not partitioning the eligible denominator, confidence outside 0..1, and
  `derivation_ids` referencing anything outside this session.
- Cited evidence must still exist (a run_id+seq that does not resolve remains an
  error); this relaxation only downgrades *missing* coverage, not *false* evidence.

## Alternatives considered

- Keep the strict rejection: discards a coherent report over a citation gap and
  leaves the product with a missing report; owner rejected.
- Demote all formal checks to warnings: would weaken the denominator and
  evidence-truth guarantees; scoped to the evidence-coverage gap only for now.

## Consequences

- The second-run E1 report would be accepted with a warning instead of dropped;
  the product multi-Trajectory analysis can then carry all three reports.

## Revisit when

The owner decides whether other formal-completeness checks (e.g. the exact
denominator partition) should also become warnings, or whether a separate
review/severity tier is needed.
