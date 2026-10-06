import { describe, expect, it, vi, type Mock } from "vitest";
import { ApiError, type ControlApiClient } from "../../src/api";
import type { MicrophoneResult } from "../../src/audio/microphone";
import type { CreateSessionResult } from "../../src/contracts/sessionApi";
import type { TransportHandlers } from "../../src/livekit/transport";
import { VoiceSessionController, type TransportPort } from "../../src/session/controller";

const TOKEN = "eyJ.controller-secret-token.sig";
const ACTIVE_REPORT = {
  statuses: {
    echoCancellation: "active",
    noiseSuppression: "active",
    autoGainControl: "active",
    channelCount: "active",
  },
  degraded: [],
} as const;

function created(overrides: Partial<CreateSessionResult["transport"]> = {}): CreateSessionResult {
  return {
    idempotentReplay: false,
    session: { sessionId: "sess-1", status: "connecting", agentActivityState: null, maximumSessionMs: 1000 },
    transport: {
      provider: "livekit",
      url: "ws://127.0.0.1:7880",
      roomName: "room",
      participantIdentity: "br",
      joinToken: TOKEN,
      tokenExpiresAt: null,
      ...overrides,
    },
    configuration: { name: "Default", version: 1, stt: "s", conversationEngine: "c", tts: "t" },
    requestId: "r",
  };
}

function fakeTrack(): MediaStreamTrack {
  return Object.assign(new EventTarget(), { stop: vi.fn() }) as unknown as MediaStreamTrack;
}

function setup(options: { mic?: MicrophoneResult; api?: Partial<ControlApiClient> } = {}) {
  const track = fakeTrack();
  const handlersRef: { current: TransportHandlers | null } = { current: null };
  const transport: Record<keyof TransportPort, Mock> = {
    connect: vi.fn().mockResolvedValue(undefined),
    publishMicrophone: vi.fn().mockResolvedValue(undefined),
    setMicMuted: vi.fn().mockResolvedValue(undefined),
    startAudio: vi.fn().mockResolvedValue(undefined),
    sendClientEvent: vi.fn().mockResolvedValue("allow"),
    getAgentAudioStats: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(undefined),
  };
  const api: ControlApiClient = {
    listAgentConfigs: vi.fn().mockResolvedValue([]),
    createSession: vi.fn().mockResolvedValue(created()),
    refreshJoinToken: vi.fn().mockResolvedValue({ sessionId: "sess-1", transport: created().transport, idempotentReplay: false }),
    endSession: vi.fn().mockResolvedValue({ sessionId: "sess-1", status: "ending", disconnectReason: "user_ended", idempotentReplay: false }),
    getSession: vi.fn().mockResolvedValue({ sessionId: "sess-1", status: "active", agentActivityState: "listening", disconnectReason: null }),
    listEvents: vi.fn().mockResolvedValue([]),
    listOperations: vi.fn().mockResolvedValue([]),
    getCosts: vi.fn().mockRejectedValue(new Error("not ready")),
    listErrors: vi.fn().mockResolvedValue([]),
    ...options.api,
  };
  let counter = 0;
  const controller = new VoiceSessionController({
    api,
    createTransport: (handlers) => {
      handlersRef.current = handlers;
      return transport;
    },
    acquireMicrophone: () => Promise.resolve(options.mic ?? { ok: true, track, report: ACTIVE_REPORT }),
    newId: () => `id-${String(counter++)}`,
    nowIso: () => "2026-09-29T10:00:00Z",
    sleep: () => Promise.resolve(),
  });
  const handlers = (): TransportHandlers => {
    if (handlersRef.current === null) {
      throw new Error("transport not created");
    }
    return handlersRef.current;
  };
  return { controller, api, transport, track, handlers };
}

