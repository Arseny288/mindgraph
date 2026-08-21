// theme.js — цвета и мелкие визуальные утилиты.
//
// Единственный источник цвета во всём интерфейсе — домен (L0). Цвет узла L1
// и символа L2 не считается заново по их собственному имени — он наследуется
// от родительского домена (см. colorForItem в main.jsx). colorForName ниже
// вызывается ровно один раз на домен — детерминированный хэш имени домена
// mod длина палитры, чтобы один и тот же домен между прогонами вьюера
// всегда получал один и тот же цвет.

export const PALETTE = [
  "#B8563A", // терракота
  "#C99A2E", // охра
  "#6B8CAE", // пыльный синий
  "#7A9A6B", // шалфей
  "#9B6B8C", // сливовый
  "#4E8B84", // тёмная бирюза
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
