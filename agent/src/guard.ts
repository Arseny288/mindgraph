// guard.ts — defense in depth (phase 2.5).
//
// After each agent move, REGARDLESS of what the tools reported: take the
// cumulative git diff in the worktree, run it through mindgraph verify.
// Exit code 3 -> roll back the move (git checkout the changed files) + log the
// incident.

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
  // Cumulative diff relative to HEAD (the session's base commit).
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
      incident: `verify: boundary violation — ${JSON.stringify(res.data.violations)}`,
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
    throw new Error(`git diff failed: ${err?.message ?? err}`);
  }
}

async function rollback(worktree: string, files: string[]): Promise<string[]> {
  if (files.length === 0) return [];
  await execFileAsync("git", ["-C", worktree, "checkout", "--", ...files], {
    maxBuffer: 64 * 1024 * 1024, encoding: "utf8",
  });
  return files;
}
