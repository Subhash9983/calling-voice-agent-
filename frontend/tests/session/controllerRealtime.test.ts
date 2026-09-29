import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Envelope, InboundMessage, PlaybackAckIdentity } from "../../src/contracts/realtime";
import { VoiceSessionController } from "../../src/session/controller";
import { PlaybackAckTracker } from "../../src/session/playbackAcks";
import { fakeDeps, type FakeDeps } from "../support/fakeDeps";

const IDENTITY: PlaybackAckIdentity = { workerGeneration: 2, cancellationGeneration: 1, segmentId: "seg-1" };

function envelope(): Envelope {
  return { eventId: "e", sessionId: "s", turnId: null, eventType: "x", sequenceNumber: null, occurredAt: "t", payload: {} };
}

const playback = (state: "started" | "completed" | "cancelled"): InboundMessage => ({
  topic: "va.playback.v1",
  envelope: envelope(),
  state,
  identity: IDENTITY,
});

async function live(fake: FakeDeps, extra: { agentSignalTimeoutMs?: number } = {}): Promise<VoiceSessionController> {
  const controller = new VoiceSessionController({
    ...fake.deps,
    nowMs: () => 0,
    ...extra,
  });
  await controller.start("cfg-1");
  return controller;
}

function sentTypes(fake: FakeDeps): string[] {
  return vi.mocked(fake.transport.sendClientEvent).mock.calls.map((call) => call[0].eventType);
}

describe("playback acknowledgements", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("echoes started, progress every 250 ms, and completed with the announced identity", async () => {
    const fake = fakeDeps();
    const controller = await live(fake);

    fake.handlers().onMessage(playback("started"));
    await vi.advanceTimersByTimeAsync(500);
    fake.handlers().onMessage(playback("completed"));
    await vi.advanceTimersByTimeAsync(1000);

    expect(sentTypes(fake)).toEqual([
      "client.ready",
      "playback.started",
      "playback.progress",
      "playback.progress",
      "playback.completed",
    ]);
    const started = vi.mocked(fake.transport.sendClientEvent).mock.calls[1]?.[0];
    expect(started?.payload).toEqual({
      worker_generation: 2,
      cancellation_generation: 1,
      segment_id: "seg-1",
    });
    await controller.stop();
  });

  it("stops progress without a completion ack when playback is cancelled", async () => {
    const fake = fakeDeps();
    await live(fake);

    fake.handlers().onMessage(playback("started"));
    fake.handlers().onMessage(playback("cancelled"));
    await vi.advanceTimersByTimeAsync(1000);

    expect(sentTypes(fake)).toEqual(["client.ready", "playback.started"]);
  });

  it("sends playback.failed when the audio element errors mid-segment", async () => {
    const fake = fakeDeps();
    await live(fake);

    fake.handlers().onMessage(playback("started"));
    fake.handlers().onAgentAudio({ phase: "failed", receiving: false, audible: false });

    expect(sentTypes(fake)).toContain("playback.failed");
  });

  it("ignores a completed message for a segment that never started", () => {
    const send = vi.fn();
    const tracker = new PlaybackAckTracker(send, () => 0);

    tracker.onPlayback("completed", IDENTITY);
    tracker.onPlayoutFailed();

    expect(send).not.toHaveBeenCalled();
  });

  it("replaces an active segment when a new one starts and disposes cleanly", () => {
    const send = vi.fn();
    const tracker = new PlaybackAckTracker(send, () => 0);

    tracker.onPlayback("started", IDENTITY);
    tracker.onPlayback("started", { ...IDENTITY, segmentId: "seg-2" });
    tracker.dispose();
    vi.advanceTimersByTime(1000);

    expect(send).toHaveBeenCalledTimes(2);
  });
});

describe("agent signal watchdog (5 s)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows recovering after 5 s without any agent signal", async () => {
    const fake = fakeDeps();
    const controller = await live(fake);
    fake.handlers().onMessage({ topic: "va.state.v1", envelope: envelope(), state: "speaking" });

    await vi.advanceTimersByTimeAsync(4999);
    expect(controller.getState().agentState).toBe("speaking");
    await vi.advanceTimersByTimeAsync(2);

    expect(controller.getState().agentState).toBe("recovering");
  });

  it("treats metrics, state and receiving agent audio as signals", async () => {
    const fake = fakeDeps();
    const controller = await live(fake);
    fake.handlers().onMessage({ topic: "va.state.v1", envelope: envelope(), state: "speaking" });

    await vi.advanceTimersByTimeAsync(4000);
    fake.handlers().onMessage({ topic: "va.metrics.v1", envelope: envelope() });
    await vi.advanceTimersByTimeAsync(4000);
    fake.handlers().onAgentAudio({ phase: "playing", receiving: true, audible: false });
    await vi.advanceTimersByTimeAsync(4000);

    expect(controller.getState().agentState).toBe("speaking");
  });

  it("reloads durable state once a signal returns after the watchdog fired", async () => {
    const fake = fakeDeps();
    const controller = await live(fake);
    await vi.advanceTimersByTimeAsync(5001);
    expect(controller.getState().agentState).toBe("recovering");

    fake.handlers().onMessage({ topic: "va.metrics.v1", envelope: envelope() });
    await vi.advanceTimersByTimeAsync(0);

    expect(fake.api.getSession).toHaveBeenCalled();
    expect(controller.getState().agentState).toBe("listening");
  });

  it("stops watching after the session ends", async () => {
    const fake = fakeDeps();
    const controller = await live(fake);

    await controller.stop();
    await vi.advanceTimersByTimeAsync(10_000);

    expect(controller.getState().agentState).not.toBe("recovering");
  });
});

describe("immediate rejoin", () => {
  it("refreshes the token and rejoins as soon as the SDK reports disconnected, without waiting", async () => {
    const sleeps: number[] = [];
    const fake = fakeDeps();
    const controller = new VoiceSessionController({
      ...fake.deps,
      sleep: (ms) => {
        sleeps.push(ms);
        return Promise.resolve();
      },
    });
    await controller.start("cfg-1");
    vi.mocked(fake.api.refreshJoinToken).mockResolvedValue({
      sessionId: "sess-1",
      transport: { provider: "livekit", url: "ws://x", roomName: null, participantIdentity: null, joinToken: "t", tokenExpiresAt: null },
      idempotentReplay: false,
    });

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("live");
    });

    expect(sleeps[0]).toBe(0);
    expect(fake.api.refreshJoinToken).toHaveBeenCalledTimes(1);
  });
});
