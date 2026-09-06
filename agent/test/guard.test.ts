import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runGuard } from "../src/guard.ts";
import type { MindGraphClient } from "../src/mindgraph.ts";

function git(dir: string, ...args: string[]) {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" });
}

test("runGuard: boundary violation -> roll back changed files", async () => {
  const dir = mkdtempSync(join(tmpdir(), "mg-guard-"));
  git(dir, "init", "-q");
  git(dir, "config", "user.email", "a@b.c");
  git(dir, "config", "user.name", "t");
  writeFileSync(join(dir, "a.py"), "x = 1\n");
  git(dir, "add", ".");
  git(dir, "commit", "-q", "-m", "base");

  // the agent "bypasses" the tools and edits the file directly
  writeFileSync(join(dir, "a.py"), "x = 999\n");

  const client = {
    verify: async () => ({
      data: { schema_version: "1", ok: false, changed: ["a.py"], violations: [{ path: "a.py", reason: "outside_node_zone" }] },
      exitCode: 3,
      stderr: "",
    }),
  } as unknown as MindGraphClient;

  const r = await runGuard(client, dir, "auth");
  assert.equal(r.ok, false);
  assert.deepEqual(r.rolledBack, ["a.py"]);
  assert.match(readFileSync(join(dir, "a.py"), "utf8"), /x = 1/); // rolled back
});
