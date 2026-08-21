# mindgraph — смысловой оверлей поверх Python-репозитория

CLI-инструмент, который сканирует Python-репозиторий, разбивает его на
**смысловые узлы** (единицы замысла: «Аутентификация», «Хранилище»…) и
показывает граф связей в браузере. Это оверлей поверх кода, а не разбиение
кода: узел ≠ файл, узел ≠ модуль.

## Установка

```
pip install -e .
mindgraph <path>            # анализ + запись .mindgraph/graph.json
mindgraph <path> --serve    # то же + вьюер на http://127.0.0.1:5173
mindgraph <path> --no-llm   # только статический анализ, без LLM-группировки
mindgraph <path> --regroup  # полный платный пересчёт LLM-группировки
mindgraph <path> --stats    # метрики shared-слоя и выход
```

## Cache-first флоу

```
mindgraph <path>
  └─ repo_hash по (путь, размер, mtime) .py-файлов + содержимое ignore-файлов
     ├─ совпал с .mindgraph/graph.json  → переиспользуем, ноль работы
     └─ изменился:
         ├─ пере-экстракт статики (ast, быстро)
         ├─ LLM-группировка СОХРАНЯЕТСЯ (новые символы → «Прочее»)
         └─ полный regroup — только по явному --regroup
```

LLM-вызовы кэшируются по `sha256(provider + model + system_prompt +
user_prompt)` в `~/.cache/mindgraph/llm/` (см. `--config cache_dir`), поэтому
повторный `--regroup` на неизменном коде бесплатен. Сырые ответы модели для
аудита лежат в `.mindgraph/runs/`.

## Настройки

Приоритет: **CLI > `MINDGRAPH_*` env > `mindgraph.toml` (корень репы) >
`~/.config/mindgraph/config.toml` > дефолты**.

Пример `mindgraph.toml`:

```toml
[llm]
provider = "deepseek"            # deepseek | anthropic
model = "deepseek-v4-flash"
node_min = 5
node_max = 15

[scan]
ignore = ["tests", "docs"]       # доп. каталоги поверх .gitignore/.mindgraphignore

[serve]
port = 5173
open_browser = true

[cache]
dir = "~/.cache/mindgraph"
```

Ключи провайдеров — `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` из окружения
или `.env` рядом с инструментом.

## Игноры

Сканирование игнорирует: `.git`, `venv`, `.venv`, `node_modules`,
`__pycache__`, `.mindgraph`, `.mindvibe` (legacy), `build`, `dist`,
`migrations`, `*.egg-info`, всё из `.gitignore` (корень + вложенные) и
`.mindgraphignore`.

## Как это работает

| этап | модуль | что делает |
|---|---|---|
| A. статика | `mindgraph/extract.py` | `ast` → символы (def/class/binding/`<module>`) и рёбра (call/uses/inherits/contains) |
| B. смысл | `mindgraph/group.py` | LLM группирует символы в 5–15 узлов; в промпт идут только `id`/`sig`/`doc`/рёбра |
| B2. домены | `mindgraph/domains.py` | второй LLM-запрос: узлы L1 → домены L0 |
| C. shared | `mindgraph/shared.py` | `owners(S)` по прямым обратным рёбрам → кто сломается при правке |
| сборка | `mindgraph/emit.py` | `graph.json` — единственный контракт с вьюером |
| вьюер | `mindgraph/serve.py` + `viewer/` | статика, React Flow + dagre |

## Структура

```
mindgraph/
  cli.py       # argparse + конфиг, оркестрация
  config.py    # Config, load_config, приоритеты
  extract.py   # этап A + .gitignore
  group.py     # этап B (LLM-узлы)
  domains.py   # этап B2 (LLM-домены)
  shared.py    # этап C (shared-слой)
  llmcache.py  # кэш LLM-ответов по хэшу промпта
  emit.py      # сборка graph.json + cache-first
  serve.py     # статик-сервер вьюера
viewer/        # index.html, main.jsx, nodes.jsx, theme.js
tests/         # fixture + тесты этапов A/B/C/emit
```

## Ключевая метрика

`shared_ratio` — доля символов, достижимых более чем из одного узла.

- ≤ ~15% — узлы изолируемы, границы записи осмысленны;
- ≥ ~30% — узлы «протекают» друг в друга, определение узла нужно менять.
