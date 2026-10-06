import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  LATENCY_POLL_INTERVAL_MS,
  LATENCY_SETTLE_DELAY_MS,
  LatencySampleTracker,
  type LatencySample,
  type StatsProvider,
} from "../../src/session/latencySamples";

function reportOf(entries: readonly Record<string, unknown>[]): RTCStatsReport {
  return {
    forEach: (callback: (value: unknown) => void) => {
      entries.forEach((entry) => {
        callback(entry);
      });
    },
  } as unknown as RTCStatsReport;
}

// `totalPlayoutDelay` is the running SUM of each sample's delay in seconds, so
// an average of 80ms over 1000 new samples adds 1000 * 0.08s = 80s total.
const MEDIA_PLAYOUT_BASELINE = { type: "media-playout", totalPlayoutDelay: 1.0, totalSamplesCount: 1000 };
const MEDIA_PLAYOUT_FINAL = { type: "media-playout", totalPlayoutDelay: 81.0, totalSamplesCount: 2000 };

function inboundRtp(totalSamplesReceived: number, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: "inbound-rtp", kind: "audio", totalSamplesReceived, ...extra };
}

/** Builds a `getStats` double that returns each report in order, then repeats the last. */
function statsSequence(reports: readonly RTCStatsReport[]): StatsProvider {
  let index = 0;
  return () =>
    Promise.resolve(reports[Math.min(index++, reports.length - 1)]);
}

/** Flushes the microtask queue so a pending `getStats()` promise settles. */
async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

/** Asserts exactly one sample was captured and returns it (noUncheckedIndexedAccess-safe). */
function onlySample(samples: readonly (readonly [string, LatencySample])[]): readonly [string, LatencySample] {
  expect(samples).toHaveLength(1);
  const sample = samples.at(0);
  if (sample === undefined) {
    throw new Error("expected one sample");
  }
  return sample;
}

