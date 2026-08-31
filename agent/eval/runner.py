#!/usr/bin/env python3
# runner.py — versioned boundary eval (phase 2.5, item 5).
#
# Tasks live in agent/eval/tasks/*.json; each declares what should happen
# (crosses — the file correct execution must write to; reads — a file it must
# read; control — an in-zone task). The runner runs the agent N times per
# configuration in parallel across tasks, computes the median/spread of
# refusals and the outcome split, and writes results to
# agent/eval/results/<label>.json.
#
# Optimizations:
#   --jobs N       parallel tasks (default 4), each in its own worktree;
#   --max-repeat N ceiling on repeats; adaptive early-stop when two consecutive
#                  runs give the same outcome (outcome, crossed, proposed);
#   --smoke        1 repeat, only the tasks marked "smoke": true;
#   --cache-dir    reuse results for the same commit+config+model+prompt;
#   error          non-zero exit, <2s wall-clock, or 0 LLM tool calls.

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_MAIN = os.path.join(ROOT, "agent", "src", "main.ts")
MINDGRAPH_BIN = os.path.join(ROOT, ".venv", "bin", "mindgraph")
DEFAULT_MODEL = "deepseek-v4-flash"

CONFIGS = {
    "baseline": ["--no-boundary"],
    "old_nomark": ["--wording", "old", "--no-mark-foreign"],
    "new_nomark": ["--wording", "new", "--no-mark-foreign"],
    "new_mark": [],
    "new_mark_on": ["--thinking", "on"],
}

BOUNDARY_KINDS = {"foreign_symbol", "read_foreign", "write_foreign", "unassigned_create"}


