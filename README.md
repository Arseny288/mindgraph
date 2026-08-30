# mindgraph

CLI-инструмент, который сканирует Python-репозиторий, разбивает его на
**смысловые узлы** (единицы замысла: «Аутентификация», «Хранилище»,
«Уведомления») и показывает граф связей между ними в браузере.

Это **оверлей поверх кода**, а не разбиение кода: узел ≠ файл, узел ≠ модуль.
Инструмент строго **read-only** — он ничего не меняет в анализируемом проекте.

## Что делает

1. **Этап A (статика)** — `ast` без запуска кода: извлекает символы
   (функции/методы/классы/синглтоны) и рёбра между ними (вызовы, упоминания,
   наследование).
2. **Этап B (смысл)** — LLM группирует символы в 5–15 смысловых узлов по
   назначению, а не по файлам. В промпт уходят только `id`/`sig`/`doc` и рёбра —
   **тела функций никогда не покидают машину**.
3. **Этап B2 (домены)** — второй LLM-запрос собирает узлы в домены верхнего
   уровня.
4. **Этап C (shared-слой)** — для каждого символа считает `owners` (кто его
   использует). Это отвечает на вопрос «реализуемы ли границы записи»: много
   shared-символов → узлы нельзя изолировать.

## Установка

Требуется Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # базово: openai + pathspec
pip install -e ".[anthropic]"   # если нужен Anthropic-провайдер
```

## Ключ API

```bash
cp .env.example .env        # впиши DEEPSEEK_API_KEY (и/или ANTHROPIC_API_KEY)
```

`.env` не коммитится (см. `.gitignore`). Ключ можно также задать переменной
окружения. Без ключа инструмент работает как `--no-llm` — только статика.

## Использование

Машинный контракт (фаза 1): результат — только в stdout, только валидный JSON
(в каждом ответе `"schema_version"`), все логи/прогресс — в stderr. Коды
возврата: `0` успех, `1` ошибка выполнения, `2` неверные аргументы,
`3` нарушение границы.

```bash
mindgraph scan <dir>                # построить/обновить graph.json (--serve/--stats)
mindgraph scan <dir> --no-llm       # только статический анализ
mindgraph scan <dir> --regroup      # полный пересчёт LLM-группировки
mindgraph graph --json [--dir .]    # весь граф
mindgraph node <id> --json          # узел: зона, контракты, смешанные файлы
mindgraph impact <sym|file> [--depth N] --json   # транзитивное множество
mindgraph boundary <node> --json    # границы: allow_write/allow_create/read
mindgraph tests --for-node <id> --json          # какие тесты (ok/unverified)
mindgraph verify --node <id> --diff <path|->    # проверка диффа (exit 3)
```

Служебное:

```bash
mindgraph --list-providers
mindgraph --version
mindgraph --help
```

Пример на реальной библиотеке, без тестов:

```bash
mindgraph scan Testing_libs/requests --ignore tests --serve
```

## Манифест узлов (стабильные id)

`<repo>/.mindvibe/nodes.yaml` (коммитится в git) фиксирует идентичность узлов —
LLM при пересчёте не переименовывает и не переразбивает их, а классифицирует
только символы, не покрытые `own_paths`:

```yaml
nodes:
  - id: auth
    title: Аутентификация
    own_paths: ["app/auth/**", "app/tokens.py"]
```

`own_paths` — gitignore-глобы относительно корня репы. Символы, попавшие под
них, приписываются к узлу механически (модель их не видит).

## Cache-first флоу

```
mindgraph scan <path>
  └─ repo_hash + config_fingerprint
      ├─ совпали с .mindgraph/graph.json  → переиспользуем, ноль работы
      └─ изменились:
          ├─ пере-экстракт статики (ast, быстро)
          ├─ LLM-группировка СОХРАНЯЕТСЯ (новые символы → «Прочее»)
          └─ полный regroup — только по явному --regroup
