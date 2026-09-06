// build.mjs — bundles the viewer into content-hashed dist/app.<hash>.js and
// dist/app.<hash>.css, generates index.html from index.template.html to
// reference them, and copies reactflow.css/fonts into dist/. Run
// `npm run build` from viewer/.
//
// Content hashing (caching hardening pass, see mindgraph/serve.py's
// cache_control_for): a changed bundle gets a NEW URL, so a browser cannot
// serve a stale one no matter what it cached — Cache-Control headers alone
// were a patch, this is the actual fix. index.html is generated here and
// must never be hand-edited or reference an unhashed bundle; old hashed
// files are removed first so dist/ cannot accumulate.
//
// The user does NOT need node: the built dist/ + generated index.html are
// committed to the repo and land in the wheel as package-data, runtime
// without an external network (0.2).
import { build } from "esbuild";
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, "dist");

mkdirSync(dist, { recursive: true });

// Remove stale hashed bundles from a previous build FIRST — otherwise dist/
// accumulates one app.<hash>.js per build forever.
const HASHED_RE = /^app\.[0-9a-f]{10}\.(js|css)$/;
for (const name of readdirSync(dist)) {
  if (HASHED_RE.test(name)) unlinkSync(path.join(dist, name));
}

function hashOf(buf) {
  return createHash("sha256").update(buf).digest("hex").slice(0, 10);
}

// --- JS bundle (hashed) -------------------------------------------------------
const jsBuild = await build({
  entryPoints: [path.join(here, "main.jsx")],
  bundle: true,
  format: "iife",
  minify: true,
  // React/ReactFlow use process.env.NODE_ENV to branch dev/prod.
  define: { "process.env.NODE_ENV": '"production"' },
  write: false,
});
const jsBuf = Buffer.from(jsBuild.outputFiles[0].contents);
const jsName = `app.${hashOf(jsBuf)}.js`;
writeFileSync(path.join(dist, jsName), jsBuf);

// --- CSS bundle (hashed) — same app.<hash>.css naming the server's cache
// policy matches on, so it gets the same immutable treatment as the JS ------
const cssBuf = readFileSync(path.join(here, "style.css"));
const cssName = `app.${hashOf(cssBuf)}.css`;
writeFileSync(path.join(dist, cssName), cssBuf);

// --- unhashed vendor assets (unchanged by this project's own edits) --------
copyFileSync(
  path.join(here, "node_modules", "reactflow", "dist", "style.css"),
  path.join(dist, "reactflow.css"),
);

// IBM Plex, bundled locally (latin + cyrillic, 400/500) — no CDN at runtime.
const fontsDir = path.join(dist, "fonts");
mkdirSync(fontsDir, { recursive: true });
for (const pkg of ["ibm-plex-sans", "ibm-plex-mono"]) {
  for (const sub of ["latin", "cyrillic"]) {
    for (const w of ["400", "500"]) {
      const name = `${pkg}-${sub}-${w}-normal.woff2`;
      copyFileSync(path.join(here, "node_modules", "@fontsource", pkg, "files", name),
                   path.join(fontsDir, name));
    }
  }
}

// --- build id: git sha + build timestamp (item 5's on-screen diagnostic) ---
let gitSha = "unknown";
try {
  gitSha = execSync("git rev-parse --short HEAD", { cwd: here }).toString().trim();
} catch {
  // Not a git checkout (e.g. built from a source tarball) — "unknown" is
  // honest and still distinguishes builds via the timestamp half.
}
const buildId = `${gitSha}-${new Date().toISOString()}`;

// --- generate index.html from the template ----------------------------------
// Never hand-edit viewer/index.html — edit index.template.html instead.
let html = readFileSync(path.join(here, "index.template.html"), "utf8");
html = html
  .replaceAll("__APP_JS__", jsName)
  .replaceAll("__APP_CSS__", cssName)
  .replaceAll("__BUILD_ID__", buildId);
writeFileSync(path.join(here, "index.html"), html);

console.log(`built dist/${jsName}, dist/${cssName}, dist/reactflow.css, dist/fonts/`);
console.log(`regenerated index.html — build ${buildId}`);
