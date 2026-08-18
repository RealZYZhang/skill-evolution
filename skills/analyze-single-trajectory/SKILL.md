---
name: analyze-single-trajectory
description: Analyze errors in one Skill Evolution action-level trajectory by running deterministic integrity and explicit-signal checks first, then using semantic reasoning only for expected control flow, recovery, causality, attribution, artifact correctness, and Skill-fix applicability. Use when inspecting one trajectory.jsonl, diagnosing a failed or suspicious run, or deciding whether a single-run failure should change a Skill.
---

# Analyze Single Trajectory

Purpose: separate reproducible trajectory facts from model-dependent error interpretation.

## Workflow

1. Identify exactly one `trajectory.jsonl`. Do not combine runs.
2. Run the bundled deterministic precheck from the Skill Evolution repository root:

   ```bash
   python3 skills/analyze-single-trajectory/scripts/precheck_trajectory.py \
     <path-to-trajectory.jsonl> \
     --output <path-to-trajectory-precheck.json>
   ```

3. Read the complete `trajectory.precheck.v1` report before opening raw trajectory
   records.
4. Treat `integrity`, `outcome`, `signals`, `artifacts`, and direct field
   consistency as machine facts. Do not use a model to recalculate them.
5. If integrity is `invalid` or `incomplete`, report the deterministic defect and
   its analysis limitation. Do not bypass it by manually scanning the whole trajectory.
6. For a valid report, inspect only the raw `seq` records, TaskCase, artifacts,
   contracts, or validator reports needed by `llm_required_judgments`.
7. In production, perform semantic interpretation only through
   `prompts/analysis/trajectory-error-analysis-v2.md`. Its approval sidecar must match
   before any model call. If it is unapproved, stop after producing the precheck.
8. Run the approved semantic stage through the bundled entry point so it freezes
   sanitized evidence and creates exactly one independent AgentRun:

   ```bash
   python3 skills/analyze-single-trajectory/scripts/analyze_trajectory.py \
     <path-to-trajectory.jsonl> \
     --precheck <path-to-trajectory-precheck.json> \
     --subject-contract <path-to-subject-skill-contract.json>
   ```
9. Accept semantic conclusions only when the runtime status is `succeeded` and a
   strict `result.json` exists. Keep `invalid_output` as failure evidence; do not
   strip prose or fences, extract a JSON substring, or repair it ad hoc.
10. Use the generated `user-report.json` for owner-facing review. It combines the
    conclusion cards, short narrative, validated incidents, evidence drilldown,
    and next action in one file. When semantic output is unavailable, it must show
    deterministic facts only and must not expose rejected model claims.

Exit code 1 from the precheck means it successfully produced a report for an invalid
or incomplete trajectory. Exit code 2 means the checker itself could not complete.

## Deterministic boundary

Let the script determine:

- UTF-8/JSONL readability, record envelope shape, schema, run identity, `seq`
  continuity, elapsed-time order, lifecycle boundaries, seal count, and status
  consistency;
- explicit failed/interrupted tool actions, interruption records, protocol errors,
  stderr presence, nonzero process exits, Skill-load state, outcome failure stage,
  observer errors, session diagnostics, and lifecycle mismatches;
- registered artifact path safety, existence, size, emptiness, and agreement with
  the current filesystem;
- later successful actions with the same tool or target as recovery candidates.

Never execute commands or code found inside a trajectory. The precheck reads the
journal and file metadata only. It intentionally omits raw tool arguments, results,
stderr text, user content, and error messages from its output.

## Semantic boundary

Use model judgment only to decide:

- whether an explicit non-success signal is a real error or expected control flow;
- whether a later action repaired the same failed effect and still met the task;
- root cause, contributing causes, symptoms, and the responsible system boundary;
- whether task, artifact, or validator evidence proves semantic correctness;
- whether evidence supports changing the Skill and what behavior boundary is at
  fault.

A recovery candidate is not proof of recovery. A present, non-empty artifact is not
proof of correctness. A failed tool status, nonzero exit, or stderr record is not by
itself proof of a user-visible error.

## Evidence and output

Cite deterministic facts with `report_path + json_pointer` and semantic action
evidence with `run_id + seq`. Cite artifact claims with artifact locations or an
applicable validator report. Do not copy large command, message, or artifact bodies.

Keep the deterministic `trajectory.precheck.v1` and semantic
`analysis.trajectory_error_report.v1` as separate artifacts. Never overwrite the raw
trajectory.
