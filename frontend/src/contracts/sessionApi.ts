/**
 * Typed control-API contracts (docs/04 §5-§11, §18-§19; backend source of
 * truth: backend/src/voice_agent/control_api/schemas/{sessions,diagnostics}.py).
 *
 * Responses are parsed field by field so an unexpected shape is a
 * normalized client error, never a runtime crash deeper in the UI.
 */
import {
  isRecord,
  readArray,
  readBoolean,
  readNumber,
  readOneOf,
  readOptionalNumber,
  readOptionalOneOf,
  readOptionalString,
  readRecord,
  readString,
} from "./validate";

export const SESSION_STATUSES = [
  "created",
  "connecting",
  "active",
  "ending",
  "ended",
  "failed",
] as const;
export type SessionStatus = (typeof SESSION_STATUSES)[number];

export const AGENT_ACTIVITY_STATES = [
  "idle",
  "listening",
  "transcribing",
  "thinking",
  "speaking",
  "interrupted",
  "recovering",
  "error",
] as const;
export type AgentActivityState = (typeof AGENT_ACTIVITY_STATES)[number];

export const DISCONNECT_REASONS = [
  "user_ended",
  "browser_closed",
  "idle_timeout",
  "maximum_duration",
  "network_lost",
  "transport_error",
  "provider_error",
  "server_shutdown",
  "unknown",
] as const;
export type DisconnectReason = (typeof DISCONNECT_REASONS)[number];

export interface AgentConfigView {
  readonly agentConfigId: string;
  readonly name: string;
  readonly version: number;
  readonly description: string | null;
}

export interface SessionBrief {
  readonly sessionId: string;
  readonly status: SessionStatus;
  readonly agentActivityState: AgentActivityState | null;
  readonly maximumSessionMs: number;
}

/** Join credentials. `joinToken` is memory-only and never persisted or logged. */
export interface TransportJoin {
  readonly provider: string;
  readonly url: string;
  readonly roomName: string | null;
  readonly participantIdentity: string | null;
  readonly joinToken: string | null;
  readonly tokenExpiresAt: string | null;
}

export interface ConfigurationLabels {
  readonly name: string | null;
  readonly version: number;
  readonly stt: string;
  readonly conversationEngine: string;
  readonly tts: string;
}

export interface CreateSessionResult {
  readonly idempotentReplay: boolean;
  readonly session: SessionBrief;
  readonly transport: TransportJoin;
  readonly configuration: ConfigurationLabels;
  readonly requestId: string;
}

export interface JoinTokenResult {
  readonly sessionId: string;
  readonly transport: TransportJoin;
  readonly idempotentReplay: boolean;
}

export interface EndSessionResult {
  readonly sessionId: string;
  readonly status: SessionStatus;
  readonly disconnectReason: DisconnectReason | null;
  readonly idempotentReplay: boolean;
}

export interface SessionEventItem {
  readonly eventId: string;
  readonly eventType: string;
  readonly severity: string;
  readonly sequenceNumber: number;
  readonly occurredAt: string;
}

/** Bounded per-stage timing (docs/02 §12; populated once the worker reports it). */
export interface LatencyMetricView {
  readonly sampleCount: number;
  readonly averageMs: number;
  readonly p50Ms: number;
  readonly p95Ms: number;
  readonly maximumMs: number;
}

/**
 * Stage/end-to-end latency summary (docs/04 §8, §17; docs/02 §12). Every
 * stage is `null` until its metric is fully available; a half-populated
 * metric is never surfaced.
 */
export interface LatencySummaryView {
  readonly sttFinalization: LatencyMetricView | null;
  readonly llmFirstToken: LatencyMetricView | null;
  readonly ttsFirstAudio: LatencyMetricView | null;
  readonly firstAudibleResponse: LatencyMetricView | null;
  readonly completeTurn: LatencyMetricView | null;
  readonly interruption: LatencyMetricView | null;
}

export interface SessionSummary {
  readonly sessionId: string;
  readonly status: SessionStatus;
  readonly agentActivityState: AgentActivityState | null;
  readonly disconnectReason: DisconnectReason | null;
  /** Absent/null until the worker populates it (WP11); never fabricated. */
  readonly latencySummary?: LatencySummaryView | null;
}

export function parseAgentConfig(raw: unknown, path = "agent_config"): AgentConfigView {
  const source = readRecord(raw, path);
  return {
    agentConfigId: readString(source, "agent_config_id", path, 64),
    name: readString(source, "name", path, 200),
    version: readNumber(source, "version", path),
    description: readOptionalString(source, "description", path, 1000),
  };
}

export function parseTransportJoin(raw: unknown, path = "transport"): TransportJoin {
  const source = readRecord(raw, path);
  return {
    provider: readString(source, "provider", path, 32),
    url: readString(source, "url", path, 512),
    roomName: readOptionalString(source, "room_name", path, 200),
    participantIdentity: readOptionalString(source, "participant_identity", path, 200),
    joinToken: readOptionalString(source, "join_token", path, 8192),
    tokenExpiresAt: readOptionalString(source, "token_expires_at", path, 64),
  };
}

