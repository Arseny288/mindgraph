// tools.ts — инструменты агента (фаза 2.3).
//
// Универсальных read/write/edit/bash НЕТ. Есть только узкий набор инструментов,
// у которых записи вне зоны нет как операции (отсутствие двери, а не забор).

import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { Session } from "./session.ts";

function text(content: string, details: any = {}) {
  return { content: [{ type: "text", text: content }], details };
}

export function buildTools(session: Session): AgentTool[] {
  return [
    {
      name: "list_nodes",
      label: "Список узлов",
      description: "Узлы графа с краткими описаниями (id, name, summary).",
      parameters: Type.Object({}),
      async execute() {
        const r = await session.listNodes();
        return text(JSON.stringify(r, null, 2));
      },
    },
    {
      name: "open_node",
      label: "Открыть узел",
      description:
        "Контекст узла: зона, контракты, смешанные файлы. Это заменяет «прочитать весь репозиторий».",
      parameters: Type.Object({ id: Type.String({ description: "id или имя узла" }) }),
      async execute(_id, params) {
        const ctx = await session.openNode(params.id);
        const b = ctx.boundary;
        const foreign = ctx.foreign_symbols.map((f) =>
          `  - ${f.symbol} (хозяин: узел «${f.owner}», ${f.file}:${f.lines[0]}-${f.lines[1]})`,
        );
        const view = {
          node_id: ctx.node_id,
          node_name: ctx.node_name,
          allow_write: b.allow_write,
          allow_create: b.allow_create,
          allow_read: b.allow_read,
          contracts: b.contracts,
          "ЧУЖИЕ_СИМВОЛЫ_В_ЗОНЕ_НЕ_ТРОГАТЬ": foreign,
        };
        return text(JSON.stringify(view, null, 2));
      },
    },
    {
      name: "read_file",
      label: "Читать файл",
      description: "Прочитать файл. Отказ, если путь вне allow_read узла.",
      parameters: Type.Object({ path: Type.String() }),
      async execute(_id, params) {
        const c = await session.readFile(params.path);
        return text(c);
      },
    },
    {
      name: "edit_file",
      label: "Правка файла",
      description:
        "Заменить содержимое файла. Отказ, если путь вне allow_write или правка затрагивает чужой символ в смешанном файле (проверка ДО записи).",
      parameters: Type.Object({
        path: Type.String(),
        content: Type.String({ description: "новое полное содержимое файла" }),
      }),
      async execute(_id, params) {
        const r = await session.editFile(params.path, params.content);
        return text(JSON.stringify(r));
      },
    },
    {
      name: "create_file",
      label: "Создать файл",
      description:
        "Создать файл в allow_create. Вне allow_create — не отказ, а маршрут в «Неразобранное».",
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
      name: "run_tests",
      label: "Прогнать тесты",
      description:
        "Прогнать тесты зоны. Состав определяет mindgraph (tests --for-node), агент не выбирает и произвольной оболочки не получает.",
      parameters: Type.Object({}),
      async execute() {
        const r = await session.runTests();
        return text(JSON.stringify(r, null, 2));
      },
    },
    {
      name: "request_contract_change",
      label: "Запрос изменения контракта",
      description:
        "Запросить у человека изменение сигнатуры контракта. Ничего не меняет — останавливает сессию и выносит запрос.",
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
