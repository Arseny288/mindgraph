# mindgraph — a semantic overlay for Python repositories

A CLI tool that scans a Python repository, splits it into **semantic nodes**
(units of intent: "Authentication", "Storage", "Notifications") and shows the
graph of connections between them in a browser.

It is an **overlay on top of the code**, not a splitting of the code: a node is
not a file, not a module, not a class. One node usually spans several files; one
file can belong to several nodes. The analyzer itself is strictly **read-only**.

The end goal (phases 2+): a graph-oriented agent that physically cannot edit
code outside the selected node. The value is not the graph or the model — it is
the enforcement loop:

1. **impact graph** — static analysis (`call` / `uses` / `inherits` + transitive closure).
2. **ownership model** — every symbol belongs to exactly one node; symbols used
   by more than one node are "contracts" whose signatures cannot change without
   explicit agreement.
3. **locked agent** — the agent edits only its own zone. The check is mechanical,
   not "we asked the model not to".
4. **verification = tests** — run the tests of the touched zones plus contract
   tests; green means "did not break, as far as the tests know".

The key architectural decision: the boundary is **not a hook bolted on top of an
agent that has a universal `write`**. The agent gets a tool set where writing
outside the zone simply does not exist as an operation. This is not a fence, it
is the absence of a door.

---

## Stack layout

- **Python part (`mindgraph/`)** — the brain. Extracts the graph, computes impact,
  ownership, contracts, boundaries. Stays on Python (never ported to TypeScript).
- **TypeScript part (`agent/`)** — the hands. A custom agent on top of the `pi`
  libraries (`pi-agent-core` + `pi-ai`), with its own tools.
- The link between them — **a CLI with JSON on stdout**. The TS side knows nothing
  about the extractor internals, only the JSON schema.

---

## Installation

Requires Python 3.11+ (for the Python part) and Node 26 (for the agent).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # base: openai + pathspec
pip install -e ".[anthropic]"    # if you need the Anthropic provider
```

Agent dependencies:

```bash
cd agent && npm install
```

## API key

```bash
cp .env.example .env             # fill in DEEPSEEK_API_KEY (and/or ANTHROPIC_API_KEY)
```

`.env` is not committed (see `.gitignore`). The key can also be set as an
environment variable. Without a key the tool works as `--no-llm` — static
analysis only.

---

## CLI — the JSON contract

The machine contract (phase 1): the result goes **only to stdout**, only valid
JSON (every response contains `"schema_version"`), all logs/progress go to
stderr. Exit codes: `0` success, `1` runtime error, `2` bad arguments,
`3` boundary violation (`verify`).

```bash
mindgraph scan <dir>                # build/update graph.json (--serve/--stats)
mindgraph scan <dir> --no-llm       # static analysis only
mindgraph scan <dir> --regroup      # full LLM regroup
mindgraph graph --json [--dir .]    # the whole graph
mindgraph node <id> --json          # node: zone, contracts, mixed files
mindgraph impact <sym|file> [--depth N] --json   # transitive set
mindgraph boundary <node> --json    # boundaries: allow_write/allow_create/read
mindgraph tests --for-node <id> --json          # which tests (ok/unverified)
mindgraph verify --node <id> --diff <path|->    # check a diff (exit 3)
```

Utility:

```bash
mindgraph --list-providers
mindgraph --version
mindgraph --help
```

Agent entry point (phase 2):

```bash
mindgraph agent --node <id> --task "..." [--worktree <path>] [--json]
```

`--json` emits a JSONL event stream (move start, tool call, tool refusal, verify
verdict, test result) — the future transport for the viewer, versioned and fixed.

---

## Node manifest (stable ids)

`<repo>/.mindvibe/nodes.yaml` (committed to git) pins the identity of nodes — the
LLM does not rename or re-split them on recompute; it only classifies symbols not
covered by `own_paths`:

```yaml
nodes:
  - id: auth
    title: Authentication
    own_paths: ["app/auth/**", "app/tokens.py"]
```

`own_paths` are gitignore-style globs relative to the repo root. Symbols under
them are assigned mechanically (the model never sees them).

Node memory lives in `<repo>/.mindvibe/notes/<node_id>.md`.

---

## Cache-first flow

```
mindgraph scan <path>
  └─ repo_hash + config_fingerprint
      ├─ match .mindgraph/graph.json  → reuse, zero work
      └─ changed:
          ├─ re-extract statics (ast, fast)
          ├─ LLM grouping PRESERVED (new symbols → "Unassigned")
          └─ full regroup — only with explicit --regroup
