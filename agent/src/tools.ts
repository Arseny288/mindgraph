// tools.ts — the agent's tools (phase 2.3).
//
// There are NO generic read/write/edit/bash tools. Only a narrow tool set,
// where writing outside the zone does not exist as an operation (the absence of
// a door, not a fence).

import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { Session } from "./session.ts";

function text(content: string, details: any = {}) {
  return { content: [{ type: "text", text: content }], details };
}

function readOnlyTools(session: Session): AgentTool[] {
  return [
    {
      name: "list_nodes",
      label: "List nodes",
      description: "The graph's nodes with brief descriptions (id, name, summary).",
      parameters: Type.Object({}),
      async execute() {
        const r = await session.listNodes();
        return text(JSON.stringify(r, null, 2));
      },
    },
    {
      name: "open_node",
      label: "Open node",
      description:
        "Node context: zone, contracts, mixed files. This replaces \"read the whole repository\".",
      parameters: Type.Object({ id: Type.String({ description: "node id or name" }) }),
      async execute(_id, params) {
        const ctx = await session.openNode(params.id);
        const b = ctx.boundary;
        const view: Record<string, unknown> = {
          node_id: ctx.node_id,
          node_name: ctx.node_name,
          allow_write: b.allow_write,
          allow_create: b.allow_create,
          allow_read: b.allow_read,
          contracts: b.contracts,
        };
        if (session.markForeign) {
          view["FOREIGN_SYMBOLS_IN_ZONE_DO_NOT_TOUCH"] = ctx.foreign_symbols.map((f) =>
            `${f.symbol} (owner: node "${f.owner}", ${f.file}:${f.lines[0]}-${f.lines[1]})`,
          );
        }
        return text(JSON.stringify(view, null, 2));
      },
    },
    {
      name: "read_file",
      label: "Read file",
      description: "Read a file. Refused if the path is outside the node's allow_read.",
      parameters: Type.Object({ path: Type.String() }),
      async execute(_id, params) {
        const c = await session.readFile(params.path);
        return text(c);
      },
    },
    {
      name: "run_tests",
      label: "Run tests",
      description:
        "Run the zone's tests. The set is decided by mindgraph (tests --for-node); the agent does not choose and has no arbitrary shell.",
      parameters: Type.Object({}),
      async execute() {
        const r = await session.runTests();
        return text(JSON.stringify(r, null, 2));
      },
    },
    {
      name: "remember",
      label: "Remember",
      description:
        "Append ONE durable fact about this codebase (or, scope=\"global\", about how the user always wants you to work) to memory that persists across conversations — conventions, how to run tests, landmines, decisions the user made. NOT conversation history, NOT a scratchpad: one short, durable line per call. Available in both planning and build mode.",
      parameters: Type.Object({
        line: Type.String({ description: "one durable fact, one line" }),
        scope: Type.Optional(Type.Union([Type.Literal("project"), Type.Literal("global")], {
          description: "\"project\" (default): this codebase only. \"global\": every codebase.",
        })),
      }),
      async execute(_id, params) {
        const r = await session.remember((params.scope as "global" | "project") ?? "project", params.line);
        return text(JSON.stringify(r));
      },
    },
  ];
}

function submitPlanTool(): AgentTool {
  return {
    name: "submit_plan",
    label: "Submit plan",
    description:
      "Submit the plan for the task: the affected nodes, files, contracts and tests. Read-only — writes nothing. Ends the planning session.",
    parameters: Type.Object({
      nodes: Type.Array(Type.String(), { description: "affected node names/ids" }),
      files: Type.Array(Type.String(), { description: "files to change" }),
      contracts: Type.Array(Type.String(), { description: "contracts whose signature must change" }),
      tests: Type.Array(Type.String(), { description: "tests to run" }),
      summary: Type.String({ description: "one sentence describing the change" }),
    }),
    async execute(_id, params) {
      return { ...text("plan submitted"), terminate: true, plan: params };
    },
  };
}

export function buildTools(session: Session): AgentTool[] {
  return [
    ...readOnlyTools(session),
    {
      name: "edit_file",
      label: "Edit file",
      description:
        "Replace a file's contents. Refused if the path is outside allow_write or the edit touches a foreign symbol in a mixed file (checked BEFORE writing).",
      parameters: Type.Object({
        path: Type.String(),
        content: Type.String({ description: "new full file contents" }),
      }),
      async execute(_id, params) {
        const r = await session.editFile(params.path, params.content);
        return text(JSON.stringify(r));
      },
    },
    {
      name: "create_file",
      label: "Create file",
      description:
        "Create a file inside allow_create. Outside allow_create — not a refusal, but a route to \"Unassigned\".",
      parameters: Type.Object({
        path: Type.String(),
        content: Type.String(),
      }),
      async execute(_id, params) {
        const r = await session.createFile(params.path, params.content);
        return text(JSON.stringify(r));
      },
    },
    {
      name: "request_contract_change",
      label: "Request contract change",
      description:
        "Ask a human to change a contract's signature. Changes nothing — stops the session and surfaces the request.",
      parameters: Type.Object({
        symbol: Type.String(),
        rationale: Type.String(),
      }),
      async execute(_id, params) {
        const r = await session.requestContractChange(params.symbol, params.rationale);
        return { ...text(JSON.stringify(r)), terminate: true };
      },
    },
  ];
}

/** Read-only tool set + submit_plan — the planning mode (STUDIO.md §7). */
export function buildPlanTools(session: Session): AgentTool[] {
  return [...readOnlyTools(session), submitPlanTool()];
}
