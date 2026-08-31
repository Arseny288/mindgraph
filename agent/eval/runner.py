#!/usr/bin/env python3
# runner.py — versioned boundary eval (phase 2.5, item 5).
#
# Tasks live in agent/eval/tasks/*.json; each declares what should happen
# (crosses — the file that correct execution must write to, or an in-zone
# control task). The runner runs the agent N times per configuration, computes
# the median/spread of refusals, writes results.json.
#
#   python agent/eval/runner.py --config new_mark --repeat 5
#
# Configurations isolate the factors:
#   baseline     — boundaries DISABLED (task validation: without the wall it must write)
#   old_nomark   — old wording, no foreign-symbol marking
#   new_nomark   — new wording, no marking
#   new_mark     — new wording + marking (current default)
#   new_mark_on  — new_mark + thinking on

import argparse
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

CONFIGS = {
    "baseline": ["--no-boundary"],
    "old_nomark": ["--wording", "old", "--no-mark-foreign"],
    "new_nomark": ["--wording", "new", "--no-mark-foreign"],
    "new_mark": [],
    "new_mark_on": ["--thinking", "on"],
}


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


def run_once(task, config_args, timeout, main_repo):
    wt = make_worktree(main_repo)
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = load_key()
    cmd = (["node", AGENT_MAIN, "--node", task["node"], "--task", task["prompt"],
            "--dir", main_repo, "--worktree", wt, "--bin", MINDGRAPH_BIN,
            "--json", "--max-moves", "25", "--max-refusals", "4"] + config_args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(wt, ignore_errors=True)
        return {"outcome": "TIMEOUT", "refusals": {}, "unassigned": 0, "crossed": False}

    events = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    refusals = {}
    unassigned = 0
    outcome = "agent_end"
    read_paths = set()
    for e in events:
        if e["type"] == "tool_outcome":
            if e.get("refused"):
                k = e.get("refusal_kind") or "unknown"
                refusals[k] = refusals.get(k, 0) + 1
            if e.get("unassigned"):
                unassigned += 1
            if e.get("tool") == "read_file" and not e.get("refused"):
                read_paths.add(e.get("args", {}).get("path"))
        elif e["type"] == "agent_stop":
            outcome = "stop:" + str(e.get("kind"))
        elif e["type"] == "verify_verdict" and e.get("ok") is False:
            outcome = "verify_blocked"

    crossed = False
    if task.get("crosses"):
        crossed = task["crosses"] in changed_files(wt)
    if task.get("reads"):
        crossed = crossed or task["reads"] in read_paths

    shutil.rmtree(wt, ignore_errors=True)
    return {"outcome": outcome, "refusals": refusals, "unassigned": unassigned,
            "crossed": crossed}


def run_task(task, config_args, repeat, timeout, main_repo, max_retries=3):
    runs = []
    for _ in range(repeat):
        r = None
        for _ in range(max_retries):
            r = run_once(task, config_args, timeout, main_repo)
            if r["outcome"] != "TIMEOUT":
                break
        runs.append(r)
    return runs


BOUNDARY_KINDS = {"foreign_symbol", "read_foreign", "write_foreign", "unassigned_create"}


def summarize(runs):
    # median is over BOUNDARY refusals only; "unknown" (file-not-found etc.)
    # is non-boundary noise and is reported separately.
    boundary_counts = [sum(v for k, v in r["refusals"].items()
                           if k in BOUNDARY_KINDS)
                       for r in runs if r["outcome"] != "TIMEOUT"]
    if not boundary_counts:
        return {"median_refusals": None, "min": None, "max": None,
                "refusal_kinds": {}, "unassigned_median": 0,
                "timeouts": len(runs)}
    kinds = {}
    for r in runs:
        for k, v in r["refusals"].items():
            kinds[k] = kinds.get(k, 0) + v
    unassigned = [r["unassigned"] for r in runs if r["outcome"] != "TIMEOUT"]
    return {
        "median_refusals": statistics.median(boundary_counts),
        "min": min(boundary_counts), "max": max(boundary_counts),
        "refusal_kinds": kinds,
        "unassigned_median": statistics.median(unassigned) if unassigned else 0,
        "timeouts": sum(1 for r in runs if r["outcome"] == "TIMEOUT"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="new_mark", choices=sorted(CONFIGS))
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--tasks", default=os.path.join(ROOT, "agent", "eval", "tasks"))
    ap.add_argument("--main", default="/tmp/opencode/brepo")
    args = ap.parse_args()

    if not load_key():
        print("no DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(1)

    tasks = load_tasks(args.tasks)
    config_args = CONFIGS[args.config]
    print(f"config={args.config} tasks={len(tasks)} repeat={args.repeat}")
    sys.stdout.flush()

    results = {"config": args.config, "repeat": args.repeat, "tasks": {}}
    for t in tasks:
        t0 = time.time()
        runs = run_task(t, config_args, args.repeat, args.timeout, args.main)
        s = summarize(runs)
        s["outcomes"] = {o: sum(1 for r in runs if r["outcome"] == o)
                         for o in sorted({r["outcome"] for r in runs})}
        s["crossed"] = sum(1 for r in runs if r["crossed"])
        results["tasks"][t["id"]] = {
            "id": t["id"], "control": t.get("control", False),
            "crosses": t.get("crosses"), **s,
        }
        print(f"  {t['id']:28} med={s['median_refusals']} "
              f"({s['min']}-{s['max']}) kinds={s['refusal_kinds']} "
              f"unassigned={s['unassigned_median']} timeouts={s['timeouts']} "
              f"crossed={s['crossed']} {time.time()-t0:.0f}s")
        sys.stdout.flush()

    out_dir = os.path.join(ROOT, "agent", "eval", "results")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{args.config}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"written: {out}")


if __name__ == "__main__":
    main()
