import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlaybackAckIdentity } from "../../src/contracts/realtime";
import { PlaybackAckTracker, PROGRESS_INTERVAL_MS, type AckSender } from "../../src/session/playbackAcks";

const IDENTITY: PlaybackAckIdentity = { workerGeneration: 1, cancellationGeneration: 2, segmentId: "seg-a" };

describe("PlaybackAckTracker: cancelled segments", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops progress and never sends a completion ack once a segment is cancelled mid-playback", () => {
    const send = vi.fn<AckSender>();
    const tracker = new PlaybackAckTracker(send, () => 0);

    tracker.onPlayback("started", IDENTITY);
    vi.advanceTimersByTime(PROGRESS_INTERVAL_MS);
    tracker.onPlayback("cancelled", IDENTITY);
    vi.advanceTimersByTime(PROGRESS_INTERVAL_MS * 4);

    expect(send.mock.calls.map((call) => call[0])).toEqual(["playback.started", "playback.progress"]);
    expect(send.mock.calls.some((call) => call[0] === "playback.completed")).toBe(false);
  });

  it("accepts a cancelled segment for a turn whose next segment can still start cleanly", () => {
    const send = vi.fn<AckSender>();
    const tracker = new PlaybackAckTracker(send, () => 0);
    const nextSegment: PlaybackAckIdentity = { ...IDENTITY, segmentId: "seg-b" };

    tracker.onPlayback("started", IDENTITY);
    tracker.onPlayback("cancelled", IDENTITY);
    tracker.onPlayback("started", nextSegment);
    vi.advanceTimersByTime(PROGRESS_INTERVAL_MS);
    tracker.onPlayback("completed", nextSegment);

    expect(send.mock.calls.map((call) => call[0])).toEqual([
      "playback.started",
      "playback.started",
      "playback.progress",
      "playback.completed",
    ]);
    const completedCall = send.mock.calls.find((call) => call[0] === "playback.completed");
    expect(completedCall?.[1]).toMatchObject({ segmentId: "seg-b" });
  });

  it("ignores a cancellation for a segment that is not the currently active one", () => {
    const send = vi.fn<AckSender>();
    const tracker = new PlaybackAckTracker(send, () => 0);
    const stale: PlaybackAckIdentity = { ...IDENTITY, segmentId: "seg-stale" };

    tracker.onPlayback("started", IDENTITY);
    tracker.onPlayback("cancelled", stale);
    vi.advanceTimersByTime(PROGRESS_INTERVAL_MS);

    // The active segment's timer must still be running: it was not reset by
    // an unrelated stale cancellation (defensive; current server behaviour
    // only ever cancels the active segment).
    expect(send.mock.calls.map((call) => call[0])).toEqual(["playback.started", "playback.progress"]);
  });
});
