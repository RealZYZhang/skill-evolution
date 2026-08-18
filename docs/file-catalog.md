# Repository file catalog

> Purpose: map the project structure and the responsibility of every
> project-owned file, so readers can find the source of truth before editing.

This catalog covers maintained project files. It excludes generated
`__pycache__/`, local `.skill-evolution/` execution evidence, and macOS
`.DS_Store`. `docs/pi-agent/upstream/` is a pinned third-party snapshot; its
files are listed separately and must be replaced as a unit, never edited.

## File-header convention

Markdown starts with a title and purpose line. Python uses a module docstring
after an optional shebang. HTML, CSS, JavaScript, and TypeScript use a leading
comment. JSON and binary formats cannot safely accept comments; their
schema/type fields and this catalog provide the purpose. Approved prompts are
content-hashed, so their existing opening instruction is their header until an
owner explicitly reapproves a changed version.

## Structure

```text
.
├── .memory/       current state, decisions, and recurring-problem records
├── .plan/         active plan and deferred planning sources
├── contracts/     current schemas and historical contract records
├── docs/          maintained technical documentation and Pi reference
├── extensions/    Pi tool-boundary extensions
├── fixtures/      canonical format fixtures
├── migrations/    reviewed legacy-to-current identity mappings
├── prompts/       versioned, approval-bound production prompts
├── scripts/       command-line and integration entry points
├── skill_evolution/  file-backed domain implementation
├── skills/        executable Skill packages and evaluated fixtures
├── task-cases/    repeatable execution inputs
├── tests/         dependency-free unit and protocol tests
├── web/           local trajectory viewer assets
├── AGENTS.md      contributor and agent operating rules
├── README.md      project entry point and runnable instructions
└── config.yaml    non-secret, project-wide Pi defaults
```

## Root and planning files

| File | Purpose |
| --- | --- |
| `AGENTS.md` | Rules for safe, reproducible development, documentation, planning, configuration, and goal/function-first owner communication. |
| `README.md` | Project introduction, prerequisites, configuration, and supported commands. |
| `config.yaml` | Validated non-secret default Pi provider, model, and thinking mode. |
| `.gitignore` | Excludes generated Python caches, coverage output, and runtime evidence. |
| `.plan/README.md` | Plan merge, prioritization, and reconciliation procedure. |
| `.plan/next.md` | Single prioritized plan for the next implementation work. |
| `.plan/future-framework.md` | Reconciled framework plan separating the implemented MVP, remaining single-Skill work, and deferred scale-out proposals. |

## Memory records

