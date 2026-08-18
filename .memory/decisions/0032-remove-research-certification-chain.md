# 0032 — 移除能力证书与盲测纯研究闸门

> Purpose: record the owner decision to drop the capability-certificate / blind
> smoke research chain from the product flow, following the error-centric
> product rework in decision 0031.

Status: Accepted
Date: 2026-08-15
Owners: project owner

## Context

Decision `0027` built a research certification chain: deterministic Harness
acceptance, frozen hidden benchmark, two independent blind sessions, human
review, and a portable capability certificate. Decision `0031` made
multi-Trajectory analysis error-centric and user-visible. The owner directed
that this analysis is the product itself and chose to remove the pure-research
certification machinery.

## Decision

- The product flow no longer requires the hidden benchmark freeze, the
  two-session blind smoke, the human review-smoke, or capability-certificate
  issue/import. These are removed as product gates.
- The product flow is: a main agent identifies all errors → one subagent per
  error re-derives and reports only the problematic dimensions → the error list
  and per-error reports are user-visible.
- Sandbox isolation (Docker, network disabled, read-only evidence, resource
  limits) remains, because it is the execution-safety boundary for the real
  per-error subagents, not research certification.
- The deterministic Harness acceptance remains available as verification
  infrastructure but is no longer a certification gate in the product flow.

## Alternatives considered

- Keep the certificate chain as a background quality check: owner chose to
  remove it (option A).
- Remove sandbox isolation too: rejected; real subagents still need the
  safety boundary.

## Consequences

- The frozen hidden benchmark, blind-smoke, review, and certificate CLI paths
  become unused from the product perspective and are treated as legacy.
- The product multi-Trajectory analysis is a direct error-centric feature with
  no certification prerequisite.

## Revisit when

The unused certification code paths are either retired or repurposed, or the
owner re-introduces a quality/certification gate with a different shape.
