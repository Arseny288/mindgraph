// limiter.ts — move limit and stop-on-repeated-refusal (phase 2.5, item 4).
//
// Without this the agent burns tokens looping on "try the same workaround
// again". A move limit + N consecutive refusals of one kind -> agent_end with a
// reason.

export interface LimiterOptions {
  maxMoves: number;
  maxConsecutiveRefusals: number;
}

export interface StopReason {
  reason: string;
  kind: "max_moves" | "repeated_refusal";
}

export class Limiter {
  private moves = 0;
  private currentKind: string | null = null;
  private consecutive = 0;
  private opts: LimiterOptions;
  stopped: StopReason | null = null;

  constructor(opts: LimiterOptions) {
    this.opts = opts;
  }

  /** Register a move; ``refusalKind`` — the refusal kind (or null on success). */
  onMove(refusalKind: string | null): boolean {
    this.moves++;
    if (refusalKind) {
      if (refusalKind === this.currentKind) {
        this.consecutive++;
      } else {
        this.currentKind = refusalKind;
        this.consecutive = 1;
      }
      if (this.consecutive >= this.opts.maxConsecutiveRefusals) {
        this.stopped = {
          kind: "repeated_refusal",
          reason: `repeated refusal "${refusalKind}" ${this.consecutive} times in a row`,
        };
        return true;
      }
    } else {
      this.currentKind = null;
      this.consecutive = 0;
    }
    if (this.moves >= this.opts.maxMoves) {
      this.stopped = { kind: "max_moves", reason: `move limit ${this.opts.maxMoves}` };
      return true;
    }
    return false;
  }
}

/** Classify a tool's refusal text into a stable ``kind``. */
export function classifyRefusal(toolName: string, text: string): string | null {
  if (text.includes("foreign symbol")) return "foreign_symbol";
  if (text.includes("allow_read")) return "read_foreign";
  if (text.includes("your zone") || text.includes("allow_write")) {
    return "write_foreign";
  }
  if (text.includes("UNASSIGNED")) return "unassigned_create";
  return null;
}