| File or files | Purpose |
| --- | --- |
| `.memory/current.md` | Concise current implementation state, verified evidence, risks, and next action. |
| `.memory/decisions/0001-rpc-process-boundary.md` | Pi process-boundary decision. |
| `.memory/decisions/0002-asynchronous-optimization-workflows.md` | File-backed asynchronous optimization decision. |
| `.memory/decisions/0003-file-only-storage-and-simple-queue.md` | File-only storage and recoverable queue decision. |
| `.memory/decisions/0004-candidate-skill-with-internal-diff.md` | Immutable candidate skill and framework-computed diff decision. |
| `.memory/decisions/0005-trajectory-includes-pi-runtime-evidence.md` | Historical Pi runtime evidence retention decision. |
| `.memory/decisions/0006-human-promotion-disclosure-and-staged-replay.md` | Human promotion disclosure and staged replay decision. |
| `.memory/decisions/0007-trajectory-first-before-evaluation.md` | Historical trajectory-first evaluation sequencing decision. |
| `.memory/decisions/0008-default-deepseek-pi-runtime.md` | Default Pi provider/model decision. |
| `.memory/decisions/0009-unified-trajectory-journal.md` | Historical unified trajectory journal decision. |
| `.memory/decisions/0010-action-level-trajectory.md` | Historical action-level trajectory representation decision. |
| `.memory/decisions/0011-replay-campaign-and-prompt-approval.md` | Replay campaign and prompt approval decision. |
| `.memory/decisions/0012-read-only-local-trajectory-viewer.md` | Historical read-only local viewer decision. |
| `.memory/decisions/0013-deterministic-harness-evidence.md` | Deterministic Harness evidence decision. |
| `.memory/decisions/0014-independent-multi-pi-analysis.md` | Independent multi-Pi analysis decision. |
| `.memory/decisions/0015-fail-closed-candidate-replay.md` | Fail-closed candidate replay decision. |
| `.memory/decisions/0016-crash-circuit-breaker.md` | Repeated-crash circuit-breaker decision. |
| `.memory/decisions/0017-execution-prompt-is-skill-neutral.md` | Skill-neutral execution-prompt decision. |
| `.memory/decisions/0018-root-configuration-and-plan-workspace.md` | Root configuration, plan workspace, and file-catalog decision. |
| `.memory/decisions/0019-package-local-thin-skill-contract.md` | Package-local thin Skill Contract, stable filename, and schema-evolution decision. |
| `.memory/decisions/0020-deterministic-first-single-trajectory-analysis.md` | Deterministic-precheck-first boundary for single-trajectory error analysis. |
| `.memory/decisions/0021-trajectory-naming-and-legacy-read-compatibility.md` | Canonical Trajectory naming, current-only writers, and historical read compatibility. |
| `.memory/decisions/0022-five-layer-single-trajectory-user-report.md` | Five-layer single-trajectory user report and read-only Viewer integration. |
| `.memory/decisions/0023-skill-first-hierarchy.md` | Accepted Skill-first ownership, Revision binding, analysis placement, migration, and Viewer decision. |
| `.memory/decisions/0024-hierarchy-cutover-disposition.md` | Accepted historical migration, standalone conversion, exact status preservation, completed cutover, and approved permanent deletion of unreconstructable spikes. |
| `.memory/decisions/0025-multi-trajectory-boundary-and-chinese-hierarchy.md` | Accepted separation of real multi-trajectory analysis from batch checks, plus the Chinese Skill → trajectory → analysis presentation rules. |
| `.memory/decisions/0026-validated-structured-analysis-submission.md` | Accepted validated tool-submission boundary for single-trajectory semantic reports. |
| `.memory/decisions/0027-research-harness-before-specialists.md` | Accepted Harness-first raw-Trajectory research, two-session capability certification, four isolated specialists, and no-aggregation boundary. |
| `.memory/decisions/TEMPLATE.md` | Template for a durable decision record. |
| `.memory/problems/0001-pi-not-on-shell-path.md` | Pi command discovery failure record. |
| `.memory/problems/0002-pi-get-commands-path-shape.md` | Pi RPC command-path incompatibility record. |
| `.memory/problems/0003-pi-model-unavailable.md` | Pi model/authentication-boundary diagnosis. |
| `.memory/problems/0004-rpc-message-update-volume.md` | High-volume RPC update handling record. |
| `.memory/problems/0005-comparator-evidence-ref-field-drift.md` | Comparator evidence-reference schema drift record. |
| `.memory/problems/0006-chrome-macos-registration-crash.md` | Chrome startup crash and retry constraint record. |
| `.memory/problems/0007-unclosed-skill-markdown-fence.md` | Resolved malformed Markdown fence in the evaluated Skill. |
| `.memory/problems/0008-trajectory-error-output-contract-drift.md` | Resolved TrajectoryErrorAnalyst output-contract drift and structured-submission verification. |
| `.memory/problems/0009-research-image-not-present.md` | Open local Docker-image prerequisite blocking real Harness acceptance and research Agent runs. |
| `.memory/problems/0010-research-pi-ambient-startup-boundary.md` | Resolved ambient Pi launcher, configuration, credential, extension, and Docker-router bypasses plus their startup attestation boundary. |
| `.memory/problems/TEMPLATE.md` | Template for a reproducible problem record. |

## Domain modules and command scripts

