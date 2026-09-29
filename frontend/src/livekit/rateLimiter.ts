/**
 * Browser-to-agent send limits (docs/06 §13): one participant-level token
 * bucket of 20 messages/second with burst 40 shared by all client events,
 * plus a stricter playback-progress limit of one event per 250 ms (4/s).
 */
export const AGGREGATE_RATE_PER_SECOND = 20;
export const AGGREGATE_BURST = 40;
export const PROGRESS_MIN_INTERVAL_MS = 250;

export type SendDecision =
  /** Within limits. */
  | "allow"
  /** Lossy progress over limit: dropped and counted. */
  | "drop"
  /** Reliable message over limit: rejected (realtime_overload). */
  | "reject";

export interface RateLimiterCounters {
  readonly droppedProgress: number;
  readonly rejectedReliable: number;
}

export class ClientSendLimiter {
  private tokens = AGGREGATE_BURST;
  private lastRefillMs: number;
  private lastProgressMs: number | null = null;
  private droppedProgress = 0;
  private rejectedReliable = 0;

  public constructor(private readonly now: () => number) {
    this.lastRefillMs = now();
  }

  public counters(): RateLimiterCounters {
    return { droppedProgress: this.droppedProgress, rejectedReliable: this.rejectedReliable };
  }

  public decide(isProgress: boolean): SendDecision {
    const current = this.now();
    if (isProgress && this.lastProgressMs !== null && current - this.lastProgressMs < PROGRESS_MIN_INTERVAL_MS) {
      this.droppedProgress += 1;
      return "drop";
    }
    this.refill(current);
    if (this.tokens < 1) {
      if (isProgress) {
        this.droppedProgress += 1;
        return "drop";
      }
      this.rejectedReliable += 1;
      return "reject";
    }
    this.tokens -= 1;
    if (isProgress) {
      this.lastProgressMs = current;
    }
    return "allow";
  }

  private refill(current: number): void {
    const elapsedSeconds = Math.max(0, current - this.lastRefillMs) / 1000;
    this.tokens = Math.min(AGGREGATE_BURST, this.tokens + elapsedSeconds * AGGREGATE_RATE_PER_SECOND);
    this.lastRefillMs = current;
  }
}
