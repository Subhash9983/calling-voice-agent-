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
  type OperationView,
} from "../contracts/diagnosticsApi";

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
