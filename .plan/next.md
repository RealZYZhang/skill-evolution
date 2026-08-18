# Current next steps

> Purpose: the single prioritized, actionable plan for the next development
> work; last reconciled 2026-08-17 for the error-centric product analysis rework
> (decisions 0031/0032), which supersedes the four-specialist research stage.

## Plan sources reconciled

- `future-framework.md` separates the implemented single-Skill product from
  deferred registry, RAG, dependency-graph, and scale-out proposals.
- Decisions `0019` through `0022` define the thin Skill Contract,
  deterministic-first single-Trajectory analysis, Trajectory naming, and five-layer user
  report.
- Decision `0023` defines the accepted Skill-first hierarchy and one-time
  migration boundary.
- Decision `0026` resolves the structured JSON delivery boundary for single-trajectory
  reports. Problem `0008` preserves the failed text-delivery history and fix.
- Decision `0027` requires a complete searchable and executable research Harness
  before single-Agent capability validation, then four isolated specialists
  without Synthesis or a product multi-Trajectory report.

There is no second active plan. Dependency Graph remains intentionally deferred.

## Completed in the latest development effort

- Reworked multi-Trajectory analysis to error-centric product analysis
  (decisions 0031/0032). A main ErrorIdentifier identifies every error that
  affects skill reusability or reliability, then one ErrorAnalyst subagent per
  error re-derives it from raw material and reports only the problematic
  dimensions. Two new submit tools (`submit_error_identification` /
  `submit_error_report`), two validators, two roles, runtime dispatch, and the
  `scripts/error_analysis.py run` CLI were added; the first real run produced a
  three-error list with per-error reports (403/403 tests).
- Removed the research certification chain (blind smoke, hidden benchmark,
  capability certificate, four isolated Specialists) from the product path;
  `scripts/multi_trajectory_research.py` is now legacy.
- Implemented the accepted Harness-first multi-Trajectory research boundary. Goal-
  specific readiness now stops incomplete or incomparable samples before any
  model call; a successful build freezes a deterministic, content-addressed,
  redacted corpus with complete raw observable Trajectory records, safe text
  artifacts, accepted single-Trajectory reports, a navigation index, and a shared
  result/resource baseline.
- Added a no-network Docker research laboratory with read-only evidence,
  disposable writable work, non-root execution, no host fallback, evidence
  before/after verification, process/time/output/disk and cumulative tool
  budgets, plus bounded auditable commands and derivations.
- Added approved-content boundaries for the four proposed research protocols
  and prompt-visible Harness context. Dynamic Prompt data is a bounded corpus
  map rather than Trajectory text; untrusted Trajectory text cannot close or replace the
  static protocol boundary.
- Added the strict terminating multi-Trajectory result tool. Every finding names the
  complete denominator, observed and checked-absent Trajectories, original evidence,
  counterexample search, derivations, limitations, and confidence; repeated
  behavior requires two independent supporting Trajectories. Result files and board
  references are reverified on every read.
- Replaced caller-authored Harness booleans with workflow-executed deterministic
  acceptance. The no-model driver exercises the production research tools and
  submission lifecycle; the frozen report binds corpus, baseline, executions,
  Docker identity, limits, implementation, and replay audit evidence.
- Implemented two-session behavior smoke cycles, hidden-benchmark human review,
  fixed failure taxonomy, append-only repair cycles, and portable capability
  certificates binding code, Prompt, Harness, model, Docker, limits, both
  run/session/result identities, and both reviews. Imported certificates cannot
  be chained and invalidate on any capability-boundary change.
- Implemented the four-role, specialists-only result board. Result reliability
  comes from the shared deterministic baseline; BehaviorPattern,
  ConditionsCoverage, OutcomeConsistency, and ResourceEfficiency run in
  isolated attempts, serially until separate concurrency certification. The
  board preserves failures and retries without Synthesis, aggregation, product
  Analysis writes, or user conclusions.
- Added the complete `multi_trajectory_research.py` CLI for assess, corpus build,
  prepare, deterministic Harness, hidden benchmark, double smoke, review,
  certificate issue/import, Specialist run/retry, status, and board reads. The
  legacy campaign entry point now fails closed.
- The five same-Revision successful document visualizer Trajectories pass behavior-
  research readiness and are frozen as the internal smoke corpus
  `corpus-8d046a5ca7b1a110f37a`. No real Agent has run: research Prompt and
  Harness-context approvals remain proposed, and the local Docker image
  prerequisite must pass before deterministic acceptance.
