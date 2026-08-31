// boundary.ts — node-zone boundary checks on the TypeScript side (phase 2.1).
//
// Tools do NOT get a generic `write`: they only have
// allow_read/allow_write/allow_create, and the path is checked BEFORE writing.
// This is not a fence, it's the absence of a door. Everything here is pure,
// easily testable functions.

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

/** gitignore-style matching: supports `**` and exact paths. */
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

/** Does range [startLine, endLine] overlap [a, b]? */
function overlaps(lo: number, hi: number, a: number, b: number): boolean {
  return !(hi < a || lo > b);
}

/** Does the edited line range overlap a foreign symbol? */
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

/** First and last differing line numbers (1-based) between old/new. */
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

/** Return the MixedFile for a path, if it's in the zone's mixed list. */
export function mixedFileOf(boundary: Boundary, path: string): MixedFile | undefined {
  return boundary.mixed.find((m) => m.file === path);
}

/** Result of a write check. */
export type WriteDecision =
  | { ok: true }
  | { ok: false; reason: "outside_node_zone"; message: string }
  | { ok: false; reason: "foreign_symbol"; foreign: ForeignRange; message: string };

/**
 * The refusal is part of the product: it is the only channel through which the
 * agent's post-wall behavior is steered. So the wording carries (1) whose
 * symbol/file it is, (2) the owner node, (3) what to do. Two wording variants
 * exist for the eval (2.5, item 2).
 */
export function checkEdit(
  boundary: Boundary,
  path: string,
  oldText: string,
  newText: string,
  wording: "old" | "new" = "new",
): WriteDecision {
  if (!matchesAny(path, boundary.allow_write)) {
    const message = wording === "old"
      ? `file ${path} is outside allow_write of node ${boundary.node_name}`
      : `REFUSED: ${path} is outside your zone (node "${boundary.node_name}"). ` +
        `It belongs to another node — you cannot edit it. ` +
        `Stay within allow_write, or open a different node (open_node).`;
    return { ok: false, reason: "outside_node_zone", message };
  }
  const mixed = mixedFileOf(boundary, path);
  if (mixed) {
    const range = changedLineRange(oldText, newText);
    if (range) {
      const foreign = touchesForeignRange(mixed, range[0], range[1]);
      if (foreign) {
        const message = wording === "old"
          ? `editing ${path}:${range[0]}-${range[1]} touches a foreign symbol ${foreign.symbol} (node ${foreign.node})`
          : `REFUSED: editing ${path}:${range[0]}-${range[1]} touches a foreign symbol ` +
            `${foreign.symbol} (owner — node "${foreign.node}"). ` +
            `The symbol does not belong to node "${boundary.node_name}". ` +
            `Edit only your own lines; to change someone else's signature call ` +
            `request_contract_change, not edit_file.`;
        return { ok: false, reason: "foreign_symbol", foreign, message };
      }
    }
  }
  return { ok: true };
}

export function checkCreate(
  boundary: Boundary,
  path: string,
  wording: "old" | "new" = "new",
): WriteDecision {
  if (matchesAny(path, boundary.allow_create)) {
    return { ok: true };
  }
  const message = wording === "old"
    ? `creating ${path} outside allow_create: routed to "Unassigned", not refused`
    : `UNASSIGNED: creating ${path} is outside allow_create of node "${boundary.node_name}". ` +
      `This is not your zone — the file was not created. Create it inside ` +
      `allow_create, or open the node that owns this directory.`;
  return { ok: false, reason: "outside_node_zone", message };
}
