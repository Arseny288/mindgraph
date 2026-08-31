# HANDOFF.md — текущее состояние и что читать дальше

> Читать вместе с `mission.md` (спека фаз 0–3). Этот файл — оперативный: где
> что лежит, что запущено, что сломано, что делать следующей сессии.

## Ветка и последние коммиты

- Ветка: `phase-2.5-hardening` (фаз 3 ещё НЕТ, не начинать).
- Последние коммиты (свежие сверху):
  - `2829370` eval: runner optimizations (parallel jobs, adaptive repeats, smoke, cache, error, label)
  - `b5f451c` eval: split agent_end into proposed/gave_up, restore contract task, foreign-contract fixture
  - `d8640fa` eval: validate task set, fix detection, boundary-only median
  - `ea9779e` Translate the codebase to English
  - `993b3a2` 2.5 (1/6): фикстура границ + harness живых прогонов отказа

## Статус фаз

| фаза | статус |
|---|---|
| 0 (гигиена) | готово |
| 1 (JSON-контракт) | готово |
| 1.5 (границы) | готово |
| 2 (агент на pi) | готово |
| 2.5 (hardening/eval) | готово, кроме незавершённого прогона и RU/EN-сравнения (см. ниже) |
| 3 (проверка = тесты) | НЕ начата |

## Что сделано в этой сессии

1. **Перевод на английский** (`ea9779e`): все промпты (grouping/domains/merge/
   manifest), системный промпт агента, описания инструментов, сообщения
   отказа, имена узлов (`Прочее`→`Unassigned`, `Проект`→`Project`), eval-задачи,
   комментарии/докстринги. Оставлены как данные: невалидный JSON в тестах
   (`"не json"`, `"это не toml"`), докстринги фикстуры `tests/fixtures/sample_repo/`
   (симулированный пользовательский код).
2. **Инвалидация кэша по промпту** — `tests/test_emit.py`:
   `test_config_fingerprint_embeds_prompt_text`,
   `test_prompt_change_forces_recalc_of_existing_graph`.
3. **Разделение данных и видимого модели** — имена узлов/содержимое заметок в
   тестах фазы 2 переведены; фейковые LLM-фикстуры фазы 0.5 оставлены.
4. **Eval-фиксы**:
   - baseline-валидация: нейтральный системный промпт при `--no-boundary`,
     проверка `crossed` (запись в чужой файл); все 9 boundary-задач проходят.
   - метрика: `agent_end` разделён на `proposed` (request_contract_change или
     запись в зоне) vs `gave_up`; `error` (ненулевой код / <2с / 0 вызовов LLM).
   - фикстура: `hash_value` передан узлу Billing → у Authentication теперь
     ЧУЖОЙ контракт (виден в `contracts` и `allow_read`), контрактные задачи
     упираются в edit_file-стену и могут предложить request_contract_change.
   - набор задач: 15 (10 boundary + 5 control), `contract_change_request`
     возвращён, `own_read` → auth/login.py, `own_contract_body` убран.
5. **Оптимизация раннера** (`2829370`) — см. отдельный блок ниже.

## Архитектура (что где лежит)

- Python-мозг: `mindgraph/` (extract/group/domains/shared/emit/model/manifest/
  impact/verify/testselect/serve/config/cli).
- TS-агент: `agent/src/` — `mindgraph.ts` (клиент), `session.ts` (сессия),
  `boundary.ts` (проверки пути/символа), `tools.ts` (7 инструментов, без bash),
  `guard.ts` (git diff + verify + откат), `limiter.ts` (лимит ходов/отказов),
  `agent.ts`/`main.ts` (headless-вход). Запускается Node 26 нативно (TS).
- Точка входа агента: `mindgraph agent --node … --task … [--worktree] [--json]`.
- Eval:
  - задачи: `agent/eval/tasks/*.json` (id, node, prompt, crosses/reads, control,
    smoke);
  - фикстура: `agent/fixture/make_fixture.py` (`python make_fixture.py <root>`
    пишет `.mindgraph/graph.json`);
  - раннер: `agent/eval/runner.py`;
  - результаты: `agent/eval/results/<label>.json` (gitignored), кэш в
    `results/.cache/`;
  - логи: `agent/eval/logs/` (gitignored).
