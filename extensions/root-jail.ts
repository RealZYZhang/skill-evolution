/**
 * Root-confined tools for analysis and candidate-proposal Pi processes.
 *
 * Built-in Pi tools must be disabled when loading this extension. Analysis
 * roles receive read/search/list only. CandidateProposer additionally receives
 * exact write/edit tools rooted in its isolated candidate workspace.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
  access,
  lstat,
  mkdir,
  readFile,
  readdir,
  realpath,
  rename,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

const MAX_READ_LINES = 1000;
const MAX_SEARCH_RESULTS = 200;
const MAX_SEARCH_FILE_BYTES = 2_000_000;

function requireRoot(variable: string): string {
  const value = process.env[variable];
  if (!value) {
    throw new Error(`${variable} is required`);
  }
  return path.resolve(value);
}

function validateRelativePath(value: string): string {
  if (!value || value.includes("\0") || path.isAbsolute(value)) {
    throw new Error("Path must be a non-empty relative path");
  }
  const normalized = path.normalize(value);
  const parts = normalized.split(path.sep);
  if (normalized === ".." || parts.includes("..")) {
    throw new Error("Parent traversal is not allowed");
  }
  return normalized === "." ? "." : normalized;
}

function isInside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return (
    relative === "" ||
    (!relative.startsWith(`..${path.sep}`) &&
      relative !== ".." &&
      !path.isAbsolute(relative))
  );
}

async function nearestExisting(candidate: string): Promise<string> {
  let current = candidate;
  while (true) {
    try {
      await access(current);
      return current;
    } catch {
      const parent = path.dirname(current);
      if (parent === current) {
        throw new Error("No existing parent for requested path");
      }
      current = parent;
    }
  }
}

async function resolveConfined(
  rootInput: string,
  relativeInput: string,
  options: { mustExist: boolean },
): Promise<string> {
  const root = await realpath(rootInput);
  const relative = validateRelativePath(relativeInput);
  const lexical = path.resolve(root, relative);
  if (!isInside(root, lexical)) {
    throw new Error("Path escapes the configured root");
  }

  if (options.mustExist) {
    const resolved = await realpath(lexical);
    if (!isInside(root, resolved)) {
      throw new Error("Symlink escape is not allowed");
    }
    return resolved;
  }

  const existing = await nearestExisting(lexical);
  const resolvedParent = await realpath(existing);
  if (!isInside(root, resolvedParent)) {
    throw new Error("Symlink escape is not allowed");
  }
  return lexical;
}

async function atomicWrite(target: string, content: string): Promise<void> {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = path.join(
    path.dirname(target),
    `.${path.basename(target)}.${crypto.randomUUID()}.tmp`,
  );
  try {
    await writeFile(temporary, content, { encoding: "utf8", flag: "wx" });
    await rename(temporary, target);
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
}

async function collectFiles(
  root: string,
  directory: string,
  signal?: AbortSignal,
): Promise<string[]> {
  const found: string[] = [];
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (signal?.aborted) {
      throw new Error("Search aborted");
    }
    const candidate = path.join(directory, entry.name);
    const metadata = await lstat(candidate);
    if (metadata.isSymbolicLink()) {
      continue;
    }
    if (metadata.isDirectory()) {
      found.push(...(await collectFiles(root, candidate, signal)));
    } else if (metadata.isFile()) {
      found.push(path.relative(root, candidate));
    }
  }
  return found;
}

export default function rootJail(pi: ExtensionAPI): void {
  const readRootInput = requireRoot("SKILL_EVOLUTION_READ_ROOT");
  const mode = process.env.SKILL_EVOLUTION_TOOL_MODE ?? "read_only";
  if (mode !== "read_only" && mode !== "candidate") {
    throw new Error("SKILL_EVOLUTION_TOOL_MODE must be read_only or candidate");
  }

  pi.registerTool({
    name: "harness_list",
    label: "List evidence",
    description: "List one directory below the frozen evidence root",
    promptSnippet: "List files inside the frozen evidence workspace",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Relative directory" })),
    }),
    async execute(_id, params) {
      const root = await realpath(readRootInput);
      const directory = await resolveConfined(
        root,
        params.path ?? ".",
        { mustExist: true },
      );
      const metadata = await stat(directory);
      if (!metadata.isDirectory()) {
        throw new Error("harness_list path must be a directory");
      }
      const entries = await readdir(directory, { withFileTypes: true });
      const rows = entries
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((entry) => ({
          path: path.relative(root, path.join(directory, entry.name)),
          type: entry.isDirectory()
            ? "directory"
            : entry.isFile()
              ? "file"
              : "unsupported",
        }));
      return {
        content: [{ type: "text", text: JSON.stringify(rows) }],
        details: { count: rows.length },
      };
    },
  });

  pi.registerTool({
    name: "harness_read",
    label: "Read evidence",
    description: "Read a bounded line range from one evidence file",
    promptSnippet: "Read exact lines from a frozen evidence file",
    parameters: Type.Object({
      path: Type.String({ description: "Relative file path" }),
      offset: Type.Optional(
        Type.Integer({ minimum: 1, description: "First line, one-based" }),
      ),
      limit: Type.Optional(
        Type.Integer({
          minimum: 1,
          maximum: MAX_READ_LINES,
          description: "Maximum lines",
        }),
      ),
    }),
    async execute(_id, params) {
      const root = await realpath(readRootInput);
      const file = await resolveConfined(root, params.path, {
        mustExist: true,
      });
      if (!(await stat(file)).isFile()) {
        throw new Error("harness_read path must be a file");
      }
      const lines = (await readFile(file, "utf8")).split(/\r?\n/);
      const offset = params.offset ?? 1;
      const limit = params.limit ?? 200;
      const selected = lines.slice(offset - 1, offset - 1 + limit);
      const numbered = selected
        .map((line, index) => `${offset + index}: ${line}`)
        .join("\n");
      return {
        content: [{ type: "text", text: numbered }],
        details: {
          path: path.relative(root, file),
          offset,
          returnedLines: selected.length,
          totalLines: lines.length,
        },
      };
    },
  });

  pi.registerTool({
    name: "harness_search",
    label: "Search evidence",
    description: "Literal, case-insensitive search under the evidence root",
    promptSnippet: "Search frozen evidence and return file/line references",
    parameters: Type.Object({
      query: Type.String({ minLength: 1 }),
      path: Type.Optional(Type.String({ description: "Relative subtree" })),
    }),
    async execute(_id, params, signal) {
      const root = await realpath(readRootInput);
      const searchRoot = await resolveConfined(root, params.path ?? ".", {
        mustExist: true,
      });
      const files = (await stat(searchRoot)).isDirectory()
        ? await collectFiles(root, searchRoot, signal)
        : [path.relative(root, searchRoot)];
      const query = params.query.toLocaleLowerCase();
      const matches: Array<{ path: string; line: number; text: string }> = [];
      for (const relative of files) {
        if (signal?.aborted) {
          throw new Error("Search aborted");
        }
        const file = await resolveConfined(root, relative, {
          mustExist: true,
        });
        const metadata = await stat(file);
        if (metadata.size > MAX_SEARCH_FILE_BYTES) {
          continue;
        }
        let text: string;
        try {
          text = await readFile(file, "utf8");
        } catch {
          continue;
        }
        const lines = text.split(/\r?\n/);
        for (let index = 0; index < lines.length; index += 1) {
          if (lines[index].toLocaleLowerCase().includes(query)) {
            matches.push({
              path: relative,
              line: index + 1,
              text: lines[index].slice(0, 500),
            });
            if (matches.length >= MAX_SEARCH_RESULTS) {
              break;
            }
          }
        }
        if (matches.length >= MAX_SEARCH_RESULTS) {
          break;
        }
      }
      return {
        content: [{ type: "text", text: JSON.stringify(matches) }],
        details: {
          count: matches.length,
          truncated: matches.length >= MAX_SEARCH_RESULTS,
        },
      };
    },
  });

  if (mode !== "candidate") {
    return;
  }
  const writeRootInput = requireRoot("SKILL_EVOLUTION_WRITE_ROOT");

  pi.registerTool({
    name: "candidate_read",
    label: "Read candidate file",
    description: "Read a bounded line range inside the candidate workspace",
    promptSnippet: "Read current candidate content before an exact edit",
    parameters: Type.Object({
      path: Type.String({ description: "Relative candidate file path" }),
      offset: Type.Optional(
        Type.Integer({ minimum: 1, description: "First line, one-based" }),
      ),
      limit: Type.Optional(
        Type.Integer({
          minimum: 1,
          maximum: MAX_READ_LINES,
          description: "Maximum lines",
        }),
      ),
    }),
    async execute(_id, params) {
      const root = await realpath(writeRootInput);
      const file = await resolveConfined(root, params.path, {
        mustExist: true,
      });
      if (!(await stat(file)).isFile()) {
        throw new Error("candidate_read path must be a file");
      }
      const lines = (await readFile(file, "utf8")).split(/\r?\n/);
      const offset = params.offset ?? 1;
      const limit = params.limit ?? 200;
      const selected = lines.slice(offset - 1, offset - 1 + limit);
      const numbered = selected
        .map((line, index) => `${offset + index}: ${line}`)
        .join("\n");
      return {
        content: [{ type: "text", text: numbered }],
        details: {
          path: path.relative(root, file),
          offset,
          returnedLines: selected.length,
          totalLines: lines.length,
        },
      };
    },
  });

  pi.registerTool({
    name: "candidate_write",
    label: "Write candidate file",
    description: "Atomically write one file inside the candidate workspace",
    promptSnippet: "Write complete candidate content inside its isolated root",
    parameters: Type.Object({
      path: Type.String({ description: "Relative candidate file path" }),
      content: Type.String(),
    }),
    async execute(_id, params) {
      const root = await realpath(writeRootInput);
      const target = await resolveConfined(root, params.path, {
        mustExist: false,
      });
      await atomicWrite(target, params.content);
      return {
        content: [
          {
            type: "text",
            text: `Wrote ${Buffer.byteLength(params.content)} bytes`,
          },
        ],
        details: { path: path.relative(root, target) },
      };
    },
  });

  pi.registerTool({
    name: "candidate_edit",
    label: "Edit candidate file",
    description: "Replace exactly one matching text block in a candidate file",
    promptSnippet: "Apply one exact replacement inside the candidate root",
    parameters: Type.Object({
      path: Type.String({ description: "Relative candidate file path" }),
      old_text: Type.String({ minLength: 1 }),
      new_text: Type.String(),
    }),
    async execute(_id, params) {
      const root = await realpath(writeRootInput);
      const target = await resolveConfined(root, params.path, {
        mustExist: true,
      });
      const original = await readFile(target, "utf8");
      const first = original.indexOf(params.old_text);
      if (first < 0) {
        throw new Error("old_text was not found");
      }
      if (original.indexOf(params.old_text, first + params.old_text.length) >= 0) {
        throw new Error("old_text is ambiguous; it occurs more than once");
      }
      const updated =
        original.slice(0, first) +
        params.new_text +
        original.slice(first + params.old_text.length);
      await atomicWrite(target, updated);
      return {
        content: [{ type: "text", text: "Applied one exact replacement" }],
        details: { path: path.relative(root, target) },
      };
    },
  });
}