```

`config_fingerprint` покрывает провайдер/модель/диапазон узлов/entry_points/
версии промптов — смена конфига инвалидирует кэш наравне с правкой кода.

LLM-ответы кэшируются по `sha256(provider + model + system_prompt +
user_prompt)` в `~/.cache/mindgraph/llm/`. Повторный `--regroup` на неизменном
коде **бесплатен** — ходит в кэш, а не в API. Сырые ответы модели для аудита
лежат в `<path>/.mindgraph/runs/`.

## Настройки

Приоритет: **CLI > `MINDGRAPH_*` env > `mindgraph.toml` (корень репы) >
`~/.config/mindgraph/config.toml` > дефолты**.

Пример `mindgraph.toml`:

```toml
[llm]
provider = "deepseek"        # deepseek | anthropic
model = "deepseek-v4-flash"
node_min = 5
node_max = 15
no_llm = false

[scan]
ignore = ["tests", "docs"]   # доп. каталоги поверх .gitignore/.mindgraphignore
entry_points = ["wsgi.py", "asgi.py", "manage.py"]

[serve]
port = 5173
open_browser = true

[cache]
dir = "~/.cache/mindgraph"
```

Переменные окружения: `MINDGRAPH_PROVIDER`, `MINDGRAPH_MODEL`,
`MINDGRAPH_NODE_MIN`, `MINDGRAPH_NODE_MAX`, `MINDGRAPH_CACHE_DIR`,
`MINDGRAPH_PORT`, `MINDGRAPH_IGNORE` (через запятую), `MINDGRAPH_NO_LLM`,
`MINDGRAPH_OPEN_BROWSER`.

## Игноры при сканировании

Всегда игнорируются: `.git`, `venv`, `.venv`, `node_modules`, `__pycache__`,
`.mindgraph`, `.mindvibe` (внутри — только манифест `nodes.yaml`, сам он не
сканируется как код), `build`, `dist`, `migrations`, `*.egg-info`, плюс всё из
`.gitignore` (корень + вложенные) и `.mindgraphignore` (формат gitignore, с
поддержкой `!`-negation).

## Как читать метрику `shared_ratio`

`mindgraph scan <path> --stats` печатает долю shared-символов — тех, кого
используют более чем из одного узла.

- до ~15% — узлы изолируемы, границы записи осмысленны;
- выше ~30% — узлы «протекают» друг в друга, определение узла нужно менять.

## Разработка

```bash
pytest tests/            # все тесты (без внешней сети — LLM подменяется фейком)
```

Тесты построены на синтетической фикстуре `tests/fixtures/sample_repo/`
(два независимых кластера + общий модуль-утилита + цикл + файл с
синтаксической ошибкой).

## Структура

```
mindgraph/
  cli.py       # argparse + конфиг, оркестрация
  config.py    # Config, приоритеты CLI/env/TOML/дефолты
  extract.py   # этап A: ast → символы и рёбра + .gitignore
  group.py     # этап B: LLM-группировка в смысловые узлы
  domains.py   # этап B2: LLM-группировка узлов в домены
  shared.py    # этап C: shared-слой (owners)
  llmcache.py  # кэш LLM-ответов по хэшу промпта
  emit.py      # сборка graph.json + cache-first
  serve.py     # статик-сервер вьюера
  model.py     # модель владения: файлы/контракты/границы/смешанные файлы
  manifest.py  # манифест .mindvibe/nodes.yaml (стабильные id узлов)
  impact.py    # транзитивное множество затронутого
  verify.py    # механическая проверка диффа против границы узла
  testselect.py# какие тесты прогонять (ok/failed/unverified)
viewer/        # index.html, main.jsx, nodes.jsx, theme.js (React Flow + dagre)
tests/         # фикстура + тесты этапов A/B/C/emit/ignore/cache/config + фаз 1/1.5
MINDGRAPH.md   # детальная документация
```

`graph.json` — единственный контракт между анализатором и вьюером: вьюер не
знает про Python и ничего не досчитывает сам.
