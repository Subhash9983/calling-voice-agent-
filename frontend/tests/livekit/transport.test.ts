import { ConnectionQuality, DataPacket_Kind, Room, RoomEvent, Track } from "livekit-client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentAudioStatus } from "../../src/audio/agentAudio";
import type { InboundMessage } from "../../src/contracts/realtime";
import {
  LiveKitTransport,
  TransportError,
  type DisconnectCause,
  type TransportHandlers,
  type TransportState,
} from "../../src/livekit/transport";

const TOKEN = "eyJ.transport-secret-token.sig";

interface Harness {
  readonly room: Room;
  readonly transport: LiveKitTransport;
  readonly states: { state: TransportState; cause: DisconnectCause | null }[];
  readonly messages: InboundMessage[];
  readonly rejected: string[];
  readonly audio: AgentAudioStatus[];
  readonly presence: boolean[];
  readonly connect: ReturnType<typeof vi.fn>;
  readonly publishData: ReturnType<typeof vi.fn>;
  readonly publishTrack: ReturnType<typeof vi.fn>;
  readonly disconnect: ReturnType<typeof vi.fn>;
  readonly host: HTMLElement;
}

function build(options: { reconnectWindowMs?: number; connectFails?: boolean } = {}): Harness {
  const room = new Room();
  const connect = vi.spyOn(room, "connect");
  if (options.connectFails === true) {
    connect.mockRejectedValue(new Error(`bad token ${TOKEN}`));
  } else {
    connect.mockResolvedValue(undefined);
  }
  const disconnect = vi.spyOn(room, "disconnect").mockResolvedValue(undefined);
  const publishData = vi.spyOn(room.localParticipant, "publishData").mockResolvedValue(undefined);
  const publishTrack = vi
    .spyOn(room.localParticipant, "publishTrack")
    .mockResolvedValue({ track: { mute: vi.fn(), unmute: vi.fn() } } as never);
  const states: Harness["states"] = [];
  const messages: InboundMessage[] = [];
  const rejected: string[] = [];
  const audio: AgentAudioStatus[] = [];
  const presence: boolean[] = [];
  const handlers: TransportHandlers = {
    onState: (state, cause) => states.push({ state, cause }),
    onAgentAudio: (status) => audio.push(status),
    onMessage: (message) => messages.push(message),
    onRejected: (reason) => rejected.push(reason),
    onQuality: () => undefined,
    onAgentPresence: (present) => presence.push(present),
  };
  const host = document.createElement("div");
  document.body.appendChild(host);
  let clock = 0;
  const transport = new LiveKitTransport({
    handlers,
    audioHost: host,
    createRoom: () => room,
    now: () => clock++ * 1000,
    ...(options.reconnectWindowMs === undefined ? {} : { reconnectWindowMs: options.reconnectWindowMs }),
  });
  return { room, transport, states, messages, rejected, audio, presence, connect, publishData, publishTrack, disconnect, host };
}

function rawMicTrack(): MediaStreamTrack {
  return Object.assign(new EventTarget(), {
    kind: "audio",
    label: "mic",
    enabled: true,
    muted: false,
    readyState: "live",
    id: "raw-mic",
    getSettings: () => ({ deviceId: "d", channelCount: 1 }),
    getConstraints: () => ({}),
    stop: vi.fn(),
    clone: vi.fn(),
  }) as unknown as MediaStreamTrack;
}

const encode = (value: unknown): Uint8Array<ArrayBuffer> => new TextEncoder().encode(JSON.stringify(value));

function stateMessage(state: string, seq = 1): Uint8Array<ArrayBuffer> {
  return encode({
    schema_version: 1,
    event_id: `e${String(seq)}`,
    session_id: "s1",
    event_type: "agent.state",
    sequence_number: seq,
    occurred_at: "2026-09-29T10:00:00Z",
    payload: { state },
  });
}

