# Current project state

> Purpose: concise current implementation state, next action, and active risks.

Updated: 2026-08-17 after the owner reworked multi-Trajectory analysis to
error-centric product analysis (decision 0031) and removed the capability
certificate / blind-smoke research chain (decision 0032). A minimal runnable
closed loop now runs a main error-identification agent plus one per-error
subagent over the frozen corpus and produced the first real error list and
per-error reports. Frozen evidence keeps its original names and the reader
compatibility layer accepts both.

## Implemented and verified

- The authoritative product path remains `Skill → 执行 → 输入 / 输出 / trajectory /
  单 trajectory 分析`, with an expandable `Skill → trajectory → 单 trajectory 分析` sidebar.
  Skill Revision is an immutable execution binding and filter, not a required
  navigation layer.
- The current runtime has 1 Skill, 2 Skill Revisions, 2 Execution Sets, 11
  Executions, 31 single-trajectory Analysis records, and 0 product multi-trajectory
  Analyses. All eleven Executions have an accepted deterministic precheck and
  an accepted structured v2 semantic result. Failed historical attempts remain
  preserved and do not masquerade as conclusions.
- Multi-Trajectory analysis is error-centric (decision 0031): a main
  ErrorIdentifier identifies every error that affects skill reusability or
  reliability, then one ErrorAnalyst subagent re-derives each error from the raw
  material and reports only the dimensions that show a problem (behavior /
  conditions / consistency / resource). The error list and per-error reports are
  the user-visible product, not an internal research layer.
- The navigation index covers every action and locator while preserving the
  original observable Trajectory, scripts, safe text artifacts, accepted
  single-Trajectory reports, failures, recovery, verification, and resource facts.
  It supplies navigation rather than precomputed semantic conclusions.
- The research laboratory mounts evidence read-only, gives each run a
  disposable quota-limited workspace, disables network and built-in host tools,
  removes credentials, runs non-root, enforces process/time/output/disk and
  cumulative tool budgets, verifies evidence before and after use, and never
  falls back to host execution. Container logging is disabled; PID 1 reaps
  children, every tool proves zero residual processes, and a cleanup failure
  permanently invalidates the session and its submission.
- Deterministic Harness acceptance now uses the strict v2 report. A trusted
  no-model provider goes through Pi 0.81.1's normal Agent loop and the production
  search, read, query, Trajectory-window, script execution, work-file, and terminating
  submission tools. Fixed positive and negative checks bind the corpus,
  baseline, actual Docker configuration, limits, implementation snapshots,
  command plan, Pi trajectories, workspace output, and replayable audit tree.
- Callers cannot provide Harness booleans or import an external acceptance
  report. Every batch reload revalidates the report file, audit bundle, corpus,
  baseline, image, result references, capability identity, and board artifacts.
- The execution identity covers the complete first-party Python dependency
  closure, Pi executable/interpreters/npm package tree, allowed launch args,
  Docker client/interpreters/context/effective endpoint, daemon engine ID, and
  stable daemon security facts. It is rechecked at Harness reload, Pi spawn,
  sandbox preflight, and immediately before container creation.
- Research Pi startup accepts one direct package-bound executable and rejects
  wrappers, `npm/npx`, shells, and `python -m/-c`. It replaces the host
  environment, creates one-shot HOME/tmp/Pi directories, disables sessions,
  project approval, built-in tools and all discovered resources, and loads only
  the identity-bound real or Harness-faux extension set plus ten research
  tools.
- A real provider receives only its selected literal API key through a
  read-only temporary descriptor; the complete auth file and env/command/OAuth
  credentials are not copied. Real models come from the bound Pi package and
  the Harness faux model only from the fixed attested driver. Before Prompt,
  Runtime verifies exact provider/model/thinking, no session file, selected
  model availability, and one exact active-tools attestation. The approved
  Harness context is an identity manifest, not a system/append prompt.
- The deterministic public drive cannot accept arbitrary extensions or a
  caller-authored Docker router. Fixed extension paths/hashes and the complete
  sandbox backend, image, control plane, container ID, limits, Docker command,
  and tool budgets must match the execution identity before workspace creation.
  Harness report summaries for validator, Runtime, tools, output, and driver
  must also match the same complete implementation fingerprint.
- Harness reports, audit manifests/inventories, and result references are read
  once from pinned no-follow file handles. Ancestor links, trust-root changes,
  and read-time inode replacement fail closed. Evidence tree hashing uses the
  same per-file handle rule.
- Formal findings require a complete denominator, affected and checked-absent
  Trajectories, original evidence, counterexample scope, derivations, limitations, and
  confidence. Repeated behavior needs at least two independent supporting
  Trajectories. Duplicate submissions and any action after submission are invalid.