| File | Purpose |
| --- | --- |
| `skill_evolution/__init__.py` | Package identity for file-backed MVP primitives. |
| `skill_evolution/agents.py` | Agent roles, model boundary, run repository, and multi-agent orchestration. |
| `skill_evolution/analysis.py` | Skill Contract parsing plus analysis campaign and evidence-request contracts. |
| `skill_evolution/candidates.py` | Immutable candidate skills and framework-generated diffs. |
| `skill_evolution/comparison.py` | Fail-closed comparison planning and effect classification. |
| `skill_evolution/comparison_harness.py` | Immutable baseline/candidate comparison batches for the Harness. |
| `skill_evolution/config.py` | Strict loader and validator for root `config.yaml`. |
| `skill_evolution/evidence.py` | Evidence references, confinement, redaction, and frozen bundles. |
| `skill_evolution/evaluation.py` | Strict EvaluationSuite approval, TaskCase resolution, and coverage-readiness contracts. |
| `skill_evolution/hierarchy.py` | Skill-first Revision, Execution, Execution Set, separately stored multi-trajectory Analysis, writer-maintained index, explicit cutover, and file-repository contracts. |
| `skill_evolution/hierarchy_analysis.py` | Dedicated Skill-owned multi-trajectory analysis lifecycle and safe unavailable-report projection. |
| `skill_evolution/hierarchy_improvements.py` | Provisional Skill-owned Improvements prototype; retained for compatibility remediation because its parallel schemas are not authoritative. |
| `skill_evolution/hierarchy_migration.py` | Hash-addressed dry-run, standalone Trajectory conversion, status-preserving staged cutover, execution-set check placement, verification, and pre-commit rollback. |
| `skill_evolution/layout.py` | Canonical file-only runtime directory layout. |
| `skill_evolution/pi_runtime.py` | One-Pi-process-per-agent RPC runtime adapter, including validated structured submission correlation. |
| `skill_evolution/research_agent_runtime.py` | Approval-bound raw-Trajectory runtime with bounded prompt map, identity-bound sandbox routing, replacement environment, isolated Pi configuration/credential bridge, pre-Prompt model/session/tool attestation, strict submission, and cleanup-poison rejection. |
| `skill_evolution/research_artifacts.py` | Immutable digest-bound Specialist and smoke result references read through pinned, no-follow file handles. |
| `skill_evolution/research_board.py` | Append-only, non-aggregating result board for four Specialist roles. |
| `skill_evolution/research_capability.py` | Complete first-party/Pi/Docker execution fingerprint, direct package-bound Pi launcher policy, conditional real/faux credential/model/extension policy, and portable certificate for two independently reviewed behavior smokes. |
| `skill_evolution/research_corpus.py` | Goal-specific readiness, deterministic redacted corpus, navigation index, and result/resource baseline. |
| `skill_evolution/research_harness_acceptance.py` | Semantic deterministic acceptance of the production research toolchain, cross-bound implementation identity, active Docker isolation/log/process boundaries, and pinned report/audit evidence. |
| `skill_evolution/research_results.py` | Strict multi-Trajectory Specialist result, denominator, evidence, counterexample, and derivation gates. |
| `skill_evolution/research_sandbox.py` | No-network Docker laboratory with read-only evidence, disposable work, bounded/no-log execution, complete control-plane identity, safe evidence hashing, and no host fallback. |
| `skill_evolution/research_workflow.py` | Harness-first batch state, hidden benchmark, double-smoke review, capability import, and four-Specialist gates. |
| `skill_evolution/reviews.py` | Human promotion disclosure and release decisions. |
| `skill_evolution/sandbox_replay.py` | Docker-only Pi runner for automatic candidate replay. |
| `skill_evolution/skill_contracts.py` | Deterministic package-local Skill Contract, Markdown, and TaskCase validation. |
| `skill_evolution/storage.py` | Atomic manifest storage and recoverable in-process queues. |
| `skill_evolution/trajectory_analysis.py` | Strict semantic single-trajectory report parser and cross-field validation. |
| `skill_evolution/trajectory_user_report.py` | Strict five-layer owner-facing report projection for accepted and unavailable analyses. |
| `skill_evolution/trajectory_user_report_localization.py` | Complete, reviewed Chinese projection of an accepted five-layer report without changing source semantics or evidence. |
| `skill_evolution/trajectory_precheck.py` | Deterministic single-trajectory integrity, explicit-signal, lifecycle, and artifact checks. |
| `skill_evolution/workflows.py` | Workflow coordinators that exchange persistent object IDs. |
| `scripts/__init__.py` | Package marker for project command and integration modules. |
| `scripts/analysis_campaign.py` | Retired legacy multi-role campaign entry point; fails closed and directs operators to the research Harness. |
| `scripts/artifact_comparator.py` | CLI and library for deterministic HTML artifact comparison. |
| `scripts/build_format_fixtures.py` | Builder for equivalent Markdown, text, DOCX, and PDF fixtures. |
| `scripts/error_analysis.py` | Error-centric multi-Trajectory analysis CLI: main agent identifies all errors, then one subagent per error reports only the problematic dimensions. |
| `scripts/harness.py` | CLI that combines trajectory profiling with artifact comparison and stores the result as an execution-set check, not multi-trajectory analysis. |
| `scripts/migrate_skill_hierarchy.py` | CLI for hierarchy migration dry-run and exact-confirmation apply. |
| `scripts/multi_trajectory_research.py` | CLI for readiness, corpus freezing, Harness acceptance, blind-smoke certification, and four Specialist internal research without aggregation. |
| `scripts/pi_rpc.py` | JSONL RPC subprocess client and manual Pi CLI, with optional complete environment replacement and targeted child file-descriptor passing. |
| `scripts/prompt_approval.py` | Inspect and explicitly approve versioned prompts. |
| `scripts/replay.py` | CLI for repeated, approval-bound trajectory capture. |
| `scripts/skill_contract.py` | CLI for `skill.validation_report.v1` generation and approval gating. |
| `scripts/skill_explorer_data.py` | Zero-write Skill-first Viewer projections, dedicated multi-trajectory reads, explicit-cutover compatibility views, and allow-listed file resolution. |
| `scripts/task_case.py` | Versioned task-case contracts and validation. |
| `scripts/trajectory_error_analysis.py` | Approval-gated single-run TrajectoryErrorAnalyst CLI, defaulting to approved v2 and also writing the five-layer user report. |
| `scripts/trajectory_precheck.py` | CLI for no-model `trajectory.precheck.v1` generation. |
| `scripts/trajectory_user_report.py` | Backfill CLI for immutable five-layer reports from preserved trajectory AgentRuns. |
| `scripts/localize_trajectory_user_report.py` | Publish one reviewed Chinese report beside its immutable source and bind it to the source digest. |
| `scripts/trajectory_profile_view.py` | Stable profile projection consumed by the viewer. |
| `scripts/trajectory_profiler.py` | Persistent, deterministic trajectory resource profiler. |
| `scripts/trajectory_spike.py` | Single Pi execution with complete action capture. |
| `scripts/trajectory_viewer.py` | Loopback-only, zero-write GET/HEAD Skill Explorer, safe text/Markdown previews, and deprecated Campaign-projection server. |
| `scripts/trajectory_viewer_data.py` | Viewer-facing normalization of replay, trajectory, and validated user-report data. |

