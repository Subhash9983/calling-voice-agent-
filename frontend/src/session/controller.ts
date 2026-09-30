/**
 * Voice session controller: orchestrates the control API, the LiveKit
 * transport and the microphone. Framework-free so it is unit-testable;
 * the React hook only subscribes to its store.
 *
 * Token policy: a join token exists only as a local value between the API
 * response and `transport.connect`. It is never written to state, storage,
 * a URL, or a log.
 */
import { ApiError, type ControlApiClient } from "../api";
import { watchDeviceLoss, type MicrophoneResult } from "../audio/microphone";
import { playbackPayload, type ClientEventInput, type ClientEventType } from "../contracts/realtime";
import { STT_COMPONENT } from "../contracts/diagnosticsApi";
import type { DisconnectReason, TransportJoin } from "../contracts/sessionApi";
import type {
  ClientEventOutcome,
  DisconnectCause,
  TransportHandlers,
  TransportState,
} from "../livekit/transport";
import { RECONNECT_WINDOW_MS } from "../livekit/transport";
import { summarizeCost, summarizeOperations } from "./evidence";
import { PlaybackAckTracker } from "./playbackAcks";
import {
  INITIAL_SESSION_STATE,
  sessionReducer,
  type SessionAction,
  type SessionError,
  type SessionViewState,
} from "./sessionState";

export interface TransportPort {
  connect(url: string, token: string): Promise<void>;
  publishMicrophone(track: MediaStreamTrack): Promise<void>;
  setMicMuted(muted: boolean): Promise<void>;
  startAudio(): Promise<void>;
  sendClientEvent(input: ClientEventInput): Promise<ClientEventOutcome>;
  disconnect(): Promise<void>;
}

export interface ControllerDeps {
  readonly api: ControlApiClient;
  readonly createTransport: (handlers: TransportHandlers) => TransportPort;
  readonly acquireMicrophone: () => Promise<MicrophoneResult>;
  readonly newId: () => string;
  readonly nowIso: () => string;
  readonly sleep: (ms: number) => Promise<void>;
  readonly reconnectWindowMs?: number;
  /** No agent signal (metrics, state, audio) for this long shows recovering. */
  readonly agentSignalTimeoutMs?: number;
  readonly nowMs?: () => number;
}

/** First rejoin attempt is immediate: the worker window starts when we leave. */
const RECOVERY_BACKOFF_MS: readonly number[] = [0, 500, 1000, 2000, 4000, 4000];
const RETRY_DELAY_MS = 500;
export const AGENT_SIGNAL_TIMEOUT_MS = 5000;
const EVENTS_LIMIT = 30;
const EVIDENCE_LIMIT = 100;

function describeError(error: unknown, fallback: string): SessionError {
  if (error instanceof Error && error.name === "MicrophoneUnavailableError") {
    return { message: error.message, retryable: true };
  }
  if (error instanceof ApiError) {
    return { message: error.message, retryable: error.retryable };
  }
  if (error instanceof Error && error.name === "TransportError") {
    return { message: error.message, retryable: true };
  }
  return { message: fallback, retryable: true };
}

/** The microphone could not be (re)acquired; there is nothing to publish. */
class MicrophoneUnavailableError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "MicrophoneUnavailableError";
  }
}

export class VoiceSessionController {
  private state: SessionViewState = INITIAL_SESSION_STATE;
  private readonly listeners = new Set<() => void>();
  private transport: TransportPort | null = null;
  private micTrack: MediaStreamTrack | null = null;
  private stopWatching: (() => void) | null = null;
  private sessionId: string | null = null;
  private recovering = false;
  private handlingLoss = false;
  private stopping = false;
  /** The user left (page hide or End) before a session id existed. */
  private startCancelled = false;
  private signalTimer: ReturnType<typeof setTimeout> | null = null;
  private signalLost = false;
  private readonly acks: PlaybackAckTracker;

