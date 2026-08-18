# 0030 — 需要结构化输出的 Prompt 内联完整 Schema 与逐字段定义

> Purpose: record the prompt-authoring convention that any prompt whose final
> output is machine-validated structured data must carry the complete schema
> and a per-field definition inline, so the model does not infer semantics.

Status: Accepted
Date: 2026-08-15
Owners: project owner

## Context

The first two blind BehaviorPattern sessions both failed the structured-result
gate, not because their research was wrong but because the model inferred the
meaning of two submission fields incorrectly: it narrowed a finding's
`eligible_trajectory_ids` to the observed subset and it wrote a corpus
revision id into `derivation_ids`. The field shape was already enforced by
the submit tool's schema, but the field *semantics* lived only in Python
validation code, which the model could not see. The owner directed that any
prompt requiring structured output must append the schema and every field's
definition to the prompt itself.

## Decision

- Every production prompt whose terminating action is a structured,
  machine-validated output must append the complete output schema together
  with a per-field definition or semantic annotation, including the
  allowed values, the denominator/set rules, and the provenance rules for
  identifiers such as `derivation_ids`.
- The inline schema is the model-facing contract. The tool's parameter schema
  and the Python validator remain the machine-side enforcement of the same
  contract; they do not substitute for documenting semantics in the prompt.
- When the output schema changes, the prompt's inline schema section is
  updated in the same change and the prompt is re-approved (content hash
  rebinds in its approval sidecar).
- The four Specialist research protocols now carry a “提交结构” section that
  lists the top-level object, `research_scope`, and every `finding` field
  with its meaning and the machine checks that reject the submission.

## Alternatives considered

- Document semantics only in tool descriptions or code: the model never sees
  the Python validator, and the first blind test demonstrated this fails.
- Add ad hoc “common pitfalls” notes instead of the full schema: owner
  rejected in favor of the complete schema with per-field annotations.

## Consequences

- Future structured-output prompts (research submission, and any later
  candidate/comparison/review outputs) follow this convention.
- Protocol content and the tool schema are now expected to stay aligned;
  drift between them is a defect to fix before approval.

## Revisit when

The framework grows a single source of truth that injects the live tool
schema into the prompt automatically; until then the inline copy remains the
authoritative model-facing contract.
