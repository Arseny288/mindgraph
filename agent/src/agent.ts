// agent.ts — headless agent entry point (phase 2.6).
//
//   node agent/src/main.ts --node <id> --task "..." \
//       --dir <main-repo> --worktree <worktree> [--json]
//
// The first mode is headless (no TUI). With --json — a JSONL event stream.

import { readFileSync } from "node:fs";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { MindGraphClient } from "./mindgraph.ts";
import { Session } from "./session.ts";
import { buildPlanTools, buildTools } from "./tools.ts";
import { runGuard } from "./guard.ts";
import { Limiter, classifyRefusal } from "./limiter.ts";

export interface AgentCliOptions {
  node: string;
  task: string;
  dir: string;
  worktree: string;
  provider?: string;
  model?: string;
  json?: boolean;
  bin?: string;
  thinking?: "off" | "on";
  maxMoves?: number;
  maxRefusals?: number;
  /** Disable boundary checks (baseline run for task validation). */
  noBoundary?: boolean;
  /** refusal wording variant (eval 2.5 item 2). */
  wording?: "old" | "new";
  /** whether to mark foreign symbols in open_node (eval 2.5 item 3). */
  markForeign?: boolean;
  /** planning mode: read-only tools + submit_plan (STUDIO.md §7). */
  plan?: boolean;
  /** item 3: path to a text file with project/global memory + a compacted
   * summary of prior turns in the active conversation, prepended to the
   * system prompt. Built by studio.py — this process has no other notion of
   * "prior conversation" (each turn is otherwise a fresh process). The CLI
   * flag is --context-file; main.ts's parser strips dashes from flag names,
   * so the option key here is the all-lowercase "contextfile". */
  contextfile?: string;
}

function emit(opts: AgentCliOptions, event: Record<string, unknown>): void {
  if (opts.json) {
    process.stdout.write(JSON.stringify(event) + "\n");
  } else {
    process.stderr.write(`[agent] ${JSON.stringify(event)}\n`);
  }
}

function errorText(c: { result: any }): string {
  const content = c.result?.content ?? [];
  return content.map((b: any) => (b.type === "text" ? b.text : "")).join("\n");
}

