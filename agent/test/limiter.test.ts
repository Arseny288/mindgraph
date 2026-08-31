import { test } from "node:test";
import assert from "node:assert/strict";
import { Limiter, classifyRefusal } from "../src/limiter.ts";

test("classifyRefusal распознаёт виды отказов", () => {
  assert.equal(classifyRefusal("edit_file", "ОТКАЗ: правка x затрагивает чужой символ y"), "foreign_symbol");
  assert.equal(classifyRefusal("read_file", "ОТКАЗ: чтение x вне allow_read узла auth"), "read_foreign");
  assert.equal(classifyRefusal("edit_file", "ОТКАЗ: x вне твоей зоны узла auth"), "write_foreign");
  assert.equal(classifyRefusal("create_file", "НЕРАЗОБРАННОЕ: создание x вне allow_create"), "unassigned_create");
  assert.equal(classifyRefusal("edit_file", "всё ок"), null);
});

test("Limiter останавливает при N подряд однотипных отказов", () => {
  const l = new Limiter({ maxMoves: 100, maxConsecutiveRefusals: 3 });
  assert.equal(l.onMove("foreign_symbol"), false);
  assert.equal(l.onMove("foreign_symbol"), false);
  assert.equal(l.onMove("foreign_symbol"), true);
  assert.equal(l.stopped?.kind, "repeated_refusal");
});

test("Limiter сбрасывает счётчик при успешном ходе", () => {
  const l = new Limiter({ maxMoves: 100, maxConsecutiveRefusals: 3 });
  l.onMove("foreign_symbol");
  l.onMove("foreign_symbol");
  l.onMove(null); // успех
  l.onMove("foreign_symbol");
  assert.equal(l.stopped, null);
});

test("Limiter останавливает по лимиту ходов", () => {
  const l = new Limiter({ maxMoves: 3, maxConsecutiveRefusals: 10 });
  assert.equal(l.onMove(null), false);
  assert.equal(l.onMove(null), false);
  assert.equal(l.onMove(null), true);
  assert.equal(l.stopped?.kind, "max_moves");
});
