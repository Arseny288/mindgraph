// agent.ts — точка входа headless-агента (фаза 2.6).
//
//   node agent/src/main.ts --node <id> --task "..." \
//       --dir <main-repo> --worktree <worktree> [--json]
//
// Первый режим работы — headless (без TUI). В --json — поток событий JSONL.

import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { MindGraphClient } from "./mindgraph.ts";
import { Session } from "./session.ts";
import { buildTools } from "./tools.ts";
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
  const session = new Session(client, opts.worktree);

  const ctx = await session.openNode(opts.node);
  emit(opts, { type: "node_opened", node_id: ctx.node_id, node_name: ctx.node_name });

  const models = createModels();
  models.setProvider(deepseekProvider());
  const model = models.getModel(provider, modelId);
  if (!model) throw new Error(`модель ${provider}/${modelId} не найдена`);

  const systemPrompt = [
    "Ты — агент вайб-кодинга с жёсткими границами узла.",
    `Открытый узел: ${ctx.node_name} (${ctx.node_id}).`,
    "Ты правишь ТОЛЬКО файлы своей зоны. У тебя нет универсального bash и",
    "нет инструментов записи вне зоны: если операции нет в списке —",
    "правильный ответ «инструмента нет», а не обходной путь.",
    "Контракты (shared-символы) читать можно, менять их сигнатуру нельзя —",
    "для изменения сигнатуры используй request_contract_change.",
    "Состав тестов определяет run_tests (mindgraph), не ты.",
    "Вердикт (ok/failed/unverified) смотри в ответе run_tests.",
    "Если инструмент отказал — не пробуй тот же обход снова, а переформулируй",
    "внутри зоны или сообщи, что не можешь.",
  ].join("\n");

  const agent = new Agent({
    initialState: {
      systemPrompt,
      model,
      tools: buildTools(session),
      thinkingLevel: opts.thinking === "on" ? "low" : "off",
    },
    streamFn: models.streamSimple.bind(models),
    // Защита в глубину + лимит ходов/отказов.
    afterToolCall: async (c) => {
      if (c.toolCall.name === "edit_file" || c.toolCall.name === "create_file") {
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
      emit(opts, {
        type: "tool_outcome",
        tool: c.toolCall.name,
        refused: c.isError,
        refusal_kind: refusalKind,
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
    }
  });

  await agent.prompt(opts.task);
  return 0;
}
