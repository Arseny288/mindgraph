// main.jsx — the studio surface: a graph as the primary object, a permanent
// conversation rail on the right (turns + composer), and a floating node
// popover on the left. Three layers of canvas (domains/features/symbols) with
// dagre layout and a skeleton edge graph. See STUDIO.md §1–§12; index.html for
// the CSS. No JSX (see nodes.jsx) — React.createElement directly.

import React, { useEffect, useMemo, useState, useCallback, useRef, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import ReactFlow, {
  applyNodeChanges, Background, MarkerType,
} from "reactflow";
import dagre from "dagre";
import { marked } from "marked";
import hljs from "highlight.js";

import { nodeTypes } from "./nodes.jsx";
import { colorForName, hexToRgba } from "./theme.js";

const h = React.createElement;

marked.setOptions({ gfm: true, breaks: false });

const DOMAIN_W = 320, FEATURE_W = 280, CARD_W = 240, CARD_H = 64;
const SYMBOL_W = 240, SYMBOL_H = 72;
const LAYER_DOMAIN = 0, LAYER_FEATURE = 1, LAYER_SYMBOL = 2;
const FIT_OPTS = { padding: 0.2, minZoom: 0.1, maxZoom: 1.2, duration: 200 };
const CASCADE_MS = 40;
const RAIL_MIN = 380, RAIL_MAX = 560;

function buildSkeleton(itemIds, edges, sizeOf) {
  const pairKey = (a, b) => (a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`);
  const buckets = new Map();
  edges.forEach((e) => {
    const k = pairKey(e.from, e.to);
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(e);
  });
  const merged = [];
  buckets.forEach((list) => {
    if (list.length === 1) { merged.push({ from: list[0].from, to: list[0].to, weight: list[0].weight }); return; }
    const total = list.reduce((s, e) => s + e.weight, 0);
    const maxW = Math.max(...list.map((e) => e.weight));
    const tied = list.filter((e) => e.weight === maxW).sort((a, b) => sizeOf(b.from) - sizeOf(a.from));
    merged.push({ from: tied[0].from, to: tied[0].to, weight: total });
  });
  const bestIncoming = new Map();
  merged.forEach((e) => {
    const cur = bestIncoming.get(e.to);
    if (!cur || e.weight > cur.weight || (e.weight === cur.weight && sizeOf(e.from) > sizeOf(cur.from))) {
      bestIncoming.set(e.to, e);
    }
  });
  const skeleton = [...bestIncoming.values()];
  const skeletonKeys = new Set(skeleton.map((e) => `${e.from}->${e.to}`));
  const hidden = merged.filter((e) => !skeletonKeys.has(`${e.from}->${e.to}`));
  const hiddenByNode = new Map(itemIds.map((id) => [id, []]));
  hidden.forEach((e) => { hiddenByNode.get(e.from)?.push(e); hiddenByNode.get(e.to)?.push(e); });
  const touched = new Set();
  merged.forEach((e) => { touched.add(e.from); touched.add(e.to); });
  const isolated = new Set(itemIds.filter((id) => !touched.has(id)));
  return { skeleton, hidden, hiddenByNode, isolated };
}

function layoutPositions(items, skeletonEdges, isolatedIds, w, itemH, saved) {
  const connected = items.filter((it) => !isolatedIds.has(it.id));
  const isolated = items.filter((it) => isolatedIds.has(it.id));
  const dagrePos = {};
  let bottomY = 0;
  if (connected.length) {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "TB", nodesep: 80, ranksep: 120, marginx: 40, marginy: 40 });
    g.setDefaultEdgeLabel(() => ({}));
    connected.forEach((it) => g.setNode(it.id, { width: w, height: itemH }));
    skeletonEdges.forEach((e) => g.setEdge(e.from, e.to));
    dagre.layout(g);
    connected.forEach((it) => {
      const p = g.node(it.id);
      dagrePos[it.id] = { x: p.x - w / 2, y: p.y - itemH / 2 };
      bottomY = Math.max(bottomY, dagrePos[it.id].y + itemH);
    });
  }
  const rowY = connected.length ? bottomY + 120 : 0;
  isolated.forEach((it, i) => { dagrePos[it.id] = { x: 40 + i * (w + 80), y: rowY }; });
  const positions = {};
  items.forEach((it) => { positions[it.id] = (saved && saved[it.id]) || dagrePos[it.id]; });
  return positions;
}

function computeDepths(itemIds, skeletonEdges, isolatedIds) {
  const parent = new Map();
  skeletonEdges.forEach((e) => parent.set(e.to, e.from));
  const depth = new Map();
  const getDepth = (id) => {
    if (depth.has(id)) return depth.get(id);
    depth.set(id, 0);
    const p = parent.get(id);
    const d = p ? getDepth(p) + 1 : 0;
    depth.set(id, d);
    return d;
  };
  itemIds.forEach(getDepth);
  let maxDepth = 0;
  itemIds.forEach((id) => { if (!isolatedIds.has(id)) maxDepth = Math.max(maxDepth, depth.get(id)); });
  const out = {};
  itemIds.forEach((id) => { out[id] = isolatedIds.has(id) ? maxDepth + 1 : depth.get(id); });
  return out;
}

function en(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

// Resolve a CSS token to a concrete color (SVG paints cannot take var() in
// presentation attributes). Re-evaluated on render, so a theme change repaints.
function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch { return fallback; }
}

// --- colored entities in agent prose (interface spec §6.4) -------------------
// Agent markdown uses an inline entity syntax:
//   @[Node]    node name  — hue of the node's domain, clickable
//   #[symbol]  symbol     — teal mono, no background
//   +46 / −118            — add/del mono, ok/fail
//   !ok[7 tests] / !fail[2 failing] — verdict chips
//   ~[4m 28s]             — metric, ink-2 mono
// Caps: ≤6 colored spans per paragraph; ≤3 distinct hues per paragraph;
// beyond the caps the text renders plain. Code blocks and inline code are
// never touched.
const HUE_SPAN = ["t-h1", "t-h2", "t-h3", "t-h4", "t-h5", "t-h6"];
function hueClassOf(name) {
  let n = 0;
  for (let i = 0; i < name.length; i++) n = (n * 31 + name.charCodeAt(i)) >>> 0;
  return HUE_SPAN[n % HUE_SPAN.length];
}
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function transformParagraph(para) {
  // para is one paragraph of agent text (no code fences — those were parked).
  let out = "";
  let spanCount = 0;
  const hues = new Set();
  const useSpan = (cls) => {
    const hue = cls.startsWith("t-h");
    if (spanCount >= 6) return false;
    if (hue) { hues.add(cls); if (hues.size > 3) return false; }
    spanCount += 1;
    return true;
  };

  const pushTag = (pre, cls, inner) => {
    const body = `<span class="${cls}">${esc(inner)}</span>`;
    if (useSpan(cls)) out += pre + body;
    else out += pre + esc(inner);
  };

  // walk the paragraph with one master regex pass
  const rx = /(@\[([^\]]+)\]|#\[([^\]]+)\]|!ok\[([^\]]*)\]|!fail\[([^\]]*)\]|~\[([^\]]*)\]|(^|[^A-Za-z0-9])([+\-\u2212])(\d+))/g;
  let last = 0, m;
  while ((m = rx.exec(para)) !== null) {
    out += para.slice(last, m.index);
    if (m[1] && m[1][0] === "@") {
      pushTag("", hueClassOf(m[2]) + " t-node", m[2]);
    } else if (m[1] && m[1][0] === "#") {
      pushTag("", "t-symbol", m[3]);
    } else if (m[1] && m[1][0] === "!") {
      if (m[1].startsWith("!ok")) pushTag("", "t-ok", `✓ ${m[4]}`);
      else pushTag("", "t-fail", `✕ ${m[5]}`);
    } else if (m[1] && m[1][0] === "~") {
      pushTag("", "t-metric", m[6]);
    } else {
      // +/- numbers — keep preceding char
      const sign = m[9];
      const num = sign === "\u2212" || sign === "-" ? `−${m[10]}` : `+${m[10]}`;
      const cls = m[9] === "\u2212" || m[9] === "-" ? "t-del" : "t-add";
      pushTag(m[8], cls, num);
    }
    last = m.index + m[0].length;
  }
  out += para.slice(last);
  return out;
}

function markEntities(text) {
  if (!text) return { text, nodes: [] };
  const codes = [];
  const nodes = [];
  let src = text.replace(/```[\s\S]*?```/g, (c) => {
    codes.push(c); return `\u0000${codes.length - 1}\u0000`;
  }).replace(/(`[^`\n]*`)/g, (c) => {
    codes.push(c); return `\u0000${codes.length - 1}\u0000`;
  });
  const paragraphs = src.split(/\n{2,}/);
  src = paragraphs.map((p) => transformParagraph(p)).join("\n\n");
  src = src.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
  // collect node tags for click-through (names may repeat)
  (text.match(/@\[([^\]]+)\]/g) || []).forEach((t) => {
    const nm = t.slice(2, -1);
    if (nm && nodes.indexOf(nm) === -1) nodes.push(nm);
  });
  return { text: src, nodes };
}

function parseSSE(text) {
  const out = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) {
      const p = line.slice(6).trim();
      if (!p) continue;
      try { out.push(JSON.parse(p)); } catch { /* skip */ }
    }
  }
  return out;
}

function ownerNodeOf(path, graph) {
  if (!graph || !path) return null;
  return graph.nodes.find((n) => n.files.includes(path)) || null;
}

function refusedOwnerNode(e, graph) {
  const m = (e.refusal_text || "").match(/node "([^"]+)"/);
  if (m && graph) { const n = graph.nodes.find((x) => x.name === m[1]); if (n) return n; }
  return ownerNodeOf(e.args?.path, graph);
}

