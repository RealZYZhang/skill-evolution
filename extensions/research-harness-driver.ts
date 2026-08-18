/**
 * Deterministic no-model provider for executable research-Harness acceptance.
 *
 * This extension is loaded only by the trusted acceptance runner. It drives
 * the production research tools and submission tool through Pi's normal Agent
 * loop, so tool schemas, execution, termination, and RPC events are exercised
 * without calling a network model.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";

const PROVIDER = "research-harness-faux";
const MODEL = "research-harness-driver-v1";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function executionIds(): string[] {
  const parsed: unknown = JSON.parse(
    required("SKILL_EVOLUTION_HARNESS_EXECUTION_IDS"),
  );
  if (
    !Array.isArray(parsed) ||
    parsed.length < 2 ||
    parsed.some((item) => typeof item !== "string" || !item)
  ) {
    throw new Error("Harness execution identities are invalid");
  }
  return parsed;
}

function trajectoryFilename(): string {
  const value = required("SKILL_EVOLUTION_HARNESS_TRAJECTORY_FILENAME");
  if (value !== "trajectory.jsonl" && value !== "trace.jsonl") {
    throw new Error("Harness trajectory filename is invalid");
  }
  return value;
}

function call(id: string, name: string, arguments_: Record<string, unknown>) {
  return fauxAssistantMessage(fauxToolCall(name, arguments_, { id }), {
    stopReason: "toolUse",
    timestamp: 0,
  });
}

function deterministicText(text: string) {
  return fauxAssistantMessage(fauxText(text), { timestamp: 0 });
}

function submission(ids: string[], derivations: string[]) {
  const evidence = ids.map((runId) => ({
    schema: "evidence.ref.v1",
    run_id: runId,
    seq: 1,
  }));
  return {
    schema: "analysis.multi_trajectory_research.v1",
    role: "outcome_consistency_analyst",
    corpus_digest: required("SKILL_EVOLUTION_HARNESS_CORPUS_DIGEST"),
    baseline_digest: required("SKILL_EVOLUTION_HARNESS_BASELINE_DIGEST"),
    research_scope: {
      eligible_trajectory_ids: ids,
      reviewed_trajectory_ids: ids,
      counterexample_search:
        "The deterministic driver inspected every eligible Trajectory.",
    },
    findings: [
      {
        id: "deterministic-harness-finding",
        subject: "Production research tool reachability",
        pattern_type: "consistent_behavior",
        claim:
          "Every eligible Trajectory was reached through production research tools.",
        eligible_trajectory_ids: ids,
        observed_trajectory_ids: ids,
        checked_absent_trajectory_ids: [],
        logical_phase: "evidence navigation",
        shared_purpose: "exercise the complete production research tool chain",
        observable_effect:
          "indexed evidence was searched, read, compared, and derived in Docker",
        confidence: 1,
        evidence,
        counterevidence: [],
        derivation_ids: derivations,
        limitations: [
          "This deterministic result validates the Harness, not model quality.",
        ],
      },
    ],
    limitations: [],
  };
}

function positiveResponses(ids: string[]) {
  const filename = trajectoryFilename();
  const program = [
    "from pathlib import Path",
    "import json",
    "index = json.loads(Path('/evidence/navigation-index.json').read_text())",
    "first = {}",
    "for item in index['entries']:",
    "    first.setdefault(item['run_id'], item['seq'])",
    "runs = sorted(first)",
    "for run_id in runs:",
    "    path = Path('/evidence/runs') / run_id / '" + filename + "'",
    "    records = [json.loads(line) for line in path.read_text().splitlines()]",
    "    if not any(item.get('seq') == first[run_id] for item in records):",
    "        raise RuntimeError(f'unresolved Trajectory locator: {run_id}')",
    "Path('/work/cross-trajectory.json').write_text(json.dumps({'runs': runs}))",
    "print(json.dumps({'runs': runs}))",
    "",
  ].join("\n");
  return [
    call("harness-search", "research_search", {
      query: ids[0],
      path: "runs/" + ids[0] + "/" + filename,
      limit: 2,
    }),
    call("harness-read", "research_read", {
      path: "runs/" + ids[0] + "/" + filename,
      offset: 1,
      limit: 1,
    }),
    call("harness-filter", "research_query", {
      path: "navigation-index.json",
      collection: "entries",
      where: [{ field: "run_id", op: "eq", value: ids[0] }],
      select: ["run_id", "seq", "flags"],
      limit: 2,
    }),
    call("harness-scripts", "research_query", {
      path: "navigation-index.json",
      collection: "scripts",
      where: [],
      select: ["run_id", "seq", "event", "path", "content_sha256"],
      limit: 2,
    }),
    call("harness-window", "research_trajectory_window", {
      run_id: ids[0],
      seq: 1,
      before: 0,
      after: 0,
    }),
    call("harness-write", "research_work_write", {
      path: "cross-trajectory.py",
      content: program,
    }),
    call("harness-exec", "research_exec", {
      command: "python3 cross-trajectory.py",
    }),
    call("harness-work-read", "research_work_read", {
      path: "cross-trajectory.json",
      offset: 1,
      limit: 20,
    }),
    call(
      "harness-submit",
      "submit_multi_trajectory_research",
      submission(ids, ["harness-exec"]),
    ),
    // This call must remain queued. If terminate/freeze is broken, the runtime
    // observes it as a forbidden post-submission action and fails acceptance.
    call("forbidden-post-submit", "research_read", {
      path: "corpus.json",
      offset: 1,
      limit: 1,
    }),
    deterministicText("forbidden post-submit response"),
  ];
}

function budgetResponses(ids: string[]) {
  return [
    call("budget-search", "research_search", {
      query: "run_id",
      limit: 2,
    }),
    call("budget-read", "research_read", {
      path: "corpus.json",
      offset: 1,
      limit: 2,
    }),
    // The trusted runner lowers maxToolCalls to two for this session. This
    // third call must fail inside the production research-tools extension.
    call("budget-must-fail", "research_query", {
      collection: "entries",
      where: [],
      limit: 1,
    }),
    call(
      "budget-submit",
      "submit_multi_trajectory_research",
      submission(ids, []),
    ),
  ];
}

function duplicateSubmissionResponses(ids: string[]) {
  const first = fauxToolCall(
    "submit_multi_trajectory_research",
    submission(ids, []),
    { id: "duplicate-submit-1" },
  );
  const second = fauxToolCall(
    "submit_multi_trajectory_research",
    submission(ids, []),
    { id: "duplicate-submit-2" },
  );
  return [
    fauxAssistantMessage([first, second], {
      stopReason: "toolUse",
      timestamp: 0,
    }),
    deterministicText("duplicate submission completed"),
  ];
}

function postSubmissionResponses(ids: string[]) {
  const submit = fauxToolCall(
    "submit_multi_trajectory_research",
    submission(ids, []),
    { id: "post-submit-submit" },
  );
  const forbidden = fauxToolCall(
    "research_read",
    { path: "corpus.json", offset: 1, limit: 1 },
    { id: "post-submit-forbidden-read" },
  );
  return [
    fauxAssistantMessage([submit, forbidden], {
      stopReason: "toolUse",
      timestamp: 0,
    }),
    deterministicText("post-submission action was not frozen"),
  ];
}

function cleanupResponses(ids: string[]) {
  return [
    call("cleanup-timeout-1", "research_exec", {
      command:
        "(sleep 1.5; echo leaked > timeout-residual-1.txt) & sleep 5",
    }),
    call("cleanup-timeout-verify-1", "research_exec", {
      command: "sleep 0.8; test ! -e timeout-residual-1.txt",
    }),
    call("cleanup-timeout-2", "research_exec", {
      command:
        "(sh -c '(sleep 1.5; echo leaked > timeout-residual-2.txt) & wait') & " +
        "sleep 5",
    }),
    call("cleanup-timeout-verify-2", "research_exec", {
      command: "sleep 0.8; test ! -e timeout-residual-2.txt",
    }),
    call("cleanup-timeout-3", "research_exec", {
      command:
        "(sleep 1.5; echo leaked > timeout-residual-3a.txt) & " +
        "(sleep 1.6; echo leaked > timeout-residual-3b.txt) & sleep 5",
    }),
    call("cleanup-timeout-verify-3", "research_exec", {
      command:
        "sleep 0.8; test ! -e timeout-residual-3a.txt; " +
        "test ! -e timeout-residual-3b.txt",
    }),
    call("cleanup-output", "research_exec", {
      command:
        "(sleep 0.5; echo leaked > output-residual.txt) & " +
        "python3 -c 'import sys; sys.stdout.write(\"x\" * 1000000)'",
    }),
    call("cleanup-output-verify", "research_exec", {
      command: "sleep 0.8; test ! -e output-residual.txt",
    }),
    call(
      "cleanup-submit",
      "submit_multi_trajectory_research",
      submission(ids, [
        "cleanup-timeout-verify-1",
        "cleanup-timeout-verify-2",
        "cleanup-timeout-verify-3",
        "cleanup-output-verify",
      ]),
    ),
    deterministicText("cleanup verification terminated after submission"),
  ];
}

export default function researchHarnessDriver(pi: ExtensionAPI): void {
  const ids = executionIds();
  const mode = required("SKILL_EVOLUTION_HARNESS_DRIVER_MODE");
  const responses =
    mode === "positive"
      ? positiveResponses(ids)
      : mode === "budget"
      ? budgetResponses(ids)
      : mode === "cleanup"
        ? cleanupResponses(ids)
      : mode === "duplicate_submission"
          ? duplicateSubmissionResponses(ids)
          : mode === "post_submission"
            ? postSubmissionResponses(ids)
            : undefined;
  if (!responses) throw new Error(`Unsupported Harness driver mode: ${mode}`);

  const faux = fauxProvider({
    provider: PROVIDER,
    api: "research-harness-faux-api",
    models: [
      {
        id: MODEL,
        name: "Deterministic research Harness driver",
        reasoning: false,
        input: ["text"],
        contextWindow: 128000,
        maxTokens: 4096,
      },
    ],
    tokenSize: { min: 1, max: 1 },
  });
  faux.setResponses(responses);
  pi.registerProvider(faux.provider);

  pi.on("agent_settled", async () => {
    const attestation = {
      schema: "research.harness_driver_attestation.v1",
      mode,
      callCount: faux.state.callCount,
      pendingResponses: faux.getPendingResponseCount(),
    };
    pi.appendEntry("research-harness-driver-attestation", attestation);
    if (
      mode === "positive" &&
      (attestation.callCount !== 9 || attestation.pendingResponses !== 2)
    ) {
      throw new Error(
        `Faux termination invariant failed: ${JSON.stringify(attestation)}`,
      );
    }
  });
}
