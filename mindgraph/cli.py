"""cli.py — argparse, config and orchestration (phase 1: JSON contract).

Two modes:

* legacy human:  ``mindgraph <path> [--serve|--stats|--no-llm|--regroup|--force]``
* machine:      subcommands ``scan/graph/node/impact/boundary/tests/verify``.

Machine contract (phase 1):
  * the result — only to stdout, only valid JSON;
  * all logs/warnings — to stderr;
  * every JSON response contains ``"schema_version"``;
  * exit codes: 0 success, 1 runtime error, 2 bad arguments,
    3 boundary violation (verify).

Settings priority: CLI > MINDGRAPH_* env > mindgraph.toml > ~/.config/... >
defaults (see mindgraph/config.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys

from . import emit, llmcache, model, verify as verify_mod, testselect, impact as impact_mod
from . import __version__
from .config import resolve_config
from .group import PROVIDERS, DEEPSEEK_MODEL, ANTHROPIC_MODEL

log = logging.getLogger("mindgraph")

# The root of the mindgraph TOOL itself (not the analyzed repo) — the provider
# keys are the tool's own credentials, .env sits next to pyproject.toml/mindgraph/.
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROVIDER_MODELS = {"deepseek": DEEPSEEK_MODEL, "anthropic": ANTHROPIC_MODEL}

# Exit codes (phase 1).
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_BOUNDARY = 3

SCHEMA_VERSION = "1"


def _load_dotenv(path: str) -> None:
    """A minimal .env loader (KEY=VALUE line by line), no external dependencies.

    Does not overwrite already-set environment variables (the shell wins).
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _print_stats(graph: dict) -> None:
    repo, stats = graph["repo"], graph["stats"]
    total = stats["symbols"]

    print(f"files {repo['files']} · symbols {total} · lines {repo['loc']}")

    unassigned_pct = (stats["unassigned"] / total * 100) if total else 0.0
    print(f"nodes {len(graph['nodes'])} · unassigned {stats['unassigned']} "
          f"({unassigned_pct:.1f}%)")

    ratio_pct = stats["shared_ratio"] * 100
    print(f"shared symbols {stats['shared_symbols']} ({ratio_pct:.1f}%)   "
          f"<- key metric")

    top = sorted((s for s in graph["symbols"] if s["shared"]),
                 key=lambda s: -len(s["owners"]))[:3]
    if top:
        parts = ", ".join(f"{s['id']} ({len(s['owners'])} nodes)" for s in top)
        print(f"top shared: {parts}")

    if ratio_pct <= 15:
        hint = "nodes are isolable, write boundaries are meaningful"
    elif ratio_pct > 30:
        hint = "nodes leak into each other, the node definition must change"
    else:
        hint = "borderline — inspect before cutting boundaries"
    print(f"  ({hint})")

    if graph["warnings"]:
        print(f"\nwarnings ({len(graph['warnings'])}):", file=sys.stderr)
        for w in graph["warnings"][:10]:
            print(f"  ! {w}", file=sys.stderr)


def _out_json(obj: dict) -> None:
    # sort_keys + ensure_ascii=False: byte-for-byte stable output between
    # runs/processes — important for the provider's prompt caching (phase 2.2).
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _load_graph(dir_: str) -> dict:
    try:
        return model.load_graph(dir_)
    except (OSError, json.JSONDecodeError) as e:
        print(f"mindgraph: could not read graph.json for {dir_!r} ({e})",
              file=sys.stderr)
        print(f"  run first: mindgraph scan {dir_}", file=sys.stderr)
        sys.exit(EXIT_RUNTIME)


def _resolve_config(root: str, args) -> "object":
    config = resolve_config(root, explicit_path=getattr(args, "config", None))
    if getattr(args, "provider", None) is not None:
        config.provider = args.provider
    if getattr(args, "model", None) is not None:
        config.model = args.model
    if getattr(args, "port", None) is not None:
        config.port = args.port
    if getattr(args, "no_llm", False):
        config.no_llm = True
    for extra in (getattr(args, "ignore", None) or []):
        config.ignore = list(dict.fromkeys(config.ignore + [extra]))
    return config


# --- Subcommands ------------------------------------------------------------

