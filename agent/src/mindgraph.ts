// mindgraph.ts — thin wrapper over the Python CLI (phase 2.2).
//
// Calls `mindgraph <subcommand> --json` as a subprocess, parses stdout as JSON,
// forwards stderr to the log, checks schema_version and fails loudly on
// mismatch. There is no other link between TS and Python.

import { spawn } from "node:child_process";

export class MindGraphError extends Error {
  readonly exitCode: number | null;
  constructor(message: string, exitCode: number | null = null) {
    super(message);
    this.exitCode = exitCode;
  }
}

export interface ClientOptions {
  /** Path to the mindgraph binary (default "mindgraph" from PATH). */
  bin?: string;
  /** Root of the MAIN repo being analyzed (its built graph.json lives in the tool's own storage root, not here). */
  dir: string;
}

export interface RunResult {
  data: any;
  exitCode: number;
  stderr: string;
}

export class MindGraphClient {
  readonly bin: string;
  readonly dir: string;

  constructor(opts: ClientOptions) {
    this.bin = opts.bin ?? "mindgraph";
    this.dir = opts.dir;
  }

  private async run(args: string[], input?: string): Promise<RunResult> {
    const fullArgs = [...args, "--dir", this.dir];
    // NOTE: execFile's `input` option deadlocks on Node 26 (the child's stdin
    // never receives EOF), so we drive stdin manually via spawn + end().
    const child = spawn(this.bin, fullArgs, { stdio: ["pipe", "pipe", "pipe"] });

    let stdout = "";
    let stderr = "";
    const maxBuffer = 256 * 1024 * 1024;
    let stdoutOverflow = false;
    let stderrOverflow = false;
    child.stdout.on("data", (d: Buffer) => {
      if (stdout.length + d.length <= maxBuffer) stdout += d.toString("utf8");
      else stdoutOverflow = true;
    });
    child.stderr.on("data", (d: Buffer) => {
      if (stderr.length + d.length <= maxBuffer) stderr += d.toString("utf8");
      else stderrOverflow = true;
    });

    const code: number = await new Promise((resolve, reject) => {
      child.on("error", (err) => reject(err));
      child.on("close", (c) => resolve(c ?? 0));
      child.stdin.write(input ?? "");
      child.stdin.end();
    });

    if (stdoutOverflow || stderrOverflow) {
      throw new MindGraphError("mindgraph: output exceeded maxBuffer", code);
    }

    let data: any;
    try {
      data = JSON.parse(stdout);
    } catch {
      // exit 3 (boundary violation) still carries valid JSON on stdout — but a
      // truly non-JSON stdout is an error.
      throw new MindGraphError(
        `mindgraph: invalid JSON on stdout: ${stdout.slice(0, 300)}`,
        code,
      );
    }
    if (data == null || typeof data.schema_version !== "string") {
      throw new MindGraphError(
        `mindgraph: response without schema_version (${stdout.slice(0, 300)})`,
        code,
      );
    }
    return { data, exitCode: code, stderr };
  }

  scan(): Promise<any> {
    return this.run(["scan", "--json"]).then((r) => r.data);
  }

  graph(): Promise<any> {
    return this.run(["graph", "--json"]).then((r) => r.data);
  }

  node(id: string): Promise<any> {
    return this.run(["node", id, "--json"]).then((r) => r.data);
  }

  boundary(nodeId: string): Promise<any> {
    return this.run(["boundary", nodeId, "--json"]).then((r) => r.data);
  }

  testsForNode(nodeId: string): Promise<any> {
    return this.run(["tests", "--for-node", nodeId, "--json"]).then((r) => r.data);
  }

  /** verify: returns the verdict + exit code (3 = boundary violation). */
  async verify(nodeId: string, diff: string, worktree?: string): Promise<RunResult> {
    const args = ["verify", "--node", nodeId, "--diff", "-"];
    if (worktree) args.push("--worktree", worktree);
    return this.run(args, diff);
  }

  /** The agent's explicit ``remember`` tool (item 3b) — the only path that
   * writes memory. Scope is project-wide, independent of the execution
   * worktree. */
  remember(scope: "global" | "project", line: string): Promise<any> {
    return this.run(["remember", "--scope", scope, "--line", line, "--json"])
      .then((r) => r.data);
  }
}
