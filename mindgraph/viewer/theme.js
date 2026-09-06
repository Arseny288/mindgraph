// theme.js — colors and small visual utilities.
//
// The single source of color in the whole interface is the domain (L0). The
// color of an L1 node or L2 symbol is NOT recomputed from its own name — it is
// inherited from the parent domain (see colorForItem in main.jsx). colorForName
// below is called exactly once per domain — a deterministic hash of the domain
// name mod the palette length, so the same domain always gets the same color
// across viewer runs.

export const PALETTE = [
  "#B8563A", // terracotta
  "#C99A2E", // ochre
  "#6B8CAE", // dusty blue
  "#7A9A6B", // sage
  "#9B6B8C", // plum
  "#4E8B84", // dark teal
];

export function colorForName(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (Math.imul(h, 31) + name.charCodeAt(i)) >>> 0;
  }
  return PALETTE[h % PALETTE.length];
}

export function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