- Upgraded deterministic acceptance to the strict v2 contract. Its trusted
  no-model provider runs through Pi 0.81.1 and the production navigation,
  analysis-code, and terminating-submission tools; fixed positive and negative
  subchecks bind observed Docker isolation/limits, implementation snapshots,
  command plans, Pi trajectories, and a replayable audit tree. The Pi extension-load
  smoke passed without a model request.
- Bound every research run to the complete first-party Python dependency
  closure, Pi executable/interpreters/npm dependency tree, and the effective
  Docker client/context/endpoint/daemon identity. Harness reload and every
  process/container start revalidate that identity and reject drift.
- Closed the ambient Pi startup boundary. Research accepts only one direct,
  package-bound Pi executable; replaces rather than inherits the host
  environment; uses one-shot HOME/tmp/Pi directories; disables discovered
  resources, built-in tools, project approval, and sessions; and exposes only
  the selected literal provider credential through a read-only descriptor.
  Before Prompt it verifies the exact model, thinking level, absent session
  file, model availability, and ten-tool attestation. Real and Harness-faux
  credential/model/extension sources are separate conditional branches of the
  same certified policy.
- Bound the deterministic public Harness drive to the fixed three extension
  paths and hashes plus the complete active sandbox context. Caller-authored
  Docker commands, container identities, limits, budgets, or contradictory
  implementation summaries now fail before workspace or Pi creation.
- Closed the remaining isolation and evidence-reference races. Docker has no
  readable container log sink; PID 1 reaps children; each tool proves residual
  processes are zero; cleanup failure poisons the complete session. Reports,
  audit files, result references, and evidence hashing use pinned no-follow
  file handles and reject ancestor or inode replacement.
- Research-related regression passes 183/183. The last complete suite with
  loopback permission passed 382/382; after this expansion, 388 of 395 tests
  pass inside the stricter default sandbox and the other seven stop at socket
  setup only because local HTTP tests cannot bind loopback there. Installed Pi
  0.81.1 completed no-model exact-state, selected-model availability, and
  active-tool attestation checks without the real credential directory. The
  Docker daemon responds, but `python:3.11-slim` is not installed locally, so
  the real smoke batch remains `prepared` with no Harness report, no AgentRun,
  no capability certificate, no Specialist board, and zero product multi-Trajectory
  analyses.

- Renamed every current trace / trace_* / Trace* name to
  trajectory / trajectory_* / Trajectory* per owner direction (decision
  0028 reverses 0021). Writers produce only the new names; the reader
  compatibility layer accepts trace-named frozen evidence and projects it
  onto the canonical model, and the full suite passes 395/395.
- Removed raw record information from the Execution overview and renamed all
  task-requirement surfaces to “任务 prompt”. Execution cards still keep the
  full prompt behind an explicit disclosure.
- Replaced structured task-input JSON with artifact-first input presentation.
  Markdown and plain text now open in an inert escaped HTML preview through the
  same allow-listed file route as HTML output.
- Added a user-controlled key/all trajectory view. The default 44-step view is a
  deterministic read-time type filter, not an LLM judgment; the full 83-step
  source remains available. Each step shows action, status, tool, parameters,
  and observable messages before optional raw data. Long messages, parameters,
  results, and records remain collapsed by default.
- Added truthful analysis empty states that distinguish an accepted
  deterministic precheck from an accepted semantic conclusion.
- Completed the six missing deterministic prechecks. All eleven trajectories are
  integrity-valid; six outcomes succeeded and five failed. No deterministic
  signal is promoted to recovery, causality, attribution, or a Skill change.
- Drafted and obtained owner approval for `trajectory-error-analysis-v2.md` without changing approved v1. V2 requires
  one JSON object, provides all four valid EvidenceRef locations, forbids extra
  evidence keys and self-causal links, and includes a final consistency check.
- Added fixtures for all six whole-trajectory assessments and every EvidenceRef
  location.
- Ran one authorized DeepSeek V4 Pro validation. The candidate JSON passed the
  report, EvidenceRef, and causal checks, but the final answer still contained
  prose and a Markdown fence. The strict gate correctly kept it invalid and the
  Viewer exposed only deterministic facts.