## Tests

Every file below is a dependency-free unit, fake-integration, or protocol test;
none is allowed to call a real model or depend on credentials.

| File | Purpose |
| --- | --- |
| `tests/test_analysis_workflow_contracts.py` | Analysis schemas, state transitions, and evidence-loop gates. |
| `tests/test_artifact_comparator.py` | Deterministic HTML artifact comparison. |
| `tests/test_candidates_and_reviews.py` | Candidate immutability, diffs, and human review. |
| `tests/test_comparison_harness.py` | Comparison-batch materialization for the Harness. |
| `tests/test_comparison_pipeline.py` | Sandbox-only comparison planning and gate classification. |
| `tests/test_config.py` | Root configuration schema and default Pi-model validation. |
| `tests/test_evidence_contracts.py` | Evidence confinement and frozen redacted bundles. |
| `tests/test_evaluation_suite.py` | EvaluationSuite approval, TaskCase resolution, condition mapping, and fail-closed coverage gates. |
| `tests/test_format_task_cases.py` | Owner-reviewable multi-format TaskCase fixtures. |
| `tests/test_harness.py` | Shared deterministic HarnessRun boundary. |
| `tests/test_hierarchy_improvements.py` | Provisional hierarchy Improvement ownership tests; does not replace accepted end-to-end improvement gates. |
| `tests/test_hierarchy_migration.py` | Hierarchy dry-run, standalone Trajectory conversion, confirmation, hash preservation, cutover marker, and rollback. |
| `tests/test_multi_pi_agents.py` | Independent active Specialist orchestration, capability identity proxy, serial-only certification gate, and legacy workflow compatibility. |
| `tests/test_multi_trajectory_research_cli.py` | Readiness, corpus, Harness, approval, and no-product-write behavior of the multi-Trajectory research CLI. |
| `tests/test_pi_agent_runtime.py` | Strict Pi output validation, structured-submission integration, and timeout handling. |
| `tests/test_pi_rpc.py` | Pi JSONL RPC subprocess protocol, replacement environment, and targeted descriptor inheritance. |
| `tests/test_prompt_inventory.py` | Approval-bound prompt inventory, blind research-protocol checks, and exact prompt-visible extension hash binding. |
| `tests/test_replay.py` | N-run replay campaign behavior. |
| `tests/test_research_agent_runtime.py` | Research Prompt boundary, isolated startup/configuration/credential policy, model/session/tool attestation, submission lifecycle, cleanup poisoning, redaction, audit, evidence integrity, and execution identity. |
| `tests/test_research_artifacts.py` | Result-reference sealing plus linked-root, deep-ancestor, read-time replacement, deletion, and identity-tamper rejection. |
| `tests/test_research_board.py` | Four-role append-only result board, retry history, and artifact re-verification. |
| `tests/test_research_capability.py` | Transitive implementation, direct Pi/npm launcher and package binding, conditional real/faux policy, Docker-control identity, and exact two-smoke certificate validation. |
| `tests/test_research_corpus.py` | Readiness, deterministic corpus/index/baseline, redaction, artifact namespace, and evidence round-trip. |
| `tests/test_research_harness_acceptance.py` | Semantic production-tool Harness checks, fixed extension and sandbox-router binding, implementation cross-field identity, live isolation/log probes, pinned audit/report reads, and tamper/TOCTOU rejection. |
| `tests/test_research_results.py` | Strict role findings, global denominators, repeated-pattern support, counterexamples, derivations, and evidence. |
| `tests/test_research_sandbox.py` | Docker client/context/daemon preflight, confinement, safe evidence hashing, no-log/process cleanup, budgets, and no fallback. |
| `tests/test_research_workflow.py` | Harness-first states, hidden benchmark, two-session review, certification portability, four Specialist retries, and artifact integrity. |
| `tests/test_retired_analysis_campaign.py` | Proof that the legacy analysis campaign cannot bypass the new Harness workflow. |
| `tests/test_sandbox_replay.py` | Docker-only candidate Pi replay adapter. |
| `tests/test_skill_contract_v2.py` | Active v2 filename, parser, runtime boundary, evolution, and v1-history checks. |
| `tests/test_skill_contracts.py` | Skill contract, package, coverage, and validation-report CLI behavior. |
| `tests/test_skill_explorer_data.py` | Skill Explorer view models, artifact roles, redaction, file boundaries, and proof that reads do not change runtime files. |
| `tests/test_skill_explorer_http.py` | Skill-first HTTP routes, safe Markdown preview, and deprecated Campaign projection. |
| `tests/test_skill_explorer_ui.py` | Chinese status labels, contextual help, prompt disclosure, progressive trajectory details, and Skill → trajectory → analysis navigation. |
| `tests/test_skill_hierarchy.py` | Revision, Execution, Execution Set, Analysis ownership, and rebuildable-index contracts. |
| `tests/test_storage_contracts.py` | Atomic manifests, queue recovery, and runtime layout. |
| `tests/test_task_case.py` | TaskCase validation and serialization. |
| `tests/test_trajectory_error_analysis.py` | Strict single-trajectory semantic parsing, every assessment fixture, sanitized evidence, and all EvidenceRef locations. |
| `tests/test_trajectory_precheck.py` | Single-trajectory deterministic checks, CLI behavior, and packaged Skill validation. |
| `tests/test_trajectory_user_report.py` | Five-layer projection, unavailable-state safety, evidence linking, and immutable write tests. |
| `tests/test_trajectory_user_report_localization.py` | Reviewed Chinese report completeness, language, source-preservation, and incident-coverage tests. |
| `tests/test_trajectory_profiler.py` | Persistent deterministic trajectory profiles. |
| `tests/test_trajectory_spike.py` | Action-level trajectory capture. |
| `tests/test_trajectory_viewer.py` | Viewer HTTP, analysis endpoint, safety, and CLI behavior. |
| `tests/test_trajectory_viewer_data.py` | Read-only viewer data normalization. |
| `tests/test_trajectory_viewer_profile.py` | Viewer profile projection. |
| `tests/trajectory_viewer_fixtures.py` | Small replay and user-report fixtures shared by viewer tests. |

