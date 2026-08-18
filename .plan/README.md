# Planning workspace

> Purpose: define how future work is collected, merged, and prioritized.

`.plan/` separates future intent from the current implementation and from
maintained technical documentation. It has one authoritative active plan:
`next.md`. Other Markdown documents retain source proposals, deferred ideas,
or superseded planning context.

## Merge and priority protocol

Before beginning a major development effort, inspect every plan document in
this directory. When more than one plan exists:

1. Extract each proposed outcome, dependency, risk, owner approval requirement,
   and implementation state.
2. Merge compatible work into `next.md`; do not lose a proposal merely because
   it is deferred.
3. Order work by safety/approval gates first, then blocking dependencies, then
   expected value and cost. Record conflicts and the reason one item is
   deferred.
4. Keep `next.md` short, actionable, and limited to the current ordered work.

At major-development completion, reconcile the plan inventory again: mark
finished items, move deferred detail to a named supporting plan when useful,
and update priorities to reflect the new state. A future-plan document is not
evidence that its architecture has been accepted or implemented.
