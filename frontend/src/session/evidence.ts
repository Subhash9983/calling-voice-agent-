/**
 * Per-session STT evidence summary (docs/04 §12, §14). Pure functions so the
 * controller and the panel stay trivial. "Not available" is a neutral state:
 * a missing or not-ready read model is never an error.
 */
import {
  CONVERSATION_COMPONENT,
  FIRST_AUDIO_MS,
  INPUT_TOKENS,
  OUTPUT_TOKENS,
  STT_COMPONENT,
  SYNTHESIZED_CHARACTERS,
  TRANSCRIBED_AUDIO_SECONDS,
  TTS_COMPONENT,
  type CostBreakdown,
  type ErrorItemView,
  type OperationView,
} from "../contracts/diagnosticsApi";
import type { DisconnectReason, LatencyMetricView, LatencySummaryView, SessionStatus } from "../contracts/sessionApi";

export interface SttOperationsSummary {
  readonly count: number;
  /** Null when no operation reported transcribed audio seconds. */
  readonly audioSeconds: number | null;
}

export interface SttCostSummary {
  /** Null when the calculation has no STT component row. */
  readonly sttUsd: string | null;
  readonly calculationStatus: string;
}

export interface ConversationOperationsSummary {
  readonly count: number;
  /** Null when usage is unavailable; never fabricated as zero (docs/08 §12). */
  readonly inputTokens: number | null;
  readonly outputTokens: number | null;
}

export interface ConversationCostSummary {
  /** Null when the calculation has no conversation-engine component row. */
  readonly conversationUsd: string | null;
  readonly calculationStatus: string;
}

export interface TtsOperationsSummary {
  readonly count: number;
  /** Null when no operation reported synthesized characters. */
  readonly charactersSynthesized: number | null;
  /** Null when no operation reported a first-audio latency (docs/09 §12). */
  readonly firstAudioMs: number | null;
}

export interface TtsCostSummary {
  /** Null when the calculation has no TTS component row. */
  readonly ttsUsd: string | null;
  readonly calculationStatus: string;
}

/** A safe error code grouping, never raw provider error text (docs/04 §13). */
export interface ErrorCodeCount {
  readonly component: string;
  readonly errorType: string;
  readonly count: number;
}

/**
 * How the session ended: known locally (what the browser requested/observed)
 * plus the safe error evidence (docs/04 §13). `errorCount` is `null`, never
 * zero, when the `/errors` fetch itself failed or has not run yet.
 */
export interface SessionOutcomeSummary {
  readonly status: SessionStatus;
  readonly disconnectReason: DisconnectReason | null;
  readonly errorCount: number | null;
  readonly errorCodes: readonly ErrorCodeCount[];
}

export function summarizeOutcome(
  session: { readonly status: SessionStatus; readonly disconnectReason: DisconnectReason | null },
  errors: readonly ErrorItemView[] | null,
): SessionOutcomeSummary {
  if (errors === null) {
    return {
      status: session.status,
      disconnectReason: session.disconnectReason,
      errorCount: null,
      errorCodes: [],
    };
  }
  const counts = new Map<string, ErrorCodeCount>();
  for (const error of errors) {
    const key = `${error.component}:${error.errorType}`;
    const existing = counts.get(key);
    counts.set(key, {
      component: error.component,
      errorType: error.errorType,
      count: (existing?.count ?? 0) + 1,
    });
  }
  return {
    status: session.status,
    disconnectReason: session.disconnectReason,
    errorCount: errors.length,
    errorCodes: Array.from(counts.values()),
  };
}

/** One row of the latency breakdown; `metric` is `null` when that stage is not available yet. */
export interface LatencyStageSummary {
  readonly key: string;
  readonly label: string;
  readonly metric: LatencyMetricView | null;
}

const LATENCY_STAGES: readonly { readonly key: keyof LatencySummaryView; readonly label: string }[] = [
  { key: "sttFinalization", label: "STT finalization" },
  { key: "llmFirstToken", label: "LLM first token" },
  { key: "ttsFirstAudio", label: "TTS first audio" },
  { key: "firstAudibleResponse", label: "First audible response" },
  { key: "completeTurn", label: "Complete turn" },
  { key: "interruption", label: "Interruption" },
];

/** Not available yet (never fabricated) when the summary itself is absent. */
export function summarizeLatency(latency: LatencySummaryView | null): readonly LatencyStageSummary[] {
  if (latency === null) {
    return LATENCY_STAGES.map((stage) => ({ key: stage.key, label: stage.label, metric: null }));
  }
  return LATENCY_STAGES.map((stage) => ({ key: stage.key, label: stage.label, metric: latency[stage.key] }));
}

export type EvidenceState =
  | { readonly status: "idle" }
  | {
      readonly status: "ready";
      readonly operations: SttOperationsSummary | null;
      readonly cost: SttCostSummary | null;
      readonly conversation?: ConversationOperationsSummary | null;
      readonly conversationCost?: ConversationCostSummary | null;
      readonly tts?: TtsOperationsSummary | null;
      readonly ttsCost?: TtsCostSummary | null;
      readonly overallCost?: OverallCostSummary | null;
      readonly outcome: SessionOutcomeSummary;
      readonly latency: readonly LatencyStageSummary[];
    };

