/**
 * Pure, immutable session view state. The controller dispatches actions;
 * the React layer only renders this state. It never holds a join token.
 */
import type { AgentAudioStatus } from "../audio/agentAudio";
import { NO_AGENT_AUDIO } from "../audio/agentAudio";
import type { ConstraintReport } from "../audio/microphone";
import type { InboundMessage } from "../contracts/realtime";
import type { AgentActivityState, ConfigurationLabels, SessionEventItem } from "../contracts/sessionApi";
import type { LinkQuality, TransportState } from "../livekit/transport";
import { NO_EVIDENCE, type EvidenceState } from "./evidence";
import { applyTranscriptLine, type TranscriptLine } from "./transcript";

export type { TranscriptLine };

export type SessionPhase =
  | "idle"
  | "starting"
  | "connecting"
  | "live"
  | "reconnecting"
  | "ending"
  | "ended"
  | "failed";

export type MicStatus = "idle" | "requesting" | "active" | "released" | "denied" | "lost" | "error";

export interface SessionError {
  readonly message: string;
  readonly retryable: boolean;
}

export interface SessionViewState {
  readonly phase: SessionPhase;
  readonly sessionId: string | null;
  readonly configuration: ConfigurationLabels | null;
  readonly agentState: AgentActivityState | null;
  readonly transport: TransportState;
  readonly quality: LinkQuality;
  readonly mic: {
    readonly status: MicStatus;
    readonly muted: boolean;
    readonly report: ConstraintReport | null;
    readonly message: string | null;
  };
  readonly agentAudio: AgentAudioStatus;
  /** Latched once audible agent audio (e.g. the backend test tone) has been received. */
  readonly audioVerified: boolean;
  readonly userTranscript: readonly TranscriptLine[];
  readonly agentResponse: readonly TranscriptLine[];
  readonly lastAgentError: string | null;
  readonly rejectedMessages: number;
  readonly lastStateSequence: number;
  readonly lastTranscriptSequence: number;
  readonly events: readonly SessionEventItem[];
  readonly evidence: EvidenceState;
  readonly error: SessionError | null;
}

export const INITIAL_SESSION_STATE: SessionViewState = {
  phase: "idle",
  sessionId: null,
  configuration: null,
  agentState: null,
  transport: "idle",
  quality: "unknown",
  mic: { status: "idle", muted: false, report: null, message: null },
  agentAudio: NO_AGENT_AUDIO,
  audioVerified: false,
  userTranscript: [],
  agentResponse: [],
  lastAgentError: null,
  rejectedMessages: 0,
  lastStateSequence: -1,
  lastTranscriptSequence: -1,
  events: [],
  evidence: NO_EVIDENCE,
  error: null,
};

export type SessionAction =
  | { readonly type: "starting" }
  | { readonly type: "session_created"; readonly sessionId: string; readonly configuration: ConfigurationLabels }
  | { readonly type: "phase"; readonly phase: SessionPhase }
  | { readonly type: "transport"; readonly state: TransportState }
  | { readonly type: "quality"; readonly quality: LinkQuality }
  | { readonly type: "mic_requesting" }
  | {
      readonly type: "mic_active";
      readonly report: ConstraintReport;
    }
  | { readonly type: "mic_failed"; readonly status: "denied" | "lost" | "error"; readonly message: string }
  | { readonly type: "mic_muted"; readonly muted: boolean }
  | { readonly type: "mic_released" }
  | { readonly type: "agent_audio"; readonly status: AgentAudioStatus }
  | { readonly type: "agent_presence"; readonly present: boolean }
  | { readonly type: "agent_state"; readonly state: AgentActivityState | null }
  | { readonly type: "message"; readonly message: InboundMessage }
  | { readonly type: "message_rejected" }
  | { readonly type: "events_loaded"; readonly events: readonly SessionEventItem[] }
  | { readonly type: "evidence_loaded"; readonly evidence: EvidenceState }
  | { readonly type: "failed"; readonly error: SessionError }
  | { readonly type: "reset" };

function applyMessage(state: SessionViewState, message: InboundMessage): SessionViewState {
  const { envelope } = message;
  switch (message.topic) {
    case "va.state.v1": {
      const sequence = envelope.sequenceNumber;
      if (sequence !== null && sequence <= state.lastStateSequence) {
        return state;
      }
      return {
        ...state,
        agentState: message.state,
        lastStateSequence: sequence ?? state.lastStateSequence,
      };
    }
    case "va.transcript.v1": {
      const sequence = envelope.sequenceNumber;
      if (sequence !== null && sequence <= state.lastTranscriptSequence) {
        return state;
      }
      return {
        ...state,
        lastTranscriptSequence: sequence ?? state.lastTranscriptSequence,
        userTranscript: applyTranscriptLine(state.userTranscript, {
          id: envelope.eventId,
          turnId: envelope.turnId,
          text: message.text,
          isFinal: message.isFinal,
        }),
      };
    }
    case "va.response.v1":
      return {
        ...state,
        agentResponse: applyTranscriptLine(state.agentResponse, {
          id: envelope.eventId,
          turnId: envelope.turnId,
          text: message.text,
          isFinal: message.isFinal,
        }),
      };
    case "va.error.v1":
      return { ...state, lastAgentError: message.message };
    case "va.playback.v1":
      // A cancelled or interrupted playback never leaves a stale "speaking" state.
      return message.state === "cancelled" || message.state === "interrupted"
        ? { ...state, agentState: message.state === "interrupted" ? "interrupted" : state.agentState }
        : state;
    case "va.metrics.v1":
      return state;
  }
}

export function sessionReducer(state: SessionViewState, action: SessionAction): SessionViewState {
  switch (action.type) {
    case "starting":
      return { ...INITIAL_SESSION_STATE, phase: "starting" };
    case "session_created":
      return {
        ...state,
        phase: "connecting",
        sessionId: action.sessionId,
        configuration: action.configuration,
      };
    case "phase":
      return { ...state, phase: action.phase };
    case "transport":
      return { ...state, transport: action.state };
    case "quality":
      return { ...state, quality: action.quality };
    case "mic_requesting":
      return { ...state, mic: { ...state.mic, status: "requesting", message: null } };
    case "mic_active":
      return {
        ...state,
        mic: { status: "active", muted: false, report: action.report, message: null },
      };
    case "mic_failed":
      return { ...state, mic: { ...state.mic, status: action.status, message: action.message } };
    case "mic_released":
      // The track is stopped. A failure outcome keeps its explanation.
      return state.mic.status === "lost" || state.mic.status === "error"
        ? state
        : { ...state, mic: { status: "released", muted: false, report: null, message: null } };
    case "mic_muted":
      return { ...state, mic: { ...state.mic, muted: action.muted } };
    case "agent_audio":
      return {
        ...state,
        agentAudio: action.status,
        audioVerified: state.audioVerified || action.status.audible,
      };
    case "agent_presence":
      // Agent gone while we stay connected: never keep a stale speaking state.
      return action.present || state.phase !== "live"
        ? state
        : { ...state, agentState: "recovering" };
    case "agent_state":
      return { ...state, agentState: action.state };
    case "message":
      return applyMessage(state, action.message);
    case "message_rejected":
      return { ...state, rejectedMessages: state.rejectedMessages + 1 };
    case "events_loaded":
      return { ...state, events: action.events };
    case "evidence_loaded":
      return { ...state, evidence: action.evidence };
    case "failed":
      return { ...state, phase: "failed", error: action.error };
    case "reset":
      return INITIAL_SESSION_STATE;
  }
}