- Манифест узлов: `.mindvibe/nodes.yaml` (id/title/own_paths, в git).
- Память узлов: `.mindvibe/notes/<node_id>.md`.

## Как гонять eval

```bash
# smoke (1 повтор, 5 ключевых задач) — СНАЧАЛА это:
.venv/bin/python agent/eval/runner.py --smoke --label smoke \
    --main /tmp/opencode/brepo

# полный прогон (параллельно, адаптивные повторы):
.venv/bin/python agent/eval/runner.py --config new_mark --max-repeat 5 \
    --jobs 4 --label new_mark_en --main /tmp/opencode/brepo
```

Конфиги (`CONFIGS` в runner.py): `baseline` (`--no-boundary`), `old_nomark`,
`new_nomark`, `new_mark` (дефолт), `new_mark_on` (`--thinking on`).

Метрика (после поправки пользователя): 0 отказов на boundary-задаче — это НЕ
провал; baseline доказывает, что агент понимает задачу (без стены пишет в
чужой файл), а со стеной — упреждающе не лезет. Смотреть: `baseline.crossed`
(годность задачи) + `proposed` vs `gave_up` (конструктивность) + `error`.

## Что сломано / известные проблемы

1. **RU-агент без node_modules.** Для RU/EN-сравнения (отложено) worktree
   `/tmp/opencode/ru-agent` (коммит `993b3a2`) не имеет `agent/node_modules/`
   (gitignored). Если возвращаться к сравнению — сначала
   `ln -s <repo>/agent/node_modules /tmp/opencode/ru-agent/agent/node_modules`.
2. **Раннер маскировал падение под gave_up** — починено в `2829370`
   (категория `error`), НЕ проверено прогоном.
3. **Общий файл результатов** — было: оба конфига писали в `new_mark.json`.
   Починено в `2829370` (`--label`), НЕ проверено.
4. **Флейк-таймаут DeepSeek** (`own_zone` ранее зависал на первой реплике) —
   раннер имеет ретрай на TIMEOUT (3 попытки), но сам факт флейка остаётся.
5. Финальные числа прогонов СТАРЫЕ/отсутствуют: после перевода валидного
   полного прогона ещё не было; `results/` сейчас пуст.

## Оптимизация раннера — отдельный блок

Запрошена пользователем (6 пунктов), реализована в `2829370`, но **НЕ
провалидирована** (smoke не запускался — сессия была прервана). Пункты:

1. `--jobs N` (по умолч. 4) — параллельные задачи, каждая в своём worktree.
2. Адаптивные повторы — стоп, когда 2 подряд прогона дали одинаковый исход
   `(outcome, crossed, proposed)`; `--max-repeat` — потолок.
3. `--smoke` — 1 повтор, только задачи с `"smoke": true` (пока smoke-флаги в
   задачах НЕ проставлены — надо пометить ~5 ключевых задач).
4. Кэш — `results/.cache/<sha256(commit+config+model+prompt)>.json`; повторный
   прогон того же — из кэша.
5. Категория `error` — ненулевой код / <2с / 0 вызовов LLM (не gave_up).
6. Имя выходного файла из `--label`.

**Первый шаг следующей сессии:** проставить `"smoke": true` в 5 ключевых
задачах (например edit_neighbor_file, edit_foreign_symbol,
change_contract_signature, create_outside, own_login_docstring), затем
`--smoke`, и только при зелёном — полный прогон.

## Что читать дальше

1. `mission.md` — спека фаз (фаза 3 — целевая, НЕ начинать).
2. `README.md` / `MINDGRAPH.md` — CLI, JSON-контракт, манифест.
3. `agent/eval/runner.py` — актуальная метрика и конфиги.
4. `mindgraph/verify.py` + `mindgraph/model.py` — семантика границ.
