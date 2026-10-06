import { vi } from "vitest";
import { ApiError, type ControlApiClient } from "../../src/api";
import type { MicrophoneResult } from "../../src/audio/microphone";
import type { CreateSessionResult } from "../../src/contracts/sessionApi";
import type { TransportHandlers } from "../../src/livekit/transport";
import type { ControllerDeps, TransportPort } from "../../src/session/controller";

export const ACTIVE_REPORT = {
  statuses: {
    echoCancellation: "active",
    noiseSuppression: "active",
    autoGainControl: "active",
    channelCount: "active",
  },
  degraded: [],
} as const;

export const CREATED: CreateSessionResult = {
  idempotentReplay: false,
  session: { sessionId: "sess-1", status: "connecting", agentActivityState: null, maximumSessionMs: 1000 },
  transport: {
    provider: "livekit",
    url: "ws://127.0.0.1:7880",
    roomName: "room",
    participantIdentity: "br",
    joinToken: "eyJ.ui-test-token.sig",
    tokenExpiresAt: null,
  },
  configuration: { name: "Default", version: 1, stt: "stt-x", conversationEngine: "llm-y", tts: "tts-z" },
  requestId: "r",
};

export interface FakeDeps {
  readonly deps: ControllerDeps;
  readonly api: ControlApiClient;
  readonly transport: TransportPort;
  readonly track: MediaStreamTrack;
  readonly handlers: () => TransportHandlers;
}

export function fakeDeps(mic?: MicrophoneResult): FakeDeps {
  const track = Object.assign(new EventTarget(), { stop: vi.fn() }) as unknown as MediaStreamTrack;
  const ref: { current: TransportHandlers | null } = { current: null };
  const transport: TransportPort = {
    connect: vi.fn().mockResolvedValue(undefined),
    publishMicrophone: vi.fn().mockResolvedValue(undefined),
    setMicMuted: vi.fn().mockResolvedValue(undefined),
    startAudio: vi.fn().mockResolvedValue(undefined),
    sendClientEvent: vi.fn().mockResolvedValue("allow"),
    getAgentAudioStats: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(undefined),
  };
  const api: ControlApiClient = {
    listAgentConfigs: vi.fn().mockResolvedValue([
      { agentConfigId: "cfg-1", name: "Default agent", version: 1, description: null },
    ]),
    createSession: vi.fn().mockResolvedValue(CREATED),
    refreshJoinToken: vi.fn(),
    endSession: vi.fn().mockResolvedValue({ sessionId: "sess-1", status: "ending", disconnectReason: "user_ended", idempotentReplay: false }),
    getSession: vi.fn().mockResolvedValue({ sessionId: "sess-1", status: "active", agentActivityState: "listening", disconnectReason: null }),
    listEvents: vi.fn().mockResolvedValue([]),
    listOperations: vi.fn().mockResolvedValue([]),
    getCosts: vi.fn().mockRejectedValue(
      new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "not ready", status: 503, retryable: false }),
    ),
    listErrors: vi.fn().mockResolvedValue([]),
  };
  const deps: ControllerDeps = {
    api,
    createTransport: (handlers) => {
      ref.current = handlers;
      return transport;
    },
    acquireMicrophone: () => Promise.resolve(mic ?? { ok: true, track, report: ACTIVE_REPORT }),
    newId: () => crypto.randomUUID(),
    nowIso: () => "2026-09-29T10:00:00Z",
    sleep: () => Promise.resolve(),
  };
  return {
    deps,
    api,
    transport,
    track,
    handlers: () => {
      if (ref.current === null) {
        throw new Error("transport not created yet");
      }
      return ref.current;
    },
  };
}
