// session.ts — node-session state and operations (phase 2.1/2.3).
//
// The session knows: the mindgraph client (graph from the main repo), the
// worktree root (files), and the currently open node (its boundary). All
// checks happen BEFORE writing.

import * as fs from "node:fs";
import * as path from "node:path";
import {
  BoundaryError,
  checkCreate,
  checkEdit,
  matchesAny,
} from "./boundary.ts";
import type { Boundary } from "./boundary.ts";
import type { MindGraphClient } from "./mindgraph.ts";

export interface FileSystem {
  readFile(p: string): string;
  writeFile(p: string, content: string): void;
  mkdir(p: string): void;
  exists(p: string): boolean;
}

export const nodeFs: FileSystem = {
  readFile: (p) => fs.readFileSync(p, "utf8"),
  writeFile: (p, c) => fs.writeFileSync(p, c, "utf8"),
  mkdir: (p) => fs.mkdirSync(p, { recursive: true }),
  exists: (p) => fs.existsSync(p),
};

export interface OpenNodeContext {
  node_id: string;
  node_name: string;
  boundary: Boundary;
  node: any;
  /** Foreign symbols in the zone's mixed files — must not be touched. */
  foreign_symbols: { symbol: string; owner: string; file: string; lines: [number, number] }[];
}

export interface SessionOptions {
  /** false — boundary checks disabled (baseline run for task validation). */
  boundaryEnabled?: boolean;
  /** refusal wording variant (eval 2.5 item 2). */
  wording?: "old" | "new";
  /** whether to mark foreign symbols in open_node (eval 2.5 item 3). */
  markForeign?: boolean;
}

export class Session {
  readonly client: MindGraphClient;
  readonly worktree: string;
  readonly fs: FileSystem;
  readonly boundaryEnabled: boolean;
  readonly wording: "old" | "new";
  readonly markForeign: boolean;
  private opened: OpenNodeContext | null = null;

  constructor(client: MindGraphClient, worktree: string, fsys: FileSystem = nodeFs,
              opts: SessionOptions = {}) {
    this.client = client;
    this.worktree = worktree;
    this.fs = fsys;
    this.boundaryEnabled = opts.boundaryEnabled ?? true;
    this.wording = opts.wording ?? "new";
    this.markForeign = opts.markForeign ?? true;
  }

  get context(): OpenNodeContext | null {
    return this.opened;
  }

  private requireBoundary(): Boundary {
    if (!this.opened) {
      throw new BoundaryError("no node is open — call open_node first");
    }
    return this.opened.boundary;
  }

  private absPath(p: string): string {
    // strip `..` — read/write is only allowed inside the worktree
    const resolved = path.resolve(this.worktree, p);
    if (!resolved.startsWith(path.resolve(this.worktree) + path.sep) &&
        resolved !== path.resolve(this.worktree)) {
      throw new BoundaryError(`path ${p} escapes the worktree`);
    }
    return resolved;
  }

  async listNodes(): Promise<{ nodes: { id: string; name: string; summary: string; symbols: number }[] }> {
    const graph = await this.client.graph();
    return {
      nodes: graph.nodes.map((n: any) => ({
        id: n.id,
        name: n.name,
        summary: n.summary ?? "",
        symbols: (n.symbols ?? []).length,
      })),
    };
  }

  async openNode(id: string): Promise<OpenNodeContext> {
    const [node, boundary] = await Promise.all([
      this.client.node(id),
      this.client.boundary(id),
    ]);
    const foreign_symbols: OpenNodeContext["foreign_symbols"] = [];
    for (const m of boundary.mixed ?? []) {
      for (const fr of m.foreign_ranges ?? []) {
        foreign_symbols.push({
          symbol: fr.symbol,
          owner: fr.node,
          file: m.file,
          lines: fr.lines,
        });
      }
    }
    this.opened = {
      node_id: boundary.node_id,
      node_name: boundary.node_name,
      boundary,
      node,
      foreign_symbols,
    };
    return this.opened;
  }

  async readFile(p: string): Promise<string> {
    const b = this.requireBoundary();
    if (this.boundaryEnabled && !matchesAny(p, b.allow_read)) {
      throw new BoundaryError(
        `REFUSED: reading ${p} is outside allow_read of node "${b.node_name}". ` +
        `It belongs to another node — you cannot read it. ` +
        `Read only allow_read, or open a different node.`,
      );
    }
    const abs = this.absPath(p);
    if (!this.fs.exists(abs)) {
      throw new BoundaryError(`file ${p} not found in the worktree`);
    }
    return this.fs.readFile(abs);
  }

  async editFile(p: string, content: string): Promise<{ ok: true; path: string }> {
    const b = this.requireBoundary();
    const abs = this.absPath(p);
    if (this.boundaryEnabled) {
      const oldText = this.fs.exists(abs) ? this.fs.readFile(abs) : "";
      const decision = checkEdit(b, p, oldText, content, this.wording);
      if (!decision.ok) {
        throw new BoundaryError(decision.message);
      }
    }
    this.fs.writeFile(abs, content);
    return { ok: true, path: p };
  }

  async createFile(p: string, content: string): Promise<{ path: string; unassigned: boolean; message: string }> {
    const b = this.requireBoundary();
    const abs = this.absPath(p);
    if (!this.boundaryEnabled) {
      this.fs.mkdir(path.dirname(abs));
      this.fs.writeFile(abs, content);
      return { path: p, unassigned: false, message: `created ${p}` };
    }
    const decision = checkCreate(b, p, this.wording);
    if (decision.ok) {
      this.fs.mkdir(path.dirname(abs));
      this.fs.writeFile(abs, content);
      return { path: p, unassigned: false, message: `created ${p}` };
    }
    // Outside allow_create — not a refusal, but a route to "Unassigned" (phase 1.5, item 4).
    return { path: p, unassigned: true, message: decision.message };
  }

  async runTests(): Promise<any> {
    const b = this.requireBoundary();
    return this.client.testsForNode(b.node_id);
  }

  async requestContractChange(symbol: string, rationale: string): Promise<{ terminate: true; symbol: string }> {
    // Changes nothing: stops the session and surfaces the request to a human.
    return { terminate: true, symbol, rationale };
  }
}
