// nodes.jsx — the node card for React Flow. ONE component for all three layers
// (domain/feature/symbol); the only differences are the data and the size
// (arrives via the node's style from main.jsx), because depth is encoded by
// card size, not by a type label (interface spec §5.2).
//
// Identity is the color bar + the mono name. State lives in the 6px dot
// bottom-right (running breathes) and in the selection ring — never on the
// stripe, never as an outline wash. No all-caps text.
//
// Double click — a NATIVE addEventListener("dblclick") directly on the card
// (React Flow stops the synthetic dblclick in the bubble phase).
//
// No JSX — React.createElement directly (see main.jsx).

import React, { useEffect, useRef } from "react";
import { Handle, Position } from "reactflow";

const h = React.createElement;
const HIDDEN_HANDLE = { opacity: 0 };

function LockGlyph({ locked }) {
  return h("svg", { width: 13, height: 13, viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" }, [
    h("rect", { key: "r", x: 5, y: 11, width: 14, height: 9, rx: 2.5,
      stroke: locked ? "currentColor" : "#A3A2AB", strokeWidth: 2.4 }),
    h("path", { key: "p", d: "M8 11V7.5a4 4 0 0 1 8 0V11",
      stroke: locked ? "currentColor" : "#A3A2AB", strokeWidth: 2.4, fill: "none" }),
  ]);
}

// state -> dot style. Dots carry the state; nothing else pulses except running.
const DOT_STATE = {
  running: "dot-running",
  passing: "dot-passing",
  failing: "dot-failing",
  stale: "dot-stale",
  queued: "dot-queued",
};

function CardNodeImpl({ data }) {
  const classes = ["node-card"];
  if (data.active) classes.push("is-selected");
  if (data.stateDot) classes.push("has-state");
  if (data.denied) classes.push("node-denied");
  if (data.unassigned) classes.push("node-unassigned");
  if (data.depth != null) classes.push("depth-" + data.depth);

  const wrapClass = "node-card-wrap" + (data.dimmed ? " node-dimmed" : "");
  const cardRef = useRef(null);
  const dblRef = useRef(null);
  dblRef.current = data.onDoubleClick;

  useEffect(() => {
    const el = cardRef.current;
    if (!el) return undefined;
    const handler = (e) => { if (dblRef.current) dblRef.current(e); };
    el.addEventListener("dblclick", handler);
    return () => el.removeEventListener("dblclick", handler);
  }, []);

  return h("div", {
    className: wrapClass,
    style: {
      "--stripe": data.color,
      "--cascade-delay": `${data.cascadeDelay || 0}ms`,
    },
  }, [
    data.eyebrow && h("div", { key: "eyebrow", className: "node-eyebrow" }, data.eyebrow),
    h("div", {
      key: "card",
      ref: cardRef,
      className: classes.join(" "),
      onClick: data.onClick,
      title: data.name,
    }, [
      h(Handle, { key: "target", type: "target", position: Position.Top, style: HIDDEN_HANDLE }),
      h("div", { key: "name", className: "node-name mono" }, data.name),
      data.meta && h("div", { key: "meta", className: "node-meta" }, data.meta),
      data.subtitle && !data.meta && h("div", { key: "sub", className: "node-meta mono" }, data.subtitle),
      // the state dot: 6px, bottom-right; running breathes
      h("span", { key: "dot", className: "node-dot " + (DOT_STATE[data.stateDot] || "dot-idle") }),
      // lock: shown when the node is write-locked; clicking toggles
      data.locked !== undefined && h("button", {
        key: "lock", className: "node-lock" + (data.locked ? " locked" : ""),
        onClick: (e) => { e.stopPropagation(); if (data.onLock) data.onLock(); },
        title: data.locked
          ? "The agent can't modify files in this node — click to unlock"
          : "The agent can modify files in this node — click to lock",
        "aria-label": data.locked ? "Unlock node" : "Lock node",
      }, h(LockGlyph, { locked: data.locked })),
      data.incomplete && h("span", { key: "inc", className: "node-incomplete", title: "impact incomplete" }, "incomplete"),
      data.tooltip && h("div", { key: "tip", className: "node-tooltip" }, data.tooltip),
      h(Handle, { key: "source", type: "source", position: Position.Bottom, style: HIDDEN_HANDLE }),
    ]),
  ]);
}

export const CardNode = React.memo(CardNodeImpl);
export const nodeTypes = { card: CardNode };
