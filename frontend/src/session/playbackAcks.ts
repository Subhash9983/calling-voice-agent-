/**
 * Playback acknowledgements (docs/06 §13). The worker announces playback on
 * `va.playback.v1` with the ack identity; the browser echoes
 * `playback.started` / `playback.progress` / `playback.completed` using that
 * identity. Progress is emitted every 250 ms while a segment is active; the
 * transport limiter still enforces the 4/s cap and the aggregate bucket.
 *
 * Acknowledgements are evidence, not authority. Only progress carries a
 * position (elapsed time since the started announcement, not a
 * sample-accurate playout position), matching the shared golden fixtures.
 */
import type { ClientEventType, PlaybackAckIdentity, PlaybackState } from "../contracts/realtime";

export const PROGRESS_INTERVAL_MS = 250;

export type AckSender = (
  eventType: ClientEventType,
  identity: PlaybackAckIdentity,
  positionMs: number | undefined,
) => void;

export class PlaybackAckTracker {
  private active: PlaybackAckIdentity | null = null;
  private startedAt = 0;
  private timer: ReturnType<typeof setInterval> | null = null;

  public constructor(
    private readonly send: AckSender,
    private readonly now: () => number,
  ) {}

  public onPlayback(state: PlaybackState, identity: PlaybackAckIdentity): void {
    if (state === "started") {
      this.stopTimer();
      this.active = identity;
      this.startedAt = this.now();
      this.send("playback.started", identity, undefined);
      this.timer = setInterval(() => {
        this.send("playback.progress", identity, this.now() - this.startedAt);
      }, PROGRESS_INTERVAL_MS);
    } else if (state === "completed") {
      if (this.active?.segmentId === identity.segmentId) {
        this.send("playback.completed", identity, undefined);
      }
      this.reset();
    } else if (state === "cancelled" || state === "interrupted" || state === "failed") {
      // A stale/foreign message for a segment that is no longer active (e.g.
      // a late cancellation racing a newer segment's `started`) must never
      // stop tracking the segment that is genuinely playing now.
      if (this.active === null || this.active.segmentId === identity.segmentId) {
        this.reset();
      }
    }
  }

  /** The audio element errored while a segment was active. */
  public onPlayoutFailed(): void {
    if (this.active !== null) {
      this.send("playback.failed", this.active, undefined);
    }
    this.reset();
  }

  public dispose(): void {
    this.reset();
  }

  private reset(): void {
    this.stopTimer();
    this.active = null;
  }

  private stopTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
