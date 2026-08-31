// limiter.ts — лимит ходов и остановка при повторяющемся отказе (фаза 2.5, п.4).
//
// Без этого агент на цикле «пробует один и тот же обход» жжёт токены. Лимит
// ходов + N подряд отказов одного вида -> agent_end с причиной.

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

  /** Зарегистрировать ход; ``refusalKind`` — тип отказа (или null, если успех). */
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
          reason: `повторный отказ «${refusalKind}» ${this.consecutive} раз подряд`,
        };
        return true;
      }
    } else {
      this.currentKind = null;
      this.consecutive = 0;
    }
    if (this.moves >= this.opts.maxMoves) {
      this.stopped = { kind: "max_moves", reason: `лимит ходов ${this.opts.maxMoves}` };
      return true;
    }
    return false;
  }
}

/** Классифицировать текст отказа инструмента в устойчивый ``kind``. */
export function classifyRefusal(toolName: string, text: string): string | null {
  if (text.includes("чужой символ")) return "foreign_symbol";
  if (text.includes("вне allow_read")) return "read_foreign";
  if (text.includes("вне твоей зоны") || text.includes("вне allow_write")) {
    return "write_foreign";
  }
  if (text.includes("НЕРАЗОБРАННОЕ")) return "unassigned_create";
  return null;
}