describe("VoiceSessionController", () => {
  it("starts: mic first, creates a session, connects with the API token, publishes the mic", async () => {
    const { controller, api, transport, track } = setup();

    await controller.start("cfg-1");

    expect(api.createSession).toHaveBeenCalledWith({ agentConfigId: "cfg-1", clientRequestId: "id-0" });
    expect(transport.connect).toHaveBeenCalledWith("ws://127.0.0.1:7880", TOKEN);
    expect(transport.publishMicrophone).toHaveBeenCalledWith(track);
    expect(controller.getState().phase).toBe("live");
    expect(controller.getState().mic.report).toEqual(ACTIVE_REPORT);
    expect(transport.sendClientEvent).toHaveBeenCalledWith(expect.objectContaining({ eventType: "client.ready" }));
  });

  it("never keeps the join token in view state", async () => {
    const { controller } = setup();

    await controller.start("cfg-1");

    expect(JSON.stringify(controller.getState())).not.toContain(TOKEN);
  });

  it("does not create a session when microphone permission is denied", async () => {
    const { controller, api } = setup({
      mic: { ok: false, error: { kind: "permission_denied", message: "Microphone access was blocked." } },
    });

    await controller.start("cfg-1");

    const state = controller.getState();
    expect(api.createSession).not.toHaveBeenCalled();
    expect(state.phase).toBe("failed");
    expect(state.mic.status).toBe("denied");
    expect(state.error?.message).toMatch(/blocked/);
  });

  it("retries a retryable create failure once with the same idempotency id", async () => {
    const createSession = vi
      .fn()
      .mockRejectedValueOnce(new ApiError({ code: "NETWORK_ERROR", message: "down", status: null, retryable: true }))
      .mockResolvedValueOnce(created());
    const { controller } = setup({ api: { createSession } });

    await controller.start("cfg-1");

    expect(createSession).toHaveBeenCalledTimes(2);
    expect(createSession.mock.calls[0]?.[0]).toEqual(createSession.mock.calls[1]?.[0]);
    expect(controller.getState().phase).toBe("live");
  });

  it("fails with a visible error and no leak when create is rejected", async () => {
    const createSession = vi
      .fn()
      .mockRejectedValue(new ApiError({ code: "PROVIDER_UNAVAILABLE", message: "A required provider is unavailable.", status: 502, retryable: false }));
    const { controller, transport, track } = setup({ api: { createSession } });

    await controller.start("cfg-1");

    const state = controller.getState();
    expect(state.phase).toBe("failed");
    expect(state.error).toEqual({ message: "A required provider is unavailable.", retryable: false });
    expect(transport.connect).not.toHaveBeenCalled();
    expect((track.stop as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
  });

  it("rejects a response without usable join credentials", async () => {
    const { controller, transport } = setup({
      api: { createSession: vi.fn().mockResolvedValue(created({ joinToken: null })) },
    });

    await controller.start("cfg-1");

    expect(transport.connect).not.toHaveBeenCalled();
    expect(controller.getState().phase).toBe("failed");
  });

  it("ends the failed session best-effort when the transport cannot connect", async () => {
    const { controller, api, transport } = setup();
    transport.connect.mockRejectedValue(Object.assign(new Error("The voice connection could not be established."), { name: "TransportError" }));

    await controller.start("cfg-1");

    expect(api.endSession).toHaveBeenCalledWith("sess-1", "transport_error");
    expect(controller.getState().phase).toBe("failed");
    expect(controller.getState().error?.message).toMatch(/could not be established/);
  });

  it("stops: end request first, then disconnect, release mic, load timeline", async () => {
    const order: string[] = [];
    const { controller, api, transport, track } = setup();
    vi.mocked(api.endSession).mockImplementation(() => {
      order.push("end");
      return Promise.resolve({ sessionId: "sess-1", status: "ending", disconnectReason: "user_ended", idempotentReplay: false });
    });
    transport.disconnect.mockImplementation(() => {
      order.push("disconnect");
      return Promise.resolve();
    });
    vi.mocked(api.listEvents).mockResolvedValue([
      { eventId: "e1", eventType: "session.ended", severity: "info", sequenceNumber: 1, occurredAt: "t" },
    ]);
    await controller.start("cfg-1");

    await controller.stop();

    expect(order).toEqual(["end", "disconnect"]);
    expect(api.endSession).toHaveBeenCalledWith("sess-1", "user_ended", "id-2");
    expect((track.stop as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
    expect(controller.getState().phase).toBe("ended");
    expect(controller.getState().events).toHaveLength(1);
  });

  it("still cleans up media and reports failure when the end call fails", async () => {
    const { controller, api, transport } = setup();
    vi.mocked(api.endSession).mockRejectedValue(new ApiError({ code: "INTERNAL_ERROR", message: "An internal error occurred.", status: 500, retryable: false }));
    await controller.start("cfg-1");

    await controller.stop();

    expect(transport.disconnect).toHaveBeenCalled();
    expect(controller.getState().phase).toBe("failed");
  });

  it("ignores duplicate stop calls", async () => {
    const { controller, api } = setup();
    await controller.start("cfg-1");

    await Promise.all([controller.stop(), controller.stop()]);

    expect(api.endSession).toHaveBeenCalledTimes(1);
  });

  it("mutes locally and sends client.mic_muted / client.mic_unmuted", async () => {
    const { controller, transport } = setup();
    await controller.start("cfg-1");

    await controller.setMuted(true);
    await controller.setMuted(false);

    expect(transport.setMicMuted).toHaveBeenNthCalledWith(1, true);
    expect(transport.setMicMuted).toHaveBeenNthCalledWith(2, false);
    const types = transport.sendClientEvent.mock.calls.map((call) => (call[0] as { eventType: string }).eventType);
    expect(types).toEqual(["client.ready", "client.mic_muted", "client.mic_unmuted"]);
    expect(controller.getState().mic.muted).toBe(false);
  });

  it("surfaces a mute failure instead of pretending the mic is muted", async () => {
    const { controller, transport } = setup();
    await controller.start("cfg-1");
    transport.setMicMuted.mockRejectedValue(new Error("nope"));

    await controller.setMuted(true);

    expect(controller.getState().mic.muted).toBe(false);
    expect(controller.getState().mic.status).toBe("error");
  });

  it("shows reconnecting and recovering on SDK reconnect, then reloads durable state", async () => {
    const { controller, api, handlers } = setup();
    await controller.start("cfg-1");

    handlers().onState("reconnecting", null);
    expect(controller.getState().phase).toBe("reconnecting");
    expect(controller.getState().agentState).toBe("recovering");

    handlers().onState("connected", null);
    await vi.waitFor(() => {
      expect(controller.getState().agentState).toBe("listening");
    });
    expect(api.getSession).toHaveBeenCalledWith("sess-1");
    expect(controller.getState().phase).toBe("live");
  });

  it("recovers from a server disconnect with a refreshed token and the same session", async () => {
    const { controller, api, transport, handlers } = setup();
    await controller.start("cfg-1");

    handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("live");
    });

    expect(api.refreshJoinToken).toHaveBeenCalledWith("sess-1", expect.any(String));
    expect(transport.connect).toHaveBeenCalledTimes(2);
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it("fails terminally when recovery is exhausted", async () => {
    const refreshJoinToken = vi
      .fn()
      .mockRejectedValue(new ApiError({ code: "NETWORK_ERROR", message: "down", status: null, retryable: true }));
    const { controller, handlers } = setup({ api: { refreshJoinToken } });
    await controller.start("cfg-1");

    handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("failed");
    });

    expect(refreshJoinToken.mock.calls.length).toBeGreaterThan(1);
    expect(controller.getState().error?.message).toMatch(/could not be restored/);
  });

  it("stops recovering when the session is terminal (non-retryable refresh error)", async () => {
    const refreshJoinToken = vi
      .fn()
      .mockRejectedValue(new ApiError({ code: "INVALID_STATE", message: "The resource state does not allow this operation.", status: 409, retryable: false }));
    const { controller, handlers } = setup({ api: { refreshJoinToken } });
    await controller.start("cfg-1");

    handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("failed");
    });

    expect(refreshJoinToken).toHaveBeenCalledTimes(1);
  });

  it("fails immediately when the SDK reports the reconnect window exhausted", async () => {
    const { controller, api, handlers } = setup();
    await controller.start("cfg-1");

    handlers().onState("disconnected", "reconnect_exhausted");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("failed");
    });

    expect(api.endSession).toHaveBeenCalledWith("sess-1", "transport_error");
  });

  it("does not run recovery for a disconnect caused by our own stop", async () => {
    const { controller, api, handlers } = setup();
    await controller.start("cfg-1");

    await controller.stop();
    handlers().onState("disconnected", "server");

    expect(api.refreshJoinToken).not.toHaveBeenCalled();
  });

  it("clears a stale speaking state when the agent participant disappears", async () => {
    const { controller, handlers } = setup();
    await controller.start("cfg-1");
    handlers().onMessage({
      topic: "va.state.v1",
      envelope: { eventId: "e", sessionId: "s", turnId: null, eventType: "x", sequenceNumber: 1, occurredAt: "t", payload: {} },
      state: "speaking",
    });

    handlers().onAgentPresence(false);

    expect(controller.getState().agentState).toBe("recovering");
  });

  it("enables audio through the transport on a user gesture", async () => {
    const { controller, transport } = setup();
    await controller.start("cfg-1");

    await controller.enableAudio();

    expect(transport.startAudio).toHaveBeenCalled();
  });

  it("allows starting a new session after the previous one ended", async () => {
    const { controller, api } = setup();
    await controller.start("cfg-1");
    await controller.stop();

    await controller.start("cfg-1");

    expect(api.createSession).toHaveBeenCalledTimes(2);
    expect(controller.getState().phase).toBe("live");
  });
});
