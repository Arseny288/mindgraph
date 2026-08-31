// guard.ts — защита в глубину (фаза 2.5).
//
// После каждого хода агента, НЕЗАВИСИМО от того, что показали инструменты:
// взять кумулятивный git diff в worktree, прогнать через mindgraph verify.
// Код 3 -> откат хода (git checkout изменённых файлов) + инцидент в лог.

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { MindGraphClient } from "./mindgraph.ts";

const execFileAsync = promisify(execFile);

export interface GuardResult {
  ok: boolean;
  verdict: any;
  rolledBack: string[];
  incident: string | null;
}

export async function runGuard(
  client: MindGraphClient,
  worktree: string,
  nodeId: string,
): Promise<GuardResult> {
  // Кумулятивный дифф относительно HEAD (базовый коммит сессии).
  const diff = await gitDiff(worktree);
  if (!diff.trim()) {
    return { ok: true, verdict: { ok: true }, rolledBack: [], incident: null };
  }

  const res = await client.verify(nodeId, diff, worktree);
  if (res.exitCode === 3 || !res.data.ok) {
    const changed = res.data.changed ?? [];
    const rolledBack = await rollback(worktree, changed);
    return {
      ok: false,
      verdict: res.data,
      rolledBack,
      incident: `verify: нарушение границы — ${JSON.stringify(res.data.violations)}`,
    };
  }
  return { ok: true, verdict: res.data, rolledBack: [], incident: null };
}

async function gitDiff(worktree: string): Promise<string> {
  try {
    const { stdout } = await execFileAsync(
      "git", ["-C", worktree, "diff", "--no-ext-diff", "--unified=3"],
      { maxBuffer: 256 * 1024 * 1024, encoding: "utf8" },
    );
    return stdout;
  } catch (err: any) {
    if (err?.stdout) return err.stdout;
    throw new Error(`git diff не удался: ${err?.message ?? err}`);
  }
}

async function rollback(worktree: string, files: string[]): Promise<string[]> {
  if (files.length === 0) return [];
  await execFileAsync("git", ["-C", worktree, "checkout", "--", ...files], {
    maxBuffer: 64 * 1024 * 1024, encoding: "utf8",
  });
  return files;
}
