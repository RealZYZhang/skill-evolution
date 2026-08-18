# 0024 — Historical Skill hierarchy cutover disposition

> Purpose: record the accepted migration rule, completed cutover, and treatment
> of legacy records that cannot satisfy the Execution definition.

Status: Accepted and applied
Date: 2026-08-09
Owners: project owner

## Context

The owner instructed the framework to migrate old data when the new definition
can be reconstructed, and to delete old data when required new facts cannot be
extracted. The historical runtime contained two Replay batches, one standalone
Trajectory, and four pre-canonical spikes.

## Decision

- Preserve both Replay batches and all ten Executions, including five failures.
- Convert the standalone historical run because it retained a frozen Skill
  package and a sealed canonical Trajectory.
- Preserve exact analysis terminal states, especially five `invalid_output`
  semantic attempts.
- Register the current approved Skill package as a separate active Revision;
  do not apply its Contract retroactively to the historical Revision.
- Exclude the four pre-canonical spikes from product data because they do not
  retain both the immutable Skill package actually used and a sealed canonical
  Trajectory. They cannot truthfully become Executions.
- Keep those four records out of product data. They may remain in a recoverable
  migration quarantine only until explicit path-level permanent-deletion
  approval is available. Quarantine is not a product archive and is not
  indexed by the Viewer.
- Use an explicit completed-cutover marker to make the hierarchy authoritative.
  Partial hierarchy data never changes compatibility routing.

## Applied result

Migration `hierarchy-migration-20260809-cutover` verified 340 source files and
completed with one Skill, two Revisions, two Execution Sets, eleven Executions,
ten single analyses, and seven multi/Harness analyses. Payload hashes matched,
and no orphan reference remained. On 2026-08-10, the owner explicitly approved
permanent deletion of the exact four quarantined directories. They were deleted
and the quarantine directory was removed after confirming it was empty.

## Consequences

- The Skill Explorer is the authoritative UI for the current runtime.
- Legacy Campaign endpoints are read-only projections from Execution Sets.
- The four deleted spikes cannot be relabeled as normal Executions, restored to
  product navigation, or recovered from the runtime.

## 2026-08-11 correction

The migration count of seven “multi/Harness analyses” described preserved
records, not seven completed multi-trajectory analyses. Decision `0025` removes
Harness and unfinished multi-role attempts from the multi-trajectory product path.
The current multi-trajectory count is zero; permanent deletion of all seven old
source records awaits explicit authorization covering the complete list.