function parseSessionBrief(raw: unknown): SessionBrief {
  const path = "session";
  const source = readRecord(raw, path);
  return {
    sessionId: readString(source, "session_id", path, 64),
    status: readOneOf(source, "status", path, SESSION_STATUSES),
    agentActivityState: readOptionalOneOf(
      source,
      "agent_activity_state",
      path,
      AGENT_ACTIVITY_STATES,
    ),
    maximumSessionMs: readNumber(source, "maximum_session_ms", path),
  };
}

function parseConfigurationLabels(raw: unknown): ConfigurationLabels {
  const path = "configuration";
  const source = readRecord(raw, path);
  return {
    name: readOptionalString(source, "name", path, 200),
    version: readNumber(source, "version", path),
    stt: readString(source, "stt", path, 200),
    conversationEngine: readString(source, "conversation_engine", path, 200),
    tts: readString(source, "tts", path, 200),
  };
}

export function parseCreateSession(raw: unknown): CreateSessionResult {
  const source = readRecord(raw, "response");
  return {
    idempotentReplay: readBoolean(source, "idempotent_replay", "response"),
    session: parseSessionBrief(source["session"]),
    transport: parseTransportJoin(source["transport"]),
    configuration: parseConfigurationLabels(source["configuration"]),
    requestId: readString(source, "request_id", "response", 64),
  };
}

export function parseJoinToken(raw: unknown): JoinTokenResult {
  const envelope = readRecord(raw, "response");
  const data = readRecord(envelope["data"], "data");
  return {
    sessionId: readString(data, "session_id", "data", 64),
    transport: parseTransportJoin(data["transport"], "data.transport"),
    idempotentReplay: readBoolean(envelope, "idempotent_replay", "response"),
  };
}

export function parseEndSession(raw: unknown): EndSessionResult {
  const envelope = readRecord(raw, "response");
  const data = readRecord(envelope["data"], "data");
  return {
    sessionId: readString(data, "session_id", "data", 64),
    status: readOneOf(data, "status", "data", SESSION_STATUSES),
    disconnectReason: readOptionalOneOf(data, "disconnect_reason", "data", DISCONNECT_REASONS),
    idempotentReplay: readBoolean(envelope, "idempotent_replay", "response"),
  };
}

/** A metric is reported only when every one of its bounded fields is present. */
function parseLatencyMetric(raw: unknown): LatencyMetricView | null {
  if (!isRecord(raw)) {
    return null;
  }
  const sampleCount = readOptionalNumber(raw, "sample_count");
  const averageMs = readOptionalNumber(raw, "average_ms");
  const p50Ms = readOptionalNumber(raw, "p50_ms");
  const p95Ms = readOptionalNumber(raw, "p95_ms");
  const maximumMs = readOptionalNumber(raw, "maximum_ms");
  if (sampleCount === null || averageMs === null || p50Ms === null || p95Ms === null || maximumMs === null) {
    return null;
  }
  return { sampleCount, averageMs, p50Ms, p95Ms, maximumMs };
}

/** Tolerant: an absent/null summary, or an absent stage within it, is "not available yet". */
export function parseLatencySummary(raw: unknown): LatencySummaryView | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    sttFinalization: parseLatencyMetric(raw["stt_finalization"]),
    llmFirstToken: parseLatencyMetric(raw["llm_first_token"]),
    ttsFirstAudio: parseLatencyMetric(raw["tts_first_audio"]),
    firstAudibleResponse: parseLatencyMetric(raw["first_audible_response"]),
    completeTurn: parseLatencyMetric(raw["complete_turn"]),
    interruption: parseLatencyMetric(raw["interruption"]),
  };
}

export function parseSessionSummary(raw: unknown): SessionSummary {
  const envelope = readRecord(raw, "response");
  const data = readRecord(envelope["data"], "data");
  return {
    sessionId: readString(data, "session_id", "data", 64),
    status: readOneOf(data, "status", "data", SESSION_STATUSES),
    agentActivityState: readOptionalOneOf(
      data,
      "agent_activity_state",
      "data",
      AGENT_ACTIVITY_STATES,
    ),
    disconnectReason: readOptionalOneOf(data, "disconnect_reason", "data", DISCONNECT_REASONS),
    latencySummary: parseLatencySummary(data["latency_summary"]),
  };
}

export function parseAgentConfigList(raw: unknown): readonly AgentConfigView[] {
  const envelope = readRecord(raw, "response");
  return readArray(envelope["items"], "response.items").map((item, index) =>
    parseAgentConfig(item, `items[${String(index)}]`),
  );
}

export function parseEventList(raw: unknown): readonly SessionEventItem[] {
  const envelope = readRecord(raw, "response");
  return readArray(envelope["items"], "response.items").map((item, index) => {
    const path = `items[${String(index)}]`;
    const source = readRecord(item, path);
    return {
      eventId: readString(source, "event_id", path, 64),
      eventType: readString(source, "event_type", path, 100),
      severity: readString(source, "severity", path, 16),
      sequenceNumber: readNumber(source, "sequence_number", path),
      occurredAt: readString(source, "occurred_at", path, 64),
    };
  });
}
