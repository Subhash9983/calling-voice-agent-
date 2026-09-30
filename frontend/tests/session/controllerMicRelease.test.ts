import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../src/api";
import { VoiceSessionController } from "../../src/session/controller";
import { CREATED, fakeDeps, type FakeDeps } from "../support/fakeDeps";

async function live(): Promise<{ fake: FakeDeps; controller: VoiceSessionController }> {
  const fake = fakeDeps();
  const controller = new VoiceSessionController(fake.deps);
  await controller.start("cfg-1");
  expect(controller.getState().mic.status).toBe("active");
  return { fake, controller };
}

function expectReleased(fake: FakeDeps, controller: VoiceSessionController): void {
  expect(fake.track.stop).toHaveBeenCalled();
  expect(controller.getState().mic.status).toBe("released");
  expect(controller.getState().mic.muted).toBe(false);
}

describe("microphone release on every end path", () => {
  it("stops the track and shows released after the user ends the session", async () => {
    const { fake, controller } = await live();
    await controller.stop("user_ended");
    expectReleased(fake, controller);
    expect(controller.getState().phase).toBe("ended");
  });

  it("releases the microphone before the durable end request completes", async () => {
    const { fake, controller } = await live();
    let releasedWhenEndCalled = false;
    vi.mocked(fake.api.endSession).mockImplementation(() => {
      releasedWhenEndCalled = vi.mocked(fake.track.stop).mock.calls.length > 0;
      return Promise.resolve({ sessionId: "sess-1", status: "ending", disconnectReason: "user_ended", idempotentReplay: false });
    });
    await controller.stop("user_ended");
    expect(releasedWhenEndCalled).toBe(true);
  });

  it("releases the microphone when ending fails", async () => {
    const { fake, controller } = await live();
    vi.mocked(fake.api.endSession).mockRejectedValue(
      new ApiError({ code: "X", message: "nope", status: 500, retryable: false }),
    );
    await controller.stop("user_ended");
    expectReleased(fake, controller);
    expect(controller.getState().phase).toBe("failed");
  });

  it("releases the microphone on browser_closed (page hide)", async () => {
    const { fake, controller } = await live();
    await controller.stop("browser_closed");
    expectReleased(fake, controller);
  });

  it("releases the microphone when the agent already ended the session", async () => {
    const { fake, controller } = await live();
    vi.mocked(fake.api.getSession).mockResolvedValue({ sessionId: "sess-1", status: "ended", agentActivityState: null, disconnectReason: "server_shutdown" });
    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });
    expectReleased(fake, controller);
  });

  it("releases the microphone when the transport fails to connect", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.transport.connect).mockRejectedValue(new Error("boom"));
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    expect(controller.getState().phase).toBe("failed");
    expectReleased(fake, controller);
  });

  it("releases the microphone when the page hides before a session id exists, then ends the late session", async () => {
    const fake = fakeDeps();
    const gate: { release: (() => void) | null } = { release: null };
    vi.mocked(fake.api.createSession).mockImplementation(
      () =>
        new Promise((resolve) => {
          gate.release = () => {
            resolve(CREATED);
          };
        }),
    );
    const controller = new VoiceSessionController(fake.deps);
    const started = controller.start("cfg-1");
    await vi.waitFor(() => {
      expect(gate.release).not.toBeNull();
    });
    await controller.stop("browser_closed");
    expectReleased(fake, controller);

    gate.release?.();
    await started;

    expect(fake.api.endSession).toHaveBeenCalledWith("sess-1", "browser_closed", expect.any(String));
    expect(fake.transport.connect).not.toHaveBeenCalled();
    expect(controller.getState().mic.status).toBe("released");
  });
});
