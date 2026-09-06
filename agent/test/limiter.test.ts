import { test } from "node:test";
import assert from "node:assert/strict";
import { Limiter, classifyRefusal } from "../src/limiter.ts";

test("classifyRefusal recognizes refusal kinds", () => {
  assert.equal(classifyRefusal("edit_file", "REFUSED: editing x touches a foreign symbol y"), "foreign_symbol");
  assert.equal(classifyRefusal("read_file", "REFUSED: reading x is outside allow_read of node auth"), "read_foreign");
  assert.equal(classifyRefusal("edit_file", "REFUSED: x is outside your zone (node auth)"), "write_foreign");
  assert.equal(classifyRefusal("create_file", "UNASSIGNED: creating x is outside allow_create"), "unassigned_create");
  assert.equal(classifyRefusal("edit_file", "all good"), null);
});

test("Limiter stops after N consecutive refusals of one kind", () => {
  const l = new Limiter({ maxMoves: 100, maxConsecutiveRefusals: 3 });
  assert.equal(l.onMove("foreign_symbol"), false);
  assert.equal(l.onMove("foreign_symbol"), false);
  assert.equal(l.onMove("foreign_symbol"), true);
  assert.equal(l.stopped?.kind, "repeated_refusal");
});

test("Limiter resets the counter on a successful move", () => {
  const l = new Limiter({ maxMoves: 100, maxConsecutiveRefusals: 3 });
  l.onMove("foreign_symbol");
  l.onMove("foreign_symbol");
  l.onMove(null); // success
  l.onMove("foreign_symbol");
  assert.equal(l.stopped, null);
});

test("Limiter stops on the move limit", () => {
  const l = new Limiter({ maxMoves: 3, maxConsecutiveRefusals: 10 });
  assert.equal(l.onMove(null), false);
  assert.equal(l.onMove(null), false);
  assert.equal(l.onMove(null), true);
  assert.equal(l.stopped?.kind, "max_moves");
});
