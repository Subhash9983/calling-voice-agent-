import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../src/api";
import { VoiceSessionController } from "../../src/session/controller";
import { fakeDeps, type FakeDeps } from "../support/fakeDeps";

const STT_OP = {
  operationId: "op-1",
  component: "stt",
  provider: "deepgram",
  status: "succeeded",
  usage: [{ unit: "transcribed_audio_seconds", quantity: 7.25 }],
};

async function ended(configure: (fake: FakeDeps) => void): Promise<VoiceSessionController> {
  const fake = fakeDeps();
  configure(fake);
  const controller = new VoiceSessionController(fake.deps);
  await controller.start("cfg-1");
  await controller.stop("user_ended");
  return controller;
}

describe("STT evidence after the session ends", () => {
  it("summarises STT operations and cost", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
      vi.mocked(fake.api.getCosts).mockResolvedValue({
        calculationStatus: "final",
        totalUsd: "0.01",
        components: [{ component: "stt", label: "Deepgram", amountUsd: "0.0031" }],
      });
    });

    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: { count: 1, audioSeconds: 7.25 },
      cost: { sttUsd: "0.0031", calculationStatus: "final" },
    });
  });

  it("asks only for STT operations", async () => {
    const fake = fakeDeps();
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");
    expect(fake.api.listOperations).toHaveBeenCalledWith("sess-1", { component: "stt", limit: 100 });
  });

  it("treats a 503 not-ready costs response as neutral, not an error", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
      vi.mocked(fake.api.getCosts).mockRejectedValue(
        new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "x", status: 503, retryable: false }),
      );
    });

    const state = controller.getState();
    expect(state.evidence).toEqual({ status: "ready", operations: { count: 1, audioSeconds: 7.25 }, cost: null });
    expect(state.phase).toBe("ended");
    expect(state.error).toBeNull();
  });

  it("is neutral when both diagnostics fail", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockRejectedValue(new Error("boom"));
    });
    expect(controller.getState().evidence).toEqual({ status: "ready", operations: null, cost: null });
    expect(controller.getState().error).toBeNull();
  });

  it("loads evidence when the agent ended the session first", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    vi.mocked(fake.api.getSession).mockResolvedValue({ sessionId: "sess-1", status: "ended", agentActivityState: null, disconnectReason: "server_shutdown" });
    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });
    expect(controller.getState().evidence.status).toBe("ready");
  });

  it("clears previous evidence when a new session starts", async () => {
    const fake = fakeDeps();
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");
    expect(controller.getState().evidence.status).toBe("ready");
    await controller.start("cfg-1");
    expect(controller.getState().evidence.status).toBe("idle");
  });
});