def load_key():
    env = os.path.join(ROOT, ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY", "")


def load_tasks(dir_):
    tasks = []
    for name in sorted(os.listdir(dir_)):
        if name.endswith(".json"):
            with open(os.path.join(dir_, name), encoding="utf-8") as fh:
                tasks.append(json.load(fh))
    return tasks


def make_worktree(src_root: str) -> str:
    wt = tempfile.mkdtemp(prefix="mg-eval-")
    for name in os.listdir(src_root):
        p = os.path.join(src_root, name)
        if name == ".mindgraph" or name.startswith(".git"):
            continue
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(wt, name))
        else:
            shutil.copy2(p, os.path.join(wt, name))
    subprocess.run(["git", "init", "-q", wt], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-q", "-m", "base"], check=True)
    return wt


def changed_files(wt: str) -> list:
    try:
        out = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return []
    return [line[3:] for line in out.splitlines() if line.strip()]


def git_commit_hash() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        return "none"


def run_once(task, config_args, timeout, main_repo):
    wt = make_worktree(main_repo)
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = load_key()
    cmd = (["node", AGENT_MAIN, "--node", task["node"], "--task", task["prompt"],
            "--dir", main_repo, "--worktree", wt, "--bin", MINDGRAPH_BIN,
            "--json", "--max-moves", "25", "--max-refusals", "4"] + config_args)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(wt, ignore_errors=True)
        return {"outcome": "TIMEOUT", "refusals": {}, "unassigned": 0,
                "crossed": False, "proposed": False, "requested_contract": False,
                "did_write": False, "elapsed": timeout, "tool_calls": 0}
    elapsed = time.time() - t0

    events = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    tool_calls = [e for e in events if e["type"] == "tool_call_start"]

    # error = crash: non-zero exit, sub-2s wall-clock, or zero LLM tool calls.
    if p.returncode != 0 or elapsed < 2.0 or not tool_calls:
        shutil.rmtree(wt, ignore_errors=True)
        return {"outcome": "error", "refusals": {}, "unassigned": 0,
                "crossed": False, "proposed": False, "requested_contract": False,
                "did_write": False, "elapsed": elapsed, "tool_calls": len(tool_calls),
                "stderr": (p.stderr or "")[:200]}

    refusals = {}
    unassigned = 0
    outcome = "agent_end"
    read_paths = set()
    requested_contract = False
    did_write = False
    for e in events:
        if e["type"] == "tool_outcome":
            if e.get("refused"):
                k = e.get("refusal_kind") or "unknown"
                refusals[k] = refusals.get(k, 0) + 1
            if e.get("unassigned"):
                unassigned += 1
            if e.get("tool") == "read_file" and not e.get("refused"):
                read_paths.add(e.get("args", {}).get("path"))
            if e.get("tool") in ("edit_file", "create_file") \
                    and not e.get("refused") and not e.get("unassigned"):
                did_write = True
        elif e["type"] == "tool_call_start" and e["tool"] == "request_contract_change":
            requested_contract = True
        elif e["type"] == "agent_stop":
            outcome = "stop:" + str(e.get("kind"))
        elif e["type"] == "verify_verdict" and e.get("ok") is False:
            outcome = "verify_blocked"

    proposed = requested_contract or did_write
    crossed = False
    if task.get("crosses"):
        crossed = task["crosses"] in changed_files(wt)
    if task.get("reads"):
        crossed = crossed or task["reads"] in read_paths

    shutil.rmtree(wt, ignore_errors=True)
    return {"outcome": outcome, "refusals": refusals, "unassigned": unassigned,
            "crossed": crossed, "proposed": proposed,
            "requested_contract": requested_contract, "did_write": did_write,
            "elapsed": elapsed, "tool_calls": len(tool_calls)}


def run_task(task, config_args, max_repeat, timeout, main_repo, max_retries=3):
    """Adaptive repeats: stop when two consecutive runs give the same outcome."""
    runs = []
    prev_sig = None
    for _ in range(max_repeat):
        r = None
        for _ in range(max_retries):
            r = run_once(task, config_args, timeout, main_repo)
            if r["outcome"] != "TIMEOUT":
                break
        runs.append(r)
        sig = (r["outcome"], r["crossed"], r["proposed"])
        if prev_sig is not None and sig == prev_sig:
            break
        prev_sig = sig
    return runs


def summarize(runs):
    non_timedout = [r for r in runs if r["outcome"] != "TIMEOUT"]
    boundary_counts = [sum(v for k, v in r["refusals"].items()
                           if k in BOUNDARY_KINDS) for r in non_timedout]
    kinds = {}
    for r in runs:
        for k, v in r["refusals"].items():
            kinds[k] = kinds.get(k, 0) + v
    unassigned = [r["unassigned"] for r in non_timedout]
    completed = [r for r in runs if r["outcome"] == "agent_end"]
    return {
        "repeats": len(runs),
        "median_refusals": statistics.median(boundary_counts) if boundary_counts else None,
        "min": min(boundary_counts) if boundary_counts else None,
        "max": max(boundary_counts) if boundary_counts else None,
        "refusal_kinds": kinds,
        "unassigned_median": statistics.median(unassigned) if unassigned else 0,
        "proposed": sum(1 for r in completed if r["proposed"]),
        "gave_up": sum(1 for r in completed if not r["proposed"]),
        "requested_contract": sum(1 for r in runs if r["requested_contract"]),
        "errors": sum(1 for r in runs if r["outcome"] == "error"),
        "timeouts": sum(1 for r in runs if r["outcome"] == "TIMEOUT"),
    }


def cache_key(task, config_args, model, commit):
    parts = [commit, model, task["node"], task["prompt"],
             task.get("crosses") or "", task.get("reads") or "",
             json.dumps(config_args, sort_keys=True)]
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def run_task_cached(task, config_args, max_repeat, timeout, main_repo, cache_dir):
    key = cache_key(task, config_args, DEFAULT_MODEL, git_commit_hash())
    if cache_dir:
        cached = os.path.join(cache_dir, key + ".json")
        if os.path.exists(cached):
            with open(cached, encoding="utf-8") as fh:
                return json.load(fh)
    runs = run_task(task, config_args, max_repeat, timeout, main_repo)
    summary = summarize(runs)
    summary["crossed"] = sum(1 for r in runs if r["crossed"])
    summary["outcomes"] = {o: sum(1 for r in runs if r["outcome"] == o)
                           for o in sorted({r["outcome"] for r in runs})}
    summary["cached"] = False
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cached, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
    return summary


def main():
    global MINDGRAPH_BIN, AGENT_MAIN

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="new_mark", choices=sorted(CONFIGS))
    ap.add_argument("--max-repeat", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--tasks", default=os.path.join(ROOT, "agent", "eval", "tasks"))
    ap.add_argument("--main", default="/tmp/opencode/brepo")
    ap.add_argument("--bin", default=MINDGRAPH_BIN)
    ap.add_argument("--agent-main", default=AGENT_MAIN)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--label", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    MINDGRAPH_BIN = args.bin
    AGENT_MAIN = args.agent_main
    label = args.label or args.config
    max_repeat = 1 if args.smoke else args.max_repeat
    cache_dir = args.cache_dir or os.path.join(ROOT, "agent", "eval", "results", ".cache")

    if not load_key():
        print("no DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(1)

    tasks = load_tasks(args.tasks)
    if args.smoke:
        tasks = [t for t in tasks if t.get("smoke")]
    config_args = CONFIGS[args.config]

    print(f"label={label} config={args.config} tasks={len(tasks)} "
          f"max-repeat={max_repeat} jobs={args.jobs}")
    sys.stdout.flush()

    results = {"label": label, "config": args.config, "max_repeat": max_repeat,
               "commit": git_commit_hash(), "tasks": {}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(run_task_cached, t, config_args, max_repeat,
                          args.timeout, args.main, cache_dir): t for t in tasks}
        for f in concurrent.futures.as_completed(futs):
            t = futs[f]
            try:
                s = f.result()
            except Exception as e:  # noqa: BLE001
                s = {"error": str(e)}
            s["id"] = t["id"]
            s["control"] = t.get("control", False)
            s["crosses"] = t.get("crosses")
            results["tasks"][t["id"]] = s
            cached = " (cached)" if s.get("cached") is False else ""
            print(f"  {t['id']:28} med={s.get('median_refusals')} "
                  f"({s.get('min')}-{s.get('max')}) proposed={s.get('proposed')} "
                  f"gave_up={s.get('gave_up')} req={s.get('requested_contract')} "
                  f"err={s.get('errors')} tmo={s.get('timeouts')} "
                  f"crossed={s.get('crossed')}{cached}")
            sys.stdout.flush()

    out_dir = os.path.join(ROOT, "agent", "eval", "results")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{label}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"written: {out}")


if __name__ == "__main__":
    main()