export const NO_EVIDENCE: EvidenceState = { status: "idle" };

export function summarizeOperations(operations: readonly OperationView[]): SttOperationsSummary | null {
  const stt = operations.filter((operation) => operation.component === STT_COMPONENT);
  if (stt.length === 0) {
    return null;
  }
  const seconds = stt
    .flatMap((operation) => operation.usage)
    .filter((item) => item.unit === TRANSCRIBED_AUDIO_SECONDS);
  return {
    count: stt.length,
    audioSeconds: seconds.length === 0 ? null : seconds.reduce((sum, item) => sum + item.quantity, 0),
  };
}

export function summarizeCost(breakdown: CostBreakdown): SttCostSummary {
  const stt = breakdown.components.find((component) => component.component === STT_COMPONENT);
  return { sttUsd: stt?.amountUsd ?? null, calculationStatus: breakdown.calculationStatus };
}

export interface OverallCostSummary {
  readonly totalUsd: string;
  readonly calculationStatus: string;
  /** Null when the session's start/end timestamps are unavailable, or the duration is not positive. */
  readonly costPerMinuteUsd: string | null;
}

const MS_PER_MINUTE = 60_000;
/** The same precision as the backend's own decimal cost strings (docs/15 §9). */
const COST_PER_MINUTE_DECIMALS = 6;

function minutesBetween(createdAt: string | null, endedAt: string | null): number | null {
  if (createdAt === null || endedAt === null) {
    return null;
  }
  const startMs = Date.parse(createdAt);
  const endMs = Date.parse(endedAt);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) {
    return null;
  }
  const minutes = (endMs - startMs) / MS_PER_MINUTE;
  return minutes > 0 ? minutes : null;
}

/** Total session cost, plus a derived (frontend-only) cost-per-minute rate. */
export function summarizeOverallCost(
  breakdown: CostBreakdown | null,
  createdAt: string | null,
  endedAt: string | null,
): OverallCostSummary | null {
  if (breakdown === null) {
    return null;
  }
  const minutes = minutesBetween(createdAt, endedAt);
  const total = Number.parseFloat(breakdown.totalUsd);
  const costPerMinuteUsd =
    minutes === null || !Number.isFinite(total) ? null : (total / minutes).toFixed(COST_PER_MINUTE_DECIMALS);
  return { totalUsd: breakdown.totalUsd, calculationStatus: breakdown.calculationStatus, costPerMinuteUsd };
}

function sumUsage(operations: readonly OperationView[], unit: string): number | null {
  const items = operations.flatMap((operation) => operation.usage).filter((item) => item.unit === unit);
  return items.length === 0 ? null : items.reduce((sum, item) => sum + item.quantity, 0);
}

/** A latency is representative, not additive: average across operations that reported it. */
function averageUsage(operations: readonly OperationView[], unit: string): number | null {
  const items = operations.flatMap((operation) => operation.usage).filter((item) => item.unit === unit);
  return items.length === 0 ? null : items.reduce((sum, item) => sum + item.quantity, 0) / items.length;
}

/** Conversation-engine (LLM) operations summary; "not available yet" is neutral, never an error. */
export function summarizeConversationOperations(
  operations: readonly OperationView[],
): ConversationOperationsSummary | null {
  const conversation = operations.filter((operation) => operation.component === CONVERSATION_COMPONENT);
  if (conversation.length === 0) {
    return null;
  }
  return {
    count: conversation.length,
    inputTokens: sumUsage(conversation, INPUT_TOKENS),
    outputTokens: sumUsage(conversation, OUTPUT_TOKENS),
  };
}

export function summarizeConversationCost(breakdown: CostBreakdown): ConversationCostSummary {
  const conversation = breakdown.components.find(
    (component) => component.component === CONVERSATION_COMPONENT,
  );
  return { conversationUsd: conversation?.amountUsd ?? null, calculationStatus: breakdown.calculationStatus };
}

/** TTS (synthesis) operations summary; "not available yet" is neutral, never an error. */
export function summarizeTtsOperations(operations: readonly OperationView[]): TtsOperationsSummary | null {
  const tts = operations.filter((operation) => operation.component === TTS_COMPONENT);
  if (tts.length === 0) {
    return null;
  }
  return {
    count: tts.length,
    charactersSynthesized: sumUsage(tts, SYNTHESIZED_CHARACTERS),
    firstAudioMs: averageUsage(tts, FIRST_AUDIO_MS),
  };
}

export function summarizeTtsCost(breakdown: CostBreakdown): TtsCostSummary {
  const tts = breakdown.components.find((component) => component.component === TTS_COMPONENT);
  return { ttsUsd: tts?.amountUsd ?? null, calculationStatus: breakdown.calculationStatus };
}