## Documentation and Pi reference

| File | Purpose |
| --- | --- |
| `docs/architecture-proposal.md` | Historical pre-0027 architecture baseline; current multi-Trajectory authority is Decision 0027 and the Harness-first guide. |
| `docs/artifact-comparator.md` | Comparator inputs, deterministic facts, and screenshot limits. |
| `docs/candidate-replay.md` | Candidate creation, sandbox replay, and release review flow. |
| `docs/evaluation-model.md` | Accepted evaluation principles plus clearly marked pre-0027 Synthesis/evidence-loop history. |
| `docs/file-catalog.md` | This repository structure and file-purpose index. |
| `docs/harness.md` | Deterministic Harness inputs, outputs, and failure handling. |
| `docs/multi-pi-analysis.md` | Harness-first raw-Trajectory research, isolated Pi startup, double-session capability certification, four independent Specialists, and no-aggregation boundary. |
| `docs/mvp-implementation-overview.html` | Owner-facing visual overview of the implemented Harness-first MVP and explicitly deferred aggregation/improvement stages. |
| `docs/replay.md` | Replay campaign schema and invocation behavior. |
| `docs/research-sandbox-tutorial.md` | 中文组件指南：一次研究运行内宿主侧 Pi 与容器侧 Docker 实验室的分工、十个研究工具、资源预算与残留进程证明、证据安全、Pi 启动隔离、execution identity 与 Harness 验收。 |
| `docs/single-trajectory-analysis.md` | Deterministic/LLM boundary, report flow, commands, and Skill package for one trajectory. |
| `docs/skill-contract.md` | Active package-local contract, validation, preflight, versioning, and known limits. |
| `docs/skill-hierarchy.md` | Skill-first data model, product/internal-research separation, structured JSON boundaries, application services, Viewer, migration, and open remediation. |
| `docs/task-case.md` | TaskCase input and expected-artifact contract. |
| `docs/trajectory-spike.md` | Manual single-run trajectory capture procedure. |
| `docs/trajectory-viewer.md` | Local read-only viewer API, interaction behavior, and explicit exclusion of internal research boards from product multi-Trajectory results. |
| `docs/trajectory-definition.md` | Evolution, current naming, compatibility, and format of action-level trajectory evidence. |
| `docs/pi-agent/README.md` | Pinned Pi package provenance and navigation. |
| `docs/pi-agent/rpc-client.md` | Project-specific Python Pi RPC client guidance, including optional environment replacement and targeted descriptor passing. |