describe("LiveKitTransport", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "MediaStream",
      class FakeMediaStream {
        public constructor(public readonly tracks: readonly MediaStreamTrack[] = []) {}
        public getTracks(): readonly MediaStreamTrack[] {
          return this.tracks;
        }
      },
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("connects with the safe url and scoped token and auto-subscribes", async () => {
    const h = build();

    await h.transport.connect("ws://127.0.0.1:7880", TOKEN);

    expect(h.connect).toHaveBeenCalledWith("ws://127.0.0.1:7880", TOKEN, { autoSubscribe: true });
    expect(h.states.map((s) => s.state)).toEqual(["connecting", "connected"]);
  });

  it("fails connect with a safe message that never contains the token", async () => {
    const h = build({ connectFails: true });

    const error = await h.transport.connect("ws://127.0.0.1:7880", TOKEN).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(TransportError);
    expect((error as Error).message).not.toContain(TOKEN);
    expect(h.states.at(-1)).toEqual({ state: "disconnected", cause: "connect_failed" });
  });

  it("never persists the token in storage, location, or console", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const h = build();

    await h.transport.connect("ws://127.0.0.1:7880", TOKEN);
    await h.transport.disconnect();

    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(window.localStorage))).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(window.sessionStorage))).not.toContain(TOKEN);
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
    expect(Object.values(h.transport).filter((value) => typeof value === "string")).not.toContain(TOKEN);
  });

  it("publishes the microphone as an audio-only microphone-source track", async () => {
    const h = build();
    await h.transport.connect("ws://127.0.0.1:7880", TOKEN);
    const raw = rawMicTrack();

    await h.transport.publishMicrophone(raw);

    expect(h.publishTrack).toHaveBeenCalledTimes(1);
    expect(h.publishTrack.mock.calls[0]?.[1]).toMatchObject({ source: Track.Source.Microphone });
  });

  it("maps a failed publish to a safe TransportError", async () => {
    const h = build();
    h.publishTrack.mockRejectedValue(new Error("internal"));
    await h.transport.connect("ws://127.0.0.1:7880", TOKEN);
    const raw = rawMicTrack();

    await expect(h.transport.publishMicrophone(raw)).rejects.toBeInstanceOf(TransportError);
  });

  it("moves connected -> reconnecting -> connected on SDK reconnect", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.Reconnecting);
    h.room.emit(RoomEvent.Reconnected);

    expect(h.states.map((s) => s.state)).toEqual(["connecting", "connected", "reconnecting", "connected"]);
  });

  it("gives up after the 20 second reconnect window and disconnects", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.Reconnecting);
    await vi.advanceTimersByTimeAsync(19_999);
    expect(h.transport.getState()).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(2);

    expect(h.disconnect).toHaveBeenCalled();
    expect(h.states.at(-1)).toEqual({ state: "disconnected", cause: "reconnect_exhausted" });
  });

  it("cancels the reconnect window when the SDK reconnects in time", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.Reconnecting);
    h.room.emit(RoomEvent.Reconnected);
    await vi.advanceTimersByTimeAsync(30_000);

    expect(h.disconnect).not.toHaveBeenCalled();
    expect(h.transport.getState()).toBe("connected");
  });

  it("reports an unexpected server disconnect but not a local one", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.Disconnected);

    expect(h.states.at(-1)).toEqual({ state: "disconnected", cause: "server" });
  });

  it("disconnect is ordered, idempotent, and stops agent audio", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    await h.transport.disconnect();
    await h.transport.disconnect();
    h.room.emit(RoomEvent.Disconnected);

    expect(h.disconnect).toHaveBeenCalledTimes(1);
    expect(h.states.filter((s) => s.state === "disconnected")).toEqual([
      { state: "disconnected", cause: "local" },
    ]);
  });

  it("validates inbound data and forwards only well-formed known topics", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.DataReceived, stateMessage("speaking"), undefined, DataPacket_Kind.RELIABLE, "va.state.v1");
    h.room.emit(RoomEvent.DataReceived, stateMessage("bogus", 2), undefined, DataPacket_Kind.RELIABLE, "va.state.v1");
    h.room.emit(RoomEvent.DataReceived, stateMessage("idle", 3), undefined, DataPacket_Kind.RELIABLE, "va.unknown.v1");

    expect(h.messages).toHaveLength(1);
    expect(h.rejected).toEqual(["invalid_message"]);
  });

  it("applies the 1200 byte lossy bound to lossy packets", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);

    h.room.emit(RoomEvent.DataReceived, new Uint8Array(1300), undefined, DataPacket_Kind.LOSSY, "va.transcript.v1");

    expect(h.rejected).toEqual(["oversized"]);
  });

  it("sends client events on va.client.v1, lossy for progress and reliable otherwise", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);
    const base = { sessionId: "s1", eventId: "e", occurredAt: "2026-09-29T10:00:00Z" } as const;

    await h.transport.sendClientEvent({ ...base, eventType: "client.mic_muted" });
    await h.transport.sendClientEvent({ ...base, eventType: "playback.progress" });

    expect(h.publishData.mock.calls[0]?.[1]).toEqual({ reliable: true, topic: "va.client.v1" });
    expect(h.publishData.mock.calls[1]?.[1]).toEqual({ reliable: false, topic: "va.client.v1" });
  });

  it("throttles browser messages at burst 40 and rejects the excess", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);
    // A frozen clock makes refill zero so exactly the burst is admitted.
    const frozen = new LiveKitTransport({
      handlers: { onState: () => undefined, onAgentAudio: () => undefined, onMessage: () => undefined, onRejected: () => undefined, onQuality: () => undefined, onAgentPresence: () => undefined },
      audioHost: h.host,
      createRoom: () => h.room,
      now: () => 0,
    });
    await frozen.connect("ws://x", TOKEN);
    const input = { eventType: "client.ready", sessionId: "s1", eventId: "e", occurredAt: "t" } as const;

    const outcomes: string[] = [];
    for (let i = 0; i < 45; i += 1) {
      outcomes.push(await frozen.sendClientEvent(input));
    }

    expect(outcomes.filter((o) => o === "allow")).toHaveLength(40);
    expect(outcomes.filter((o) => o === "reject")).toHaveLength(5);
    expect(frozen.limiterCounters().rejectedReliable).toBe(5);
  });

  it("does not send while disconnected", async () => {
    const h = build();

    const outcome = await h.transport.sendClientEvent({
      eventType: "client.ready",
      sessionId: "s1",
      eventId: "e",
      occurredAt: "t",
    });

    expect(outcome).toBe("not_connected");
    expect(h.publishData).not.toHaveBeenCalled();
  });

  it("attaches only the agent-audio track and ignores other subscriptions", async () => {
    const h = build();
    await h.transport.connect("ws://x", TOKEN);
    const element = document.createElement("audio");
    const track = {
      attach: vi.fn(() => element),
      detach: vi.fn(() => [element]),
      getRTCStatsReport: vi.fn(() => Promise.resolve(undefined)),
    };

    h.room.emit(RoomEvent.TrackSubscribed, track as never, { kind: Track.Kind.Audio, trackName: "other" } as never, {} as never);
    expect(track.attach).not.toHaveBeenCalled();

    h.room.emit(RoomEvent.TrackSubscribed, track as never, { kind: Track.Kind.Audio, trackName: "agent-audio" } as never, {} as never);
    expect(track.attach).toHaveBeenCalledTimes(1);
    expect(h.host.contains(element)).toBe(true);
    expect(h.audio.at(-1)?.phase).toBe("subscribed");

    h.room.emit(RoomEvent.TrackUnsubscribed, track as never, { kind: Track.Kind.Audio, trackName: "agent-audio" } as never, {} as never);
    expect(h.audio.at(-1)?.phase).toBe("none");
  });

  it("reports agent presence changes and local connection quality", async () => {
    const h = build();
    const qualities: string[] = [];
    await h.transport.connect("ws://x", TOKEN);
    const other = new LiveKitTransport({
      handlers: { onState: () => undefined, onAgentAudio: () => undefined, onMessage: () => undefined, onRejected: () => undefined, onQuality: (q) => qualities.push(q), onAgentPresence: () => undefined },
      audioHost: h.host,
      createRoom: () => h.room,
    });

    h.room.emit(RoomEvent.ParticipantConnected, {} as never);
    h.room.emit(RoomEvent.ParticipantDisconnected, {} as never);
    h.room.emit(RoomEvent.ConnectionQualityChanged, ConnectionQuality.Poor, h.room.localParticipant);
    h.room.emit(RoomEvent.ConnectionQualityChanged, ConnectionQuality.Good, {} as never);

    expect(h.presence).toEqual([true, false]);
    expect(qualities).toEqual(["poor"]);
    expect(other.getState()).toBe("idle");
  });

  it("mutes and unmutes the published microphone track", async () => {
    const h = build();
    const mute = vi.fn().mockResolvedValue(undefined);
    const unmute = vi.fn().mockResolvedValue(undefined);
    h.publishTrack.mockResolvedValue({ track: { mute, unmute } });
    await h.transport.connect("ws://x", TOKEN);
    await h.transport.publishMicrophone(rawMicTrack());

    await h.transport.setMicMuted(true);
    await h.transport.setMicMuted(false);

    expect(mute).toHaveBeenCalledTimes(1);
    expect(unmute).toHaveBeenCalledTimes(1);
  });

  it("ignores mute requests before a microphone is published", async () => {
    const h = build();

    await expect(h.transport.setMicMuted(true)).resolves.toBeUndefined();
  });

  it("starts audio playback on a user gesture", async () => {
    const h = build();
    const startAudio = vi.spyOn(h.room, "startAudio").mockResolvedValue(undefined);
    await h.transport.connect("ws://x", TOKEN);

    await h.transport.startAudio();

    expect(startAudio).toHaveBeenCalledTimes(1);
  });

  it("returns failed when the data publish throws", async () => {
    const h = build();
    h.publishData.mockRejectedValue(new Error("closed"));
    await h.transport.connect("ws://x", TOKEN);

    const outcome = await h.transport.sendClientEvent({
      eventType: "client.ready",
      sessionId: "s1",
      eventId: "e",
      occurredAt: "t",
    });

    expect(outcome).toBe("failed");
  });
});