  public constructor(private readonly deps: ControllerDeps) {
    this.acks = new PlaybackAckTracker(
      (eventType, identity, positionMs) => {
        void this.emit(eventType, playbackPayload(identity, positionMs));
      },
      deps.nowMs ?? ((): number => performance.now()),
    );
  }

  public getState = (): SessionViewState => this.state;

  public subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private dispatch(action: SessionAction): void {
    this.state = sessionReducer(this.state, action);
    this.listeners.forEach((listener) => {
      listener();
    });
  }

  /** Starts a session: microphone first (so denial never leaves an orphan session). */
  public async start(agentConfigId: string): Promise<void> {
    if (this.state.phase !== "idle" && this.state.phase !== "ended" && this.state.phase !== "failed") {
      return;
    }
    this.stopping = false;
    this.startCancelled = false;
    this.dispatch({ type: "starting" });
    this.dispatch({ type: "mic_requesting" });
    const mic = await this.deps.acquireMicrophone();
    if (!mic.ok) {
      this.dispatch({
        type: "mic_failed",
        status: mic.error.kind === "permission_denied" ? "denied" : "error",
        message: mic.error.message,
      });
      this.dispatch({ type: "failed", error: { message: mic.error.message, retryable: true } });
      return;
    }
    if (this.startCancelled) {
      mic.track.stop();
      this.dispatch({ type: "phase", phase: "idle" });
      return;
    }
    this.adoptMicrophone(mic.track);
    this.dispatch({ type: "mic_active", report: mic.report });
    await this.createAndConnect(agentConfigId);
  }

  private adoptMicrophone(track: MediaStreamTrack): void {
    this.micTrack = track;
    this.stopWatching?.();
    this.stopWatching = watchDeviceLoss(track, () => {
      void this.onDeviceLost();
    });
  }

  /** livekit-client stops local tracks on a server disconnect, so re-acquire if ended. */
  private async ensureLiveMicrophone(): Promise<void> {
    if (this.micTrack !== null && this.micTrack.readyState !== "ended") {
      return;
    }
    const mic = await this.deps.acquireMicrophone();
    if (!mic.ok) {
      this.dispatch({ type: "mic_failed", status: "error", message: mic.error.message });
      throw new MicrophoneUnavailableError(mic.error.message);
    }
    this.adoptMicrophone(mic.track);
    this.dispatch({ type: "mic_active", report: mic.report });
  }

  /** Device loss: try once to re-acquire; otherwise alert and end the session cleanly. */
  private async onDeviceLost(): Promise<void> {
    if (this.stopping || this.sessionId === null || this.handlingLoss) {
      return;
    }
    this.handlingLoss = true;
    try {
      this.dispatch({
        type: "mic_failed",
        status: "lost",
        message: "The microphone was disconnected. Trying to reconnect it.",
      });
      const mic = await this.deps.acquireMicrophone();
      if (mic.ok && !this.stopping && this.transport !== null) {
        this.adoptMicrophone(mic.track);
        await this.transport.publishMicrophone(mic.track);
        this.dispatch({ type: "mic_active", report: mic.report });
        return;
      }
      if (mic.ok) {
        mic.track.stop();
      }
    } catch {
      // Falls through to the terminal path below.
    } finally {
      this.handlingLoss = false;
    }
    this.dispatch({
      type: "mic_failed",
      status: "lost",
      message: "The microphone was disconnected and could not be restored. The session is ending.",
    });
    await this.stop("transport_error");
  }

  private async createAndConnect(agentConfigId: string): Promise<void> {
    try {
      const created = await this.withRetry((requestId) =>
        this.deps.api.createSession({ agentConfigId, clientRequestId: requestId }),
      );
      this.sessionId = created.session.sessionId;
      if (this.startCancelled) {
        await this.endCancelledStart(created.session.sessionId);
        return;
      }
      this.dispatch({
        type: "session_created",
        sessionId: created.session.sessionId,
        configuration: created.configuration,
      });
      await this.connectTransport(created.transport);
      if (this.stopping) {
        return;
      }
      this.dispatch({ type: "phase", phase: "live" });
      this.noteAgentSignal();
      void this.emit("client.ready");
    } catch (error: unknown) {
      await this.abort(describeError(error, "The session could not be started."));
    }
  }