def _cmd_scan(args) -> int:
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"mindgraph: {args.dir!r} is not a directory", file=sys.stderr)
        return EXIT_RUNTIME

    config = _resolve_config(root, args)
    cache_base = llmcache.resolve_base(config.cache_dir)

    graph, from_cache = emit.load_or_build(
        root, provider_name=config.provider, no_llm=config.no_llm,
        force=args.force, regroup=args.regroup, extra_ignore=config.ignore,
        model=config.model, cache_base=cache_base,
        entry_points=config.entry_points,
        node_min=config.node_min, node_max=config.node_max)

    path = emit.graph_path(root)
    if from_cache:
        print(f"graph.json is up to date ({path}), reusing — --force to recompute",
              file=sys.stderr)
    else:
        print(f"written: {path}", file=sys.stderr)

    if args.json:
        _out_json({
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "graph_path": path,
            "from_cache": from_cache,
            "stats": graph["stats"],
            "warnings": graph["warnings"],
        })
        return EXIT_OK

    if args.stats:
        _print_stats(graph)
    if args.serve:
        from . import serve
        serve.run(root, port=config.port, open_browser=config.open_browser)
    return EXIT_OK


def _cmd_graph(args) -> int:
    graph = _load_graph(args.dir)
    if args.json:
        _out_json({"schema_version": SCHEMA_VERSION, **graph})
        return EXIT_OK
    print(f"repo {graph['repo']['name']}: {len(graph['nodes'])} nodes, "
          f"{graph['stats']['symbols']} symbols")
    for n in graph["nodes"]:
        print(f"  {n['id']}\t{n['name']}  ({len(n['symbols'])} symbols)")
    return EXIT_OK


def _cmd_node(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.id)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME
    view = model.node_view(graph, node)

    if args.json:
        _out_json({
            "schema_version": SCHEMA_VERSION,
            "node": {
                "id": node["id"], "name": node["name"],
                "summary": node.get("summary", ""),
                "files": node.get("files", []),
                "symbols": node.get("symbols", []),
            },
            "owned": {"files": view["owned_files"],
                      "symbols": view["owned_symbols"]},
            "contracts": view["contracts"],
            "foreign": {"files": view["foreign_files"]},
            "mixed": view["mixed"],
        })
        return EXIT_OK

    print(f"node {node['name']} ({node['id']})")
    print(f"  files: {len(view['owned_files'])}, symbols: {len(view['owned_symbols'])}, "
          f"contracts: {len(view['contracts'])}")
    return EXIT_OK


def _cmd_impact(args) -> int:
    graph = _load_graph(args.dir)
    result = impact_mod.impact(graph, args.query, depth=args.depth)
    result["schema_version"] = SCHEMA_VERSION
    if args.json:
        _out_json(result)
        return EXIT_OK
    print(f"impact {args.query}: {len(result['symbols'])} symbols, "
          f"{len(result['files'])} files")
    for f in result["files"]:
        print(f"  {f}")
    return EXIT_OK


def _cmd_boundary(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.node)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME
    bnd = model.boundary(graph, node)
    if args.json:
        _out_json(bnd)
        return EXIT_OK
    print(f"boundaries of node {bnd['node_name']}:")
    print(f"  allow_write: {bnd['allow_write']}")
    print(f"  deny_write:  {bnd['deny_write']}")
    print(f"  contracts:   {len(bnd['contracts'])}")
    return EXIT_OK


def _cmd_tests(args) -> int:
    graph = _load_graph(args.dir)
    if args.for_node:
        try:
            node = model.node_by_ref(graph, args.for_node)
        except KeyError as e:
            print(f"mindgraph: {e}", file=sys.stderr)
            return EXIT_RUNTIME
        result = testselect.node_tests(graph, node, f"for-node {args.for_node}")
    else:
        diff_text = _read_diff(args.for_diff)
        changed = {f["path"] for f in verify_mod._parse_diff(diff_text)}
        result = testselect.diff_tests(graph, changed, f"for-diff {args.for_diff}")

    if args.json:
        _out_json(result)
        return EXIT_OK
    print(f"tests ({result['query']}): {result['status']} — {len(result['tests'])}")
    for t in result["tests"]:
        print(f"  {t}")
    if result["status"] != model.STATUS_OK:
        print(f"  (unverified: {result.get('reason', '?')})", file=sys.stderr)
    return EXIT_OK


def _cmd_verify(args) -> int:
    graph = _load_graph(args.dir)
    try:
        node = model.node_by_ref(graph, args.node)
    except KeyError as e:
        print(f"mindgraph: {e}", file=sys.stderr)
        return EXIT_RUNTIME

    diff_text = _read_diff(args.diff)
    worktree = getattr(args, "worktree", None) or args.dir
    result = verify_mod.verify(graph, node, diff_text,
                               original_dir=os.path.abspath(worktree))
    _out_json(result)
    return EXIT_BOUNDARY if not result["ok"] else EXIT_OK


