/**
 * Normalized `va.*.v1` LiveKit data-topic contracts (docs/06 §11-§13,
 * docs/01 §9, §20).
 *
 * Frozen wire contract: envelope fields as in docs/06 §11, canonical payload
 * keys per topic (state / text+is_final / playback identity / code+message+
 * retryable). Unknown topics, schema versions and event types are
 * ignored safely; malformed known messages are rejected with a path-only
 * reason so no payload text can leak into logs.
 */
import {
  ContractError,
  readBoolean,
  readNumber,
  readOneOf,
  readOptionalString,
  readRecord,
  readString,
  type JsonRecord,
} from "./validate";
import { AGENT_ACTIVITY_STATES, type AgentActivityState } from "./sessionApi";

export const TOPIC_STATE = "va.state.v1";
export const TOPIC_TRANSCRIPT = "va.transcript.v1";
export const TOPIC_RESPONSE = "va.response.v1";
export const TOPIC_PLAYBACK = "va.playback.v1";
export const TOPIC_ERROR = "va.error.v1";
export const TOPIC_METRICS = "va.metrics.v1";
export const TOPIC_CLIENT = "va.client.v1";

export const INBOUND_TOPICS = [
  TOPIC_STATE,
  TOPIC_TRANSCRIPT,
  TOPIC_RESPONSE,
  TOPIC_PLAYBACK,
  TOPIC_ERROR,
  TOPIC_METRICS,
] as const;
export type InboundTopic = (typeof INBOUND_TOPICS)[number];

export const SCHEMA_VERSION = 1;
/** Maximum encoded reliable application payload (docs/06 §11). */
export const MAX_RELIABLE_BYTES = 8 * 1024;
/** Maximum encoded lossy application payload (docs/06 §11). */
export const MAX_LOSSY_BYTES = 1200;
const MAX_TEXT_CHARS = 4000;

export interface Envelope {
  readonly eventId: string;
  readonly sessionId: string;
  readonly turnId: string | null;
  readonly eventType: string;
  readonly sequenceNumber: number | null;
  readonly occurredAt: string;
  readonly payload: JsonRecord;
}

export type InboundMessage =
  | { readonly topic: "va.state.v1"; readonly envelope: Envelope; readonly state: AgentActivityState }
  | {
      readonly topic: "va.transcript.v1";
      readonly envelope: Envelope;
      readonly text: string;
      readonly isFinal: boolean;
    }
  | {
      readonly topic: "va.response.v1";
      readonly envelope: Envelope;
      readonly text: string;
      readonly isFinal: boolean;
    }
  | {
      readonly topic: "va.playback.v1";
      readonly envelope: Envelope;
      readonly state: PlaybackState;
      readonly identity: PlaybackAckIdentity;
    }
  | {
      readonly topic: "va.error.v1";
      readonly envelope: Envelope;
      readonly code: string;
      readonly message: string;
      readonly retryable: boolean;
    }
  | { readonly topic: "va.metrics.v1"; readonly envelope: Envelope };

export const PLAYBACK_STATES = [
  "started",
  "progress",
  "completed",
  "cancelled",
  "interrupted",
  "failed",
] as const;
export type PlaybackState = (typeof PLAYBACK_STATES)[number];

export type DecodeResult =
  | { readonly ok: true; readonly message: InboundMessage }
  | { readonly ok: false; readonly reason: DecodeFailure };

export type DecodeFailure =
  | "unknown_topic"
  | "oversized"
  | "invalid_json"
  | "unsupported_schema_version"
  | "invalid_message";

function isInboundTopic(topic: string): topic is InboundTopic {
  return (INBOUND_TOPICS as readonly string[]).includes(topic);
}

function parseEnvelope(raw: unknown): Envelope {
  const source = readRecord(raw, "envelope");
  if (source["schema_version"] !== SCHEMA_VERSION) {
    throw new UnsupportedVersionError();
  }
  const sequence = source["sequence_number"];
  return {
    eventId: readString(source, "event_id", "envelope", 64),
    sessionId: readString(source, "session_id", "envelope", 64),
    turnId: readOptionalString(source, "turn_id", "envelope", 64),
    eventType: readString(source, "event_type", "envelope", 100),
    sequenceNumber:
      sequence === undefined || sequence === null
        ? null
        : readNumber(source, "sequence_number", "envelope"),
    occurredAt: readString(source, "occurred_at", "envelope", 64),
    payload: readRecord(source["payload"], "envelope.payload"),
  };
}

class UnsupportedVersionError extends ContractError {
  public constructor() {
    super("envelope.schema_version", "unsupported schema version");
  }
}

