// nodes.jsx — карточка узла для React Flow. ОДИН компонент для всех трёх
// слоёв (домен/фича/символ) — визуально они идентичны по спеке ("остальное
// оформление то же"), разница только в данных (есть ли subtitle, sharedDot,
// active) и в размере, который приходит через style узла из main.jsx.
//
// Производительность: компонент обёрнут в React.memo и не подписан ни на
// какое hover-состояние — подсветка при наведении и тултип с метаданными
// сделаны чистым CSS (:hover) в index.html, без единого setState на
// движение мыши.
//
// Двойной клик — НАТИВНЫЙ addEventListener("dblclick") прямо на карточке, а не
// синтетический onDoubleClick: React Flow останавливает dblclick на своей
// обёртке узла в bubble-фазе, и делегированный React-обработчик (#root)
// события не видит, хотя до самой карточки нативный dblclick доходит. Ловим
// на target-элементе — это срабатывает раньше чужого stopPropagation.
// Последний колбэк держим в ref, чтобы не перевешивать листенер на каждый рендер.
//
// Без JSX/сборки: см. комментарий в main.jsx — расширение .jsx оставлено
// ради совпадения со структурой спеки, реальный код — React.createElement.

import React, { useEffect, useRef } from "react";
import { Handle, Position } from "reactflow";

const h = React.createElement;
const HIDDEN_HANDLE = { opacity: 0 };

function LockBadge() {
  return h("svg", { width: 9, height: 9, viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" }, [
    h("rect", { key: "r", x: 5, y: 11, width: 14, height: 9, rx: 2.5, stroke: "#B8565A", strokeWidth: 2.6 }),
    h("path", { key: "p", d: "M8 11V7.5a4 4 0 0 1 8 0V11", stroke: "#B8565A", strokeWidth: 2.6, fill: "none" }),
  ]);
}

function CardNodeImpl({ data }) {
  const classes = ["node-card"];
  if (data.active) classes.push("active");

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
    className: "node-card-wrap",
    style: {
      "--stripe": data.color,
      "--cascade-delay": `${data.cascadeDelay || 0}ms`,
    },
  }, [
    h("div", { key: "eyebrow", className: "node-eyebrow" }, "ФИЧА"),
    h("div", {
      key: "card",
      ref: cardRef,
      className: classes.join(" "),
      onClick: data.onClick,
    }, [
      h(Handle, { key: "target", type: "target", position: Position.Top, style: HIDDEN_HANDLE }),
      h("div", { key: "lock", className: "node-lock" }, h(LockBadge)),
      h("div", { key: "name", className: "node-name" }, data.name),
      data.subtitle && h("div", { key: "sub", className: "node-subtitle" }, data.subtitle),
      data.sharedDot && h("div", { key: "dot", className: "node-shared-dot", style: { background: data.color } }),
      data.tooltip && h("div", { key: "tip", className: "node-tooltip" }, data.tooltip),
      h(Handle, { key: "source", type: "source", position: Position.Bottom, style: HIDDEN_HANDLE }),
    ]),
  ]);
}

export const CardNode = React.memo(CardNodeImpl);
export const nodeTypes = { card: CardNode };