function groupDiffByNode(changed, graph) {
  const groups = new Map();
  (changed || []).forEach((p) => {
    const n = ownerNodeOf(p, graph);
    const key = n ? n.name : "Other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  return [...groups.entries()];
}

function eventNodeLabel(e, graph, activeName) {
  if (e.type === "node_opened") return e.node_name || "session";
  if (e.type === "session.started" || e.type === "session.finished") return "session";
  if (e.type === "plan.ready") return "plan";
  if (e.type === "tool_outcome" && e.refused) {
    const n = refusedOwnerNode(e, graph);
    return n ? n.name : (activeName || "session");
  }
  return activeName || "session";
}

function relTime(ts) {
  if (!ts) return "";
  const s = Math.round((Date.now() / 1000) - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function relAgoISO(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return relTime(t / 1000);
}

// --- assistant message internals (STUDIO.md §5/§8, rail rebuild) ------------

const TOOL_VERB = {
  read_file: "read", edit_file: "edit", create_file: "create", run_tests: "tests",
  open_node: "open", list_nodes: "list", request_contract_change: "contract",
  submit_plan: "plan", impact: "impact", verify: "verify",
};

function toolVerb(name) {
  return TOOL_VERB[name] || (name || "").replace(/_/g, " ");
}

function ownerNameOfRefusal(e, graph) {
  const m = (e.refusal_text || "").match(/node "([^"]+)"/);
  if (m && graph) {
    const n = graph.nodes.find((x) => x.name === m[1]);
    if (n) return n.name;
  }
  const owner = ownerNodeOf(e.args?.path, graph);
  return owner ? owner.name : null;
}

function targetOf(e) {
  return e.args?.path || e.args?.symbol || e.args?.tool || null;
}

/**
 * Split a session's raw event log into assistant "steps" (STUDIO.md §8: the
 * rail reads as one assistant document per phase, not a pile of raw events).
 *
 * Each ``session.started`` begins a new step (a plan pass, then an execution
 * pass). Within a step the deltas are merged into a chronological list of
 * parts: ``{kind:"text", s}`` runs of prose, ``{kind:"trace", ...}`` tool
 * activity in flow, and a single aggregated ``thinking`` block on top.
 */
function buildSteps(events) {
  const steps = [];
  let cur = null;

  const pushText = (delta) => {
    if (!cur) return;
    const last = cur.parts[cur.parts.length - 1];
    if (last && last.kind === "text") last.s += delta;
    else cur.parts.push({ kind: "text", s: delta });
  };

  events.forEach((e) => {
    if (e.type === "session.started") {
      cur = { parts: [], thinking: null, plan: null };
      steps.push(cur);
      return;
    }
    if (!cur) { cur = { parts: [], thinking: null, plan: null }; steps.push(cur); }
    switch (e.type) {
      case "assistant_text_delta":
        pushText(e.delta || "");
        break;
      case "assistant_thinking_start":
        if (!cur.thinking) cur.thinking = { s: "", t0: e.ts || null, t1: null };
        break;
      case "assistant_thinking_delta":
        if (cur.thinking) cur.thinking.s += e.delta || "";
        break;
      case "assistant_thinking_end":
        if (cur.thinking) cur.thinking.t1 = e.ts || null;
        break;
      case "plan.ready":
        cur.plan = { nodes: e.nodes || [], files: e.files || [], contracts: e.contracts || [],
                     tests: e.tests || [], summary: e.summary || "" };
        cur.parts.push({ kind: "trace", tool: "plan", verb: "plan", denied: false,
                         note: "plan submitted", path: null });
        break;
      case "tool_call_start":
        cur.parts.push({ kind: "trace", tool: e.tool, verb: toolVerb(e.tool),
                         denied: false, path: null, open: true });
        break;
      case "tool_outcome": {
        const openTrace = cur.parts.length && cur.parts[cur.parts.length - 1].kind === "trace"
          && cur.parts[cur.parts.length - 1].tool === e.tool && cur.parts[cur.parts.length - 1].open
          ? cur.parts.pop() : { tool: e.tool };
        cur.parts.push({ kind: "trace", tool: e.tool, verb: toolVerb(e.tool),
                         denied: !!e.refused, unassigned: !!e.unassigned,
                         refusal_text: e.refusal_text || null,
                         path: targetOf(e), open: false });
        break;
      }
      case "verify_verdict":
        cur.parts.push({ kind: "trace", tool: "verify", verb: "verify", denied: e.ok === false,
                         ok: e.ok, rolled_back: e.rolled_back,
                         n_violations: (e.violations || []).length, path: null });
        break;
      default:
        break;
    }
  });
  return steps;
}

function countThinkingSeconds(thinking) {
  if (!thinking || !thinking.t0 || !thinking.t1) return null;
  return Math.max(1, Math.round(thinking.t1 - thinking.t0));
}

function ownerNameOfTrace(t, graph) {
  const m = (t.refusal_text || "").match(/node "([^"]+)"/);
  if (m && graph) {
    const n = graph.nodes.find((x) => x.name === m[1]);
    if (n) return n.name;
  }
  const owner = ownerNodeOf(t.path, graph);
  return owner ? owner.name : null;
}

function TraceText({ t, graph }) {
  if (t.verb === "verify") {
    return h("span", { className: "trace-line" + (t.denied ? " trace-denied" : "") },
      t.denied ? `verify blocked${t.rolled_back ? " · rolled back" : ""}`
               : "verify ok — no boundary violation");
  }
  if (t.denied) {
    const owner = ownerNameOfTrace(t, graph);
    return h("span", { className: "trace-line trace-denied" },
      `✕ ${t.verb}${t.path ? ` ${t.path}` : ""}${owner ? ` — owned by ${owner}` : ""}`);
  }
  if (t.unassigned) {
    return h("span", { className: "trace-line" },
      `→ create ${t.path || ""} — routed to Unassigned`);
  }
  return h("span", { className: "trace-line" }, `→ ${t.verb}${t.path ? ` ${t.path}` : ""}`);
}

function traceKey(t) { return `${t.verb}|${t.path || ""}|${t.denied ? 1 : 0}`; }

/**
 * Tool activity as one line per call inside the assistant message. Consecutive
 * non-denied calls of the same kind collapse into one counted line that
 * expands on click; a denial never collapses and never fades (rail rebuild §6).
 */
function Traces({ items, graph }) {
  const [expanded, setExpanded] = useState(false);
  const rows = [];
  items.forEach((t) => {
    const last = rows[rows.length - 1];
    if (!t.denied && t.ok !== false && last && !last.expandedItems && last.head.verb === t.verb
        && last.head.path === t.path) {
      last.count += 1;
      last.items.push(t);
    } else {
      rows.push({ head: t, count: 1, items: [t], expandedItems: false });
    }
  });
  return h("div", { className: "traces" },
    rows.map((r, i) => {
      const k = traceKey(r.head) + i;
      if (r.count > 1 && !expanded) {
        return h("span", { key: k, className: "trace-line trace-group",
          title: "click to expand", onClick: () => setExpanded(true) },
          `→ ${r.head.verb} ${r.count} ${r.head.path ? r.head.path + " · " : ""}${r.head.verb === "read" ? "files" : "calls"}`);
      }
      if (r.count > 1 && expanded) {
        return h("span", { key: k, className: "trace-line trace-group",
          title: "click to collapse", onClick: () => setExpanded(false) }, [
          h(TraceText, { key: "a", t: r.head, graph }),
          ...r.items.slice(1).map((it, j) => h(TraceText, { key: "b" + j, t: it, graph })),
        ]);
      }
      return h(TraceText, { key: k, t: r.head, graph });
    }));
}

// Markdown + code highlighting + colored entities (spec §6.4/§6.5).
function Md({ text, onNodeTag }) {
  const ref = useRef(null);
  const prepared = useMemo(() => markEntities(text || ""), [text]);
  const html = useMemo(() => {
    if (!prepared.text) return "";
    try { return marked.parse(prepared.text); } catch { return `<p>${prepared.text}</p>`; }
  }, [prepared.text]);
  useLayoutEffect(() => {
    if (!ref.current) return;
    ref.current.querySelectorAll("pre code").forEach((el) => {
      if (el.__mg) return;
      el.__mg = true;
      const pre = el.closest("pre");
      if (!pre) return;
      const lang = (el.className.match(/language-([\w-]+)/) || [])[1] || "";
      if (lang && hljs.getLanguage(lang)) hljs.highlightElement(el);
      const bar = document.createElement("div");
      bar.className = "code-bar";
      const label = document.createElement("span");
      label.className = "code-lang"; label.textContent = lang;
      const copy = document.createElement("button");
      copy.className = "code-copy"; copy.textContent = "copy";
      copy.onclick = () => {
        const txt = el.textContent || "";
        if (navigator.clipboard) navigator.clipboard.writeText(txt);
        copy.textContent = "copied";
        setTimeout(() => { copy.textContent = "copy"; }, 1200);
      };
      bar.appendChild(label); bar.appendChild(copy);
      pre.prepend(bar);
      pre.classList.add("code-block");
    });
    if (onNodeTag) {
      ref.current.querySelectorAll(".t-node").forEach((el) => {
        const nm = el.textContent || "";
        el.addEventListener("click", () => onNodeTag(nm));
        el.style.cursor = "pointer";
      });
    }
  }, [html, onNodeTag]);
  return h("div", { ref, className: "md", dangerouslySetInnerHTML: { __html: html } });
}

// Reasoning: one collapsed line up top, expanded shows the trace subordinate
// to the answer (§5). It self-collapses when the answer streams unless open.
function Reason({ thinking, open, onToggle }) {
  const secs = countThinkingSeconds(thinking);
  const text = (thinking && thinking.s || "").trim();
  return h("div", { className: "reason" }, [
    h("div", { className: "reason-head", onClick: onToggle },
      `${open ? "▾" : "▸"} ${secs ? `Thought for ${secs}s` : "Thought"}`),
    open && text && h("div", { className: "reason-body" }, text),
  ]);
}

function App() {
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState(null);
  const [layer, setLayer] = useState(LAYER_DOMAIN);
  const [domainId, setDomainId] = useState(null);
  const [nodeId, setNodeId] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [clickRect, setClickRect] = useState(null); // clicked node's screen rect — the popover must never cover it
  const [hoveredId, setHoveredId] = useState(null);
  const [showAllEdges, setShowAllEdges] = useState(false);

  const [sessions, setSessions] = useState([]);
  const [current, setCurrent] = useState(null); // latest session state (meta)
  const [graphVersion, setGraphVersion] = useState(null);
  const [taskText, setTaskText] = useState("");
  const [lastPrompt, setLastPrompt] = useState("");
  const [mode, setMode] = useState("plan"); // "plan" | "build" — composer (§7 rail rebuild)
  const [agentLabel, setAgentLabel] = useState("deepseek-v4-flash");
  const [rebuilding, setRebuilding] = useState(false);
  const [appliedOnce, setAppliedOnce] = useState(false); // quiet "applied" note after Apply
  // Theme: the stored CHOICE is "system" | "light" | "dark" (new installs
  // default to "system"; an existing "light"/"dark" choice from before this
  // setting existed is respected as-is). "system" tracks prefers-color-scheme
  // live via a matchMedia listener, not just at load.
  const [themeChoice, setThemeChoice] = useState(() => {
    try { return localStorage.getItem("mindvibe-theme") || "system"; }
    catch { return "system"; }
  });
  const [systemDark, setSystemDark] = useState(() => {
    try { return window.matchMedia("(prefers-color-scheme: dark)").matches; }
    catch { return false; }
  });
  useEffect(() => {
    let mq;
    try { mq = window.matchMedia("(prefers-color-scheme: dark)"); } catch { return undefined; }
    const onChange = (e) => setSystemDark(e.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  const theme = themeChoice === "system" ? (systemDark ? "dark" : "light") : themeChoice;
  const [scopeNodeId, setScopeNodeId] = useState(null); // null = whole project
  const [railWidth, setRailWidth] = useState(420);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [positions, setPositions] = useState({});
  const [denial, setDenial] = useState(null);
  const [highlight, setHighlight] = useState({}); // nodeId -> true (brief, after apply)
  const [nodes, setNodes] = useState([]); // React Flow controlled nodes (stable positions)
  const [layoutResetCount, setLayoutResetCount] = useState(0); // force node rebuild
  const [diffReady, setDiffReady] = useState({}); // sessionId -> diff was shown (§9: Apply needs it)
  const [locks, setLocks] = useState({}); // nodeId -> true — the agent can't write there

  // React Flow's instance arrives from onInit, which React Flow calls once its
  // internal ResizeObserver has measured the container — asynchronously, off
  // React's own commit/effect cycle. A plain ref survives that fine for
  // one-off reads (keyboard shortcut), but an effect that gates on
  // `!rfRef.current` can permanently miss the moment it becomes non-null: a
  // ref write doesn't schedule a re-run, so if the effect's other deps
  // already settled by the time onInit fires, it never gets a reason to
  // check again (root cause of the unfitted-viewport bug). State makes the
  // instance becoming ready itself a dependency.
  const [rfInstance, setRfInstance] = useState(null);
  const rfRef = useRef(null); // kept for the synchronous keyboard-shortcut read
  const taRef = useRef(null);
  const turnsRef = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    try { localStorage.setItem("mindvibe-theme", themeChoice); } catch { /* ignore */ }
  }, [themeChoice]);

  // --- load graph ---
  const loadGraph = useCallback(() => {
    fetch("/graph.json").then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(setGraph).catch(() => setError("No graph yet — run `mindgraph scan <repo>` first."));
  }, []);
  useEffect(loadGraph, [loadGraph]);

  // --- load layout once ---
  useEffect(() => {
    fetch("/api/layout").then((r) => r.json()).then((d) => {
      const l = d.layout || {};
      const view = l.view || {};
      setRailWidth(Math.min(560, Math.max(320, view.rail_width || 420)));
      // Collapsed must not be the initial state (bug 1): always start open;
      // the user's later toggle still persists for the session.
      setRailCollapsed(false);
      setPositions(l.positions || {});
    }).catch(() => {});
  }, []);

  // --- poll state + sessions ---
  useEffect(() => {
    let stop = false;
    async function poll() {
      if (stop) return;
      try {
        const st = await (await fetch("/api/state")).json();
        setCurrent(st.state || null);
        if (st.agent?.model) setAgentLabel(st.agent.model);
        else if (st.agent?.provider && !st.agent.model) setAgentLabel(st.agent.provider);
        const gv = st.graph_version;
        setGraphVersion((prev) => {
          if (prev && gv && prev !== gv) { loadGraph(); setHighlight({}); }
          return gv;
        });
        const ss = await (await fetch("/api/sessions")).json();
        setSessions(ss.sessions || []);
      } catch { /* studio not running */ }
      setTimeout(poll, 800);
    }
    poll();
    return () => { stop = true; };
  }, [loadGraph]);

  // --- persist rail width / collapse ---
  useEffect(() => {
    const t = setTimeout(() => {
      fetch("/api/layout", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ view: { rail_width: railWidth, rail_collapsed: railCollapsed } }) }).catch(() => {});
    }, 400);
    return () => clearTimeout(t);
  }, [railWidth, railCollapsed]);

  // --- indexes ---
  const domainById = useMemo(() => {
    if (!graph) return {};
    const m = {};
    graph.domains.forEach((d) => { m[d.id] = { ...d, color: colorForName(d.name) }; });
    return m;
  }, [graph]);
  const nodeById = useMemo(() => {
    if (!graph) return {};
    const m = {};
    graph.nodes.forEach((n) => { m[n.id] = n; });
    return m;
  }, [graph]);
  const symbolById = useMemo(() => {
    if (!graph) return {};
    const m = {};
    graph.symbols.forEach((s) => { m[s.id] = s; });
    return m;
  }, [graph]);
  const colorOfNode = useCallback((n) => (n && domainById[n.parent]?.color) || "#8A8880", [domainById]);
  const domainStats = useMemo(() => {
    if (!graph) return {};
    const m = {};
    graph.domains.forEach((d) => { m[d.id] = { nodeCount: 0, files: new Set(), symbols: 0, shared: 0 }; });
    graph.nodes.forEach((n) => {
      const s = m[n.parent];
      if (!s) return;
      s.nodeCount += 1; n.files.forEach((f) => s.files.add(f));
      s.symbols += n.symbols.length; s.shared += n.shared_count;
    });
    return m;
  }, [graph]);

  // --- navigation ---
  const goToRoot = useCallback(() => { setDomainId(null); setNodeId(null); setSelectedId(null); setLayer(LAYER_DOMAIN); }, []);
  const goToDomain = useCallback((id) => { setDomainId(id); setNodeId(null); setSelectedId(null); setLayer(LAYER_FEATURE); }, []);
  const goToNode = useCallback((id) => { setNodeId(id); setSelectedId(null); setLayer(LAYER_SYMBOL); }, []);
  const goUp = useCallback(() => {
    setLayer((cur) => {
      if (cur === LAYER_SYMBOL) { setSelectedId(null); setNodeId(null); return LAYER_FEATURE; }
      if (cur === LAYER_FEATURE) { setDomainId(null); return LAYER_DOMAIN; }
      return cur;
    });
  }, []);

  // --- browser history: the drill path is real navigation (item 5) ---------
  // Each level change pushes {layer, domainId, nodeId} plus a readable URL
  // (repo/Domain/Node); Back/Forward restore it via popstate. The ref stops
  // the push-effect from re-pushing the very state popstate just applied.
  const fromPopRef = useRef(false);
  const mountedRef = useRef(false);
  useEffect(() => {
    function onPop(e) {
      fromPopRef.current = true;
      const st = e.state || {};
      setSelectedId(null); setScopeNodeId(null); setClickRect(null);
      setDomainId(st.domainId || null);
      setNodeId(st.nodeId || null);
      setLayer(st.layer ?? LAYER_DOMAIN);
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  useEffect(() => {
    if (!graph) return undefined;
    if (fromPopRef.current) { fromPopRef.current = false; return undefined; }
    const segs = [graph.repo.name || graph.repo.root || "repo"];
    if (domainId && domainById[domainId]) segs.push(domainById[domainId].name);
    if (nodeId && nodeById[nodeId]) segs.push(nodeById[nodeId].name);
    const url = "/" + segs.map(encodeURIComponent).join("/");
    const state = { layer, domainId, nodeId };
    if (!mountedRef.current) { mountedRef.current = true; history.replaceState(state, "", url); return undefined; }
    if (location.pathname !== url) history.pushState(state, "", url);
    return undefined;
  }, [graph, layer, domainId, nodeId, domainById, nodeById]);

  // Drilling into an empty node/domain would show an empty canvas instead —
  // show a brief inline hint at the clicked card and do nothing else.
  const [emptyHint, setEmptyHint] = useState(null); // {rect, text}
  const showEmptyHint = useCallback((rect, text) => {
    setEmptyHint({ rect, text });
    setTimeout(() => setEmptyHint((cur) => (cur && cur.rect === rect ? null : cur)), 1600);
  }, []);

  // --- keyboard ---
  useEffect(() => {
    function onKey(e) {
      const mod = e.metaKey || e.ctrlKey;
      const tag = document.activeElement?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); setRailCollapsed((v) => !v); }
      else if (e.key === "Escape") {
        // Esc closes whatever's open first (selection/popover); only once
        // nothing is selected does it act as "go up a level" (item 5).
        if (selectedId) { setSelectedId(null); setScopeNodeId(null); setClickRect(null); }
        else if (layer !== LAYER_DOMAIN) goUp();
      }
      else if (e.key === "Backspace" && !typing && layer !== LAYER_DOMAIN) { e.preventDefault(); goUp(); }
      else if (e.key === "1") rfRef.current?.fitView(FIT_OPTS);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, layer, goUp]);

  // --- fitView after the layout pass, once nodes carry measured dimensions ---
  // Keyed on the actual node count of the CURRENT React Flow state (not just
  // layer/domainId/nodeId) so a fit is never skipped when rfRef wasn't ready
  // yet on an earlier render (e.g. first load, before the graph fetch
  // resolved) and never skipped waiting on itemSetKey/layoutResetCount to
  // settle. Two rAFs wait out layout + one paint so nodes have real
  // getBoundingClientRect() sizes before fitView measures them.
  useEffect(() => {
    if (!rfInstance || nodes.length === 0) return undefined;
    const t = setTimeout(() => rfInstance.fitView(FIT_OPTS), 60);
    return () => clearTimeout(t);
  }, [rfInstance, layer, domainId, nodeId, railWidth, railCollapsed, nodes.length]);

  // Locks: a write-locked node is off-limits to the agent (visual + composer
  // hint; the boundary itself is enforced by the agent's tools).
  const toggleLock = useCallback((id) => {
    setLocks((prev) => {
      const nx = { ...prev };
      if (nx[id]) delete nx[id]; else nx[id] = true;
      return nx;
    });
  }, []);

  // --- visible items + skeleton ---
  const visible = useMemo(() => {
    const empty = { items: [], skeletonEdges: [], hiddenEdges: [], hiddenByNode: new Map(),
      isolatedIds: new Set(), w: CARD_W, h: CARD_H };
    if (!graph) return empty;
    function finish(items, rawEdges, colorOf, w, itemH) {
      const ids = items.map((it) => it.id);
      const sizeOf = (id) => items.find((it) => it.id === id)?.size ?? 0;
      const { skeleton, hidden, hiddenByNode, isolated } = buildSkeleton(ids, rawEdges, sizeOf);
      const withColor = (e) => ({ ...e, color: colorOf(e.from) });
      const hbn = new Map([...hiddenByNode.entries()].map(([id, es]) => [id, es.map(withColor)]));
      return { items, skeletonEdges: skeleton.map(withColor), hiddenEdges: hidden.map(withColor),
        hiddenByNode: hbn, isolatedIds: isolated, w, h: itemH };
    }
    if (layer === LAYER_DOMAIN) {
      const items = graph.domains.map((d) => {
        const st = domainStats[d.id] || { nodeCount: 0, files: new Set(), symbols: 0, shared: 0 };
        const files = st.files.size;
        return {
          id: d.id, name: d.name, color: colorForName(d.name), depth: 0,
          meta: st.nodeCount + files > 0
            ? `${en(st.nodeCount, "node")} · ${en(files, "file")} · ${en(st.symbols, "symbol")}` : null,
          size: st.symbols,
          tooltip: `${en(st.nodeCount, "node")} · ${en(files, "file")} · ${en(st.symbols, "symbol")}`,
          onClick: (e) => { setSelectedId(d.id); setClickRect(e.currentTarget.getBoundingClientRect()); },
          onDoubleClick: (e) => {
            if (st.nodeCount === 0) { showEmptyHint(e.currentTarget.getBoundingClientRect(), "Nothing inside this domain yet"); return; }
            goToDomain(d.id);
          },
        };
      });
      return finish(items, graph.domain_edges || [], (id) => colorForName(domainById[id]?.name || id), DOMAIN_W, CARD_H);
    }
    if (layer === LAYER_FEATURE) {
      const ns = graph.nodes.filter((n) => n.parent === domainId);
      const idSet = new Set(ns.map((n) => n.id));
      const items = ns.map((n) => {
        const unassigned = n.name === "Unassigned";
        return {
          id: n.id, name: n.name, color: colorOfNode(n), depth: 1,
          meta: (n.files.length || n.symbols.length)
            ? `${en(n.files.length, "file")} · ${en(n.symbols.length, "symbol")}` : null,
          unassigned, locked: locks[n.id] ? true : undefined,
          onLock: () => toggleLock(n.id),
          size: n.symbols.length,
          tooltip: unassigned
            ? "symbols not assigned to any area — name this area to claim them"
            : `${en(n.files.length, "file")} · ${en(n.symbols.length, "symbol")}`,
          onClick: (e) => { setSelectedId(n.id); setScopeNodeId(n.id); setClickRect(e.currentTarget.getBoundingClientRect()); },
          onDoubleClick: (e) => {
            if (n.symbols.length === 0) { showEmptyHint(e.currentTarget.getBoundingClientRect(), "Nothing inside this node yet"); return; }
            goToNode(n.id);
          },
        };
      });
      const edges = graph.node_edges.filter((e) => idSet.has(e.from) && idSet.has(e.to));
      return finish(items, edges, (id) => colorOfNode(nodeById[id]), FEATURE_W, CARD_H);
    }
    const node = nodeById[nodeId];
    if (!node) return empty;
    const symIds = new Set(node.symbols);
    const color = colorOfNode(node);
    const items = node.symbols.map((sid) => {
      const s = symbolById[sid];
      if (!s) return null;
      const isModule = s.kind === "module";
      return { id: s.id, name: isModule ? sid.slice(0, -".<module>".length) : sid.split(".").pop(),
        depth: 2, meta: isModule ? "module" : (s.sig || ""), color, size: s.loc || 1,
        sharedDot: !!s.shared, tooltip: s.shared ? `shared · also in: ${(s.owners || []).filter((o) => o !== node.name).join(", ") || "—"}` : (s.doc || s.kind),
        onClick: (e) => { setSelectedId(s.id); setClickRect(e.currentTarget.getBoundingClientRect()); } };
    }).filter(Boolean);
    const edges = graph.symbol_edges.filter((e) => symIds.has(e.from) && symIds.has(e.to));
    return finish(items, edges, () => color, SYMBOL_W, SYMBOL_H);
  }, [graph, layer, domainId, nodeId, domainById, domainStats, nodeById, symbolById, colorOfNode, goToDomain, goToNode, locks, toggleLock, showEmptyHint]);

  // --- popover content: everything the popover shows is already loaded on the
  // client (graph.json) — no per-node fetch, no loading state, no cache. A
  // node's description is generated at build time (mindgraph scan) and lives
  // on the node/domain object as `description`; the popover only reads it.
  const popoverData = useMemo(() => {
    if (!selectedId || !graph) return null;
    const dom = domainById[selectedId];
    if (dom) {
      const st = domainStats[selectedId] || { nodeCount: 0, files: new Set(), symbols: 0 };
      const neighbor = (side) => (graph.domain_edges || [])
        .filter((e) => e[side] === selectedId)
        .map((e) => { const oid = e[side === "from" ? "to" : "from"]; return { id: oid, name: domainById[oid]?.name || oid }; })
        .filter((n, i, arr) => arr.findIndex((x) => x.id === n.id) === i);
      return {
        kind: "domain", id: selectedId, name: dom.name,
        counters: `${en(st.nodeCount, "area")} · ${en(st.files.size, "file")} · ${en(st.symbols, "symbol")}`,
        description: dom.description ?? null,
        files: [...st.files].sort(),
        dependsOn: neighbor("from"), usedBy: neighbor("to"),
        openLabel: "Open this domain", onOpen: () => goToDomain(selectedId),
      };
    }
    const nd = nodeById[selectedId];
    if (nd) {
      const neighbor = (side) => (graph.node_edges || [])
        .filter((e) => e[side] === selectedId)
        .map((e) => { const oid = e[side === "from" ? "to" : "from"]; return { id: oid, name: nodeById[oid]?.name || oid }; })
        .filter((n, i, arr) => arr.findIndex((x) => x.id === n.id) === i);
      return {
        kind: "node", id: selectedId, name: nd.name,
        counters: `${en(nd.files.length, "file")} · ${en(nd.symbols.length, "symbol")}`,
        description: nd.description ?? null,
        files: [...nd.files].sort(),
        dependsOn: neighbor("from"), usedBy: neighbor("to"),
        openLabel: "Open this node", onOpen: () => goToNode(selectedId),
      };
    }
    const sym = symbolById[selectedId];
    if (sym) {
      return {
        kind: "symbol", id: selectedId, name: selectedId.split(".").pop(),
        counters: sym.kind || "symbol",
        description: sym.description ?? sym.doc ?? sym.sig ?? null,
        files: [sym.file],
        dependsOn: [], usedBy: [],
        openLabel: null, onOpen: null,
      };
    }
    return null;
  }, [selectedId, graph, domainById, domainStats, nodeById, symbolById, goToDomain, goToNode]);

  // --- node states from the live session ---
  const nodeState = useMemo(() => {
    const m = {};
    if (!graph || !current) return m;
    const st = current.state;
    const active = current.scope_node_id;
    const planNodes = new Set(current.plan?.nodes || []);
    const verdictNodes = new Set();
    (current.verdict?.changed || []).forEach((p) => {
      graph.nodes.forEach((n) => { if (n.files.includes(p)) verdictNodes.add(n.id); });
    });
    graph.nodes.forEach((n) => {
      let kind = "idle", dimmed = false;
      if (st === "executing") { if (n.id === active) kind = "active"; else dimmed = true; }
      else if (st === "plan_ready" && planNodes.has(n.id)) kind = "in_plan";
      else if (st === "review" && verdictNodes.has(n.id)) {
        const v = current.verdict?.state;
        kind = v === "failed" ? "failed" : v === "unverified" ? "unverified" : v === "aborted" ? "unverified" : "ok";
      }
      m[n.id] = { kind, dimmed };
    });
    return m;
  }, [graph, current]);

  // --- denial detection ---
  const lastCountRef = useRef(0);
  useEffect(() => {
    const live = sessions[sessions.length - 1];
    const evs = live?.events || [];
    if (evs.length <= lastCountRef.current) { lastCountRef.current = evs.length; return; }
    lastCountRef.current = evs.length;
    const last = evs[evs.length - 1];
    if (last.type === "tool_outcome" && last.refused) {
      const owner = refusedOwnerNode(last, graph);
      setDenial({ tool: last.tool, path: last.args?.path, owner: owner?.id || null, active: live.scope_node_id });
      const t = setTimeout(() => setDenial(null), 700);
      return () => clearTimeout(t);
    }
  }, [sessions, graph]);

  // --- highlight nodes briefly after graph recompute (post-apply) ---
  const appliedRef = useRef(null);
  useEffect(() => {
    if (!graphVersion) return;
    const live = sessions[sessions.length - 1];
    if (!appliedRef.current || live?.state !== "review") return;
    const changed = appliedRef.current;
    appliedRef.current = null;
    const nodes = {};
    (changed || []).forEach((p) => {
      graph?.nodes.forEach((n) => { if (n.files.includes(p)) nodes[n.id] = true; });
    });
    setHighlight(nodes);
    const t = setTimeout(() => setHighlight({}), 2500);
    return () => clearTimeout(t);
  }, [graphVersion, sessions, graph]);

  // --- React Flow nodes: stable objects, positions managed by onNodesChange ---
  // Structural node set (id/type/position/style + base data) — depends only on
  // the visible item set and the saved positions, NOT on selection/state, so a
  // drag is never interrupted by a regenerated array.
  const baseNodes = useMemo(() => {
    const pos = layoutPositions(visible.items, visible.skeletonEdges, visible.isolatedIds, visible.w, visible.h, positions);
    const depths = computeDepths(visible.items.map((it) => it.id), visible.skeletonEdges, visible.isolatedIds);
    return visible.items.map((it) => ({
      id: it.id, type: "card", position: pos[it.id] || { x: 0, y: 0 },
      style: { width: visible.w, height: visible.h }, draggable: true, selectable: false,
      data: {
        ...it,
        cascadeDelay: (depths[it.id] ?? 0) * CASCADE_MS,
        onMouseEnter: () => setHoveredId(it.id),
        onMouseLeave: () => setHoveredId((cur) => (cur === it.id ? null : cur)),
      },
    }));
  }, [visible, positions]);

  // (Re)build only when the set of items changes (layer/graph), keeping the
  // positions that onNodesChange has been maintaining.
  const itemSetKey = useMemo(() => visible.items.map((it) => it.id).join("\u0000"), [visible]);
  useEffect(() => { setNodes(baseNodes); }, [itemSetKey, layoutResetCount]); // eslint-disable-line react-hooks/exhaustive-deps

  // Transient data (state/selection/denial) is patched in place, positions kept.
  useEffect(() => {
    setNodes((nds) => nds.map((n) => {
      const ns = nodeState[n.id] || { kind: "idle", dimmed: false };
      const dot = ns.kind === "active" ? "running"
        : ns.kind === "ok" ? "passing"
        : ns.kind === "failed" ? "failing"
        : (ns.kind === "unverified" || ns.kind === "stale") ? "stale"
        : undefined;
      return {
        ...n,
        className: undefined, // selection is drawn via data.active
        data: {
          ...n.data,
          active: n.id === selectedId,
          stateDot: highlight[n.id] ? "passing" : (dot || undefined),
          dimmed: ns.dimmed,
          incomplete: (ns.kind === "ok" || ns.kind === "failed" || ns.kind === "unverified")
            && current?.verdict?.complete === false,
          denied: denial && denial.owner === n.id,
        },
      };
    }));
  }, [nodeState, selectedId, denial, current, highlight]);

  const onNodesChange = useCallback((changes) => {
    setNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  const onNodeDragStop = useCallback((_e, node) => {
    setPositions((prev) => {
      const next = { ...prev, [node.id]: { x: node.position.x, y: node.position.y } };
      fetch("/api/layout", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ positions: next }) }).catch(() => {});
      return next;
    });
  }, []);

  const resetLayout = useCallback(() => {
    setPositions({});
    setLayoutResetCount((c) => c + 1);
    fetch("/api/layout", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: null }) }).catch(() => {});
  }, []);

  const focusId = hoveredId || selectedId;
  const edgeStyle = (e, opacity, width, dashed) => ({ stroke: hexToRgba(e.color, opacity), strokeWidth: width, ...(dashed ? { strokeDasharray: "4 4" } : {}) });
  const toRfEdge = (e, i, prefix, opacity, width, dashed, className) => ({
    id: `${prefix}${i}_${e.from}_${e.to}`, source: e.from, target: e.to, type: "smoothstep",
    pathOptions: { borderRadius: 20 }, animated: false, className,
    style: edgeStyle(e, opacity, width, dashed),
    markerEnd: { type: MarkerType.ArrowClosed, color: hexToRgba(e.color, opacity), width: 10, height: 10 },
  });
  const skeletonRfEdges = useMemo(() => visible.skeletonEdges.map((e, i) => {
    const touches = focusId && (e.from === focusId || e.to === focusId);
    return toRfEdge(e, i, "sk", focusId && !touches ? 0.15 : 0.5, 1.5, false, "skeleton-edge");
  }), [visible.skeletonEdges, focusId]);
  const revealed = focusId ? (visible.hiddenByNode.get(focusId) || []) : (showAllEdges ? visible.hiddenEdges : []);
  const hiddenRfEdges = useMemo(() => revealed.map((e, i) => toRfEdge(e, i, "hid", 0.35, 1, true, "hidden-edge")), [revealed]);
  const denialEdge = useMemo(() => {
    if (!denial || layer !== LAYER_FEATURE || !denial.active || !denial.owner) return [];
    return [{ id: "denial", source: denial.active, target: denial.owner, type: "smoothstep", className: "denial-edge",
      style: { stroke: "#B8565A", strokeWidth: 2, strokeDasharray: "5 5" },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#B8565A", width: 12, height: 12 } }];
  }, [denial, layer]);
  const rfEdges = useMemo(() => [...skeletonRfEdges, ...hiddenRfEdges, ...denialEdge], [skeletonRfEdges, hiddenRfEdges, denialEdge]);

  // --- actions (rail rebuild: one slot, one action per state) ---
  const post = useCallback(async (path, body) => {
    try {
      await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const st = await (await fetch("/api/state")).json();
      setCurrent(st.state || null);
      const ss = await (await fetch("/api/sessions")).json();
      setSessions(ss.sessions || []);
    } catch { /* ignore */ }
  }, []);

  // The composer model: a user message ALWAYS starts a read-only plan. Build
  // mode runs the pending plan of the latest turn instead of starting over.
  const lastSession = sessions[sessions.length - 1] || null;
  const lastState = lastSession?.state || null;
  const lastBusy = lastState === "planning" || lastState === "executing";
  const lastPlanReady = lastState === "plan_ready";
  const lastBlockedPlan = lastState === "idle" && !!(lastSession?.plan) && !!lastSession?.plan_blocked;
  const [narrowNode, setNarrowNode] = useState(null); // chip picked on a >3-area plan

  useEffect(() => {
    // Mode follows the session: a ready plan is built, everything else plans.
    if (lastPlanReady || lastBlockedPlan) setMode("build");
    else if (!lastBusy) setMode("plan");
  }, [lastPlanReady, lastBlockedPlan, lastBusy]);
  useEffect(() => { setNarrowNode(null); }, [lastSession?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const sendPlan = useCallback(() => {
    if (!taskText.trim()) return;
    setLastPrompt(taskText.trim());
    setAppliedOnce(false);
    post("/api/plan", { scope_node_id: scopeNodeId, prompt: taskText.trim() });
    setTaskText("");
  }, [taskText, scopeNodeId, post]);

  // item 6: the failed-turn error in the feed gets a Retry — resubmits that
  // turn's own prompt/scope as a fresh plan (not whatever's in the composer).
  const retryTurn = useCallback((session) => {
    setAppliedOnce(false);
    post("/api/plan", { scope_node_id: session.scope_node_id || null, prompt: session.prompt || "" });
  }, [post]);

  const runPlan = useCallback((sid, scopeOverride) => {
    const s = sessions.find((x) => x.id === sid);
    const node = scopeOverride || scopeNodeId || (s?.plan?.nodes?.length === 1 ? s.plan.nodes[0] : null);
    setAppliedOnce(false);
    setNarrowNode(null);
    post("/api/run", { session_id: sid, scope_node_id: node });
  }, [sessions, scopeNodeId, post]);

  const applyTurn = useCallback((sid) => {
    const s = sessions.find((x) => x.id === sid);
    appliedRef.current = s?.verdict?.changed || [];
    setAppliedOnce(true);
    post("/api/apply", { session_id: sid });
  }, [sessions, post]);

  const discardTurn = useCallback((sid) => {
    setAppliedOnce(false);
    setNarrowNode(null);
    post("/api/discard", { session_id: sid });
  }, [post]);

  const stopTurn = useCallback((sid) => {
    post("/api/stop", { session_id: sid });
  }, [post]);

  const rebuildGraph = useCallback(() => {
    setRebuilding(true);
    fetch("/api/rebuild", { method: "POST" })
      .then(() => loadGraph())
      .finally(() => setRebuilding(false));
  }, [loadGraph]);

  // Name an "Unassigned" area (§10): pins its files as a manifest node and
  // recomputes server-side; the poll then picks up the new graph_version.
  const nameArea = useCallback(async (title, symbolIds) => {
    try {
      await fetch("/api/nodes", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, symbol_ids: symbolIds }) });
    } catch { /* the next poll surfaces the error in the header */ }
    setSelectedId(null);
    setScopeNodeId(null);
  }, []);

  const focusTurn = useCallback((sid) => {
    const el = document.getElementById(`turn-${sid}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  const onRailDivider = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX, startW = railWidth;
    const onMove = (ev) => setRailWidth(Math.min(RAIL_MAX, Math.max(RAIL_MIN, startW - (ev.clientX - startX))));
    const onUp = () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [railWidth]);

  const terminalReview = lastState === "review" || lastState === "aborted" || lastState === "failed";
  const noChangedFiles = !(lastSession?.verdict?.changed || []).length;
  const applyEnabled = !!(lastSession && (noChangedFiles || diffReady[lastSession.id]));
  const composer = {
    lastSession, lastState, lastBusy, lastPlanReady, lastBlockedPlan,
    narrowNode, appliedOnce, applyEnabled,
    canType: !lastBusy && !lastPlanReady && !lastBlockedPlan && !(lastState === "review" && !appliedOnce),
  };

  // item 5: the drill path as breadcrumbs — one clickable segment per level,
  // rendered mono with "/" separators (item 3's identity row). Computed
  // before the early returns below (Rules of Hooks: every render must call
  // the same hooks in the same order, so this can't sit after them).
  const pathSegments = useMemo(() => {
    if (!graph) return [];
    const segs = [{ label: graph.repo.name || graph.repo.root || "repo", onClick: goToRoot }];
    if (domainId && domainById[domainId]) segs.push({ label: domainById[domainId].name, onClick: () => goToDomain(domainId) });
    if (nodeId && nodeById[nodeId]) segs.push({ label: nodeById[nodeId].name, onClick: () => goToNode(nodeId) });
    return segs;
  }, [graph, domainId, nodeId, domainById, nodeById, goToRoot, goToDomain, goToNode]);

  if (error) return h("div", { className: "state-message" }, [h("div", { key: "m" }, error), h("div", { key: "h", style: { fontSize: 12 } }, "Then reload this page.")]);
  if (!graph) return h("div", { className: "state-message" }, "loading graph…");

  const activeName = current?.scope_node_id ? (nodeById[current.scope_node_id]?.name || "") : null;
  const blockedCount = (sessions[sessions.length - 1]?.events || []).filter((e) => (e.type === "tool_outcome" && e.refused) || (e.type === "verify_verdict" && e.ok === false)).length;

  const scopeLabel = (s) => (s && s.scope_node_id)
    ? (nodeById[s.scope_node_id]?.name || s.scope_node_id) : "Whole project";

  return h("div", { className: "app-root", style: { "--rail-width": railWidth + "px" } }, [
    // 56px header: the repo identity lives in the canvas overlay now (item 3,
    // directly above the control group) — the header itself just carries the
    // right-side controls (item 4: a settings menu instead of a standalone
    // theme toggle button).
    h("header", { key: "top", className: "topbar" }, [
      h("div", { key: "right", className: "topbar-right" }, [
        h(SettingsMenu, { key: "settings", themeChoice, onThemeChange: setThemeChoice }),
      ]),
    ]),
    h("div", { key: "body", className: "app-body",
      style: { "--panel-w": (railCollapsed ? 46 : railWidth) + "px" } }, [
      h("div", { key: "canvas", className: "canvas-area" }, [
        h("div", { key: "flow", className: "canvas" + (selectedId ? " has-selection" : "") }, [
          h(ReactFlow, {
            // No fitView/fitViewOptions prop here: that fits on React Flow's
            // own mount effect, before the layout pass has run and before
            // nodes have measured DOM dimensions (the "one node clipped, rest
            // off-screen" bug). onInit only stores the instance; the actual
            // fit runs from the post-layout effect above, keyed on node count.
            nodes, edges: rfEdges, nodeTypes,
            onInit: (inst) => { rfRef.current = inst; setRfInstance(inst); },
            proOptions: { hideAttribution: true },
            onPaneClick: () => { setSelectedId(null); setScopeNodeId(null); },
            minZoom: 0.1, maxZoom: 5, nodesConnectable: false, nodesDraggable: true,
            elementsSelectable: false, onNodesChange, onNodeDragStop,
          }, [
            h(Background, { variant: "dots", gap: 30, size: 2.2, color: cssVar("--canvas-dot", "#CFCCC5") }),
          ]),
        ]),
        // item 3: one control group, top-left, positioned against the canvas
        // container (never the viewport) — the repo identity/breadcrumb row
        // sits directly above it, left-aligned to the same x.
        h("div", { key: "chrome", className: "canvas-chrome" }, [
          h(Breadcrumbs, { key: "crumbs", segments: pathSegments }),
          h(CanvasControls, {
            key: "controls",
            onReset: resetLayout,
            showAllEdges, onToggleEdges: () => setShowAllEdges((v) => !v),
            onZoomIn: () => rfInstance?.zoomIn({ duration: 150 }),
            onZoomOut: () => rfInstance?.zoomOut({ duration: 150 }),
            onFit: () => rfInstance?.fitView(FIT_OPTS),
          }),
        ]),
        // item 5: a node/domain with no children doesn't drill into an empty
        // canvas — just a brief hint at the card that was double-clicked.
        // Fixed position: emptyHint.rect is viewport coordinates.
        emptyHint && h("div", {
          key: "hint", className: "empty-hint",
          style: { left: emptyHint.rect.left + "px", top: (emptyHint.rect.bottom + 8) + "px" },
        }, emptyHint.text),
        popoverData && h(NodePopover, {
          key: "insp-" + selectedId, data: popoverData, avoidRect: clickRect,
          onClose: () => { setSelectedId(null); setScopeNodeId(null); setClickRect(null); },
          onJump: (id) => {
            const el = document.querySelector(`.react-flow__node[data-id="${id}"]`);
            setClickRect(el ? el.getBoundingClientRect() : null);
            setSelectedId(id); setScopeNodeId(id);
          },
          onNameArea: nameArea,
        }),
      ]),
      h("div", { key: "divider", className: "rail-divider", onMouseDown: onRailDivider }),
      h("aside", { key: "rail", className: "rail" + (railCollapsed ? " rail-collapsed" : "") }, [
        h("div", { key: "strip", className: "rail-strip", onClick: () => setRailCollapsed(false), title: "Expand (Ctrl/Cmd+B)" }, [
          h("span", { key: "l", style: { writingMode: "vertical-rl", fontSize: 12 } }, activeName || "studio"),
          blockedCount > 0 && h("span", { key: "u", className: "rail-unread" }, blockedCount),
        ]),
        // item 6: title left, collapse chevron right, nothing else — the
        // blocked-refusals count moved off the header (each refusal already
        // renders inline, in red, at the trace line itself).
        h("div", { key: "head", className: "rail-head" }, [
          h("div", { className: "rail-title" }, "Conversation"),
          h("button", { className: "rail-collapse", onClick: () => setRailCollapsed((v) => !v), "aria-label": "collapse", title: "Collapse (Ctrl/Cmd+B)" }, "›"),
        ]),
        h("div", { key: "turns", className: "turns", ref: turnsRef },
          sessions.length === 0
            ? h(EmptyFeed, { onSuggest: (text) => { setTaskText(text); taRef.current?.focus(); } })
            : sessions.map((s, idx) => h(Turn, {
                key: s.id, session: s, graph, nodeById, isLast: idx === sessions.length - 1,
                showScopeMeta: idx === 0 || scopeLabel(sessions[idx - 1]) !== scopeLabel(s),
                onChip: (id) => { setSelectedId(id); setScopeNodeId(id); setClickRect(null); },
                onRun: (scopeOverride) => runPlan(s.id, scopeOverride),
                onStop: () => stopTurn(s.id),
                onApply: () => applyTurn(s.id),
                onDiscard: () => discardTurn(s.id),
                onDiffSeen: () => setDiffReady((prev) => ({ ...prev, [s.id]: true })),
                onRetry: retryTurn,
              }))),
        h(Composer, {
          key: "composer",
          model: composer, mode, setMode, agentLabel,
          scopeName: scopeNodeId ? (nodeById[scopeNodeId]?.name || null) : null,
          nodeNameOf: (id) => nodeById[id]?.name || id,
          taskText, lastPrompt, taRef, onTaskChange: setTaskText,
          onSend: sendPlan, onRunPlan: () => runPlan(lastSession?.id, narrowNode || undefined),
          onStop: () => stopTurn(lastSession?.id), onApply: () => applyTurn(lastSession?.id), onDiscard: () => discardTurn(lastSession?.id),
          onNarrow: (id) => { setSelectedId(id); setScopeNodeId(id); setNarrowNode(id); setClickRect(null); },
          onScopeClear: () => { setSelectedId(null); setScopeNodeId(null); },
          terminalReview,
        }),
      ]),
    ]),
  ]);
}

// items[0] is the repo root (always present, clickable via onCrumb — item 5
// extends this with the drill path: one clickable segment per level).
// segments: [{label, onClick}], mono, "/"-separated — the last segment is the
// current level (still clickable: a no-op back to itself, kept simple).
function Breadcrumbs({ segments }) {
  const children = [];
  segments.forEach((seg, i) => {
    if (i > 0) children.push(h("span", { key: "sep" + i, className: "crumb-sep" }, "/"));
    children.push(h("span", { key: "c" + i, className: "crumb", onClick: seg.onClick }, seg.label));
  });
  return h("div", { className: "breadcrumbs mono" }, children);
}

// One control group, top-left of the canvas: reset | edges | divider | zoom
// in | zoom out | fit — a single row, shared container, 32px square buttons.
function CanvasControls({ onReset, showAllEdges, onToggleEdges, onZoomIn, onZoomOut, onFit }) {
  return h("div", { className: "canvas-controls" }, [
    h("button", { key: "reset", onClick: onReset, title: "Reset layout", "aria-label": "Reset layout" }, "↺"),
    h("button", { key: "edges", className: showAllEdges ? "active" : "", onClick: onToggleEdges,
      title: "Show all connections", "aria-label": "Show all connections" }, "⇄"),
    h("div", { key: "div", className: "canvas-controls-divider" }),
    h("button", { key: "zi", onClick: onZoomIn, title: "Zoom in", "aria-label": "Zoom in" }, "+"),
    h("button", { key: "zo", onClick: onZoomOut, title: "Zoom out", "aria-label": "Zoom out" }, "−"),
    h("button", { key: "fit", onClick: onFit, title: "Fit to view", "aria-label": "Fit to view" }, "⤢"),
  ]);
}

const THEME_OPTIONS = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];

// item 4: a settings icon opens a small popover menu, built as a LIST of rows
// (one today — Theme) so future settings are additions, not a rewrite.
function SettingsMenu({ themeChoice, onThemeChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); window.removeEventListener("keydown", onKey); };
  }, [open]);

  const rows = [
    {
      key: "theme", label: "Theme",
      control: h("div", { className: "settings-options" },
        THEME_OPTIONS.map(([val, label]) => h("button", {
          key: val, className: "settings-option" + (themeChoice === val ? " active" : ""),
          onClick: () => onThemeChange(val),
        }, label))),
    },
    // future rows go here — same shape: { key, label, control }.
  ];

  return h("div", { ref, className: "settings-menu-wrap" }, [
    h("button", {
      key: "btn", className: "topbar-btn settings-btn", onClick: () => setOpen((v) => !v),
      "aria-label": "Settings", title: "Settings",
    }, "⚙"),
    open && h("div", { key: "menu", className: "settings-menu" },
      rows.map((r) => h("div", { key: r.key, className: "settings-row" }, [
        h("div", { key: "l", className: "settings-row-label" }, r.label),
        r.control,
      ]))),
  ]);
}

function VerdictBadge({ state }) {
  const cls = state === "ok" ? "ok" : state === "failed" ? "failed" : "unverified";
  return h("span", { className: "verdict-badge verdict-" + cls }, state === "unverified" ? "unverified" : state);
}

// --- node popover: left side, explains what the node MEANS ------------------
// Everything shown here comes straight off the loaded graph — no per-node
// fetch, no cache, no loading state (item 7: descriptions are generated once
// at build time and stored on the node; the viewer only reads them). The one
// piece of local logic is placement: fixed to the left edge, vertically
// centered by default, but nudged up/down when that would cover the node
// that was clicked (never sideways — it must stay anchored left).
function NodePopover({ data, avoidRect, onClose, onJump, onNameArea }) {
  const [showFiles, setShowFiles] = useState(false);
  const [areaName, setAreaName] = useState("");
  const [naming, setNaming] = useState(false);
  const ref = useRef(null);
  const [top, setTop] = useState(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const vh = window.innerHeight;
    const ph = el.getBoundingClientRect().height;
    let t = (vh - ph) / 2;
    if (avoidRect) {
      const left = 16, right = 16 + 360;
      const overlapsX = avoidRect.right > left && avoidRect.left < right;
      const overlapsY = avoidRect.bottom > t && avoidRect.top < t + ph;
      if (overlapsX && overlapsY) {
        const spaceBelow = vh - avoidRect.bottom - 16;
        const spaceAbove = avoidRect.top - 16;
        t = (spaceBelow >= ph || spaceBelow >= spaceAbove)
          ? Math.min(vh - ph - 16, avoidRect.bottom + 12)
          : Math.max(16, avoidRect.top - ph - 12);
      }
    }
    setTop(Math.max(16, t));
  }, [avoidRect, data]);

  const close = h("button", { key: "x", className: "pop-close", onClick: onClose, "aria-label": "close" }, "\u00d7");
  const isUnassigned = data.kind === "node" && data.name === "Unassigned";

  const submitName = () => {
    const t = areaName.trim();
    if (!t || naming) return;
    setNaming(true);
    Promise.resolve(onNameArea(t, data.symbolIds || [])).finally(() => setNaming(false));
  };

  const neighborList = (label, list) => list.length > 0 && h("div", { key: label, className: "pop-neighbors" }, [
    h("span", { key: "lbl", className: "pop-neighbors-label" }, label + ": "),
    list.map((n, i) => h("span", { key: n.id }, [
      i > 0 && h("span", { key: "sep" }, ", "),
      h("span", { key: "link", className: "pop-link", onClick: () => onJump(n.id) }, n.name),
    ])),
  ]);

  return h("div", {
    ref, className: "node-popover",
    style: { top: top == null ? undefined : top + "px", visibility: top == null ? "hidden" : "visible" },
  }, [
    h("div", { key: "head", className: "pop-head" }, [
      h("div", { key: "t", className: "pop-title mono" }, data.name),
      close,
    ]),
    h("div", { key: "counters", className: "pop-counters" }, data.counters),

    h("div", { key: "desc", className: "pop-desc" },
      data.description || "No description generated"),

    (data.dependsOn.length > 0 || data.usedBy.length > 0) && h("div", { key: "conn", className: "pop-section" }, [
      neighborList("Depends on", data.dependsOn),
      neighborList("Used by", data.usedBy),
    ]),

    data.files.length > 0 && h("div", { key: "files", className: "pop-section" }, [
      h("div", { className: "pop-collapse", onClick: () => setShowFiles((v) => !v) },
        `${data.files.length} file${data.files.length === 1 ? "" : "s"}`),
      showFiles && data.files.map((f) => h("div", { key: f, className: "pop-row mono" }, f)),
    ]),

    isUnassigned && h("div", { key: "name", className: "name-area" }, [
      h("div", { className: "pop-empty" }, "Give this area a name \u2014 the assignment becomes mechanical."),
      h("div", { className: "name-row" }, [
        h("input", {
          className: "name-input", placeholder: "name this area", value: areaName, autoFocus: true,
          onChange: (e) => setAreaName(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") submitName(); },
        }),
        h("button", { className: "btn primary small", disabled: !areaName.trim() || naming,
          onClick: submitName }, naming ? "Naming\u2026" : "Name"),
      ]),
    ]),

    data.onOpen && !isUnassigned && h("button", { key: "open", className: "btn primary pop-action",
      onClick: data.onOpen }, data.openLabel),
  ]);
}

// --- a turn in the conversation rail (rail rebuild) ------------------------

function segmentsOf(parts) {
  const segs = [];
  parts.forEach((p) => {
    const kind = p.kind === "text" ? "text" : "trace";
    const last = segs[segs.length - 1];
    if (last && last.kind === kind) last.items.push(p);
    else segs.push({ kind, items: [p] });
  });
  return segs;
}

// The plan is a panel (STUDIO.md §7), not a bubble of text.
function PlanPanel({ plan, blocked, nodeNameOf, onChip }) {
  const nodes = plan.nodes || [];
  const files = plan.files || [];
  const contracts = plan.contracts || [];
  const tests = plan.tests || [];
  return h("div", { className: "plan-panel" }, [
    h("div", { className: "plan-eyebrow" }, "Plan"),
    plan.summary && h(Md, { key: "s", text: plan.summary }),
    nodes.length > 0 && h("div", { key: "chips", className: "turn-chips" },
      nodes.map((n) => h("span", { key: n, className: "pop-chip plan-chip",
        title: "focus this area", onClick: () => onChip(n) }, nodeNameOf(n)))),
    files.length > 0 && h("div", { key: "f", className: "plan-list" }, [
      h("div", { key: "fh", className: "plan-lbl" }, "Files"),
      files.map((f) => h("div", { key: f, className: "mono" }, f)),
    ]),
    contracts.length > 0 && h("div", { key: "c", className: "plan-list" }, [
      h("div", { key: "ch", className: "plan-lbl" }, "Contracts"),
      contracts.map((c) => h("div", { key: c, className: "mono plan-contract" }, `🔒 ${c}`)),
    ]),
    tests.length > 0 && h("div", { key: "t", className: "plan-list" }, [
      h("div", { key: "th", className: "plan-lbl" }, "Tests"),
      tests.map((t) => h("div", { key: t, className: "mono" }, t)),
    ]),
    blocked && h("div", { key: "b", className: "turn-blocked" },
      `This touches ${nodes.length} areas. Pick one and describe a smaller change.`),
  ]);
}

// One phase of a turn (plan pass / execution pass): thinking on top, then the
// assistant's prose and tool traces in flow.
function StepView({ step, graph, showPlan, planBlocked, nodeNameOf, onChip, onNodeTag }) {
  const [reasonOpen, setReasonOpen] = useState(false);
  const segs = segmentsOf(step.parts);
  const hasDoc = step.parts.some((p) => p.kind === "text") || step.parts.some((p) => p.kind === "trace");
  const liveText = step.parts.length === 0;
  if (liveText && !step.plan) return null; // nothing yet — text appearing IS the loading state
  return h("div", { className: "step" }, [
    step.thinking && h(Reason, { key: "r", thinking: step.thinking, open: reasonOpen,
      onToggle: () => setReasonOpen((v) => !v) }),
    segs.map((seg, i) => {
      if (seg.kind === "text") {
        const text = seg.items.map((p) => p.s).join("");
        return h(Md, { key: "t" + i, text, onNodeTag });
      }
      return h(Traces, { key: "g" + i, items: seg.items, graph });
    }),
    showPlan && step.plan && step.plan.nodes && h(PlanPanel, {
      key: "plan", plan: step.plan, blocked: planBlocked, nodeNameOf, onChip,
    }),
  ]);
}

// Diff grouped by node with real content (STUDIO.md §9 / /api/diff).
function DiffView({ sessionId, onSeen }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(false);
  const seenRef = useRef(false);
  useEffect(() => {
    if (data) return;
    fetch(`/api/diff?session_id=${encodeURIComponent(sessionId)}`)
      .then((r) => r.json())
      .then((d) => { if (!d.error) setData(d); })
      .catch(() => {});
  }, [sessionId, data]);
  if (!data || !data.groups || data.groups.length === 0) return null;
  const areaCount = data.groups.length;
  const toggle = () => {
    const next = !open;
    setOpen(next);
    // §9: Apply is only meaningful after the diff was actually shown.
    if (next && onSeen && !seenRef.current) { seenRef.current = true; onSeen(); }
  };
  return h("div", { className: "diff-panel" }, [
    h("div", { className: "diff-head", onClick: toggle },
      `${open ? "▾" : "▸"} diff · ${areaCount} area${areaCount === 1 ? "" : "s"}`),
    open && data.groups.map((g) => h("div", { key: g.node, className: "diff-group" }, [
      h("div", { className: "diff-node" }, g.node),
      g.files.map((f) => h("div", { key: f.path, className: "diff-file" }, [
        h("div", { className: "diff-file-head" }, [
          h("span", { className: "mono" }, f.path),
          f.status === "new" && h("span", { className: "diff-status new" }, "new"),
        ]),
        h("pre", { className: "patch" },
          (f.diff || "").split("\n").map((ln, i) => {
            let cls = "";
            if (ln.startsWith("@@")) cls = "hunk";
            else if (ln.startsWith("+")) cls = "add";
            else if (ln.startsWith("-")) cls = "del";
            else if (ln.startsWith("diff --git") || ln.startsWith("index ") || ln.startsWith("new file")
                     || ln.startsWith("--- ") || ln.startsWith("+++ ")) cls = "meta";
            return h("div", { key: i, className: cls }, ln);
          })),
      ])),
    ])),
  ]);
}

function Turn({ session, graph, nodeById, isLast, onChip, onRun, onStop, onApply, onDiscard, onDiffSeen, onRetry, showScopeMeta }) {
  const st = session.state;
  const plan = session.plan;
  const verdict = session.verdict;
  const events = session.events || [];
  const steps = useMemo(() => buildSteps(events), [events]);
  const planStepIdx = steps.findIndex((s) => s.plan);
  const planBlocked = !!(plan && plan.blocked);
  const nodeNameOf = (id) => nodeById[id]?.name || id;
  const scopeName = session.scope_node_id ? nodeNameOf(session.scope_node_id) : "Whole project";
  const verdictState = verdict?.state || "unverified";
  const busyNow = st === "planning" || st === "executing";
  const showPlan = !!plan && planStepIdx >= 0;
  const scopeIsNode = !!session.scope_node_id;
  // @[node] tags in agent prose focus that node on the graph.
  const onNodeTag = (name) => {
    const nd = (graph?.nodes || []).find((x) => x.name === name);
    if (nd) onChip(nd.id);
  };

  return h("div", { className: "turn", id: `turn-${session.id}` }, [
    // User message: plain text with a quote bar — no card, no chip. The scope
    // appears as a meta line under the text only when it differs from the
    // previous message (interface spec §6.2).
    h("div", { key: "user", className: "msg-user" }, [
      h("div", { className: "turn-text" }, session.prompt || ""),
      showScopeMeta && h("div", { key: "m", className: "msg-meta" },
        scopeIsNode ? h("span", { className: "mono" }, scopeName) : "Whole project"),
    ]),

    steps.map((step, i) => h(StepView, {
      key: "st" + i, step, graph,
      showPlan: showPlan && i === planStepIdx,
      planBlocked, nodeNameOf, onChip, onNodeTag,
    })),

    // item 6: the one place a failed turn's error shows — was also
    // duplicated under the composer; that copy is gone, this one gets Retry.
    session.error && h("div", { key: "err", className: "turn-error" }, [
      h("span", { key: "t" }, session.error),
      h("button", { key: "r", className: "btn small turn-retry", onClick: () => onRetry(session) }, "Retry"),
    ]),

    // verdict + actions (review). Actions for the latest turn live in the
    // composer's single slot; older turns keep their own quiet buttons.
    (st === "review" || st === "aborted" || st === "applied") && h("div", { key: "rev", className: "turn-verdict" }, [
      h(VerdictBadge, { key: "b", state: st === "aborted" ? "aborted" : st === "applied" ? "ok" : verdictState }),
      h("span", { key: "l", className: "turn-verdict-line" },
        st === "applied" ? "applied — the changes are in the working tree"
          : verdictState === "ok" ? "green means: nothing broke, as far as the tests know"
          : verdictState === "failed" ? "tests broke"
          : verdictState === "aborted" ? "aborted — worktree kept for inspection" : "not covered by tests"),
      verdict?.complete === false && h("span", { key: "i", title: "impact incomplete" }, "⚠"),
    ]),
    (st === "review" || st === "applied") && h(DiffView, { key: "dv", sessionId: session.id, onSeen: onDiffSeen }),

    (!isLast) && (st === "review" || st === "aborted") && h("div", { key: "a", className: "turn-actions" }, [
      st === "aborted" && h("span", { key: "kept", className: "turn-kept" }, "worktree kept"),
      h("button", { className: "btn small", onClick: onApply }, "Apply"),
      h("button", { className: "btn danger small", onClick: onDiscard }, "Discard"),
    ]),
    !isLast && busyNow && h("div", { key: "stop", className: "turn-actions" },
      h("button", { className: "btn danger small", onClick: onStop }, "Stop")),
  ]);
}

// --- empty state (interface spec §6.6): what to do + prefill suggestions ---
function EmptyFeed({ onSuggest }) {
  const suggestions = [
    "Add docstrings to the selected area",
    "Explain how the auth flow works",
    "Find a bug in the login flow",
  ];
  return h("div", { className: "empty-feed" }, [
    h("div", { key: "l1", className: "empty-line" },
      "Pick a node on the graph, or describe a change to the whole project."),
    h("div", { key: "chips", className: "empty-chips" },
      suggestions.map((t) => h("button", { key: t, className: "empty-chip",
        onClick: () => onSuggest(t) }, t))),
  ]);
}

// --- composer: one slot, one action, mode = Plan/Build (rail rebuild §3/§7) ---
function Composer({ model, mode, setMode, agentLabel, scopeName, nodeNameOf,
                    taskText, lastPrompt, taRef, onTaskChange, onSend, onRunPlan,
                    onStop, onApply, onDiscard, onNarrow, onScopeClear }) {
  const lastSession = model.lastSession;
  const st = model.lastState;
  const plan = lastSession?.plan || null;
  const nodes = (plan?.nodes) || [];
  const busy = model.lastBusy;
  const blockedAreas = model.lastBlockedPlan ? nodes : [];
  const narrow = model.narrowNode;
  const canType = model.canType;

  const onInput = (e) => {
    onTaskChange(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };
  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); return; }
    if (e.key === "ArrowUp" && e.target.value === "") onTaskChange(lastPrompt);
  };

  // --- busy: the slot shows Running · Stop --------------------------------
  if (busy) {
    return h("div", { className: "composer slot-running" }, [
      h("span", { key: "say", className: "slot-line" },
        `${st === "planning" ? "Planning" : "Working"}${lastSession?.scope_node_id ? ` in ${nodeNameOf(lastSession.scope_node_id)}` : ""} · streams live above`),
      h("button", { key: "stop", className: "btn danger", onClick: onStop }, "Stop"),
    ]);
  }

  // --- a plan is pending: Run is the only main action -----------------------
  if (model.lastPlanReady && !blockedAreas.length) {
    return h("div", { className: "composer mode-build slot-plan" }, [
      h("div", { key: "note", className: "mode-word build" }, "Build"),
      h("div", { key: "l", className: "slot-line" }, "Build will run the plan from the last message."),
      h("div", { key: "row", className: "slot-row" }, [
        h("button", { key: "run", className: "btn primary", onClick: () => onRunPlan() }, "Run"),
        h("button", { key: "d", className: "btn ghost-quiet", onClick: onDiscard }, "Discard plan"),
      ]),
    ]);
  }

  // --- a >3-area plan: refuse Build, narrow by a chip -----------------------
  if (model.lastBlockedPlan) {
    if (!narrow) {
      return h("div", { className: "composer slot-narrow" }, [
        h("div", { key: "line", className: "slot-line refuse" },
          `This touches ${nodes.length} areas. Pick one to narrow the change.`),
        h("div", { key: "chips", className: "turn-chips" },
          nodes.map((n) => h("span", { key: n, className: "pop-chip",
            onClick: () => onNarrow(n) }, nodeNameOf(n)))),
        h("button", { key: "d", className: "btn ghost-quiet", onClick: onDiscard }, "Discard plan"),
      ]);
    }
    return h("div", { className: "composer mode-build slot-plan" }, [
      h("div", { key: "note", className: "mode-word build" }, "Build"),
      h("div", { key: "l", className: "slot-line" },
        `Build will run the plan in ${nodeNameOf(narrow)} only.`),
      h("div", { key: "row", className: "slot-row" }, [
        h("button", { key: "run", className: "btn primary", onClick: () => onRunPlan() }, `Run in ${nodeNameOf(narrow)}`),
        h("button", { key: "d", className: "btn ghost-quiet", onClick: onDiscard }, "Discard plan"),
      ]),
    ]);
  }

  // --- review: verdict + Apply (Discard quieter beside it) ------------------
  if (st === "review" && !model.appliedOnce) {
    const vs = lastSession?.verdict?.state || "unverified";
    const aborted = vs === "aborted";
    return h("div", { className: "composer slot-review" }, [
      h("span", { key: "v", className: "verdict-badge verdict-" + (vs === "ok" ? "ok" : vs === "failed" ? "failed" : "unverified") }, vs),
      h("div", { key: "row", className: "slot-row" }, [
        h("button", { key: "apply", className: "btn primary", disabled: !model.applyEnabled,
          title: model.applyEnabled ? "" : "open the diff above first", onClick: onApply },
          aborted ? "Apply what changed" : "Apply"),
        h("button", { key: "disc", className: "btn ghost-quiet", onClick: onDiscard }, "Discard"),
      ]),
    ]);
  }

  // --- input: Plan/Build modes, one accent per mode -------------------------
  // A failed session's error already renders in the feed (turn-error, with
  // Retry) — item 6 drops the duplicate that used to also show here.
  const note = st === "applied"
      ? "Applied — the changes are in the working tree; the graph rebuilds below."
      : (st === "aborted" || (st === "review" && model.appliedOnce))
        ? "Session closed — describe the next change."
        : (st === "idle" && lastSession?.plan)
          ? "Plan discarded — describe the next change."
          : null;
  const pendingPlan = !!plan;
  // item 6: one rounded container (input on top, a plain control row below,
  // inside the same box) — no pills, no borders on the scope/attach
  // affordances; the send icon is the one filled element, and only once
  // there's something to send.
  return h("div", { className: "composer" }, [
    h("div", { key: "box", className: "composer-box" }, [
      h("textarea", {
        ref: taRef, className: "composer-input", rows: 1,
        placeholder: "Describe a change…",
        value: taskText, disabled: !canType, onChange: onInput, onKeyDown,
      }),
      h("div", { key: "controls", className: "composer-controls" }, [
        h("button", { key: "scope", className: "composer-scope-btn", title: "Esc clears scope", onClick: onScopeClear },
          (scopeName || "Whole project") + " ⌄"),
        h("button", { key: "attach", className: "composer-attach", "aria-label": "Attach", title: "Attach" }, "📎"),
        h("button", {
          key: "send", className: "composer-send" + (taskText.trim() ? " filled" : ""),
          disabled: !canType || !taskText.trim(), onClick: onSend, "aria-label": "Send", title: "Send",
        }, "↑"),
      ]),
    ]),
    note && h("div", { key: "note", className: "composer-note" }, note),
    pendingPlan && h("div", { key: "hint", className: "composer-note" },
      "A plan is pending above — run it when ready."),
  ]);
}

const root = createRoot(document.getElementById("root"));
root.render(h(App));