function readIdentity(payload: JsonRecord): PlaybackAckIdentity {
  const workerGeneration = readNumber(payload, "worker_generation", "payload");
  const cancellationGeneration = readNumber(payload, "cancellation_generation", "payload");
  if (
    !Number.isInteger(workerGeneration) ||
    workerGeneration < 1 ||
    !Number.isInteger(cancellationGeneration) ||
    cancellationGeneration < 0
  ) {
    throw new ContractError("payload.generation", "generation out of range");
  }
  return {
    workerGeneration,
    cancellationGeneration,
    segmentId: readString(payload, "segment_id", "payload", 64),
  };
}

/** Canonical agent-to-browser payload keys (frozen contract, WP6). */
function buildMessage(topic: InboundTopic, envelope: Envelope): InboundMessage {
  const { payload } = envelope;
  switch (topic) {
    case TOPIC_STATE:
      return {
        topic,
        envelope,
        state: readOneOf(payload, "state", "payload", AGENT_ACTIVITY_STATES),
      };
    case TOPIC_TRANSCRIPT:
    case TOPIC_RESPONSE:
      return {
        topic,
        envelope,
        text: readString(payload, "text", "payload", MAX_TEXT_CHARS),
        isFinal: readBoolean(payload, "is_final", "payload"),
      };
    case TOPIC_PLAYBACK:
      return {
        topic,
        envelope,
        state: readOneOf(payload, "state", "payload", PLAYBACK_STATES),
        identity: readIdentity(payload),
      };
    case TOPIC_ERROR:
      return {
        topic,
        envelope,
        code: readString(payload, "code", "payload", 64),
        message: readString(payload, "message", "payload", 500),
        retryable: readBoolean(payload, "retryable", "payload"),
      };
    case TOPIC_METRICS:
      return { topic, envelope };
  }
}

/**
 * Decodes and validates one received data packet. Never throws: any failure
 * becomes a `DecodeResult` with a category-only reason.
 */
export function decodeInbound(
  topic: string | undefined,
  data: Uint8Array,
  isReliable: boolean,
): DecodeResult {
  if (topic === undefined || !isInboundTopic(topic)) {
    return { ok: false, reason: "unknown_topic" };
  }
  const limit = isReliable ? MAX_RELIABLE_BYTES : MAX_LOSSY_BYTES;
  if (data.byteLength > limit) {
    return { ok: false, reason: "oversized" };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(data));
  } catch {
    return { ok: false, reason: "invalid_json" };
  }
  try {
    return { ok: true, message: buildMessage(topic, parseEnvelope(parsed)) };
  } catch (error) {
    if (error instanceof UnsupportedVersionError) {
      return { ok: false, reason: "unsupported_schema_version" };
    }
    return { ok: false, reason: "invalid_message" };
  }
}

// ---------------------------------------------------------------- outbound --

export type ClientEventType =
  | "client.ready"
  | "client.mic_muted"
  | "client.mic_unmuted"
  | "playback.started"
  | "playback.progress"
  | "playback.completed"
  | "playback.failed";

/** Approved playback acknowledgement identity (docs/06 §13). */
export interface PlaybackAckIdentity {
  readonly workerGeneration: number;
  readonly cancellationGeneration: number;
  readonly segmentId: string;
}

export interface ClientEventInput {
  readonly eventType: ClientEventType;
  readonly sessionId: string;
  readonly eventId: string;
  readonly occurredAt: string;
  readonly payload?: Readonly<Record<string, string | number | boolean>>;
}

export interface EncodedClientEvent {
  readonly topic: typeof TOPIC_CLIENT;
  readonly reliable: boolean;
  readonly bytes: Uint8Array<ArrayBuffer>;
}

/** Progress is lossy (freshness over replay); everything else is reliable. */
export function encodeClientEvent(input: ClientEventInput): EncodedClientEvent {
  const reliable = input.eventType !== "playback.progress";
  const body = {
    schema_version: SCHEMA_VERSION,
    event_id: input.eventId,
    session_id: input.sessionId,
    event_type: input.eventType,
    occurred_at: input.occurredAt,
    payload: input.payload ?? {},
  };
  const bytes = new TextEncoder().encode(JSON.stringify(body));
  const limit = reliable ? MAX_RELIABLE_BYTES : MAX_LOSSY_BYTES;
  if (bytes.byteLength > limit) {
    throw new ContractError("client_event", "encoded event exceeds the approved payload bound");
  }
  return { topic: TOPIC_CLIENT, reliable, bytes };
}

export function playbackPayload(
  identity: PlaybackAckIdentity,
  positionMs?: number,
): Readonly<Record<string, string | number>> {
  const base = {
    worker_generation: identity.workerGeneration,
    cancellation_generation: identity.cancellationGeneration,
    segment_id: identity.segmentId,
  };
  return positionMs === undefined ? base : { ...base, position_ms: Math.max(0, Math.round(positionMs)) };
}

