/**
 * Pi tools routed exclusively into a pre-created disposable Docker container.
 *
 * The host Pi process retains model credentials. The container receives no
 * credentials and is expected to have only the current run directory mounted.
 * Load this extension together with --no-builtin-tools.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";

const MAX_OUTPUT_BYTES = 2_000_000;

function requireValue(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function validatePath(value: string): string {
  if (
    !value ||
    value.includes("\0") ||
    value.startsWith("/") ||
    value.split(/[\\/]+/).includes("..")
  ) {
    throw new Error("Path must stay below the container workspace");
  }
  return value;
}

function dockerExec(
  docker: string,
  container: string,
  command: string,
  args: string[],
  input: string | undefined,
  signal?: AbortSignal,
): Promise<{ stdout: string; stderr: string; exitCode: number }> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      docker,
      [
        "exec",
        "-i",
        "--workdir",
        "/workspace",
        container,
        "sh",
        "-c",
        command,
        "skill-evolution-tool",
        ...args,
      ],
      {
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    const abort = () => child.kill("SIGTERM");
    signal?.addEventListener("abort", abort, { once: true });
    child.stdout.on("data", (chunk: Buffer) => {
      outputBytes += chunk.length;
      if (outputBytes > MAX_OUTPUT_BYTES) {
        child.kill("SIGTERM");
        reject(new Error("Container tool output exceeded its size limit"));
        return;
      }
      stdout.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      outputBytes += chunk.length;
      stderr.push(chunk);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      signal?.removeEventListener("abort", abort);
      resolve({
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
        exitCode: code ?? 255,
      });
    });
    if (input !== undefined) {
      child.stdin.end(input, "utf8");
    } else {
      child.stdin.end();
    }
  });
}

export default function dockerToolRouter(pi: ExtensionAPI): void {
  const docker = process.env.SKILL_EVOLUTION_DOCKER_COMMAND ?? "docker";
  const container = requireValue("SKILL_EVOLUTION_DOCKER_CONTAINER");

  pi.registerTool({
    name: "read",
    label: "Read sandbox file",
    description: "Read a bounded line range inside the disposable workspace",
    parameters: Type.Object({
      path: Type.String(),
      offset: Type.Optional(Type.Integer({ minimum: 1 })),
      limit: Type.Optional(
        Type.Integer({ minimum: 1, maximum: 1000 }),
      ),
    }),
    async execute(_id, params, signal) {
      const relative = validatePath(params.path);
      const offset = params.offset ?? 1;
      const limit = params.limit ?? 200;
      const end = offset + limit - 1;
      const result = await dockerExec(
        docker,
        container,
        'test -f "$1" && sed -n "$2,$3p" -- "$1"',
        [relative, String(offset), String(end)],
        undefined,
        signal,
      );
      if (result.exitCode !== 0) {
        throw new Error(result.stderr || "Unable to read sandbox file");
      }
      return {
        content: [{ type: "text", text: result.stdout }],
        details: { path: relative, offset, limit },
      };
    },
  });

  pi.registerTool({
    name: "write",
    label: "Write sandbox file",
    description: "Atomically write a file inside the disposable workspace",
    parameters: Type.Object({
      path: Type.String(),
      content: Type.String(),
    }),
    async execute(_id, params, signal) {
      const relative = validatePath(params.path);
      const result = await dockerExec(
        docker,
        container,
        [
          'target="$1"; parent=$(dirname -- "$target");',
          'mkdir -p -- "$parent" || exit 1;',
          'tmp="$parent/.skill-evolution-write-$$";',
          'cat > "$tmp" && mv -- "$tmp" "$target"',
        ].join(" "),
        [relative],
        params.content,
        signal,
      );
      if (result.exitCode !== 0) {
        throw new Error(result.stderr || "Unable to write sandbox file");
      }
      return {
        content: [
          {
            type: "text",
            text: `Wrote ${Buffer.byteLength(params.content)} bytes`,
          },
        ],
        details: { path: relative },
      };
    },
  });

  pi.registerTool({
    name: "edit",
    label: "Edit sandbox file",
    description: "Replace one exact text block inside a sandbox file",
    parameters: Type.Object({
      path: Type.String(),
      old_text: Type.String({ minLength: 1 }),
      new_text: Type.String(),
    }),
    async execute(_id, params, signal) {
      const relative = validatePath(params.path);
      const readResult = await dockerExec(
        docker,
        container,
        'test -f "$1" && cat -- "$1"',
        [relative],
        undefined,
        signal,
      );
      if (readResult.exitCode !== 0) {
        throw new Error(readResult.stderr || "Unable to read sandbox file");
      }
      const first = readResult.stdout.indexOf(params.old_text);
      if (first < 0) {
        throw new Error("old_text was not found");
      }
      if (
        readResult.stdout.indexOf(
          params.old_text,
          first + params.old_text.length,
        ) >= 0
      ) {
        throw new Error("old_text is ambiguous");
      }
      const updated =
        readResult.stdout.slice(0, first) +
        params.new_text +
        readResult.stdout.slice(first + params.old_text.length);
      const writeResult = await dockerExec(
        docker,
        container,
        [
          'target="$1"; parent=$(dirname -- "$target");',
          'tmp="$parent/.skill-evolution-edit-$$";',
          'cat > "$tmp" && mv -- "$tmp" "$target"',
        ].join(" "),
        [relative],
        updated,
        signal,
      );
      if (writeResult.exitCode !== 0) {
        throw new Error(writeResult.stderr || "Unable to edit sandbox file");
      }
      return {
        content: [{ type: "text", text: "Applied one exact replacement" }],
        details: { path: relative },
      };
    },
  });

  pi.registerTool({
    name: "bash",
    label: "Run sandbox command",
    description: "Run a shell command only inside the disposable container",
    parameters: Type.Object({
      command: Type.String({ minLength: 1 }),
    }),
    async execute(_id, params, signal) {
      const result = await dockerExec(
        docker,
        container,
        params.command,
        [],
        undefined,
        signal,
      );
      const text = [result.stdout, result.stderr]
        .filter((item) => item)
        .join("\n");
      return {
        content: [{ type: "text", text }],
        details: {
          exitCode: result.exitCode,
          container,
        },
        isError: result.exitCode !== 0,
      };
    },
  });
}
