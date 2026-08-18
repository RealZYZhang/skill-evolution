# Skill Evolution Development Guide

> Purpose: operational rules for people and agents changing this repository.

This repository builds a framework that improves agent skills from execution
trajectories. Treat reproducibility, auditability, and safe evaluation as core
product requirements.

## Before changing code

1. Read `.memory/current.md`, then relevant entries under
   `.memory/decisions/` and `.memory/problems/`.
2. Confirm the requested scope. Do not implement a proposed architecture
   decision until it has been accepted.
3. Inspect the installed Pi version and `docs/pi-agent/README.md` before
   changing the RPC adapter.

## Repository documents, plans, and configuration

The root intentionally contains only two Markdown entry points. Keep their
purposes distinct and keep both current after every major development effort:

| Root file | Purpose | Required update |
| --- | --- | --- |
| `AGENTS.md` | Development, safety, documentation, configuration, and planning rules for contributors and agents. | Update when the workflow, repository policy, or document map changes. |
| `README.md` | Public project introduction, setup, configuration, and runnable commands. | Update when user-facing behavior, prerequisites, commands, or primary entry points change. |

The complete first-party file and directory catalog is
`docs/file-catalog.md`. It is the index for repository structure and each
tracked project's file purpose; do not duplicate that catalog in ad hoc root
documents. Design and operating documentation belongs in `docs/`, not at the
root. Long-horizon or unaccepted work belongs in `.plan/`, not in documents
that describe the current implementation.

Every project-owned, human-readable file needs a concise purpose at its file
header: a Markdown title and purpose line, a Python module docstring (after a
shebang when present), or an HTML/CSS/JS/TS comment. For structured formats
where comments are invalid, use the format's schema/title field and retain its
entry in `docs/file-catalog.md`. Do not change an approved production prompt
solely to add a header: that changes its content hash and requires a new owner
approval.

`config.yaml` is the authoritative, non-secret project configuration. It
currently defines the default Pi provider, model, and thinking mode. API keys
remain only in Pi's user configuration; never add credentials, tokens, or
machine-local paths to `config.yaml`. Per-run task inputs, artifact locations,
and explicit CLI overrides are captured in their run manifests rather than
silently promoted to global configuration.

`.plan/` is the required home for next-step planning. `.plan/next.md` is the
single current, prioritized execution plan. Additional plan documents may
preserve proposals or source material, but whenever more than one plan exists,
the owner of the next major development must compare their scope, dependencies,
risks, and approval state; merge compatible work into `next.md`, sequence
dependencies, and explicitly prioritize conflicting work. Update the plan
inventory and the resulting priorities before implementation begins and again
when the major development work ends.

## Development workflow

1. Make the smallest coherent change that satisfies the requirement.
2. Keep domain logic independent from Pi. Pi-specific behavior belongs in an
   adapter behind a runtime interface.
3. Add or update tests with the code change.
4. Run the narrow tests first, then the complete test suite.
5. Update user-facing documentation and `.memory/current.md`.
6. Record durable decisions or recurring problems in `.memory/`; do not use
   memory files as an unstructured activity log.
7. Store every production prompt as a versioned file. Do not execute it until
   the project owner has reviewed it and its approval sidecar matches the
   current content. Do not introduce ad hoc prompt strings in production CLIs.
   Skill execution templates must inject the complete current `SKILL.md`
   through the single `{{SKILL_CONTENT}}` placeholder.
8. Every executable Skill package must keep its active contract beside
   `SKILL.md` under the fixed name `skill_contract.json`. Contract fields are
   strict and versioned; add future fields through an accepted schema revision,
   not by allowing unknown properties in an existing version.
9. At the end of every major development effort, update `README.md`,
   `docs/file-catalog.md`, `.plan/next.md` (including any plan merge and
   priority decision), and `.memory/current.md`. Update `AGENTS.md` and
   `config.yaml` too when the project workflow or shared defaults changed.

## Python style and comments

- Target Python 3.11 or newer and prefer the standard library for the MVP.
- Use four spaces, a maximum line length of 88, type hints on public APIs, and
  `from __future__ import annotations`.
- Use `snake_case` for functions and modules, `PascalCase` for classes, and
  descriptive names rather than abbreviations.
- Keep functions focused. Separate domain policy from filesystem, process,
  network, and database side effects.
- Public modules, classes, and non-obvious functions require docstrings.
- Comments explain constraints, intent, or a surprising trade-off. Do not
  narrate code that is already clear.
- Never log credentials, full environment mappings, hidden model reasoning, or
  unsanitized user data.

## Reporting to the project owner

Assume the project owner does not know the implementation details unless they
explicitly ask for them. Status reports and handoffs must lead with the design
and functional meaning of the work:

Before every owner-facing progress update, question, or final report, state the
goal being pursued, the user-visible capability affected, the current
functional stage, and the remaining functional gap. Do not lead with internal
objects, code structure, field names, file paths, tools, or implementation
mechanics. Technical detail is secondary and should be included only when the
owner asks for it or needs it to make a decision.

1. Explain what system capability or user-visible behavior now exists.
2. Describe failures in terms of the affected workflow stage, their practical
   impact, and what conclusions can or cannot be trusted.
3. Explain the repair as a design or behavior change, together with the test or
   acceptance condition that will prove it works.
4. Clearly distinguish a process that ran successfully from a result that was
   validated and accepted.
5. Keep class names, schema fields, hashes, internal IDs, parser details, and
   storage paths out of the main explanation. Include them only when the owner
   asks, when they are needed to make a decision, or in a clearly secondary
   technical appendix.

Do not substitute implementation vocabulary for an explanation of outcome,
impact, and next action.

## Documentation

- `README.md` is the project entry point. It records every supported way to run
  the project.
- `docs/file-catalog.md` describes the repository structure and every
  project-owned file's purpose. Update it with every file addition, removal,
  rename, or material responsibility change.
- `docs/` contains maintained project documentation.
- `docs/pi-agent/upstream/` is a versioned vendor snapshot. Do not hand-edit
  files there; replace the snapshot as one operation when upgrading Pi and
  update its provenance metadata.
- Proposed designs must be marked `Proposed`. Accepted designs become decision
  records under `.memory/decisions/`.
- Any behavior, CLI option, storage schema, or public interface change must
  update its documentation in the same change.

## Testing

- Tests live under `tests/` and mirror the production module name.
- Use `unittest` for the dependency-free MVP. Test names describe behavior.
- Unit tests must not call a real model, depend on user credentials, or mutate
  global Pi state.
- Subprocess protocol tests use a fake JSONL child process. Real Pi smoke tests
  are separate and must work without an LLM request where possible.
- Test success, invalid input, timeout/process exit, and boundary cases for each
  public interface.
- Run all tests with:

  ```bash
  python3 -m unittest discover -s tests -v
  ```

## Memory protocol

- `.memory/current.md`: concise current state, next action, and active risks.
- `.memory/decisions/NNNN-*.md`: durable architectural or process decisions,
  including alternatives and consequences.
- `.memory/problems/NNNN-*.md`: reproducible failures, diagnosis, workaround,
  and resolution status.
- Never put secrets, credentials, private chain-of-thought, raw prompts with
  sensitive data, or large generated artifacts in `.memory/`.
- Prefer links to code, tests, trajectories, and version records over copying
  their contents.

## Definition of done

- The requested behavior is implemented.
- Relevant automated tests pass.
- Failure paths are explicit and actionable.
- Documentation matches the code.
- The file catalog, current plan, and current-state memory reflect the completed
  major development work.
- Durable decisions and unresolved problems are reflected in `.memory/`.