`docs/pi-agent/upstream/` is the immutable Pi 0.81.1 documentation snapshot:

| File | Purpose |
| --- | --- |
| `CHANGELOG.md`, `PI_CODING_AGENT_README.md` | Upstream release history and top-level package readme. |
| `compaction.md`, `containerization.md`, `custom-provider.md`, `development.md` | Upstream maintenance and deployment guidance. |
| `docs.json`, `index.md`, `packages.md` | Upstream documentation index and package map. |
| `extensions.md`, `json.md`, `keybindings.md`, `skills.md`, `themes.md`, `tui.md` | Upstream extension, structured-output, UI, skill, theme, and terminal-interface references. |
| `llama-cpp.md`, `models.md`, `providers.md`, `prompt-templates.md` | Upstream model, provider, and prompt configuration references. |
| `quickstart.md`, `usage.md`, `terminal-setup.md`, `shell-aliases.md`, `tmux.md`, `termux.md`, `windows.md` | Upstream installation and environment-specific usage guides. |
| `rpc.md`, `sdk.md`, `session-format.md`, `sessions.md`, `settings.md`, `security.md` | Upstream integration, persistence, configuration, and security references. |
| `images/doom-extension.png`, `images/exy.png`, `images/interactive-mode.png`, `images/tree-view.png` | Images used by the upstream documentation snapshot. |

## Prompts, contracts, task cases, fixtures, and skill fixture