- Confirmed Pi 0.81.1 RPC has no native `response_format/json_schema` prompt
  option. Added a dedicated terminating submission action that validates every
  JSON field before the existing signal, evidence, and causal gates run.
- Corrected submission-event correlation after the first real structured attempt
  exposed that arguments arrive at tool start while success arrives at tool end.
  The failed attempt remains immutable and visible.
- Re-ran the same authorized redacted trajectory with DeepSeek V4 Pro. The structured
  submission and all current report/evidence gates passed, producing the first
  accepted semantic report and a five-layer Viewer projection.
- Completed browser acceptance for compact overview, safe Markdown preview,
  key/all trajectory switching, readable tool actions, default-collapsed long
  content, and precheck-only analysis status. The current full suite has 222
  passing tests.
- Corrected the Skill Explorer to a Chinese-first product interface. All status
  labels are Chinese; Skill versions and execution batches use plain-language
  names with contextual question-mark explanations.
- Added an auditable Chinese presentation for the first accepted semantic
  report. The original English report remains unchanged; the Viewer prefers the
  Chinese projection only while its recorded source digest still matches.
- Completed structured v2 semantic analysis for all eleven trajectories after owner
  approval. Nine of the remaining ten passed on their first attempt; one
  contradictory Skill-fix result was rejected and preserved before a second
  accepted attempt. Every trajectory now has one accepted semantic conclusion.
- Published a reviewed Chinese projection for every accepted semantic report.
  The localization publisher requires complete incident coverage, preserves the
  source report and evidence, and binds each projection to its source digest.
- Completed browser acceptance for a failed execution, a recovered execution,
  and a report that recommends evaluating a Skill change. The full suite now
  has 226 passing tests.
- Translated the remaining analysis status values such as evidence strength and
  Skill-change applicability. Drafted an unapproved v3 prompt that requires all
  future user-facing semantic fields to use Simplified Chinese.
- Execution cards now show a compact task summary. The full prompt is available
  only through an explicit disclosure and a dedicated task-requirement tab.
- Replaced the flat Skill rail with an expandable
  `Skill → trajectory → single-trajectory analysis` tree. The Skill card retains execution,
  version, and analysis statistics; trajectory and analysis children are plain menu
  rows rather than nested cards.
- Established a dedicated empty multi-trajectory analysis store. Harness and
  unfinished legacy multi-role attempts no longer enter the multi-trajectory API,
  page, or count. The current truthful count is zero.
- Completed Chrome acceptance for tree collapse/expand, direct analysis
  navigation, Chinese statuses, friendly version/batch names, separate prompt
  disclosure, keyboard-visible term help, stable URL state, and the explicit
  unimplemented multi-trajectory empty state.
- The former 213/216/219-test checkpoints are superseded by the current
  222-test run.
- Completed the non-technical Skill Explorer contract. The page presents Skill,
  current Contract, Skill version history, eleven executions, input, output,
  trajectory, and all five single-trajectory layers including incidents.
- Browser location now preserves Skill, Execution, section, detail tab, and
  Trajectory sequence. Skill switches clear old detail, caches are scoped by Skill,
  stale requests cannot overwrite current state, and an unavailable side panel
  does not make the whole Skill unusable.
- Viewer reads are truly zero-write. Catalog and Skill indexes are maintained by
  writers and migration; GET only reads. Automated tests compare all runtime
  files and modification times before and after browsing.
- Added an explicit completed-cutover marker. Compatibility APIs switch to the
  hierarchy only after this marker exists, not when a partial Skill directory
  happens to appear.
- Extended migration to accept a standalone historical run only when it has a
  frozen Skill package and a complete Trajectory. The complete standalone run was
  migrated as a direct Execution.
- Applied migration `hierarchy-migration-20260809-cutover`. It verified 340
  source files before commit and produced one Skill, two Execution Sets,
  eleven Executions, ten single analyses, and seven preserved batch-level
  Harness/unfinished workflow records. Those seven are not multi-trajectory analyses.
- Preserved five failed and six successful Executions. Preserved five semantic
  analyses as `invalid_output` instead of weakening them to a generic state.
  Historical batch-level records are no longer exposed as analysis results.
- Registered the current approved document HTML Skill as a separate active
  Revision. The historical Revision correctly remains
  `missing_at_execution`.
- Four pre-canonical spike attempts could not truthfully become Executions
  because they lacked both the frozen Skill actually used and a sealed
  canonical Trajectory. After explicit owner approval, they were permanently
  deleted on 2026-08-10 and are not recoverable from the runtime.