```

`config_fingerprint` covers provider/model/node range/entry points/prompt
versions — a config change invalidates the cache just like a code edit.

LLM responses are cached by `sha256(provider + model + system_prompt +
user_prompt)` in `~/.cache/mindgraph/llm/`. A repeated `--regroup` on unchanged
code is **free** — it hits the cache, not the API. Raw model responses for audit
live in `<path>/.mindgraph/runs/`.

---

## Settings

Priority: **CLI > `MINDGRAPH_*` env > `mindgraph.toml` (repo root) >
`~/.config/mindgraph/config.toml` > defaults**.

Example `mindgraph.toml`:

```toml
[llm]
provider = "deepseek"        # deepseek | anthropic
model = "deepseek-v4-flash"
node_min = 5
node_max = 15
no_llm = false

[scan]
ignore = ["tests", "docs"]   # extra dirs on top of .gitignore/.mindgraphignore
entry_points = ["wsgi.py", "asgi.py", "manage.py"]

[serve]
port = 5173
open_browser = true

[cache]
dir = "~/.cache/mindgraph"
```

Environment variables: `MINDGRAPH_PROVIDER`, `MINDGRAPH_MODEL`,
`MINDGRAPH_NODE_MIN`, `MINDGRAPH_NODE_MAX`, `MINDGRAPH_CACHE_DIR`,
`MINDGRAPH_PORT`, `MINDGRAPH_IGNORE` (comma-separated), `MINDGRAPH_NO_LLM`,
`MINDGRAPH_OPEN_BROWSER`.

---

## Scan ignores

Always ignored: `.git`, `venv`, `.venv`, `node_modules`, `__pycache__`,
`.mindgraph`, `.mindvibe` (inside — only the `nodes.yaml` manifest, which is not
scanned as code), `build`, `dist`, `migrations`, `*.egg-info`, plus everything
from `.gitignore` (root + nested) and `.mindgraphignore` (gitignore format, with
`!`-negation).

---

## The `shared_ratio` metric

`mindgraph scan <path> --stats` prints the share of shared symbols — those used
from more than one node.

- up to ~15% — nodes are isolable, write boundaries are meaningful;
- above ~30% — nodes leak into each other, the node definition must change.

---

## Ownership model and boundaries

Every symbol/file belongs to exactly one owning node. A symbol used by more than
one node is marked a **contract**: its signature is pinned by hash and cannot
change without explicit agreement.

`node --json` returns:

- `owned` — paths and symbols the node can edit freely;
- `contracts` — symbols the node uses but does not own: readable, signature locked;
- `foreign` — everything else: unreachable even for reading by default.

`boundary --json` returns the zone that the TS side turns into path checks:

```json
{
  "schema_version": "1",
  "node_id": "auth",
  "allow_write": ["app/auth/**", "tests/auth/**"],
  "deny_write": ["**"],
  "allow_read": ["app/auth/**", "app/models.py"],
  "contracts": [
    {"symbol": "User.check_password", "signature_hash": "..."}
  ]
}
```

Resolution order — deny by default, allow pointwise. Paths are normalized; `..`
and symlinks outside the repo are rejected.

`verify` accepts a unified diff (stdin or file) and answers mechanically (no LLM):

```json
{
  "schema_version": "1",
  "ok": false,
  "violations": [
    {"path": "app/billing/models.py", "reason": "outside_node_zone", "owner": "billing"},
    {"symbol": "User.check_password", "reason": "contract_signature_changed"}
  ]
}
```

Changing a contract's signature is a violation even if the file is in the node's
zone.

---

## The agent (phase 2)

The agent has **no generic `read/write/edit/bash`**. Its tool set:

- `list_nodes()` — graph nodes with brief descriptions;
- `open_node(id)` — node context: zone, contracts, mixed files (replaces "read the whole repo");
- `read_file(path)` — refused if the path is outside `allow_read`;
- `edit_file(path, ...)` — refused if the path is outside `allow_write` or the edit touches a foreign symbol in a mixed file (checked **before** writing);
- `create_file(path, ...)` — same rule; outside `allow_create` it routes to "Unassigned";
- `run_tests()` — no arguments from the model; the command is decided by `mindgraph tests --for-node`;
- `request_contract_change(symbol, rationale)` — changes nothing; stops the session and surfaces the request to a human.

Isolation: every node session runs in its own git worktree. Rollback on a
violation or red tests is free — tear down the worktree.

Defense in depth: after every move, regardless of what the tools reported, the
agent takes the cumulative `git diff`, runs it through `mindgraph verify`, and on
exit code 3 rolls back the move and logs the incident. A tool that let a write
outside the zone slip through is a high-priority bug.

---

## Evaluation (phase 2.5)

Tasks live in `agent/eval/tasks/*.json`; each declares what should happen
(`crosses` — the file correct execution must write to; `reads` — a file it must
read; `control` — an in-zone task). The runner runs the agent N times per
configuration in parallel across tasks, computes the median/spread of refusals
and the outcome split, and writes results to `agent/eval/results/<label>.json`.

```bash
# smoke (1 repeat, the key tasks) — run this FIRST:
.venv/bin/python agent/eval/runner.py --smoke --label smoke \
    --main /tmp/opencode/brepo

# full run (parallel, adaptive repeats):
.venv/bin/python agent/eval/runner.py --config new_mark --max-repeat 5 \
    --jobs 4 --label new_mark_en --main /tmp/opencode/brepo
```

Configs (`CONFIGS` in `runner.py`): `baseline` (`--no-boundary`), `old_nomark`,
`new_nomark`, `new_mark` (default), `new_mark_on` (`--thinking on`).

Metrics: `crossed` in baseline (task validity), `proposed` vs `gave_up`
(constructiveness), `error` (crashes). Zero refusals on a boundary task is **not**
a failure: the baseline proves the agent understands the task (without the wall
it writes to a foreign file), and with the wall it proactively does not cross.

The fixture (`agent/fixture/make_fixture.py`) builds a small deterministic repo
with a deliberately MIXED file (`shared/mixed.py`: `auth_part` -> Authentication,
`billing_part` -> Billing) and a CONTRACT (`shared/contract.py::hash_value`,
owned by Billing, used by both):

```bash
.venv/bin/python agent/fixture/make_fixture.py /tmp/opencode/brepo
```

---

## Project structure

```
mindgraph/
  cli.py        # argparse + config, orchestration
  config.py     # Config, CLI/env/TOML/defaults priority
  extract.py    # stage A: ast -> symbols and edges + .gitignore
  group.py      # stage B: LLM grouping into semantic nodes
  domains.py    # stage B2: LLM grouping of nodes into domains
  shared.py     # stage C: shared layer (owners)
  llmcache.py   # LLM response cache keyed by prompt hash
  emit.py       # graph.json build + cache-first
  serve.py      # viewer static server
  model.py      # ownership model: files/contracts/boundaries/mixed files
  manifest.py   # .mindvibe/nodes.yaml manifest (stable node ids)
  impact.py     # transitive set of what is touched
  verify.py     # mechanical diff check against a node's boundary
  testselect.py # which tests to run (ok/failed/unverified)
  viewer/       # index.html, main.jsx, nodes.jsx, theme.js (React Flow + dagre)
agent/
  src/          # mindgraph.ts, session.ts, boundary.ts, tools.ts, guard.ts,
                # limiter.ts, agent.ts, main.ts
  eval/         # runner.py + tasks/*.json
  fixture/      # make_fixture.py
  harness/      # harness.py (live refusal runs)
tests/          # fixture + tests for stages A/B/C/emit/ignore/cache/config + phases 1/1.5/2
Testing_libs/   # third-party repos for manual analysis (not part of the tool)
```

`graph.json` is the only contract between the analyzer and the viewer: the viewer
knows nothing about Python and computes nothing itself.

---

## Development

```bash
pytest tests/            # all tests (no network — the LLM is replaced by a fake)
```

Tests are built on a synthetic fixture `tests/fixtures/sample_repo/` (two
independent clusters + a shared utility module + a cycle + a file with a syntax
error).

---

## Current status

| phase | status |
|---|---|
| 0 (hygiene) | done |
| 1 (JSON contract) | done |
| 1.5 (boundaries) | done |
| 2 (agent on pi) | done |
| 2.5 (hardening/eval) | done |
| 3 (verification = tests) | NOT started |

The whole codebase is in English (prompts, tool descriptions, agent messages,
node names). Russian-language strings survive only as data in test fixtures
(invalid JSON, simulated user code) — those are data, not prompts.

## Next steps

1. **Phase 3 — verification = tests.** `run_tests` currently selects tests but
   does not execute them. In phase 3 it really runs: tests of the touched zones +
   contract tests, and the result becomes the node's verdict. Three states, not
   two: `ok`, `failed`, `unverified` (an empty or low-confidence selection must
   not look like success). Green means "did not break, as far as the tests know",
   with an explicit list of what was run and what is not covered. Running tests is
   executing arbitrary user-project code — needs a timeout, a command restriction,
   and a decision on where to take the project environment from.
2. **Deferred on purpose**: RU/EN comparison, the import resolver (src-layout
   repos give an incomplete graph, `complete=false`), the full
   `mindgraph` -> `mindvibe` rename.
3. **Product (after phase 3)**: one-command install, watcher + incremental
   recompute, plan-before-execute, canvas as a live surface. Readiness metric: a
   stranger installs the tool on their repo and goes from install to an accepted
   green change in ninety seconds.

## Run rules (learned the hard way)

- `setsid`, not `nohup` — survives terminal close.
- PID to a file, not `echo` — the order of output after `&` is not guaranteed.
- One configuration at a time until `--label` is confirmed.
- Write results after each task, not at the end (`--resume` for resuming).
- Copy `baseline.json` to `results/keep/` — it is the only valid run, costs money,
  the next failure overwrites it.
- Long runs — in the background with output to a file. To chat, only the summary
  table: raw LLM-cycle logs eat the session context.