| File | Purpose |
| --- | --- |
| `contracts/schemas/analysis-record-v1.schema.json` | Skill-owned single- or multi-Trajectory Analysis record and result-reference contract. |
| `contracts/schemas/skill-contract-v2.schema.json` | Accepted strict JSON Schema for the package-local thin Skill Contract v2. |
| `contracts/schemas/evaluation-suite-v1.schema.json` | Strict approval and TaskCase/condition contract for coverage-bearing research suites. |
| `contracts/schemas/execution-set-v1.schema.json` | Same-Revision Execution Set identity and member contract. |
| `contracts/schemas/multi-trajectory-research-v1.schema.json` | Strict structured output contract for each internal multi-Trajectory Specialist. |
| `contracts/schemas/multi-trajectory-view-v1.schema.json` | Read-only product projection for an unavailable or future accepted multi-Trajectory Analysis. |
| `contracts/schemas/multi-trajectory-errors-v1.schema.json` | Read-only product projection for error-centric multi-Trajectory Analysis (error list + per-error dimension reports). |
| `contracts/schemas/research-harness-acceptance-v1.schema.json` | Historical first Harness acceptance shape, retained as an explicit superseded contract. |
| `contracts/schemas/research-harness-acceptance-v2.schema.json` | Active Harness contract binding semantic subchecks, full Pi/Docker execution identity and RPC policy, conditional real/faux sources, observed no-log isolation, implementation snapshots, and a replayable audit bundle. |
| `contracts/schemas/research-validation-benchmark-v1.schema.json` | Owner-reviewed hidden benchmark contract for two-session blind research validation. |
| `contracts/schemas/skill-execution-v1.schema.json` | Immutable Skill Execution identity, task, setup, artifact, Trajectory, and lifecycle contract. |
| `contracts/schemas/skill-revision-v1.schema.json` | Content-bound Skill Revision and active Contract reference contract. |
| `migrations/skill-hierarchy-map-v1.json` | Owner-reviewed legacy source suffix to stable Skill identity mapping. |
| `contracts/skills/document-html-visualizer-v1.json` | Historical proposed v1 capability contract retained for compatibility tests and old campaign reads. |
| `prompts/execution/document-html-visualizer-v1.md` | Version 1 execution prompt template. |
| `prompts/execution/document-html-visualizer-v1.md.approval.json` | Approval state and content hash for execution prompt v1. |
| `prompts/execution/document-html-visualizer-v2.md` | Proposed skill-neutral version 2 execution prompt template. |
| `prompts/execution/document-html-visualizer-v2.md.approval.json` | Approval state and content hash for execution prompt v2. |
| `prompts/analysis/candidate-proposer-v1.md` | CandidateProposer's approved-output instruction. |
| `prompts/analysis/candidate-proposer-v1.md.approval.json` | Approval state for CandidateProposer v1. |
| `prompts/analysis/capability-coverage-v1.md` | Pre-0027 legacy CapabilityCoverageAnalyst instruction; not a current research protocol. |
| `prompts/analysis/capability-coverage-v1.md.approval.json` | Historical approval state for the pre-0027 CapabilityCoverageAnalyst prompt. |
| `prompts/analysis/behavior-pattern-research-v1.md` | Proposed open-ended protocol for recurring problems, recovery/success, repeated scripts, and implicit behavior. |
| `prompts/analysis/behavior-pattern-research-v1.md.approval.json` | Proposed approval state for BehaviorPattern research v1. |
| `prompts/analysis/conditions-coverage-research-v1.md` | Proposed protocol for occurrence conditions, EvaluationSuite coverage, zero samples, and insufficient evidence. |
| `prompts/analysis/conditions-coverage-research-v1.md.approval.json` | Proposed approval state for conditions and coverage research v1. |
| `prompts/analysis/error-analyst-v1.md` | Approved subagent protocol: one identified error, four dimension modules, only problematic dimensions reported. |
| `prompts/analysis/error-analyst-v1.md.approval.json` | Approved content hash and owner record for the single-error analyst prompt. |
| `prompts/analysis/error-identification-v1.md` | Approved main-agent protocol: exhaustively identify errors affecting skill reusability/reliability and emit a structured error list. |
| `prompts/analysis/error-identification-v1.md.approval.json` | Approved content hash and owner record for the error-identification prompt. |
| `prompts/analysis/outcome-consistency-research-v1.md` | Proposed protocol for same-condition process stability and earliest divergence. |
| `prompts/analysis/outcome-consistency-research-v1.md.approval.json` | Proposed approval state for outcome and consistency research v1. |
| `prompts/analysis/research-harness-context-v1.json` | Proposed owner-reviewable manifest binding all prompt-visible research tool descriptions and hashes. |
| `prompts/analysis/research-harness-context-v1.json.approval.json` | Proposed approval state for the prompt-visible research Harness context. |
| `prompts/analysis/resource-efficiency-research-v1.md` | Proposed protocol for time, token, failed-action, rework, and efficient behavior comparisons. |
| `prompts/analysis/resource-efficiency-research-v1.md.approval.json` | Proposed approval state for resource-efficiency research v1. |
| `prompts/analysis/outcome-consistency-v1.md` | Pre-0027 legacy OutcomeConsistencyAnalyst instruction; not a current research protocol. |
| `prompts/analysis/outcome-consistency-v1.md.approval.json` | Historical approval state for the pre-0027 OutcomeConsistencyAnalyst prompt. |
| `prompts/analysis/replay-judge-v1.md` | Independent ReplayJudge instruction. |
| `prompts/analysis/replay-judge-v1.md.approval.json` | Approval state for ReplayJudge v1. |
| `prompts/analysis/resource-efficiency-v1.md` | Pre-0027 legacy ResourceEfficiencyAnalyst instruction; not a current research protocol. |
| `prompts/analysis/resource-efficiency-v1.md.approval.json` | Historical approval state for the pre-0027 ResourceEfficiencyAnalyst prompt. |
| `prompts/analysis/synthesis-v1.md` | Pre-0027 legacy SynthesisAgent instruction; Decision 0027 does not authorize Synthesis. |
| `prompts/analysis/synthesis-v1.md.approval.json` | Historical approval state for the pre-0027 SynthesisAgent prompt. |
| `prompts/analysis/trajectory-error-analysis-v1.md` | Approved semantic-only interpretation of prechecked single-trajectory signals, recovery, and causality. |
| `prompts/analysis/trajectory-error-analysis-v1.md.approval.json` | Approved content hash and owner record for TrajectoryErrorAnalyst v1. |
| `prompts/analysis/trajectory-error-analysis-v2.md` | Approved JSON-only single-trajectory semantic prompt with complete EvidenceRef examples and causal-link constraints. |
| `prompts/analysis/trajectory-error-analysis-v2.md.approval.json` | Owner approval and content hash for TrajectoryErrorAnalyst v2. |
| `prompts/analysis/trajectory-error-analysis-v3.md` | Proposed v3 single-trajectory prompt requiring Simplified Chinese in every user-facing narrative field. |
| `prompts/analysis/trajectory-error-analysis-v3.md.approval.json` | Proposed approval state for TrajectoryErrorAnalyst v3; it cannot run until owner approval. |
| `task-cases/document-formats/docx.json` | DOCX TaskCase input. |
| `task-cases/document-formats/inline-text.json` | Inline-text TaskCase input. |
| `task-cases/document-formats/markdown.json` | Markdown TaskCase input. |
| `task-cases/document-formats/pdf.json` | PDF TaskCase input. |
| `task-cases/document-formats/text.json` | Plain-text TaskCase input. |
| `evaluation-suites/document-html-visualizer-v2.json` | Proposed EvaluationSuite for document-format conditions and coverage; cannot gate research until owner approval. |
| `validation-benchmarks/document-html-visualizer-five-trajectory-v1.json` | Frozen hidden five-Trajectory acceptance benchmark for the repeated temporary generation-flow discovery. |
| `fixtures/document-formats/canonical.docx` | Canonical DOCX source fixture. |
| `fixtures/document-formats/canonical.md` | Canonical Markdown source fixture. |
| `fixtures/document-formats/canonical.pdf` | Canonical PDF source fixture. |
| `fixtures/document-formats/canonical.txt` | Canonical text source fixture. |
| `skills/document-html-visualizer-skill/SKILL.md` | Skill content under evaluation. |
| `skills/document-html-visualizer-skill/skill_contract.json` | Approved package-local runtime and EvaluationSuite binding for the evaluated Skill. |
| `skills/document-html-visualizer-skill/example/AI工具辅助方案_日常法务工作方向_V2.md` | Example input document used by the skill fixture. |
| `skills/analyze-single-trajectory/SKILL.md` | Deterministic-first workflow and semantic boundary for analyzing one trajectory. |
| `skills/analyze-single-trajectory/agents/openai.yaml` | UI name, description, and default invocation for the single-trajectory Skill. |
| `skills/analyze-single-trajectory/scripts/analyze_trajectory.py` | Bundled entry point for one approved semantic TrajectoryErrorAnalyst run. |
| `skills/analyze-single-trajectory/scripts/precheck_trajectory.py` | Bundled entry point for the repository precheck implementation. |
| `skills/analyze-single-trajectory/skill_contract.json` | Approved runtime and EvaluationSuite binding for the single-trajectory Skill. |

