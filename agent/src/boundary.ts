// boundary.ts — проверка границ зоны узла на TS-стороне (фаза 2.1).
//
// Инструменты НЕ получают универсальный write: у них есть только
// allow_read/allow_write/allow_create, и проверка пути идёт ДО записи. Это не
// забор, это отсутствие двери. Здесь — чистые функции, легко тестируемые.

export interface ForeignRange {
  symbol: string;
  node: string;
  lines: [number, number];
}

export interface MixedFile {
  file: string;
  owner: string;
  foreign_symbols: string[];
  foreign_nodes: string[];
  foreign_ranges: ForeignRange[];
}

export interface Boundary {
  schema_version: string;
  node_id: string;
  node_name: string;
  allow_write: string[];
  allow_create: string[];
  deny_write: string[];
  allow_read: string[];
  contracts: { symbol: string; node: string; signature_hash: string }[];
  mixed: MixedFile[];
}

/** gitignore-стиль совпадение: поддерживает `**` и точный путь. */
export function matchesGlob(path: string, glob: string): boolean {
  if (glob === "**") return true;
  if (glob.endsWith("/**")) {
    const prefix = glob.slice(0, -3);
    return path === prefix || path.startsWith(prefix + "/");
  }
  return path === glob;
}

export function matchesAny(path: string, globs: string[]): boolean {
  return globs.some((g) => matchesGlob(path, g));
}

/** Диапазон [startLine, endLine] пересекает [a, b]? */
function overlaps(lo: number, hi: number, a: number, b: number): boolean {
  return !(hi < a || lo > b);
}

/** Диапазон строк, которые правка затрагивает, пересекает чужой символ? */
export function touchesForeignRange(
  mixed: MixedFile | undefined,
  startLine: number,
  endLine: number,
): ForeignRange | null {
  if (!mixed) return null;
  for (const fr of mixed.foreign_ranges) {
    if (overlaps(startLine, endLine, fr.lines[0], fr.lines[1])) {
      return fr;
    }
  }
  return null;
}

/** Первый и последний отличающиеся номера строк (1-based) между old/new. */
export function changedLineRange(
  oldText: string,
  newText: string,
): [number, number] | null {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const n = Math.max(a.length, b.length);
  let first = -1;
  let last = -1;
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) {
      if (first === -1) first = i + 1;
      last = i + 1;
    }
  }
  return first === -1 ? null : [first, last];
}

export class BoundaryError extends Error {
  constructor(message: string) {
    super(message);
  }
}

/** Вернуть MixedFile для пути, если он в mixed-списке зоны. */
export function mixedFileOf(boundary: Boundary, path: string): MixedFile | undefined {
  return boundary.mixed.find((m) => m.file === path);
}

/** Результат проверки записи. */
export type WriteDecision =
  | { ok: true }
  | { ok: false; reason: "outside_node_zone"; message: string }
  | { ok: false; reason: "foreign_symbol"; foreign: ForeignRange; message: string };

export function checkEdit(
  boundary: Boundary,
  path: string,
  oldText: string,
  newText: string,
): WriteDecision {
  if (!matchesAny(path, boundary.allow_write)) {
    return {
      ok: false,
      reason: "outside_node_zone",
      message: `файл ${path} вне allow_write узла ${boundary.node_name}`,
    };
  }
  const mixed = mixedFileOf(boundary, path);
  if (mixed) {
    const range = changedLineRange(oldText, newText);
    if (range) {
      const foreign = touchesForeignRange(mixed, range[0], range[1]);
      if (foreign) {
        return {
          ok: false,
          reason: "foreign_symbol",
          foreign,
          message: `правка ${path}:${range[0]}-${range[1]} затрагивает чужой символ ${foreign.symbol} (узел ${foreign.node})`,
        };
      }
    }
  }
  return { ok: true };
}

export function checkCreate(
  boundary: Boundary,
  path: string,
): WriteDecision {
  if (matchesAny(path, boundary.allow_create)) {
    return { ok: true };
  }
  return {
    ok: false,
    reason: "outside_node_zone",
    message: `создание ${path} вне allow_create: маршрутизирую в «Неразобранное», а не отказываю`,
  };
}