  /** The user left while the session was being created: end it without connecting. */
  private async endCancelledStart(sessionId: string): Promise<void> {
    await this.deps.api.endSession(sessionId, "browser_closed", this.deps.newId()).catch(() => undefined);
    this.sessionId = null;
    this.releaseMicrophone();
    this.dispatch({ type: "phase", phase: "ended" });
  }

  private async connectTransport(join: TransportJoin): Promise<void> {
    if (join.provider !== "livekit" || join.joinToken === null) {
      throw new ApiError({
        code: "INVALID_RESPONSE",
        message: "The server did not return usable join credentials.",
        status: null,
        retryable: false,
      });
    }
    if (this.stopping) {
      return; // Never re-acquire the microphone once the user ended the session.
    }
    await this.ensureLiveMicrophone();
    this.transport ??= this.deps.createTransport(this.handlers());
    await this.transport.connect(join.url, join.joinToken);
    if (this.micTrack !== null) {
      await this.transport.publishMicrophone(this.micTrack);
    }
  }

  /** One retry with the SAME idempotency id for retryable failures. */
  private async withRetry<T>(call: (requestId: string) => Promise<T>): Promise<T> {
    const requestId = this.deps.newId();
    try {
      return await call(requestId);
    } catch (error: unknown) {
      if (error instanceof ApiError && error.retryable) {
        await this.deps.sleep(RETRY_DELAY_MS);
        return call(requestId);
      }
      throw error;
    }
  }

  private async abort(error: SessionError): Promise<void> {
    this.stopSignalWatch();
    this.acks.dispose();
    this.releaseMicrophone();
    await this.transport?.disconnect().catch(() => undefined);
    if (this.sessionId !== null) {
      await this.deps.api.endSession(this.sessionId, "transport_error").catch(() => undefined);
    }
    this.dispatch({ type: "failed", error });
  }

  /**
   * If the durable session is already terminal (for example the agent ended it
   * normally) show that outcome locally and never POST an end request.
   */
  private async finishIfTerminal(): Promise<boolean> {
    const sessionId = this.sessionId;
    if (sessionId === null) {
      return false;
    }
    let status: string;
    try {
      status = (await this.deps.api.getSession(sessionId)).status;
    } catch {
      return false;
    }
    if (status !== "ended" && status !== "failed") {
      return false;
    }
    this.stopSignalWatch();
    this.acks.dispose();
    this.releaseMicrophone();
    await this.transport?.disconnect().catch(() => undefined);
    await this.loadDiagnostics(sessionId);
    if (status === "ended") {
      this.dispatch({ type: "phase", phase: "ended" });
    } else {
      this.dispatch({
        type: "failed",
        error: { message: "The session ended with an error.", retryable: true },
      });
    }
    this.sessionId = null;
    this.transport = null;
    return true;
  }

  private async abortUnlessTerminal(error: SessionError): Promise<void> {
    if (!(await this.finishIfTerminal())) {
      await this.abort(error);
    }
  }

  /** Ends the session: durable end request first, then media cleanup. */
  public async stop(reason: DisconnectReason = "user_ended"): Promise<void> {
    if (this.stopping) {
      return;
    }
    if (this.sessionId === null) {
      this.cancelPendingStart();
      return;
    }
    this.stopping = true;
    this.stopSignalWatch();
    this.acks.dispose();
    // Privacy: turn the microphone off first, never after network round trips.
    this.releaseMicrophone();
    this.dispatch({ type: "phase", phase: "ending" });
    const sessionId = this.sessionId;
    let failure: SessionError | null = null;
    try {
      await this.withRetry((requestId) => this.deps.api.endSession(sessionId, reason, requestId));
    } catch (error: unknown) {
      failure = describeError(error, "The session could not be ended cleanly.");
    }
    await this.transport?.disconnect().catch(() => undefined);
    await this.loadDiagnostics(sessionId);
    if (failure === null) {
      this.dispatch({ type: "phase", phase: "ended" });
    } else {
      this.dispatch({ type: "failed", error: failure });
    }
    this.sessionId = null;
    this.transport = null;
  }

