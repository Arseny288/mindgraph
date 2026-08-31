import { test } from "node:test";
import assert from "node:assert/strict";
import {
  changedLineRange,
  checkCreate,
  checkEdit,
  matchesGlob,
  type Boundary,
} from "../src/boundary.ts";

const boundary: Boundary = {
  schema_version: "1",
  node_id: "auth",
  node_name: "auth",
  allow_write: ["auth/**", "tests/auth/**"],
  allow_create: ["auth/**"],
  deny_write: ["**"],
  allow_read: ["auth/**", "common/utils.py"],
  contracts: [{ symbol: "common.utils.hash_value", node: "common", signature_hash: "sha256:x" }],
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

test("matchesGlob: ** и каталог/**", () => {
  assert.equal(matchesGlob("auth/login.py", "auth/**"), true);
  assert.equal(matchesGlob("auth/sub/x.py", "auth/**"), true);
  assert.equal(matchesGlob("billing/x.py", "auth/**"), false);
  assert.equal(matchesGlob("anything", "**"), true);
});

test("checkEdit: вне allow_write — отказ", () => {
  const d = checkEdit(boundary, "billing/invoice.py", "a", "b");
  assert.equal(d.ok, false);
  if (!d.ok) assert.equal(d.reason, "outside_node_zone");
});

test("checkEdit: в зоне без чужого символа — ok", () => {
  const d = checkEdit(boundary, "auth/login.py", "a\nb\n", "a\nc\n");
  assert.equal(d.ok, true);
});

test("checkEdit: правка чужого символа в смешанном файле — отказ", () => {
  const oldText = "def own():\n    pass\n\ndef bill_util():\n    pass\n";
  const newText = "def own():\n    pass\n\ndef bill_util(x):\n    pass\n";
  const d = checkEdit(boundary, "auth/shared.py", oldText, newText);
  assert.equal(d.ok, false);
  if (!d.ok) {
    assert.equal(d.reason, "foreign_symbol");
    assert.equal(d.foreign.symbol, "auth.shared.bill_util");
  }
});

test("checkEdit: правка своего символа в смешанном файле — ok", () => {
  const oldText = "def own():\n    pass\n\ndef bill_util():\n    pass\n";
  const newText = "def own(x):\n    pass\n\ndef bill_util():\n    pass\n";
  const d = checkEdit(boundary, "auth/shared.py", oldText, newText);
  assert.equal(d.ok, true);
});

test("checkCreate: в allow_create — ok, вне — unassigned", () => {
  assert.equal(checkCreate(boundary, "auth/new.py").ok, true);
  const d = checkCreate(boundary, "app/new.py");
  assert.equal(d.ok, false);
  if (!d.ok) assert.equal(d.reason, "outside_node_zone");
});

test("changedLineRange находит первый/последний изменённый", () => {
  assert.deepEqual(changedLineRange("a\nb\nc", "a\nb\nc"), null);
  assert.deepEqual(changedLineRange("a\nb\nc", "a\nB\nc"), [2, 2]);
  assert.deepEqual(changedLineRange("a\nb\nc", "a\nB\nC"), [2, 3]);
});