describe("LatencySampleTracker", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("computes browser_playout_ms from the media-playout entry when present", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(100)]),
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(100)]),
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(500)]),
      reportOf([MEDIA_PLAYOUT_FINAL, inboundRtp(500)]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-1");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(samples).toHaveLength(1);
    const first = samples.at(0);
    if (first === undefined) {
      throw new Error("expected one sample");
    }
    const [turnId, sample] = first;
    expect(turnId).toBe("turn-1");
    // (81.0 - 1.0) / (2000 - 1000) * 1000 = 80ms
    expect(sample.browserPlayoutMs).toBe(80);
    expect(sample.networkOneWayMs).toBeNull();
  });

  it("falls back to inbound-rtp jitterBufferDelay when media-playout is absent", async () => {
    const samples: Array<[string, LatencySample]> = [];
    // Same cumulative-sum units as `totalPlayoutDelay`: 60ms over 1000 new
    // samples adds 1000 * 0.06s = 60s total.
    const baselineInbound = inboundRtp(100, { jitterBufferDelay: 2.0, jitterBufferEmittedCount: 1000 });
    const risingInbound = inboundRtp(500, { jitterBufferDelay: 2.0, jitterBufferEmittedCount: 1000 });
    const finalInbound = inboundRtp(500, { jitterBufferDelay: 62.0, jitterBufferEmittedCount: 2000 });
    const getStats = statsSequence([
      reportOf([baselineInbound]),
      reportOf([baselineInbound]),
      reportOf([risingInbound]),
      reportOf([finalInbound]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-2");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(samples).toHaveLength(1);
    // (2.06 - 2.0) / (2000 - 1000) * 1000 = 60ms
    expect(onlySample(samples)[1].browserPlayoutMs).toBe(60);
  });

  it("computes network_one_way_ms from the selected candidate pair", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const transport = { type: "transport", selectedCandidatePairId: "pair-1" };
    const pair = { type: "candidate-pair", id: "pair-1", currentRoundTripTime: 0.08, state: "succeeded", nominated: true };
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline, transport, pair]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline, transport, pair]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising, transport, pair]),
      reportOf([MEDIA_PLAYOUT_FINAL, rising, transport, pair]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-3");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(samples).toHaveLength(1);
    // round(0.08 * 1000 / 2) = 40ms
    expect(onlySample(samples)[1].networkOneWayMs).toBe(40);
  });

  it("falls back to a succeeded+nominated candidate pair when no transport entry is selected", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const pair = { type: "candidate-pair", id: "pair-2", currentRoundTripTime: 0.02, state: "succeeded", nominated: true };
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline, pair]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline, pair]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising, pair]),
      reportOf([MEDIA_PLAYOUT_FINAL, rising, pair]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-4");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(onlySample(samples)[1].networkOneWayMs).toBe(10);
  });

  it("omits network_one_way_ms when no RTT estimate is available", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]),
      reportOf([MEDIA_PLAYOUT_FINAL, rising]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-5");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(samples).toHaveLength(1);
    expect(onlySample(samples)[1].networkOneWayMs).toBeNull();
  });

  it("sends nothing when stats disappear at the final snapshot", async () => {
    const send = vi.fn();
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    let callIndex = 0;
    const reports = [
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]),
    ];
    const getStats: StatsProvider = () => Promise.resolve(reports[Math.min(callIndex++, reports.length - 1)] ?? undefined);
    const tracker = new LatencySampleTracker(getStats, send);

    tracker.onPlaybackStarted("turn-final-gone");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(send).not.toHaveBeenCalled();
  });

  it("sends nothing when the final snapshot cannot compute a delta (deltaCount <= 0)", async () => {
    const send = vi.fn();
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    // The final snapshot's media-playout sample count did not advance past
    // the baseline, so neither computation path yields a usable delta.
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]),
    ]);
    const tracker = new LatencySampleTracker(getStats, send);

    tracker.onPlaybackStarted("turn-no-delta");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(send).not.toHaveBeenCalled();
  });

  it("cancels a pending settle wait when a new turn starts before it completes", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]), // turn-x baseline
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]), // turn-x poll 1 (no change)
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]), // turn-x poll 2 (playing detected, settle scheduled)
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-x");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    // A new turn starts before the 1s settle wait for turn-x elapses.
    tracker.onPlaybackStarted("turn-y");
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS * 2);

    // turn-x's stale settle timer must never fire and must not produce a
    // sample; turn-y never saw its own baseline growth so it also sends
    // nothing, but no crash or cross-turn sample occurs either.
    expect(samples.some(([turnId]) => turnId === "turn-x")).toBe(false);
  });

  it("sends nothing when no playout stat is available at all", async () => {
    const send = vi.fn();
    // Only ever an outbound/video stat: never a usable inbound-rtp audio or
    // media-playout entry, so "playing" is never detected and nothing sends.
    const getStats: StatsProvider = () => Promise.resolve(reportOf([{ type: "outbound-rtp", kind: "audio" }]));
    const tracker = new LatencySampleTracker(getStats, send);

    tracker.onPlaybackStarted("turn-6");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS * 5);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(send).not.toHaveBeenCalled();
    tracker.dispose();
  });

  it("sends exactly once per turn_id even when invoked multiple times", async () => {
    const samples: Array<[string, LatencySample]> = [];
    const baseline = inboundRtp(100);
    const rising = inboundRtp(500);
    const getStats = statsSequence([
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, baseline]),
      reportOf([MEDIA_PLAYOUT_BASELINE, rising]),
      reportOf([MEDIA_PLAYOUT_FINAL, rising]),
    ]);
    const tracker = new LatencySampleTracker(getStats, (turnId, sample) => samples.push([turnId, sample]));

    tracker.onPlaybackStarted("turn-7");
    tracker.onPlaybackStarted("turn-7");
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    tracker.onPlaybackStarted("turn-7");
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);
    tracker.onPlaybackStarted("turn-7");
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS * 3);

    expect(samples).toHaveLength(1);
  });

  it("never samples a turn with no turn_id (the greeting)", async () => {
    const send = vi.fn();
    const getStats: StatsProvider = () => Promise.resolve(undefined);
    const tracker = new LatencySampleTracker(getStats, send);

    tracker.onPlaybackStarted(null);
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS * 5);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    expect(send).not.toHaveBeenCalled();
  });

  it("produces the exact wire payload shape expected by the backend", () => {
    // This mirrors the envelope the session controller builds around a sample
    // (frontend/src/session/controller.ts `emitLatencySample`).
    const sample: LatencySample = { browserPlayoutMs: 80, networkOneWayMs: 40 };
    const payload =
      sample.networkOneWayMs === null
        ? { browser_playout_ms: sample.browserPlayoutMs }
        : { browser_playout_ms: sample.browserPlayoutMs, network_one_way_ms: sample.networkOneWayMs };
    const body = {
      schema_version: 1,
      event_id: "11111111-1111-1111-1111-111111111111",
      session_id: "22222222-2222-2222-2222-222222222222",
      turn_id: "33333333-3333-3333-3333-333333333333",
      event_type: "client.latency_sample",
      occurred_at: "2026-10-06T00:00:00.000Z",
      payload,
    };

    expect(JSON.stringify(body)).toBe(
      '{"schema_version":1,"event_id":"11111111-1111-1111-1111-111111111111","session_id":"22222222-2222-2222-2222-222222222222","turn_id":"33333333-3333-3333-3333-333333333333","event_type":"client.latency_sample","occurred_at":"2026-10-06T00:00:00.000Z","payload":{"browser_playout_ms":80,"network_one_way_ms":40}}',
    );
  });
});
