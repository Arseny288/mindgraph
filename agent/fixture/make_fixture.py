# make_fixture.py — детерминированная фикстура для живых прогонов отказа.
#
# Строит маленький репозиторий с намеренно СМЕШАННЫМ файлом (shared/mixed.py:
# auth_part -> узел «Аутентификация», billing_part -> узел «Биллинг») и
# КОНТРАКТОМ (shared/contract.py::hash_value, shared, используется обоими).
# Запускается так: python make_fixture.py <root>  (пишет .mindgraph/graph.json).
#
# Логика: extract_repo даёт символы/рёбра с ТОЧНЫМИ строками; назначение узлов
# задаём вручную; owners/shared считаем через compute_owners_by_node.

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mindgraph.extract import extract_repo
from mindgraph.shared import compute_owners_by_node

FILES = {
    "auth/login.py": (
        "from shared.mixed import auth_part\n"
        "from shared.contract import hash_value\n\n\n"
        "def login(username):\n"
        "    return hash_value(username)\n"
    ),
    "billing/invoice.py": (
        "from shared.mixed import billing_part\n"
        "from shared.contract import hash_value\n\n\n"
        "def make_invoice(amount):\n"
        "    return hash_value(billing_part(amount))\n"
    ),
    "shared/mixed.py": (
        "def auth_part(x):\n"
        "    return x\n\n\n"
        "def billing_part(y):\n"
        "    return y * 2\n"
    ),
    "shared/contract.py": (
        "def hash_value(v):\n"
        "    return str(len(v))\n"
    ),
}

NODE_OF = {
    "auth.login.<module>": "Аутентификация",
    "auth.login.login": "Аутентификация",
    "shared.mixed.<module>": "Аутентификация",
    "shared.mixed.auth_part": "Аутентификация",
    "shared.contract.<module>": "Аутентификация",
    "shared.contract.hash_value": "Аутентификация",
    "billing.invoice.<module>": "Биллинг",
    "billing.invoice.make_invoice": "Биллинг",
    "shared.mixed.billing_part": "Биллинг",
}

SUMMARIES = {
    "Аутентификация": "Вход и токены.",
    "Биллинг": "Счета и суммы.",
}


def _slug(name: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", name.strip().lower()).strip("_")
    return s or "node"


def build(root: str) -> dict:
    for rel, content in FILES.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)

    ext = extract_repo(root)
    symbols = ext["symbols"]
    edges = ext["symbol_edges"]

    node_of = {s["id"]: NODE_OF.get(s["id"], "Прочее") for s in symbols}
    direct = compute_owners_by_node(symbols, edges, node_of)

    out_symbols = []
    for s in symbols:
        o = direct.get(s["id"], {"node": node_of[s["id"]], "owners": [], "shared": False})
        e = dict(s)
        e.update({"node": o["node"], "shared": o["shared"], "owners": o["owners"]})
        out_symbols.append(e)

    measured = [s for s in symbols if not s.get("synthetic")]
    shared_syms = [s for s in measured if direct[s["id"]]["shared"]]

    by_node = {}
    for s in symbols:
        by_node.setdefault(node_of[s["id"]], []).append(s)

    nodes = []
    for name in sorted(by_node):
        members = by_node[name]
        nodes.append({
            "id": "n_" + _slug(name), "name": name,
            "summary": SUMMARIES.get(name, ""),
            "symbols": sorted(m["id"] for m in members),
            "files": sorted({m["file"] for m in members}),
            "loc": sum(m["loc"] for m in members),
            "shared_count": sum(1 for m in members
                                if not m.get("synthetic") and direct[m["id"]]["shared"]),
        })
    node_id = {n["name"]: n["id"] for n in nodes}

    agg = {}
    for e in edges:
        a = node_id.get(node_of.get(e["from"]))
        b = node_id.get(node_of.get(e["to"]))
        if a is None or b is None or a == b:
            continue
        agg[(a, b)] = agg.get((a, b), 0) + e["weight"]
    node_edges = [{"from": a, "to": b, "weight": w, "weak": w == 1}
                  for (a, b), w in sorted(agg.items())]

    graph = {
        "version": 2,
        "grouping": "llm",
        "repo": {
            "root": os.path.abspath(root),
            "name": os.path.basename(os.path.abspath(root)),
            "repo_hash": "fixture",
            "config_fingerprint": "fixture",
            "files": len({s["file"] for s in symbols}),
            "loc": sum(s["loc"] for s in symbols),
            "generated_at": "2026-01-01T00:00:00Z",
        },
        "symbols": sorted(out_symbols, key=lambda s: s["id"]),
        "symbol_edges": edges,
        "nodes": nodes,
        "node_edges": node_edges,
        "domains": [{"id": "d_all", "name": "Проект"}],
        "domain_edges": [],
        "stats": {
            "symbols": len(measured),
            "unassigned": 0,
            "shared_symbols": len(shared_syms),
            "shared_ratio": (len(shared_syms) / len(measured)) if measured else 0.0,
            "external": ext["external"],
        },
        "warnings": ext["warnings"],
    }
    for n in nodes:
        n["parent"] = "d_all"

    out = os.path.join(root, ".mindgraph")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "graph.json"), "w", encoding="utf-8") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=2)
    return graph


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    g = build(root)
    print(f"graph.json записан: {len(g['symbols'])} символов, "
          f"{len(g['nodes'])} узлов, shared {g['stats']['shared_symbols']}")
