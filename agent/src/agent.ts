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

export interface AgentCliOptions {
  node: string;
  task: string;
  dir: string;
  worktree: string;
  provider?: string;
  model?: string;
  json?: boolean;
  bin?: string;
}

function emit(opts: AgentCliOptions, event: Record<string, unknown>): void {
  if (opts.json) {
    process.stdout.write(JSON.stringify(event) + "\n");
  } else {
    process.stderr.write(`[agent] ${JSON.stringify(event)}\n`);
  }
}

export async function runAgent(opts: AgentCliOptions): Promise<number> {
  const provider = opts.provider ?? "deepseek";
  const modelId = opts.model ?? "deepseek-v4-flash";

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
  ].join("\n");

  const agent = new Agent({
    initialState: {
      systemPrompt,
      model,
      tools: buildTools(session),
      thinkingLevel: "off",
    },
    streamFn: models.streamSimple.bind(models),
    // Защита в глубину: после каждой записи — git diff + verify.
    afterToolCall: async (c) => {
      if (c.toolCall.name !== "edit_file" && c.toolCall.name !== "create_file") {
        return undefined;
      }
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
      return undefined;
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
      emit(opts, { type: "agent_end" });
    }
  });

  await agent.prompt(opts.task);
  return 0;
}
