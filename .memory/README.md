# Project Memory

`.memory/` is the repository's durable engineering memory. It records context
that future contributors and agents need in order to avoid repeating mistakes
or silently reversing decisions.

It is not a transcript, scratchpad, telemetry store, or a place for hidden
reasoning. Keep entries concise, factual, reviewable, and safe to commit.

## Layout

- `current.md` — current implementation state, next action, and active risks.
- `decisions/` — accepted decision records. Copy `TEMPLATE.md` for new entries.
- `problems/` — diagnosed development problems. Copy `TEMPLATE.md` for new
  entries.

Use the next four-digit sequence number in each directory. A decision begins as
`Proposed` and changes to `Accepted` only after the project owner approves it.

