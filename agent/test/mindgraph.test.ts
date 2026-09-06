// mindgraph.test.ts — the subprocess wrapper closes stdin (regression test for
// the Node 26 execFile `input` deadlock: the child never saw EOF and blocked in
// stdin.read() forever).
//
// The real CLI is a Python subprocess; we point the client at a tiny executable
// that reads stdin to EOF and echoes its length, which hangs if stdin is not
// ended.

import { test } from "node:test";
import assert from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { MindGraphClient } from "../src/mindgraph.ts";

test("MindGraphClient.run writes input and closes stdin", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mg-cli-"));
  const script = path.join(tmp, "echo-stdin.py");
  fs.writeFileSync(script, [
    "#!/usr/bin/env python3",
    "import sys, json",
    "d = sys.stdin.read()",
    "print(json.dumps({'schema_version': '1', 'n': len(d)}))",
    "",
  ].join("\n"));
  fs.chmodSync(script, 0o755);

  const client = new MindGraphClient({ bin: script, dir: tmp });
  const payload = "hello-stdin";
  const r = await client.verify("auth", payload);

  assert.strictEqual(r.exitCode, 0);
  assert.strictEqual(r.data.n, payload.length);
});

test("MindGraphClient.remember passes scope and line as CLI args", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mg-cli-"));
  const script = path.join(tmp, "echo-argv.py");
  fs.writeFileSync(script, [
    "#!/usr/bin/env python3",
    "import sys, json",
    "sys.stdin.read()",
    "print(json.dumps({'schema_version': '1', 'argv': sys.argv[1:]}))",
    "",
  ].join("\n"));
  fs.chmodSync(script, 0o755);

  const client = new MindGraphClient({ bin: script, dir: tmp });
  const data = await client.remember("project", "Run tests with pytest -q.");

  assert.deepStrictEqual(data.argv, [
    "remember", "--scope", "project", "--line", "Run tests with pytest -q.",
    "--json", "--dir", tmp,
  ]);
});