export async function runAgent(opts: AgentCliOptions): Promise<number> {
  const provider = opts.provider ?? "deepseek";
  const modelId = opts.model ?? "deepseek-v4-flash";
  const limiter = new Limiter({
    maxMoves: opts.maxMoves ?? 40,
    maxConsecutiveRefusals: opts.maxRefusals ?? 4,
  });

  const client = new MindGraphClient({ bin: opts.bin, dir: opts.dir });
  const session = new Session(client, opts.worktree, undefined, {
    boundaryEnabled: !opts.noBoundary,
    wording: opts.wording ?? "new",
    markForeign: opts.markForeign ?? true,
  });

  const ctx = opts.node ? await session.openNode(opts.node) : null;
  if (ctx) {
    emit(opts, { type: "node_opened", node_id: ctx.node_id, node_name: ctx.node_name });
  }

  const models = createModels();
  models.setProvider(deepseekProvider());
  const model = models.getModel(provider, modelId);
  if (!model) throw new Error(`model ${provider}/${modelId} not found`);

  const systemPrompt = opts.plan
    ? (ctx ? [
        "You are a vibe-coding agent in PLANNING mode.",
        `Open node: ${ctx.node_name} (${ctx.node_id}).`,
        "You have READ-ONLY tools (list_nodes/open_node/read_file/run_tests).",
        "You physically cannot write anything. Explore the node context and",
        "produce a plan for the user's task, then call submit_plan with:",
        "the affected nodes, the files to change, the contracts whose signature",
        "must change, and the tests to run. Do not over-scope.",
        "Use studio markup for entity references in every written answer: @[node name] for nodes, #[symbol] for symbols, `path` for files, +N / \u2212N for added/removed line counts, !ok[...] / !fail[...] for test verdicts, ~[duration] for timing. Never invent other tags."
      ].join("\n") : [
        "You are a vibe-coding agent in PLANNING mode, scoped to the WHOLE project.",
        "You have READ-ONLY tools (list_nodes/open_node/read_file/run_tests).",
        "You physically cannot write anything. Explore the graph (list_nodes, then",
        "open_node on the areas that matter) and produce a plan for the user's",
        "task, then call submit_plan with: the affected nodes, the files to",
        "change, the contracts whose signature must change, and the tests to run.",
        "Do not over-scope.",
        "Use studio markup for entity references in every written answer: @[node name] for nodes, #[symbol] for symbols, `path` for files, +N / \u2212N for added/removed line counts, !ok[...] / !fail[...] for test verdicts, ~[duration] for timing. Never invent other tags."
      ].join("\n"))
    : opts.noBoundary
      ? [
          "You are a vibe-coding agent. Complete the user's task.",
          "You have the tools list_nodes/open_node/read_file/edit_file/",
          "create_file/run_tests/request_contract_change. Edit the files you need.",
          "Use studio markup for entity references in every written answer: @[node name] for nodes, #[symbol] for symbols, `path` for files, +N / \u2212N for added/removed line counts, !ok[...] / !fail[...] for test verdicts, ~[duration] for timing. Never invent other tags."
        ].join("\n")
      : [
          "You are a vibe-coding agent with hard node boundaries.",
          `Open node: ${ctx.node_name} (${ctx.node_id}).`,
          "You edit ONLY files in your zone. You have no generic bash and no",
          "write tools outside your zone: if an operation is not in the list, the",
          "correct answer is \"no such tool\", not a workaround.",
          "Contracts (shared symbols) can be read, their signatures cannot be",
          "changed — to change a signature use request_contract_change.",
          "The test set is decided by run_tests (mindgraph), not by you.",
          "Read the verdict (ok/failed/unverified) from the run_tests response.",
          "If a tool refused, do not retry the same workaround — rephrase within",
          "the zone or report that you cannot.",
        ].join("\n");

  // item 3: memory (global + project) and a compacted summary of prior turns
  // in the active conversation, when studio.py supplied one. Prepended, not
  // appended — durable facts and prior decisions outrank the generic mode
  // instructions above when the model is deciding how to act.
  let fullSystemPrompt = systemPrompt;
  if (opts.contextfile) {
    try {
      const context = readFileSync(opts.contextfile, "utf8").trim();
      if (context) fullSystemPrompt = `${context}\n\n---\n\n${systemPrompt}`;
    } catch {
      // Missing/unreadable context file — proceed without it rather than fail
      // the whole turn over what is, at worst, missing memory.
    }
  }

  const agent = new Agent({
    initialState: {
      systemPrompt: fullSystemPrompt,
      model,
      tools: opts.plan ? buildPlanTools(session) : buildTools(session),
      thinkingLevel: opts.thinking === "on" ? "low" : "off",
    },
    streamFn: models.streamSimple.bind(models),
    // Defense in depth + move/refusal limit.
    afterToolCall: async (c) => {
      if (!opts.noBoundary && !opts.plan &&
          (c.toolCall.name === "edit_file" || c.toolCall.name === "create_file")) {
        const guard = await runGuard(client, opts.worktree, ctx.node_id);
        emit(opts, {
          type: "verify_verdict",
          tool: c.toolCall.name,
          ok: guard.ok,
          violations: guard.verdict?.violations ?? [],
          rolled_back: guard.rolledBack,
        });
        if (!guard.ok && guard.incident) {
          return {
            content: [{ type: "text", text: guard.incident }],
            details: {},
            isError: true,
            terminate: true,
          };
        }
      }

      const refusalKind = c.isError
        ? classifyRefusal(c.toolCall.name, errorText(c))
        : null;
      const resultText = errorText(c);
      const unassigned =
        c.toolCall.name === "create_file" && resultText.includes("UNASSIGNED");
      if (c.toolCall.name === "submit_plan") {
        emit(opts, { type: "plan.ready", ...c.args });
      }
      emit(opts, {
        type: "tool_outcome",
        tool: c.toolCall.name,
        refused: c.isError,
        refusal_kind: refusalKind,
        unassigned,
        refusal_text: c.isError ? errorText(c) : null,
        args: c.args,
      });
      if (limiter.onMove(refusalKind)) {
        emit(opts, {
          type: "agent_stop",
          reason: limiter.stopped?.reason,
          kind: limiter.stopped?.kind,
        });
        return { terminate: true };
      }
      return undefined;
    },
    shouldStopAfterTurn: async () => {
      if (limiter.stopped) {
        emit(opts, {
          type: "agent_stop",
          reason: limiter.stopped.reason,
          kind: limiter.stopped.kind,
        });
        return true;
      }
      return false;
    },
  });

  agent.subscribe((event) => {
    if (event.type === "tool_execution_start") {
      emit(opts, { type: "tool_call_start", tool: event.toolName });
    } else if (event.type === "tool_execution_end") {
      emit(opts, { type: "tool_call_end", tool: event.toolName });
    } else if (event.type === "agent_start") {
      emit(opts, { type: "agent_start" });
    } else if (event.type === "agent_end") {
      emit(opts, {
        type: "agent_end",
        stop_reason: limiter.stopped?.reason ?? null,
      });
    } else if (event.type === "message_update") {
      // Stream the model's text/reasoning deltas to the event log.
      const ae = event.assistantMessageEvent;
      if (ae.type === "text_delta") {
        emit(opts, { type: "assistant_text_delta", delta: ae.delta });
      } else if (ae.type === "thinking_delta") {
        emit(opts, { type: "assistant_thinking_delta", delta: ae.delta });
      } else if (ae.type === "thinking_start") {
        emit(opts, { type: "assistant_thinking_start" });
      } else if (ae.type === "thinking_end") {
        emit(opts, { type: "assistant_thinking_end" });
      }
    }
  });

  await agent.prompt(opts.task);
  return 0;
}