- Strengthened `AGENTS.md`: owner communication must lead with goal,
  user-visible capability, current functional stage, and remaining gap; it may
  not lead with implementation vocabulary.
- The earlier Chrome acceptance of Skill overview, execution navigation, sealed
  trajectory, and guarded single-trajectory results remains valid; the current effort
  supersedes its former multi-trajectory presentation.

## Ordered work

1. DONE: multi-Trajectory analysis was reworked to error-centric product
   analysis (decisions 0031/0032). The main ErrorIdentifier plus one ErrorAnalyst
   subagent per error run over the frozen corpus via `scripts/error_analysis.py
   run`; the first real run (2026-08-17) produced a three-error list with
   per-error reports covering only the problematic dimensions.
2. DONE: the old research certification chain (blind smoke, hidden benchmark,
   capability certificate, four isolated Specialists, decision 0027) is now
   legacy and removed from the product path (decision 0032).
3. Next: wire the error list + per-error reports into the product web app's
   multi-Trajectory analysis section (`web/trajectory-viewer`), with a writer that
   stores them under the existing multi-analysis model and a reader that renders
   them Chinese-first, consistent with the current style.
4. Decide whether the identified errors (output-token-limit write failure,
   generated-script KeyError, grep/diff false-positive validation) should become
   Skill improvement candidates; if so, define the smallest guidance change and
   regression cases before release.
5. Review and approve `trajectory-error-analysis-v3.md` before requiring future model
   reports to be Chinese. Revalidate one trajectory and add a deterministic language
   gate if prompt compliance is not reliable. Until then, keep using reviewed,
   source-bound Chinese projections without changing accepted v2 reports.
6. Obtain explicit authorization covering the complete seven-record list, then
   permanently delete the four historical Harness checks and three unfinished
   multi-role attempts that are already hidden from product reads. Update the
   prepared correction record from pending to completed and verify the old
   directory is gone.
7. Make every future Capture and Replay execute only the already-frozen Skill
   Revision, so a source edit during startup can never make the recorded version
   differ from the version actually used. Ensure every created Execution ends
   in a recoverable terminal state.
8. Remove temporary legacy Campaign materialization from new Harness and
   evidence work. New reports must contain stable Skill/Execution references,
   never temporary or machine-local paths.
9. Replace the provisional parallel Improvements objects with an ownership
   adapter over the accepted Candidate, Comparison, independent Judge, and human
   Review flow. Keep Docker fail-closed and all existing safety gates.
10. Review and approve the skill-neutral execution prompt before any new real
   model execution campaign.
11. Before screenshots or Candidate comparison, complete the Chrome crash
   preflight and Docker daemon/image checks. Never fall back to host execution.

## Active constraints

- The new hierarchy and Viewer are authoritative for the current runtime, but
  future execution integrity and Improvements unification remain open work.
- The four pre-canonical spikes are not product data and were permanently
  deleted after explicit path-level approval; they cannot be recovered from the
  runtime.
- The five v1 semantic attempts, first v2 text attempt, one structured event-
  correlation attempt, and one contradictory batch result produced no accepted
  conclusions. Their user reports remain historical evidence. All eleven trajectories
  now also have a later accepted structured v2 conclusion and a current Chinese
  projection.
- Internal multi-Trajectory research is implemented through four non-aggregated
  Specialist reports. Formal aggregation and product multi-Trajectory Analysis are
  not implemented, so the product count truthfully remains zero. Seven
  historical Harness/unfinished workflow records remain hidden from product
  reads and on disk until the owner explicitly authorizes deletion of all seven.
- The five-Trajectory smoke corpus is frozen and its real-environment Docker
  Harness acceptance passed 2026-08-15 (python:3.11-slim provisioned locally;
  the framework still never pulls implicitly). The BehaviorPattern protocol and
  prompt-visible Harness context are approved; the other three Specialist
  protocols and the EvaluationSuite remain proposed, so full six-objective
  research is still blocked there. Full research also lacks declared
  comparable groups and complete resource records, and is blocked by the
  proposed EvaluationSuite and missing historical TaskCase/condition mapping;
  zero-sample conditions cannot be treated as performance evidence.
- Dependency Graph, remote Registry, RAG, multi-tenant permissions, and
  large-scale indexing are deferred and must not be added to Contract v2.
