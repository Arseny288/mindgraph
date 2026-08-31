// session.ts — состояние и операции сессии узла (фаза 2.1/2.3).
//
// Сессия знает: клиент mindgraph (граф из главного репо), корень worktree
// (файлы), и текущий открытый узел (его boundary). Все проверки — ДО записи.

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
}

export class Session {
  readonly client: MindGraphClient;
  readonly worktree: string;
  readonly fs: FileSystem;
  private opened: OpenNodeContext | null = null;

  constructor(client: MindGraphClient, worktree: string, fsys: FileSystem = nodeFs) {
    this.client = client;
    this.worktree = worktree;
    this.fs = fsys;
  }

  get context(): OpenNodeContext | null {
    return this.opened;
  }

  private requireBoundary(): Boundary {
    if (!this.opened) {
      throw new BoundaryError("узел не открыт — сначала open_node");
    }
    return this.opened.boundary;
  }

  private absPath(p: string): string {
    // отсечь `..` — читать/писать можно только внутри worktree
    const resolved = path.resolve(this.worktree, p);
    if (!resolved.startsWith(path.resolve(this.worktree) + path.sep) &&
        resolved !== path.resolve(this.worktree)) {
      throw new BoundaryError(`путь ${p} выходит за пределы worktree`);
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
    this.opened = {
      node_id: boundary.node_id,
      node_name: boundary.node_name,
      boundary,
      node,
    };
    return this.opened;
  }

  async readFile(p: string): Promise<string> {
    const b = this.requireBoundary();
    if (!matchesAny(p, b.allow_read)) {
      throw new BoundaryError(`чтение ${p} запрещено: вне allow_read узла ${b.node_name}`);
    }
    const abs = this.absPath(p);
    if (!this.fs.exists(abs)) {
      throw new BoundaryError(`файл ${p} не найден в worktree`);
    }
    return this.fs.readFile(abs);
  }

  async editFile(p: string, content: string): Promise<{ ok: true; path: string }> {
    const b = this.requireBoundary();
    const abs = this.absPath(p);
    const oldText = this.fs.exists(abs) ? this.fs.readFile(abs) : "";
    const decision = checkEdit(b, p, oldText, content);
    if (!decision.ok) {
      throw new BoundaryError(decision.message);
    }
    this.fs.writeFile(abs, content);
    return { ok: true, path: p };
  }

  async createFile(p: string, content: string): Promise<{ path: string; unassigned: boolean; message: string }> {
    const b = this.requireBoundary();
    const abs = this.absPath(p);
    const decision = checkCreate(b, p);
    if (decision.ok) {
      this.fs.mkdir(path.dirname(abs));
      this.fs.writeFile(abs, content);
      return { path: p, unassigned: false, message: `создан ${p}` };
    }
    // Вне allow_create — не отказ, а маршрут в «Неразобранное» (фаза 1.5, п.4).
    return { path: p, unassigned: true, message: decision.message };
  }

  async runTests(): Promise<any> {
    const b = this.requireBoundary();
    return this.client.testsForNode(b.node_id);
  }

  async requestContractChange(symbol: string, rationale: string): Promise<{ terminate: true; symbol: string }> {
    // Не меняет ничего: останавливает сессию и выносит запрос человеку.
    return { terminate: true, symbol, rationale };
  }
}