## Runtime extensions and viewer assets

| File | Purpose |
| --- | --- |
| `extensions/root-jail.ts` | Read-only/candidate-write confinement extension for analysis Pi processes. |
| `extensions/trajectory-error-output.ts` | Strict terminating structured-output tool for one-trajectory semantic reports. |
| `extensions/docker-tool-router.ts` | No-network Docker tool-routing extension for automatic replay. |
| `extensions/research-tools.ts` | Prompt-visible navigation, queries, Trajectory windows, disposable work, bounded Docker execution, synchronous process cleanup, and session poisoning. |
| `extensions/research-output.ts` | Sole terminating multi-Trajectory submission tool, cleanup-poison rejection, and session-start active-tool attestation. |
| `extensions/research-harness-driver.ts` | Trusted no-model Pi provider that deterministically exercises the production research tools during Harness acceptance. |
| `web/trajectory-viewer/index.html` | Chinese-first static shell for Skill, execution, input/output, trajectory, analysis, and improvement navigation. |
| `web/trajectory-viewer/app.js` | Skill → trajectory tree rendering, Chinese statuses and explanations, concise execution cards, single-trajectory reports, empty multi-trajectory state, URL state, and evidence navigation. |
| `web/trajectory-viewer/styles.css` | Responsive styling for the hierarchy tree, term tooltips, prompt disclosure, Skill home, execution detail, trajectory, analysis, and artifact views. |