- The earlier research certification chain (decision 0027: two-session blind
  smoke, hidden-benchmark review, portable capability certificate, and the four
  isolated dimension Specialists) is legacy per decision 0032 and no longer sits
  in the product path. Its code and frozen batch data remain on disk but are not
  executed by the error-centric flow.
- `scripts/multi_trajectory_research.py` remains as the legacy research CLI
  (readiness, corpus build, Harness acceptance, smoke, review, certificate,
  specialists); the product entry point is now `scripts/error_analysis.py run`.
- Research-related regression passes 183/183. The last full run with loopback
  permission passed 382/382; after the current expansion, 388 of 395 tests pass
  in the stricter default sandbox and the remaining seven stop at socket setup
  only because loopback binding raises `PermissionError`. Installed Pi 0.81.1
  completed no-model state, selected-model availability, and exact active-tool
  attestation checks without using the real credential directory.

## Prepared real research data

- The five current successful document-visualizer Executions belong to
  `rev-d06ece0ddc22cb38` and pass the behavior-pattern smoke readiness gate.
- They are frozen as `corpus-8d046a5ca7b1a110f37a`, content digest
  `8d046a5ca7b1a110f37a7f3eb0c9dd15b948a87ee9837d8d6e7323a28690d1f6`,
  with baseline digest
  `80260aa69546ac3a5fb471c58c9d10e01e92a344d0e78bc32c28c00bd81f3dc2`.
- The frozen corpus `corpus-8d046a5ca7b1a110f37a` is the input to the
  error-centric analysis. On 2026-08-17 the minimal closed loop ran the main
  ErrorIdentifier plus three ErrorAnalyst subagents for real (DeepSeek) and
  produced a three-error list (E1 output-token-limit write failure, E2 generated
  script KeyError, E3 grep/diff false-positive validation) with per-error
  reports covering only the problematic dimensions. All runs succeeded.
- The legacy blind-smoke batch `research-smoke-...-five-trajectory-v1` and its
  frozen hidden benchmark are no longer used by the product flow (decision 0032).

## Approved production boundaries

- `skill.contract.v2`, the current document HTML Skill Contract, and Decision
  0026's structured single-Trajectory submission boundary.
- `prompts/analysis/trajectory-error-analysis-v1.md` and approved production v2.
- Decision 0027's certification chain is superseded by 0031 (error-centric
  product analysis) and 0032 (cert chain removed). The deterministic Harness
  acceptance and sandbox isolation remain available as verification
  infrastructure but no longer gate the product flow.
- Two new protocols are approved: `error-identification-v1.md` (main agent) and
  `error-analyst-v1.md` (per-error subagent); the four former role protocols are
  repurposed as dimension modules (decision 0030 inline schemas).

## Awaiting owner approval or external provisioning

- `evaluation-suites/document-html-visualizer-v2.json` remains `proposed`; the
  error analysis "conditions" dimension still cannot cite it for coverage
  claims until it is approved and snapshotted.
- `prompts/execution/document-html-visualizer-v2.md` and
  `prompts/analysis/trajectory-error-analysis-v3.md` remain proposed.
- The product UI still shows the multi-Trajectory analysis section as empty
  ("能力尚未实现"); wiring the error list + per-error reports into that section
  is the next implementation task.

## Active risks

- Seven historical records remain hidden from product multi-Trajectory reads but
  still exist on disk: four old Harness checks and three unfinished multi-role
  attempts. Permanent deletion requires new explicit authorization covering all
  seven paths.
- Future Capture/Replay may still reread the mutable source Skill during startup
  after registering its frozen Revision. It must execute only the frozen copy
  before new capture is considered fully integrity-safe.
- Legacy Harness compatibility still materializes an old Campaign shape and may
  preserve temporary paths. New research uses stable corpus/batch identities,
  but compatibility remediation remains open.
- The provisional hierarchy Improvements model is not the accepted
  Candidate/Comparison/Review chain and must not receive production data.
- Chrome crash preflight and Docker gates for future Candidate evaluation remain
  open. The repository directory is not a Git worktree, so candidate diffs are
  framework-computed.
- The Docker mount and no-follow protections isolate the untrusted Agent from
  host evidence. They do not claim protection from a separate host process that
  already has the same UID and can replace and restore evidence throughout the
  complete Agent window; that stronger threat requires an OS identity, ACL, or
  immutable-snapshot boundary.

## Next action

Wire the error-centric analysis into the product web app: add a writer that
stores the error list + per-error reports under the existing multi-Trajectory
analysis model, and render them in `web/trajectory-viewer` under the Skill's
"多 trajectory 分析" section, consistent with the current Chinese-first style.
The complete priority order remains in `.plan/next.md`.
