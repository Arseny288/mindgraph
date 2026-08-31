import { test } from "node:test";
import assert from "node:assert/strict";
import { Session, type FileSystem } from "../src/session.ts";
import { BoundaryError } from "../src/boundary.ts";
import type { MindGraphClient } from "../src/mindgraph.ts";

const boundary = {
  schema_version: "1",
  node_id: "auth",
  node_name: "auth",
  allow_write: ["auth/**"],
  allow_create: ["auth/**"],
  deny_write: ["**"],
  allow_read: ["auth/**", "common/utils.py"],
  contracts: [],
  mixed: [
    {
      file: "auth/shared.py",
      owner: "auth",
      foreign_symbols: ["auth.shared.bill_util"],
      foreign_nodes: ["billing"],
      foreign_ranges: [{ symbol: "auth.shared.bill_util", node: "billing", lines: [4, 5] }],
    },
  ],
};

const nodeView = { schema_version: "1", node: { id: "n_auth", name: "auth" }, owned: {}, contracts: [], foreign: {}, mixed: boundary.mixed };

function fakeClient(): MindGraphClient {
  return {
    bin: "mindgraph",
    dir: "/main",
    graph: async () => ({ nodes: [{ id: "n_auth", name: "auth", symbols: [] }] }),
    node: async () => nodeView,
    boundary: async () => boundary,
    testsForNode: async () => ({ schema_version: "1", status: "ok", tests: [] }),
    scan: async () => ({}),
    verify: async () => ({ data: { schema_version: "1", ok: true }, exitCode: 0, stderr: "" }),
  } as unknown as MindGraphClient;
}

class MemFs implements FileSystem {
  files = new Map<string, string>();
  readFile(p: string) {
    const c = this.files.get(p);
    if (c === undefined) throw new Error(`no file ${p}`);
    return c;
  }
  writeFile(p: string, c: string) { this.files.set(p, c); }
  mkdir(_p: string) {}
  exists(p: string) { return this.files.has(p); }
}

function makeSession(worktree = "/wt", fs = new MemFs()): { s: Session; fs: MemFs } {
  const s = new Session(fakeClient(), worktree, fs);
  fs.files.set("/wt/auth/shared.py", "def own():\n    pass\n\ndef bill_util():\n    pass\n");
  fs.files.set("/wt/common/utils.py", "def hash_value(v: str) -> str:\n    return str(len(v))\n");
  return { s, fs };
}

test("readFile refuses outside allow_read", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  await assert.rejects(() => s.readFile("billing/invoice.py"), BoundaryError);
});

test("readFile reads a file from allow_read", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  const c = await s.readFile("common/utils.py");
  assert.match(c, /hash_value/);
});

test("editFile refuses outside allow_write", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  await assert.rejects(() => s.editFile("app/models.py", "x"), BoundaryError);
});

test("editFile refuses when editing a foreign symbol in a mixed file", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  await assert.rejects(
    () => s.editFile("auth/shared.py", "def own():\n    pass\n\ndef bill_util(x):\n    pass\n"),
    BoundaryError,
  );
});

test("editFile writes when editing your own symbol", async () => {
  const { s, fs } = makeSession();
  await s.openNode("auth");
  const r = await s.editFile("auth/shared.py", "def own(x):\n    pass\n\ndef bill_util():\n    pass\n");
  assert.equal(r.ok, true);
  assert.match(fs.readFile("/wt/auth/shared.py"), /def own\(x\)/);
});

test("createFile outside allow_create goes to \"Unassigned\", not a refusal", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  const r = await s.createFile("app/new.py", "x = 1");
  assert.equal(r.unassigned, true);
});

test("runTests returns the node verdict", async () => {
  const { s } = makeSession();
  await s.openNode("auth");
  const r = await s.runTests();
  assert.equal(r.status, "ok");
});