  private cancelPendingStart(): void {
    const starting = this.state.phase === "starting" || this.state.phase === "connecting";
    if (!starting) {
      return;
    }
    this.startCancelled = true;
    this.releaseMicrophone();
  }

  public async setMuted(muted: boolean): Promise<void> {
    if (this.transport === null || this.state.mic.status !== "active") {
      return;
    }
    try {
      await this.transport.setMicMuted(muted);
    } catch {
      this.dispatch({
        type: "mic_failed",
        status: "error",
        message: "The microphone mute state could not be changed.",
      });
      return;
    }
    this.dispatch({ type: "mic_muted", muted });
    void this.emit(muted ? "client.mic_muted" : "client.mic_unmuted");
  }

  /** Must be called from a user gesture when the browser blocked autoplay. */
  public async enableAudio(): Promise<void> {
    await this.transport?.startAudio().catch(() => undefined);
  }

  private async emit(
    eventType: ClientEventType,
    payload?: ClientEventInput["payload"],
  ): Promise<void> {
    if (this.transport === null || this.sessionId === null) {
      return;
    }
    await this.transport.sendClientEvent({
      eventType,
      sessionId: this.sessionId,
      eventId: this.deps.newId(),
      occurredAt: this.deps.nowIso(),
      ...(payload === undefined ? {} : { payload }),
    });
  }

  /** Any metrics, state or agent audio counts as proof the agent is alive. */
  private noteAgentSignal(): void {
    if (this.signalTimer !== null) {
      clearTimeout(this.signalTimer);
    }
    if (this.signalLost) {
      this.signalLost = false;
      void this.reloadDurableState();
    }
    const timeout = this.deps.agentSignalTimeoutMs ?? AGENT_SIGNAL_TIMEOUT_MS;
    this.signalTimer = setTimeout(() => {
      this.signalTimer = null;
      if (this.state.phase === "live") {
        this.signalLost = true;
        this.dispatch({ type: "agent_state", state: "recovering" });
      }
    }, timeout);
  }

  private stopSignalWatch(): void {
    if (this.signalTimer !== null) {
      clearTimeout(this.signalTimer);
      this.signalTimer = null;
    }
    this.signalLost = false;
  }

  private releaseMicrophone(): void {
    this.stopWatching?.();
    this.stopWatching = null;
    if (this.micTrack === null) {
      return;
    }
    this.micTrack.stop();
    this.micTrack = null;
    this.dispatch({ type: "mic_released" });
  }

  private async loadDiagnostics(sessionId: string): Promise<void> {
    await Promise.all([this.loadEvents(sessionId), this.loadEvidence(sessionId)]);
  }

  /** Best-effort: any failure (including a 503 not-ready) is a neutral "not available". */
  private async loadEvidence(sessionId: string): Promise<void> {
    const [operations, costs] = await Promise.allSettled([
      this.deps.api.listOperations(sessionId, { component: STT_COMPONENT, limit: EVIDENCE_LIMIT }),
      this.deps.api.getCosts(sessionId),
    ]);
    this.dispatch({
      type: "evidence_loaded",
      evidence: {
        status: "ready",
        operations: operations.status === "fulfilled" ? summarizeOperations(operations.value) : null,
        cost: costs.status === "fulfilled" ? summarizeCost(costs.value) : null,
      },
    });
  }

  private async loadEvents(sessionId: string): Promise<void> {
    try {
      const events = await this.deps.api.listEvents(sessionId, EVENTS_LIMIT);
      this.dispatch({ type: "events_loaded", events });
    } catch {
      // Timeline is best-effort diagnostics; the session outcome is already known.
    }
  }

