# HTML Artifact Comparator

`scripts/artifact_comparator.py` is a read-only harness component. It extracts
objective facts from HTML artifacts and compares repeated outputs without
assigning a quality score, ranking a run, or modifying preserved replay data.

## Run it

```bash
python3 scripts/artifact_comparator.py \
  --campaign .skill-evolution/replays/<campaign-id> \
  --output .skill-evolution/harness-runs/<harness-id>/artifact-comparison.json
```

Add `--screenshots` to request fixed desktop (`1440x900`) and mobile
(`390x844`) captures. `--chrome` may name a Chrome executable when automatic
discovery is insufficient. The common HarnessRun orchestrator is responsible
for choosing the output directory and combining this report with the
trajectory profile.

The command exits successfully for `complete` and `partial` reports. It exits
with status 1 only when no HTML artifact can be compared, and status 2 for an
invalid invocation or unreadable campaign.

## Output contract

The report schema is `artifact.comparison.v1`:

- `artifacts` contains one static fact record per declared HTML artifact.
- `pairwise` compares artifacts with the same expected-artifact path across
  runs. Different HTML roles are never compared to one another.
- `source` contains deterministic Markdown source facts when a Markdown input
  snapshot is available.
- `issues` keeps missing, unsafe, unsupported, and screenshot problems
  visible.

Each artifact has an `evidence.ref.v1` root containing campaign, run, and
artifact path. Nested HTML facts carry artifact-local line and selector
locations. Combining the root with a local location produces a stable
down-drill reference without repeating the full path hundreds of times.

The static parser records:

- DOM element and depth counts, tag counts, heading outline, landmarks,
  classes, custom elements, `data-component` values, and tables;
- IDs, duplicate IDs, local anchors, external links, ARIA/label ID references,
  and unresolved references;
- external HTML and CSS dependencies;
- CSS variable names and values, colors, fonts, and media queries;
- inline and external script counts and inline byte size;
- normalized visible text and evidence-linked text blocks.

For Markdown input it separately records preservation facts for heading order,
number occurrences, URLs, table headers, and normalized text blocks. These are
literal observations, not semantic-fidelity judgments.

Pairwise deltas report scalar changes, changed tag and landmark counts, class
and CSS set differences, heading-outline differences, unresolved-reference
changes, script-size differences, and visible-text block overlap. There is no
aggregate score, preferred artifact, or integrity hash.

## Screenshot isolation

Screenshots render a temporary copy; the original HTML is never edited. The
copy receives:

- a CSP that denies network, frames, objects, forms, and untrusted scripts;
- a nonce-limited local browser probe;
- CSS that disables animation, transition, and smooth scrolling.

If Chrome is absent or a capture fails, static comparison remains available
and the report status becomes `partial`. Screenshot failure must not be
interpreted as an artifact-quality failure.

## Current limitations

- The parser is tolerant and dependency-free; it does not implement the HTML5
  browser tree-construction algorithm.
- Markdown preservation is literal and deterministic. Reworded but
  semantically equivalent content may appear as not preserved.
- Screenshot capture records files and viewport facts. It does not perform
  pixel comparison or visual-model evaluation.
- Non-Markdown source formats are recorded as unsupported for source
  preservation until their format adapters are implemented.
