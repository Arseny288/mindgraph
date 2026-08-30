// build.mjs — собирает вьюер в единый самодостаточный бандл dist/app.js
// и копирует reactflow.css в dist/. Запускается `npm run build` из viewer/.
// Пользователю node НЕ нужен: собранный dist/ коммитится в репозиторий и
// попадает в wheel как package-data, рантайм без внешней сети (см. 0.2).
import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, "dist");

mkdirSync(dist, { recursive: true });

await build({
  entryPoints: [path.join(here, "main.jsx")],
  bundle: true,
  format: "iife",
  minify: true,
  // React/ReactFlow используют process.env.NODE_ENV для ветвления dev/prod.
  define: { "process.env.NODE_ENV": '"production"' },
  outfile: path.join(dist, "app.js"),
});

copyFileSync(
  path.join(here, "node_modules", "reactflow", "dist", "style.css"),
  path.join(dist, "reactflow.css"),
);

console.log("собран dist/app.js и dist/reactflow.css");