  private handlers(): TransportHandlers {
    return {
      onState: (state, cause) => {
        this.dispatch({ type: "transport", state });
        this.onTransportState(state, cause);
      },
      onAgentAudio: (status) => {
        this.dispatch({ type: "agent_audio", status });
        if (status.receiving) {
          this.noteAgentSignal();
        }
        if (status.phase === "failed") {
          this.acks.onPlayoutFailed();
        }
      },
      onMessage: (message) => {
        this.noteAgentSignal();
        this.dispatch({ type: "message", message });
        if (message.topic === "va.playback.v1") {
          this.acks.onPlayback(message.state, message.identity);
        }
      },
      onRejected: () => {
        this.dispatch({ type: "message_rejected" });
      },
      onQuality: (quality) => {
        this.dispatch({ type: "quality", quality });
      },
      onAgentPresence: (present) => {
        this.dispatch({ type: "agent_presence", present });
      },
    };
  }

  private onTransportState(state: TransportState, cause: DisconnectCause | null): void {
    if (this.stopping || this.sessionId === null) {
      return;
    }
    const phase = this.state.phase;
    if (state === "reconnecting" && phase === "live") {
      this.dispatch({ type: "phase", phase: "reconnecting" });
      this.dispatch({ type: "agent_state", state: "recovering" });
    } else if (state === "connected" && phase === "reconnecting") {
      this.dispatch({ type: "phase", phase: "live" });
      this.noteAgentSignal();
      void this.reloadDurableState();
    } else if (state === "disconnected" && cause === "server" && phase !== "connecting") {
      void this.recover();
    } else if (state === "disconnected" && cause === "reconnect_exhausted") {
      void this.abortUnlessTerminal({
        message: "The connection was lost and could not be restored.",
        retryable: true,
      });
    }
  }

  /** After an SDK-level reconnect the durable state is authoritative (docs/06 §14). */
  private async reloadDurableState(): Promise<void> {
    if (this.sessionId === null) {
      return;
    }
    try {
      const summary = await this.deps.api.getSession(this.sessionId);
      this.dispatch({ type: "agent_state", state: summary.agentActivityState });
    } catch {
      // Keep the realtime view; the next state message will correct it.
    }
  }

  /** Application-level recovery: refreshed scoped token, same room and identity. */
  private async recover(): Promise<void> {
    if (this.recovering || this.sessionId === null) {
      return;
    }
    this.recovering = true;
    this.dispatch({ type: "phase", phase: "reconnecting" });
    this.dispatch({ type: "agent_state", state: "recovering" });
    if (await this.finishIfTerminal()) {
      this.recovering = false;
      return;
    }
    const window = this.deps.reconnectWindowMs ?? RECONNECT_WINDOW_MS;
    let waited = 0;
    try {
      for (const delay of RECOVERY_BACKOFF_MS) {
        if (waited >= window || this.stopping) {
          break;
        }
        await this.deps.sleep(delay);
        waited += delay;
        if (await this.tryReconnect()) {
          return;
        }
      }
      await this.abortUnlessTerminal({
        message: "The connection was lost and could not be restored.",
        retryable: true,
      });
    } finally {
      this.recovering = false;
    }
  }

  private async tryReconnect(): Promise<boolean> {
    const sessionId = this.sessionId;
    if (sessionId === null) {
      return false;
    }
    try {
      const refreshed = await this.deps.api.refreshJoinToken(sessionId, this.deps.newId());
      await this.connectTransport(refreshed.transport);
      this.dispatch({ type: "phase", phase: "live" });
      this.noteAgentSignal();
      await this.reloadDurableState();
      return true;
    } catch (error: unknown) {
      if (error instanceof MicrophoneUnavailableError) {
        await this.abort(describeError(error, "The microphone is unavailable."));
        return true;
      }
      if (error instanceof ApiError && !error.retryable) {
        // Terminal session or forbidden refresh: stop trying.
        await this.abortUnlessTerminal(describeError(error, "The session is no longer available."));
        return true;
      }
      return false;
    }
  }
}
