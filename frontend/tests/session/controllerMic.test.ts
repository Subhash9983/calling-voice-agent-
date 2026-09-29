import { describe, expect, it, vi } from "vitest";
import type { MicrophoneResult } from "../../src/audio/microphone";
import { VoiceSessionController } from "../../src/session/controller";
import { ACTIVE_REPORT, fakeDeps, type FakeDeps } from "../support/fakeDeps";

const LIVE: MediaStreamTrackState = "live";

interface FakeTrack extends MediaStreamTrack {
  readyState: MediaStreamTrackState;
}

function track(): FakeTrack {
  return Object.assign(new EventTarget(), {
    readyState: LIVE,
    stop: vi.fn(),
  }) as unknown as FakeTrack;
}

function ok(t: MediaStreamTrack): MicrophoneResult {
  return { ok: true, track: t, report: ACTIVE_REPORT };
}

const NO_MIC: MicrophoneResult = {
  ok: false,
  error: { kind: "no_device", message: "No microphone was found. Connect one and try again." },
};

async function started(results: readonly MicrophoneResult[]): Promise<{
  fake: FakeDeps;
  controller: VoiceSessionController;
  acquire: ReturnType<typeof vi.fn<() => Promise<MicrophoneResult>>>;
}> {
  const fake = fakeDeps();
  const queue = [...results];
  const acquire = vi.fn<() => Promise<MicrophoneResult>>(() => {
    const next = queue.shift();
    return Promise.resolve(next ?? NO_MIC);
  });
  const controller = new VoiceSessionController({ ...fake.deps, acquireMicrophone: acquire });
  await controller.start("cfg-1");
  return { fake, controller, acquire };
}

describe("microphone after app-level recovery", () => {
  it("re-acquires the microphone when the SDK stopped the track on disconnect", async () => {
    const first = track();
    const second = track();
    const { fake, controller, acquire } = await started([ok(first), ok(second)]);
    vi.mocked(fake.api.refreshJoinToken).mockResolvedValue({
      sessionId: "sess-1",
      transport: { provider: "livekit", url: "ws://x", roomName: null, participantIdentity: null, joinToken: "t", tokenExpiresAt: null },
      idempotentReplay: false,
    });
    first.readyState = "ended";

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("live");
    });

    expect(acquire).toHaveBeenCalledTimes(2);
    expect(fake.transport.publishMicrophone).toHaveBeenLastCalledWith(second);
  });

  it("reuses a still-live track without prompting again", async () => {
    const first = track();
    const { fake, controller, acquire } = await started([ok(first)]);
    vi.mocked(fake.api.refreshJoinToken).mockResolvedValue({
      sessionId: "sess-1",
      transport: { provider: "livekit", url: "ws://x", roomName: null, participantIdentity: null, joinToken: "t", tokenExpiresAt: null },
      idempotentReplay: false,
    });

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("live");
    });

    expect(acquire).toHaveBeenCalledTimes(1);
    expect(fake.transport.publishMicrophone).toHaveBeenLastCalledWith(first);
  });

  it("fails visibly (no silent session) when the microphone cannot be re-acquired", async () => {
    const first = track();
    const { fake, controller } = await started([ok(first), NO_MIC]);
    vi.mocked(fake.api.refreshJoinToken).mockResolvedValue({
      sessionId: "sess-1",
      transport: { provider: "livekit", url: "ws://x", roomName: null, participantIdentity: null, joinToken: "t", tokenExpiresAt: null },
      idempotentReplay: false,
    });
    first.readyState = "ended";

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("failed");
    });

    expect(controller.getState().mic.status).toBe("error");
  });
});

describe("microphone device loss", () => {
  it("re-acquires once and republishes when the device comes back", async () => {
    const first = track();
    const second = track();
    const { fake, controller } = await started([ok(first), ok(second)]);

    first.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => {
      expect(fake.transport.publishMicrophone).toHaveBeenLastCalledWith(second);
    });

    expect(controller.getState().mic.status).toBe("active");
    expect(controller.getState().phase).toBe("live");
    expect(fake.api.endSession).not.toHaveBeenCalled();
  });

  it("shows the alert and ends the session cleanly when re-acquisition fails", async () => {
    const first = track();
    const { fake, controller } = await started([ok(first), NO_MIC]);

    first.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });

    expect(controller.getState().mic.status).toBe("lost");
    expect(controller.getState().mic.message).toMatch(/microphone/i);
    expect(fake.api.endSession).toHaveBeenCalledWith("sess-1", "transport_error", expect.any(String));
    expect(fake.transport.disconnect).toHaveBeenCalled();
  });
});

describe("agent-ended session", () => {
  it("shows ended and posts no end request when the durable session is already terminal", async () => {
    const { fake, controller } = await started([ok(track())]);
    vi.mocked(fake.api.getSession).mockResolvedValue({
      sessionId: "sess-1",
      status: "ended",
      agentActivityState: null,
      disconnectReason: "user_ended",
    });

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });

    expect(fake.api.endSession).not.toHaveBeenCalled();
    expect(fake.api.refreshJoinToken).not.toHaveBeenCalled();
    expect(controller.getState().error).toBeNull();
  });

  it("does not post end for a terminal session when the refresh is refused", async () => {
    const { fake, controller } = await started([ok(track())]);
    const { ApiError } = await import("../../src/api");
    vi.mocked(fake.api.refreshJoinToken).mockRejectedValue(
      new ApiError({ code: "INVALID_STATE", message: "no", status: 409, retryable: false }),
    );
    vi.mocked(fake.api.getSession)
      .mockResolvedValueOnce({ sessionId: "sess-1", status: "active", agentActivityState: null, disconnectReason: null })
      .mockResolvedValue({ sessionId: "sess-1", status: "ended", agentActivityState: null, disconnectReason: null });

    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });

    expect(fake.api.endSession).not.toHaveBeenCalled();
  });
});