def _read_diff(diff: str) -> str:
    if diff == "-":
        return sys.stdin.read()
    try:
        with open(diff, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        print(f"mindgraph: could not read the diff {diff!r} ({e})",
              file=sys.stderr)
        sys.exit(EXIT_RUNTIME)


def _agent_main() -> str:
    return os.path.join(_TOOL_ROOT, "agent", "src", "main.ts")


def _cmd_agent(args) -> int:
    """Launch the TS agent (phase 2.5, item 5). ``node agent/src/main.ts`` — internal."""
    agent_main = _agent_main()
    if not os.path.exists(agent_main):
        print(f"mindgraph: agent not found ({agent_main}); "
              f"need an agent/ directory at the tool root", file=sys.stderr)
        return EXIT_RUNTIME

    node_bin = shutil.which("node")
    if node_bin is None:
        print("mindgraph: node not found (needed for the agent)", file=sys.stderr)
        return EXIT_RUNTIME

    # The binary the agent will use to call mindgraph inside itself.
    mindgraph_bin = shutil.which("mindgraph") or sys.argv[0]

    cmd = [node_bin, agent_main,
           "--node", args.node, "--task", args.task,
           "--dir", os.path.abspath(args.dir),
           "--worktree", os.path.abspath(args.worktree or args.dir),
           "--bin", mindgraph_bin]
    if args.json:
        cmd.append("--json")
    if args.no_boundary:
        cmd.append("--no-boundary")
    if args.thinking:
        cmd += ["--thinking", args.thinking]
    if args.model:
        cmd += ["--model", args.model]

    return subprocess.call(cmd)


# --- Parser ----------------------------------------------------------------

def _add_llm_flags(p, with_regroup=True):
    p.add_argument("--provider", default=None, choices=sorted(PROVIDERS))
    p.add_argument("--model", default=None)
    p.add_argument("--ignore", action="append", default=[], metavar="DIR")
    p.add_argument("--config", default=None, metavar="FILE")
    p.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mindgraph",
        description="A semantic overlay on top of a Python repository.")
    ap.add_argument("--verbose", action="store_true",
                    help="stage and progress logs to stderr (stdout stays clean)")
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--version", action="version", version=f"mindgraph {__version__}")

    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("scan", help="build/update graph.json")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-llm", action="store_true")
    g.add_argument("--regroup", action="store_true")
    _add_llm_flags(p)
    p.add_argument("--port", type=int, default=None)

    p = sub.add_parser("graph", help="the whole graph (nodes, edges, metadata)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("node", help="one node: files, symbols, zone, contracts")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("impact", help="the transitive set of what is touched")
    p.add_argument("query", help="symbol_id or a file path")
    p.add_argument("--depth", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("boundary", help="a node's zone boundaries")
    p.add_argument("node")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("tests", help="which tests to run")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--for-node", dest="for_node")
    g.add_argument("--for-diff", dest="for_diff")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("verify", help="check a diff against a node's boundary")
    p.add_argument("--node", required=True)
    p.add_argument("--diff", required=True, help="path to the diff or '-' (stdin)")
    p.add_argument("--dir", default=".")
    p.add_argument("--worktree", default=None,
                   help="the worktree root with the diff's new side (default --dir)")

    p = sub.add_parser("agent", help="headless agent with node boundaries")
    p.add_argument("--node", required=True, help="node id/name")
    p.add_argument("--task", required=True, help="task")
    p.add_argument("--dir", default=".", help="the main repository (graph.json)")
    p.add_argument("--worktree", default=None, help="git worktree of the session")
    p.add_argument("--model", default=None)
    p.add_argument("--thinking", default=None, choices=["off", "on"])
    p.add_argument("--no-boundary", action="store_true",
                   help="disable boundary checks (task validation)")
    p.add_argument("--json", action="store_true")

    return ap


def _list_providers() -> None:
    for name in sorted(PROVIDERS):
        print(f"{name:<10} default model: {_PROVIDER_MODELS[name]}")


_DISPATCH = {
    "scan": _cmd_scan,
    "graph": _cmd_graph,
    "node": _cmd_node,
    "impact": _cmd_impact,
    "boundary": _cmd_boundary,
    "tests": _cmd_tests,
    "verify": _cmd_verify,
    "agent": _cmd_agent,
}


def main(argv=None) -> int:
    _load_dotenv(os.path.join(_TOOL_ROOT, ".env"))
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="mindgraph: %(message)s", stream=sys.stderr)

    if getattr(args, "list_providers", False):
        _list_providers()
        return EXIT_OK

    if args.command is None:
        print("mindgraph: specify a subcommand (see --help)", file=sys.stderr)
        return EXIT_USAGE

    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
