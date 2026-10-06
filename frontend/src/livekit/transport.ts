/**
 * LiveKit browser transport (docs/06 §5, §9-§14).
 *
 * All livekit-client objects stay inside this module. The join token is
 * passed straight to `Room.connect` and is never stored in a field,
 * storage, URL, or log.
 */
import {
  ConnectionQuality,
  ConnectionState,
  DataPacket_Kind,
  LocalAudioTrack,
  Room,
  RoomEvent,
  Track,
  type LocalTrackPublication,
  type RemoteAudioTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import {
  AGENT_AUDIO_TRACK_NAME,
  AgentAudioPlayer,
  NO_AGENT_AUDIO,
  type AgentAudioStatus,
} from "../audio/agentAudio";
import { REQUESTED_MIC_CONSTRAINTS } from "../audio/microphone";
import {
  decodeInbound,
  encodeClientEvent,
  type ClientEventInput,
  type DecodeFailure,
  type InboundMessage,
} from "../contracts/realtime";
import { ClientSendLimiter, type SendDecision } from "./rateLimiter";

/** Application reconnect window (docs/06 §14). */
export const RECONNECT_WINDOW_MS = 20_000;

export type TransportState = "idle" | "connecting" | "connected" | "reconnecting" | "disconnected";

export type DisconnectCause =
  | "local"
  | "server"
  | "reconnect_exhausted"
  | "connect_failed";

export type LinkQuality = "excellent" | "good" | "poor" | "lost" | "unknown";

export interface TransportHandlers {
  readonly onState: (state: TransportState, cause: DisconnectCause | null) => void;
  readonly onAgentAudio: (status: AgentAudioStatus) => void;
  readonly onMessage: (message: InboundMessage) => void;
  readonly onRejected: (reason: DecodeFailure) => void;
  readonly onQuality: (quality: LinkQuality) => void;
  readonly onAgentPresence: (present: boolean) => void;
}

export interface TransportOptions {
  readonly handlers: TransportHandlers;
  readonly audioHost: HTMLElement;
  readonly createRoom?: () => Room;
  readonly now?: () => number;
  readonly reconnectWindowMs?: number;
}

export type ClientEventOutcome = SendDecision | "not_connected" | "failed";

const QUALITY_MAP: Readonly<Record<ConnectionQuality, LinkQuality>> = {
  [ConnectionQuality.Excellent]: "excellent",
  [ConnectionQuality.Good]: "good",
  [ConnectionQuality.Poor]: "poor",
  [ConnectionQuality.Lost]: "lost",
  [ConnectionQuality.Unknown]: "unknown",
};

export class LiveKitTransport {
  private readonly room: Room;
  private readonly player: AgentAudioPlayer;
  private readonly limiter: ClientSendLimiter;
  private readonly reconnectWindowMs: number;
  private state: TransportState = "idle";
  private closing = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private micPublication: LocalTrackPublication | null = null;

  public constructor(private readonly options: TransportOptions) {
    this.room = (options.createRoom ?? ((): Room => new Room()))();
    this.limiter = new ClientSendLimiter(options.now ?? ((): number => performance.now()));
    this.reconnectWindowMs = options.reconnectWindowMs ?? RECONNECT_WINDOW_MS;
    this.player = new AgentAudioPlayer({
      host: options.audioHost,
      onStatus: options.handlers.onAgentAudio,
    });
    this.bind();
  }

  public getState(): TransportState {
    return this.state;
  }

  public limiterCounters(): ReturnType<ClientSendLimiter["counters"]> {
    return this.limiter.counters();
  }

  /** Connects with the scoped token; resolves once the room is joined. */
  public async connect(url: string, token: string): Promise<void> {
    this.closing = false;
    this.setState("connecting", null);
    try {
      await this.room.connect(url, token, { autoSubscribe: true });
    } catch {
      this.setState("disconnected", "connect_failed");
      throw new TransportError("The voice connection could not be established.");
    }
    this.setState("connected", null);
    this.player.setPlaybackBlocked(!this.room.canPlaybackAudio);
  }

  /** Publishes the captured microphone track (audio only, no video). */
  public async publishMicrophone(mediaTrack: MediaStreamTrack): Promise<void> {
    const local = new LocalAudioTrack(
      mediaTrack,
      { ...REQUESTED_MIC_CONSTRAINTS },
      true,
    );
    const previous = this.micPublication?.track;
    if (previous !== undefined) {
      // Replace a dead capture track (device loss) instead of leaving it published.
      await this.room.localParticipant.unpublishTrack(previous, false).catch(() => undefined);
    }
    try {
      this.micPublication = await this.room.localParticipant.publishTrack(local, {
        source: Track.Source.Microphone,
        name: "microphone",
      });
    } catch {
      throw new TransportError("The microphone could not be published to the room.");
    }
  }

  public async setMicMuted(muted: boolean): Promise<void> {
    const track = this.micPublication?.track;
    if (track === undefined) {
      return;
    }
    if (muted) {
      await track.mute();
    } else {
      await track.unmute();
    }
  }

  /** Enables audio output after a browser autoplay block (needs a user gesture). */
  public async startAudio(): Promise<void> {
    await this.room.startAudio();
    this.player.setPlaybackBlocked(!this.room.canPlaybackAudio);
  }

  /** Read-only agent-audio receiver stats, for the composed response-latency sample (docs/06 §15). */
  public async getAgentAudioStats(): Promise<RTCStatsReport | undefined> {
    return this.player.getStatsReport();
  }

  public async sendClientEvent(input: ClientEventInput): Promise<ClientEventOutcome> {
    if (this.state !== "connected") {
      return "not_connected";
    }
    const decision = this.limiter.decide(input.eventType === "playback.progress");
    if (decision !== "allow") {
      return decision;
    }
    const encoded = encodeClientEvent(input);
    try {
      await this.room.localParticipant.publishData(encoded.bytes, {
        reliable: encoded.reliable,
        topic: encoded.topic,
      });
    } catch {
      return "failed";
    }
    return "allow";
  }

  /** Idempotent, ordered cleanup: playback, publications, then the room. */
  public async disconnect(): Promise<void> {
    if (this.state === "disconnected" || this.state === "idle") {
      this.player.detach();
      return;
    }
    this.closing = true;
    this.clearReconnectTimer();
    this.player.detach();
    this.micPublication = null;
    await this.room.disconnect(true);
    this.setState("disconnected", "local");
  }

  private setState(next: TransportState, cause: DisconnectCause | null): void {
    if (this.state === next && cause === null) {
      return;
    }
    this.state = next;
    this.options.handlers.onState(next, cause);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private bind(): void {
    const { room } = this;
    room.on(RoomEvent.Reconnecting, this.onReconnecting);
    room.on(RoomEvent.Reconnected, this.onReconnected);
    room.on(RoomEvent.Disconnected, this.onDisconnected);
    room.on(RoomEvent.DataReceived, this.onData);
    room.on(RoomEvent.TrackSubscribed, this.onTrackSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, this.onTrackUnsubscribed);
    room.on(RoomEvent.ParticipantConnected, this.onParticipantConnected);
    room.on(RoomEvent.ParticipantDisconnected, this.onParticipantDisconnected);
    room.on(RoomEvent.AudioPlaybackStatusChanged, this.onPlaybackStatus);
    room.on(RoomEvent.ConnectionQualityChanged, this.onQuality);
  }

  private readonly onReconnecting = (): void => {
    this.player.detach();
    this.setState("reconnecting", null);
    this.clearReconnectTimer();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.state === "reconnecting") {
        this.closing = true;
        void this.room.disconnect(true).finally(() => {
          this.setState("disconnected", "reconnect_exhausted");
        });
      }
    }, this.reconnectWindowMs);
  };

  private readonly onReconnected = (): void => {
    this.clearReconnectTimer();
    this.setState("connected", null);
    this.player.setPlaybackBlocked(!this.room.canPlaybackAudio);
  };

  private readonly onDisconnected = (): void => {
    this.clearReconnectTimer();
    this.player.detach();
    if (this.closing || this.state === "disconnected") {
      return;
    }
    this.setState("disconnected", "server");
  };

  private readonly onData = (
    payload: Uint8Array,
    _participant?: unknown,
    kind?: DataPacket_Kind,
    topic?: string,
  ): void => {
    const result = decodeInbound(topic, payload, kind !== DataPacket_Kind.LOSSY);
    if (result.ok) {
      this.options.handlers.onMessage(result.message);
    } else if (result.reason !== "unknown_topic") {
      this.options.handlers.onRejected(result.reason);
    }
  };

  private readonly onTrackSubscribed = (
    track: unknown,
    publication: RemoteTrackPublication,
  ): void => {
    // Only the named agent-audio track is ever attached for playout.
    if (publication.kind !== Track.Kind.Audio || publication.trackName !== AGENT_AUDIO_TRACK_NAME) {
      return;
    }
    this.player.attach(track as RemoteAudioTrack);
    this.player.setPlaybackBlocked(!this.room.canPlaybackAudio);
  };

  private readonly onTrackUnsubscribed = (
    _track: unknown,
    publication: RemoteTrackPublication,
  ): void => {
    if (publication.trackName === AGENT_AUDIO_TRACK_NAME) {
      this.player.detach();
      this.options.handlers.onAgentAudio(NO_AGENT_AUDIO);
    }
  };

  private readonly onParticipantConnected = (): void => {
    this.options.handlers.onAgentPresence(true);
  };

  private readonly onParticipantDisconnected = (): void => {
    this.options.handlers.onAgentPresence(false);
  };

  private readonly onPlaybackStatus = (): void => {
    this.player.setPlaybackBlocked(!this.room.canPlaybackAudio);
  };

  private readonly onQuality = (quality: ConnectionQuality, participant: unknown): void => {
    if (participant === this.room.localParticipant) {
      this.options.handlers.onQuality(QUALITY_MAP[quality]);
    }
  };

  /** True once the SDK reports it is fully connected. */
  public isRoomConnected(): boolean {
    return this.room.state === ConnectionState.Connected;
  }
}

export class TransportError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "TransportError";
  }
}
