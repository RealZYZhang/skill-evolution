/**
 * Docker-routed tools for evidence-backed multi-Trajectory research.
 *
 * The host Pi process keeps model credentials. Every tool executes in one
 * pre-created container with `/evidence` mounted read-only and `/work` backed
 * by a quota-limited tmpfs. Built-in Pi tools must remain disabled.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import crypto from "node:crypto";

const MAX_PAGE_SIZE = 200;
const MAX_READ_LINES = 1000;
const MAX_WINDOW = 20;
const MAX_WRITE_CHARACTERS = 1_000_000;
const MAX_EDIT_CHARACTERS = 200_000;
const MAX_COMMAND_CHARACTERS = 20_000;
const SESSION_POISON_ENV = "SKILL_EVOLUTION_RESEARCH_SESSION_POISONED";

const PROCESS_CLEANUP = String.raw`
import json
import os
import signal
import time

SELF = os.getpid()
DEADLINE = time.monotonic() + 5
ROUNDS = 0

while True:
    observed = sorted(
        int(name) for name in os.listdir("/proc") if name.isdigit()
    )
    residual = [pid for pid in observed if pid not in (1, SELF)]
    if not residual:
        print(json.dumps({
            "snapshot": ["pid1", "cleanup"],
            "observed_process_count": len(observed),
            "residual_process_count": 0,
            "rounds": ROUNDS,
        }, separators=(",", ":")))
        break
    ROUNDS += 1
    for pid in residual:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if time.monotonic() >= DEADLINE:
        raise RuntimeError(f"residual processes after cleanup: {residual}")
    time.sleep(0.01)
`;

const PYTHON_HELPER = String.raw`
import hashlib
import json
import os
import stat
import sys
import tempfile

EVIDENCE = os.path.realpath("/evidence")
WORK = os.path.realpath("/work")
MAX_LINE_CHARS = 20000
MAX_MATCH_CHARS = 4000
MAX_RECORD_CHARS = 50000

def fail(message):
    raise ValueError(message)

def request():
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        fail("Tool parameters must be an object")
    return value

def relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        fail("Path must be a non-empty relative path")
    if value.startswith("/") or "\\" in value:
        fail("Absolute and backslash paths are not allowed")
    if ".." in value.split("/"):
        fail("Parent traversal is not allowed")
    normalized = os.path.normpath(value)
    if normalized == ".." or normalized.startswith("../"):
        fail("Parent traversal is not allowed")
    return "." if normalized == "." else normalized

def reject_link_components(root, candidate):
    rel = os.path.relpath(candidate, root)
    if rel == ".":
        return
    current = root
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
            fail("Symlink paths are not allowed")

def confined(root, value, must_exist=True):
    root = os.path.realpath(root)
    normalized = relative(value)
    candidate = os.path.abspath(os.path.join(root, normalized))
    if os.path.commonpath([root, candidate]) != root:
        fail("Path escapes its configured root")
    reject_link_components(root, candidate)
    if must_exist and not os.path.exists(candidate):
        fail("Requested path does not exist")
    existing = candidate
    while not os.path.exists(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            fail("Requested path has no existing parent")
        existing = parent
    if os.path.commonpath([root, os.path.realpath(existing)]) != root:
        fail("Path escapes through an existing parent")
    return candidate

def regular_file(root, value):
    candidate = confined(root, value, True)
    metadata = os.lstat(candidate)
    if not stat.S_ISREG(metadata.st_mode):
        fail("Requested path must be a regular file")
    return candidate

def clipped(text, maximum):
    if len(text) <= maximum:
        return text, False
    return text[:maximum], True

def page(values, cursor, limit):
    selected = values[cursor:cursor + limit]
    next_cursor = cursor + len(selected)
    return selected, (next_cursor if next_cursor < len(values) else None)

def op_list(params):
    path = params.get("path", ".")
    cursor = params.get("cursor", 0)
    limit = params.get("limit", 100)
    directory = confined(EVIDENCE, path, True)
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        fail("research_list path must be a directory")
    rows = []
    for name in sorted(os.listdir(directory)):
        candidate = os.path.join(directory, name)
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode):
            fail("Evidence contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            fail("Evidence contains a special file")
        rows.append({
            "path": os.path.relpath(candidate, EVIDENCE).replace(os.sep, "/"),
            "type": kind,
            "bytes": metadata.st_size if kind == "file" else None,
        })
    selected, next_cursor = page(rows, cursor, limit)
    return {
        "path": relative(path),
        "items": selected,
        "total": len(rows),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }

def read_lines(root, params):
    path = params["path"]
    offset = params.get("offset", 1)
    limit = params.get("limit", 200)
    target = regular_file(root, path)
    rows = []
    total = 0
    with open(target, "r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, start=1):
            total = line_number
            if line_number < offset or len(rows) >= limit:
                continue
            value, truncated = clipped(line.rstrip("\r\n"), MAX_LINE_CHARS)
            rows.append({
                "line": line_number,
                "text": value,
                "text_truncated": truncated,
            })
    next_offset = offset + len(rows)
    return {
        "path": relative(path),
        "offset": offset,
        "lines": rows,
        "total_lines": total,
        "next_offset": next_offset if next_offset <= total else None,
        "truncated": next_offset <= total,
    }

def iter_files(root, subtree):
    target = confined(root, subtree, True)
    metadata = os.lstat(target)
    if stat.S_ISREG(metadata.st_mode):
        yield target
        return
    if not stat.S_ISDIR(metadata.st_mode):
        fail("Search path must be a file or directory")
    for current, directories, files in os.walk(target, followlinks=False):
        directories.sort()
        files.sort()
        for name in list(directories):
            candidate = os.path.join(current, name)
            if stat.S_ISLNK(os.lstat(candidate).st_mode):
                fail("Evidence contains a symlink")
        for name in files:
            candidate = os.path.join(current, name)
            metadata = os.lstat(candidate)
            if not stat.S_ISREG(metadata.st_mode):
                fail("Evidence contains a symlink or special file")
            yield candidate

def binary_file(path):
    with open(path, "rb") as stream:
        return b"\x00" in stream.read(8192)

def op_search(params):
    query = params["query"].casefold()
    subtree = params.get("path", ".")
    cursor = params.get("cursor", 0)
    limit = params.get("limit", 100)
    selected = []
    total_matches = 0
    skipped_binary_paths = []
    skipped_binary_count = 0
    for candidate in iter_files(EVIDENCE, subtree):
        relative_candidate = os.path.relpath(candidate, EVIDENCE).replace(
            os.sep, "/"
        )
        if binary_file(candidate):
            skipped_binary_count += 1
            if len(skipped_binary_paths) < 100:
                skipped_binary_paths.append(relative_candidate)
            continue
        with open(candidate, "r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, start=1):
                if query not in line.casefold():
                    continue
                if cursor <= total_matches < cursor + limit:
                    value, truncated = clipped(
                        line.rstrip("\r\n"), MAX_MATCH_CHARS
                    )
                    selected.append({
                        "path": relative_candidate,
                        "line": line_number,
                        "text": value,
                        "text_truncated": truncated,
                    })
                total_matches += 1
    next_cursor_value = cursor + len(selected)
    next_cursor = (
        next_cursor_value if next_cursor_value < total_matches else None
    )
    return {
        "query": params["query"],
        "path": relative(subtree),
        "matches": selected,
        "total_matches": total_matches,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
        "skipped_binary_count": skipped_binary_count,
        "skipped_binary_paths": skipped_binary_paths,
        "skipped_binary_paths_truncated": skipped_binary_count > 100,
    }

def dotted(record, field):
    current = record
    for part in field.split("."):
        if not part:
            fail("Query fields must not contain empty path segments")
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current

def compare_filter(record, condition):
    present, current = dotted(record, condition["field"])
    operator = condition["op"]
    expected = condition.get("value")
    if operator == "exists":
        return present is bool(expected)
    if not present:
        return False
    if operator == "eq":
        return current == expected
    if operator == "ne":
        return current != expected
    if operator == "contains":
        if isinstance(current, str) and isinstance(expected, str):
            return expected.casefold() in current.casefold()
        if isinstance(current, list):
            return expected in current
        return False
    if operator == "in":
        return isinstance(expected, list) and current in expected
    if operator == "gte":
        try:
            return current >= expected
        except TypeError:
            return False
    if operator == "lte":
        try:
            return current <= expected
        except TypeError:
            return False
    fail("Unsupported query operator")

def projected(record, fields):
    if not fields:
        return record
    value = {}
    for field in fields:
        present, item = dotted(record, field)
        if present:
            value[field] = item
    return value

def op_query(params):
    path = params.get("path", "navigation-index.json")
    collection = params.get("collection", "entries")
    cursor = params.get("cursor", 0)
    limit = params.get("limit", 100)
    conditions = params.get("where", [])
    select = params.get("select", [])
    target = regular_file(EVIDENCE, path)
    indexed_records = []
    if path.endswith(".jsonl"):
        with open(target, "r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    fail(f"Index line {line_number} is invalid JSON: {error}")
                indexed_records.append((line_number, record))
    else:
        with open(target, "r", encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict):
            fail("Navigation index must be an object")
        records = document.get(collection)
        if not isinstance(records, list):
            fail("Navigation index collection must be a list")
        indexed_records = list(enumerate(records, start=1))
    matches = []
    for position, record in indexed_records:
        if not isinstance(record, dict):
            fail(f"Index position {position} is not an object")
        if all(compare_filter(record, condition) for condition in conditions):
            matches.append({
                "index_position": position,
                "record": projected(record, select),
            })
    selected, next_cursor = page(matches, cursor, limit)
    return {
        "path": relative(path),
        "collection": collection,
        "records": selected,
        "total_matches": len(matches),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }

def bounded_record(record):
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= MAX_RECORD_CHARS:
        return {"record": record, "truncated": False}
    return {
        "seq": record.get("seq") if isinstance(record, dict) else None,
        "record_preview": encoded[:MAX_RECORD_CHARS],
        "record_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "truncated": True,
    }

def op_trajectory_window(params):
    run_id = params["run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or "/" in run_id
        or "\\" in run_id
        or run_id in {".", ".."}
    ):
        fail("run_id must be one path-safe identifier")
    target_seq = params["seq"]
    before = params.get("before", 2)
    after = params.get("after", 2)
    path = f"runs/{run_id}/trajectory.jsonl"
    if not os.path.exists(confined(EVIDENCE, path, False)):
        path = f"runs/{run_id}/trace.jsonl"
    target = regular_file(EVIDENCE, path)
    records = []
    found = False
    lower = max(1, target_seq - before)
    upper = target_seq + after
    with open(target, "r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as error:
                fail(f"Trajectory line {line_number} is invalid JSON: {error}")
            if not isinstance(record, dict):
                fail(f"Trajectory line {line_number} is not an object")
            seq = record.get("seq")
            if seq == target_seq:
                found = True
            if isinstance(seq, int) and lower <= seq <= upper:
                records.append(bounded_record(record))
    if not found:
        fail("Requested trajectory seq does not exist")
    return {
        "run_id": run_id,
        "target_seq": target_seq,
        "before": before,
        "after": after,
        "records": records,
    }

def atomic_write(params):
    path = params["path"]
    content = params["content"]
    target = confined(WORK, path, False)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    confined(WORK, os.path.relpath(parent, WORK), True)
    if os.path.lexists(target):
        metadata = os.lstat(target)
        if not stat.S_ISREG(metadata.st_mode):
            fail("Work target must be a regular file")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".research-write-", dir=parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    encoded = content.encode("utf-8")
    return {
        "path": relative(path),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }

def op_edit(params):
    target = regular_file(WORK, params["path"])
    with open(target, "r", encoding="utf-8") as stream:
        original = stream.read()
    old = params["old_text"]
    first = original.find(old)
    if first < 0:
        fail("old_text was not found")
    if original.find(old, first + len(old)) >= 0:
        fail("old_text is ambiguous; it occurs more than once")
    updated = original[:first] + params["new_text"] + original[first + len(old):]
    return atomic_write({"path": params["path"], "content": updated})

PARAMS = request()
OPERATION = sys.argv[1]
if OPERATION == "list":
    RESULT = op_list(PARAMS)
elif OPERATION == "read_evidence":
    RESULT = read_lines(EVIDENCE, PARAMS)
elif OPERATION == "search":
    RESULT = op_search(PARAMS)
elif OPERATION == "query":
    RESULT = op_query(PARAMS)
elif OPERATION == "trajectory_window":
    RESULT = op_trajectory_window(PARAMS)
elif OPERATION == "read_work":
    RESULT = read_lines(WORK, PARAMS)
elif OPERATION == "write_work":
    RESULT = atomic_write(PARAMS)
elif OPERATION == "edit_work":
    RESULT = op_edit(PARAMS)
else:
    fail("Unsupported research helper operation")
json.dump(RESULT, sys.stdout, ensure_ascii=False, separators=(",", ":"))
`;

type DockerResult = {
  stdout: string;
  stderr: string;
  exitCode: number;
  timedOut: boolean;
  aborted: boolean;
  outputLimitExceeded: boolean;
  stdoutBytes: number;
  stderrBytes: number;
  stdoutSha256: string;
  stderrSha256: string;
  cleanupVerified: boolean;
  cleanupSnapshot: ["pid1", "cleanup"];
  cleanupObservedProcessCount: number;
  cleanupResidualProcessCount: number;
  cleanupRounds: number;
};

type ProcessCleanupResult = {
  snapshot: ["pid1", "cleanup"];
  observedProcessCount: number;
  residualProcessCount: number;
  rounds: number;
};

function requireValue(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function positiveInteger(name: string, maximum: number): number {
  const raw = requireValue(name);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0 || value > maximum) {
    throw new Error(`${name} must be a positive integer at most ${maximum}`);
  }
  return value;
}

function validateContainer(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)) {
    throw new Error("Research container id is invalid");
  }
  return value;
}

function cleanupContainerProcesses(
  docker: string,
  container: string,
): Promise<ProcessCleanupResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      docker,
      [
        "exec",
        "-i",
        "--workdir",
        "/work",
        container,
        "python3",
        "-c",
        PROCESS_CLEANUP,
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    const outputs: Buffer[] = [];
    const errors: Buffer[] = [];
    let outputBytes = 0;
    let errorBytes = 0;
    let settled = false;
    const timer = setTimeout(() => child.kill("SIGKILL"), 7000);
    child.stdout.on("data", (chunk: Buffer) => {
      const room = Math.max(0, 4096 - outputBytes);
      if (room > 0) outputs.push(chunk.subarray(0, room));
      outputBytes += chunk.length;
    });
    child.stderr.on("data", (chunk: Buffer) => {
      const room = Math.max(0, 4096 - errorBytes);
      if (room > 0) errors.push(chunk.subarray(0, room));
      errorBytes += chunk.length;
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) {
        try {
          const value = JSON.parse(Buffer.concat(outputs).toString("utf8"));
          if (
            outputBytes > 4096 ||
            JSON.stringify(value.snapshot) !==
              JSON.stringify(["pid1", "cleanup"]) ||
            value.observed_process_count !== 2 ||
            value.residual_process_count !== 0 ||
            !Number.isSafeInteger(value.rounds) ||
            value.rounds < 0
          ) {
            throw new Error("invalid /proc cleanup attestation");
          }
          resolve({
            snapshot: ["pid1", "cleanup"],
            observedProcessCount: value.observed_process_count,
            residualProcessCount: value.residual_process_count,
            rounds: value.rounds,
          });
        } catch (error) {
          reject(
            new Error(
              `Research container process cleanup attestation failed: ${error}`,
            ),
          );
        }
      } else {
        reject(
          new Error(
            "Research container process cleanup failed: " +
              (Buffer.concat(errors).toString("utf8") || `exit=${code}`),
          ),
        );
      }
    });
  });
}

function runDocker(
  docker: string,
  container: string,
  command: string[],
  input: string | undefined,
  options: {
    signal?: AbortSignal;
    timeoutMs: number;
    maxOutputBytes: number;
    workdir: "/evidence" | "/work";
    onCleanupFailure: (error: unknown) => void;
  },
): Promise<DockerResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      docker,
      [
        "exec",
        "-i",
        "--workdir",
        options.workdir,
        container,
        ...command,
      ],
      { stdio: ["pipe", "pipe", "pipe"] },
    );
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    const stdoutHash = crypto.createHash("sha256");
    const stderrHash = crypto.createHash("sha256");
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let storedBytes = 0;
    let timedOut = false;
    let aborted = false;
    let outputLimitExceeded = false;
    let finished = false;
    let forceTimer: ReturnType<typeof setTimeout> | undefined;

    const stop = () => {
      child.kill("SIGTERM");
      if (forceTimer === undefined) {
        forceTimer = setTimeout(() => child.kill("SIGKILL"), 2000);
      }
    };
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, options.timeoutMs);
    const abort = () => {
      aborted = true;
      stop();
    };
    options.signal?.addEventListener("abort", abort, { once: true });

    const collect = (
      target: Buffer[],
      hash: ReturnType<typeof crypto.createHash>,
      chunk: Buffer,
    ) => {
      hash.update(chunk);
      const remaining = Math.max(0, options.maxOutputBytes - storedBytes);
      if (remaining > 0) {
        const selected = chunk.subarray(0, remaining);
        target.push(selected);
        storedBytes += selected.length;
      }
      if (chunk.length > remaining) {
        outputLimitExceeded = true;
        stop();
      }
    };

    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBytes += chunk.length;
      collect(stdout, stdoutHash, chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderrBytes += chunk.length;
      collect(stderr, stderrHash, chunk);
    });
    child.on("error", (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (forceTimer !== undefined) clearTimeout(forceTimer);
      options.signal?.removeEventListener("abort", abort);
      reject(error);
    });
    child.on("close", async (code) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (forceTimer !== undefined) clearTimeout(forceTimer);
      options.signal?.removeEventListener("abort", abort);
      try {
        const cleanup = await cleanupContainerProcesses(docker, container);
        resolve({
          stdout: Buffer.concat(stdout).toString("utf8"),
          stderr: Buffer.concat(stderr).toString("utf8"),
          exitCode: code ?? 255,
          timedOut,
          aborted,
          outputLimitExceeded,
          stdoutBytes,
          stderrBytes,
          stdoutSha256: stdoutHash.digest("hex"),
          stderrSha256: stderrHash.digest("hex"),
          cleanupVerified: true,
          cleanupSnapshot: cleanup.snapshot,
          cleanupObservedProcessCount: cleanup.observedProcessCount,
          cleanupResidualProcessCount: cleanup.residualProcessCount,
          cleanupRounds: cleanup.rounds,
        });
      } catch (error) {
        options.onCleanupFailure(error);
        reject(error);
      }
    });
    if (input === undefined) {
      child.stdin.end();
    } else {
      child.stdin.on("error", () => undefined);
      child.stdin.end(input, "utf8");
    }
  });
}

const pageFields = {
  cursor: Type.Optional(Type.Integer({ minimum: 0 })),
  limit: Type.Optional(
    Type.Integer({ minimum: 1, maximum: MAX_PAGE_SIZE }),
  ),
};

const pathField = Type.String({
  minLength: 1,
  description: "Relative path inside the named research root",
});

const queryValue = Type.Union([
  Type.String(),
  Type.Number(),
  Type.Boolean(),
  Type.Null(),
  Type.Array(
    Type.Union([Type.String(), Type.Number(), Type.Boolean(), Type.Null()]),
  ),
]);

export default function researchTools(pi: ExtensionAPI): void {
  const docker = requireValue("SKILL_EVOLUTION_DOCKER_COMMAND");
  const container = validateContainer(
    requireValue("SKILL_EVOLUTION_RESEARCH_CONTAINER"),
  );
  const timeoutMs = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_COMMAND_TIMEOUT_MS",
    300_000,
  );
  const maxOutputBytes = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_MAX_OUTPUT_BYTES",
    2_000_000,
  );
  const maxToolCalls = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_MAX_TOOL_CALLS",
    10_000,
  );
  const maxConcurrentToolCalls = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_MAX_CONCURRENT_TOOL_CALLS",
    3,
  );
  if (maxConcurrentToolCalls !== 1) {
    throw new Error(
      "Research Docker calls must be serial for process-cleanup isolation",
    );
  }
  const maxTotalOutputBytes = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_OUTPUT_BYTES",
    100_000_000,
  );
  const maxTotalCommandMs = positiveInteger(
    "SKILL_EVOLUTION_RESEARCH_MAX_TOTAL_COMMAND_MS",
    7_200_000,
  );
  const timeoutSeconds = Math.max(1, Math.floor(timeoutMs / 1000));
  let activeToolCalls = 0;
  let completedToolCalls = 0;
  let totalOutputBytes = 0;
  let totalCommandMs = 0;
  let sessionPoisoned = process.env[SESSION_POISON_ENV] === "1";

  const poisonSession = (error: unknown) => {
    if (sessionPoisoned) return;
    sessionPoisoned = true;
    process.env[SESSION_POISON_ENV] = "1";
    pi.appendEntry("research-session-poisoned", {
      schema: "research.session_poisoned.v1",
      reason: "container_process_cleanup_unverified",
      error: String(error).slice(0, 2000),
    });
  };

  const budgetedDocker = async (
    execute: (remainingOutputBytes: number) => Promise<DockerResult>,
  ): Promise<DockerResult> => {
    if (sessionPoisoned) {
      throw new Error(
        "Research session is poisoned because container process cleanup failed",
      );
    }
    if (completedToolCalls + activeToolCalls >= maxToolCalls) {
      throw new Error("Research tool-call budget exhausted");
    }
    if (activeToolCalls >= maxConcurrentToolCalls) {
      throw new Error("Research concurrent tool-call budget exhausted");
    }
    if (totalOutputBytes >= maxTotalOutputBytes) {
      throw new Error("Research cumulative output budget exhausted");
    }
    if (totalCommandMs >= maxTotalCommandMs) {
      throw new Error("Research cumulative command-time budget exhausted");
    }
    const remainingOutputBytes = Math.min(
      maxOutputBytes,
      maxTotalOutputBytes - totalOutputBytes,
    );
    const startedAt = Date.now();
    activeToolCalls += 1;
    try {
      const result = await execute(remainingOutputBytes);
      totalOutputBytes += result.stdoutBytes + result.stderrBytes;
      return result;
    } finally {
      activeToolCalls -= 1;
      completedToolCalls += 1;
      totalCommandMs += Math.max(0, Date.now() - startedAt);
    }
  };

  const budgetDetails = () => ({
    completedToolCalls,
    activeToolCalls,
    totalOutputBytes,
    totalCommandMs,
    maxToolCalls,
    maxConcurrentToolCalls,
    maxTotalOutputBytes,
    maxTotalCommandMs,
  });

  const helper = async (
    id: string,
    operation: string,
    params: unknown,
    signal?: AbortSignal,
  ) => {
    const result = await budgetedDocker((remainingOutputBytes) =>
      runDocker(
        docker,
        container,
        [
          "timeout",
          "--signal=TERM",
          "--kill-after=2s",
          `${timeoutSeconds}s`,
          "python3",
          "-c",
          PYTHON_HELPER,
          operation,
        ],
        JSON.stringify(params),
        {
          signal,
          timeoutMs: timeoutMs + 5000,
          maxOutputBytes: remainingOutputBytes,
          workdir: "/work",
          onCleanupFailure: poisonSession,
        },
      ),
    );
    if (
      result.exitCode !== 0 ||
      result.timedOut ||
      result.aborted ||
      result.outputLimitExceeded
    ) {
      const detail = result.stderr || result.stdout || "Research tool failed";
      throw new Error(
        `${detail}\nexit=${result.exitCode} timed_out=${result.timedOut} ` +
          `aborted=${result.aborted} output_limit=${result.outputLimitExceeded}`,
      );
    }
    return {
      content: [{ type: "text" as const, text: result.stdout }],
      details: {
        auditId: id,
        operation,
        exitCode: result.exitCode,
        stdoutBytes: result.stdoutBytes,
        stderrBytes: result.stderrBytes,
        stdoutSha256: result.stdoutSha256,
        stderrSha256: result.stderrSha256,
        cleanupVerified: result.cleanupVerified,
        cleanupSnapshot: result.cleanupSnapshot,
        cleanupObservedProcessCount: result.cleanupObservedProcessCount,
        cleanupResidualProcessCount: result.cleanupResidualProcessCount,
        cleanupRounds: result.cleanupRounds,
        budget: budgetDetails(),
      },
    };
  };

  pi.registerTool({
    name: "research_list",
    label: "List research evidence",
    description: "List one page of entries below the read-only evidence root",
    promptSnippet: "List a bounded page of frozen evidence paths",
    parameters: Type.Object(
      {
        path: Type.Optional(pathField),
        ...pageFields,
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "list", params, signal);
    },
  });

  pi.registerTool({
    name: "research_read",
    label: "Read research evidence",
    description: "Read a bounded line page from one frozen evidence file",
    promptSnippet: "Read exact lines from frozen evidence",
    parameters: Type.Object(
      {
        path: pathField,
        offset: Type.Optional(Type.Integer({ minimum: 1 })),
        limit: Type.Optional(
          Type.Integer({ minimum: 1, maximum: MAX_READ_LINES }),
        ),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "read_evidence", params, signal);
    },
  });

  pi.registerTool({
    name: "research_search",
    label: "Search research evidence",
    description:
      "Literal case-insensitive search with explicit pagination and binary skips",
    promptSnippet: "Search all text evidence without silently skipping large files",
    parameters: Type.Object(
      {
        query: Type.String({ minLength: 1, maxLength: 1000 }),
        path: Type.Optional(pathField),
        ...pageFields,
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "search", params, signal);
    },
  });

  pi.registerTool({
    name: "research_query",
    label: "Query the research index",
    description:
      "Apply typed filters to a deterministic navigation collection without raw SQL",
    promptSnippet: "Filter the local action index and select exact fields",
    parameters: Type.Object(
      {
        path: Type.Optional(pathField),
        collection: Type.Optional(
          Type.Union([Type.Literal("entries"), Type.Literal("scripts")]),
        ),
        where: Type.Optional(
          Type.Array(
            Type.Object(
              {
                field: Type.String({ minLength: 1, maxLength: 200 }),
                op: Type.Union([
                  Type.Literal("eq"),
                  Type.Literal("ne"),
                  Type.Literal("contains"),
                  Type.Literal("in"),
                  Type.Literal("exists"),
                  Type.Literal("gte"),
                  Type.Literal("lte"),
                ]),
                value: queryValue,
              },
              { additionalProperties: false },
            ),
            { maxItems: 20 },
          ),
        ),
        select: Type.Optional(
          Type.Array(Type.String({ minLength: 1, maxLength: 200 }), {
            maxItems: 50,
            uniqueItems: true,
          }),
        ),
        ...pageFields,
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "query", params, signal);
    },
  });

  pi.registerTool({
    name: "research_trajectory_window",
    label: "Read a Trajectory action window",
    description: "Read exact actions around one run_id and seq locator",
    promptSnippet: "Return to the original Trajectory around a candidate pattern",
    parameters: Type.Object(
      {
        run_id: Type.String({ minLength: 1, maxLength: 200 }),
        seq: Type.Integer({ minimum: 1 }),
        before: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_WINDOW })),
        after: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_WINDOW })),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "trajectory_window", params, signal);
    },
  });

  pi.registerTool({
    name: "research_work_read",
    label: "Read a research work file",
    description: "Read a bounded line page from the temporary research work area",
    promptSnippet: "Inspect a program or intermediate research result",
    parameters: Type.Object(
      {
        path: pathField,
        offset: Type.Optional(Type.Integer({ minimum: 1 })),
        limit: Type.Optional(
          Type.Integer({ minimum: 1, maximum: MAX_READ_LINES }),
        ),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "read_work", params, signal);
    },
  });

  pi.registerTool({
    name: "research_work_write",
    label: "Write a research work file",
    description: "Atomically write one file inside the temporary work area",
    promptSnippet: "Save a research program or intermediate result",
    parameters: Type.Object(
      {
        path: pathField,
        content: Type.String({ maxLength: MAX_WRITE_CHARACTERS }),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "write_work", params, signal);
    },
  });

  pi.registerTool({
    name: "research_work_edit",
    label: "Edit a research work file",
    description: "Replace exactly one text block in a temporary work file",
    promptSnippet: "Apply one exact edit to a research program",
    parameters: Type.Object(
      {
        path: pathField,
        old_text: Type.String({ minLength: 1, maxLength: MAX_EDIT_CHARACTERS }),
        new_text: Type.String({ maxLength: MAX_EDIT_CHARACTERS }),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      return helper(id, "edit_work", params, signal);
    },
  });

  pi.registerTool({
    name: "research_exec",
    label: "Run research code",
    description:
      "Run shell or Python analysis only inside the disposable research container",
    promptSnippet:
      "Execute a saved program in /work; frozen evidence is available at /evidence",
    parameters: Type.Object(
      {
        command: Type.String({
          minLength: 1,
          maxLength: MAX_COMMAND_CHARACTERS,
        }),
      },
      { additionalProperties: false },
    ),
    async execute(id, params, signal) {
      const result = await budgetedDocker((remainingOutputBytes) =>
        runDocker(
          docker,
          container,
          [
            "sh",
            "-c",
            'exec timeout --signal=TERM --kill-after=2s "$1" sh -c "$2"',
            "research-exec",
            `${timeoutSeconds}s`,
            params.command,
          ],
          undefined,
          {
            signal,
            timeoutMs: timeoutMs + 5000,
            maxOutputBytes: remainingOutputBytes,
            workdir: "/work",
            onCleanupFailure: poisonSession,
          },
        ),
      );
      const commandTimedOut = result.timedOut || result.exitCode === 124;
      const failed =
        result.exitCode !== 0 ||
        commandTimedOut ||
        result.aborted ||
        result.outputLimitExceeded;
      const text = JSON.stringify({
        stdout: result.stdout,
        stderr: result.stderr,
        exit_code: result.exitCode,
        timed_out: commandTimedOut,
        aborted: result.aborted,
        output_limit_exceeded: result.outputLimitExceeded,
      });
      return {
        content: [{ type: "text" as const, text }],
        details: {
          auditId: id,
          derivationId: id,
          exitCode: result.exitCode,
          timedOut: commandTimedOut,
          aborted: result.aborted,
          outputLimitExceeded: result.outputLimitExceeded,
          stdoutBytes: result.stdoutBytes,
          stderrBytes: result.stderrBytes,
          stdoutSha256: result.stdoutSha256,
          stderrSha256: result.stderrSha256,
          cleanupVerified: result.cleanupVerified,
          cleanupSnapshot: result.cleanupSnapshot,
          cleanupObservedProcessCount: result.cleanupObservedProcessCount,
          cleanupResidualProcessCount: result.cleanupResidualProcessCount,
          cleanupRounds: result.cleanupRounds,
          budget: budgetDetails(),
        },
        isError: failed,
      };
    },
  });
}
