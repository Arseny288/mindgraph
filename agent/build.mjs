// build.mjs — bundles the TS agent into a single self-contained bundle shipped
// in the wheel as package-data (esbuild, platform=node, format=cjs). Run
// `npm run build` from agent/. The user does NOT need to run this: the built
// bundle is committed to the repo, so the runtime needs neither the pi deps nor
// native TypeScript (Node 26 type-stripping) — just any modern Node.
import { build } from "esbuild";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgDir = path.join(here, "..", "mindgraph", "_agent");
const outfile = path.join(pkgDir, "agent.cjs");

mkdirSync(pkgDir, { recursive: true });

await build({
  entryPoints: [path.join(here, "src", "main.ts")],
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  // CJS: the `yaml` dependency does `require("process")` (dynamic require), which
  // ESM output cannot represent. Keep the bundle readable; pi deps are inlined.
  minify: false,
  outfile,
});

console.log(`built ${path.relative(here, outfile)}`);
