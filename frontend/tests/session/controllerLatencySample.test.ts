import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Envelope, InboundMessage } from "../../src/contracts/realtime";
import { LATENCY_POLL_INTERVAL_MS, LATENCY_SETTLE_DELAY_MS } from "../../src/session/latencySamples";
import { VoiceSessionController } from "../../src/session/controller";
import { fakeDeps, type FakeDeps } from "../support/fakeDeps";

function envelope(turnId: string | null): Envelope {
  return { eventId: "e", sessionId: "s", turnId, eventType: "x", sequenceNumber: null, occurredAt: "t", payload: {} };
}

const IDENTITY = { workerGeneration: 1, cancellationGeneration: 0, segmentId: "seg-1" };

function started(turnId: string | null): InboundMessage {
  return { topic: "va.playback.v1", envelope: envelope(turnId), state: "started", identity: IDENTITY };
}

function reportOf(entries: readonly Record<string, unknown>[]): RTCStatsReport {
  return {
    forEach: (callback: (value: unknown) => void) => {
      entries.forEach((entry) => {
        callback(entry);
      });
    },
  } as unknown as RTCStatsReport;
}

function inboundRtp(totalSamplesReceived: number): Record<string, unknown> {
  return { type: "inbound-rtp", kind: "audio", totalSamplesReceived };
}

const MEDIA_PLAYOUT_BASELINE = { type: "media-playout", totalPlayoutDelay: 1.0, totalSamplesCount: 1000 };
const MEDIA_PLAYOUT_FINAL = { type: "media-playout", totalPlayoutDelay: 81.0, totalSamplesCount: 2000 };

async function live(fake: FakeDeps): Promise<VoiceSessionController> {
  const controller = new VoiceSessionController({ ...fake.deps, nowMs: () => 0 });
  await controller.start("cfg-1");
  return controller;
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("client.latency_sample", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("sends exactly one composed latency sample per turn, with the envelope turn_id", async () => {
    const fake = fakeDeps();
    let callIndex = 0;
    const reports = [
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(100)]),
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(100)]),
      reportOf([MEDIA_PLAYOUT_BASELINE, inboundRtp(500)]),
      reportOf([MEDIA_PLAYOUT_FINAL, inboundRtp(500)]),
    ];
    vi.mocked(fake.transport.getAgentAudioStats).mockImplementation(() =>
      Promise.resolve(reports[Math.min(callIndex++, reports.length - 1)]),
    );
    const controller = await live(fake);

    fake.handlers().onMessage(started("turn-abc"));
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    const calls = vi.mocked(fake.transport.sendClientEvent).mock.calls.filter(
      (c) => c[0].eventType === "client.latency_sample",
    );
    expect(calls).toHaveLength(1);
    const matched = calls.at(0);
    if (matched === undefined) {
      throw new Error("expected one client.latency_sample call");
    }
    const [input] = matched;
    expect(input.turnId).toBe("turn-abc");
    expect(input.payload).toEqual({ browser_playout_ms: 80 });

    await controller.stop();
  });

  it("never sends a sample for a turn with no turn_id (the greeting)", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.transport.getAgentAudioStats).mockResolvedValue(undefined);
    await live(fake);

    fake.handlers().onMessage(started(null));
    await flush();
    await vi.advanceTimersByTimeAsync(LATENCY_POLL_INTERVAL_MS * 5);
    await vi.advanceTimersByTimeAsync(LATENCY_SETTLE_DELAY_MS);

    const calls = vi.mocked(fake.transport.sendClientEvent).mock.calls.filter(
      (c) => c[0].eventType === "client.latency_sample",
    );
    expect(calls).toHaveLength(0);
  });
});
