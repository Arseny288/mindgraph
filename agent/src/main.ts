// main.ts — CLI parsing for the headless agent.

import { runAgent } from "./agent.ts";

function parseArgv(argv: string[]) {
  const opts: Record<string, string | boolean> = { json: false };
  const valueFlags = new Set([
    "node", "task", "dir", "worktree", "provider", "model", "bin",
    "thinking", "max-moves", "max-refusals", "wording",
  ]);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--json") opts.json = true;
    else if (a === "--no-boundary") opts.noBoundary = true;
    else if (a === "--no-mark-foreign") opts.markForeign = false;
    else if (a.startsWith("--") && valueFlags.has(a.slice(2))) {
      opts[a.slice(2).replace(/-/g, "")] = argv[++i];
    }
  }
  if (!opts.node || !opts.task || !opts.dir || !opts.worktree) {
    process.stderr.write(
      "usage: node agent/src/main.ts --node <id> --task <...> --dir <main> --worktree <wt> [--json]\n",
    );
    process.exit(2);
  }
  return opts;
}

const opts = parseArgv(process.argv.slice(2));

runAgent(opts as any)
  .then((code) => process.exit(code))
  .catch((err) => {
    process.stderr.write(`agent: ${err?.message ?? err}\n`);
    process.exit(1);
  });
