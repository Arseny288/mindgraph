// mindgraph.ts — тонкая обёртка над Python-CLI (фаза 2.2).
//
// Вызывает `mindgraph <subcommand> --json` подпроцессом, парсит stdout как
// JSON, пробрасывает stderr в лог, проверяет schema_version и громко падает
// при несовпадении. Никакой другой связи между TS и Python нет.

import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export class MindGraphError extends Error {
  readonly exitCode: number | null;
  constructor(message: string, exitCode: number | null = null) {
    super(message);
    this.exitCode = exitCode;
  }
}

export interface ClientOptions {
  /** Путь к бинарнику mindgraph (по умолчанию "mindgraph" из PATH). */
  bin?: string;
  /** Корень ГЛАВНОГО репозитория (где .mindgraph/graph.json). */
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
    const res = await execFileAsync(this.bin, fullArgs, {
      input: input ?? "",
      maxBuffer: 256 * 1024 * 1024,
      encoding: "utf8",
    }).catch((err: any) => {
      // exit 3 (нарушение границы) всё равно несёт валидный JSON в stdout.
      if (err && typeof err.stdout === "string" && err.stdout.length > 0) {
        return err as { stdout: string; stderr: string; code: number };
      }
      throw new MindGraphError(
        err?.stderr || err?.message || String(err),
        err?.code ?? null,
      );
    });

    const stdout: string = (res as any).stdout ?? "";
    const stderr: string = (res as any).stderr ?? "";
    const code: number = (res as any).code ?? 0;

    let data: any;
    try {
      data = JSON.parse(stdout);
    } catch {
      throw new MindGraphError(
        `mindgraph: невалидный JSON в stdout: ${stdout.slice(0, 300)}`,
        code,
      );
    }
    if (data == null || typeof data.schema_version !== "string") {
      throw new MindGraphError(
        `mindgraph: ответ без schema_version (${stdout.slice(0, 300)})`,
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

  /** verify: возвращает вердикт + код возврата (3 = нарушение границы). */
  async verify(nodeId: string, diff: string, worktree?: string): Promise<RunResult> {
    const args = ["verify", "--node", nodeId, "--diff", "-"];
    if (worktree) args.push("--worktree", worktree);
    return this.run(args, diff);
  }
}
