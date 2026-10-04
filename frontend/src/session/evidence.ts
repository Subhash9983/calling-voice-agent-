/**
 * Per-session STT evidence summary (docs/04 §12, §14). Pure functions so the
 * controller and the panel stay trivial. "Not available" is a neutral state:
 * a missing or not-ready read model is never an error.
 */
import {
  CONVERSATION_COMPONENT,
  INPUT_TOKENS,
  OUTPUT_TOKENS,
  STT_COMPONENT,
  TRANSCRIBED_AUDIO_SECONDS,
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

export type EvidenceState =
  | { readonly status: "idle" }
  | {
      readonly status: "ready";
      readonly operations: SttOperationsSummary | null;
      readonly cost: SttCostSummary | null;
      readonly conversation?: ConversationOperationsSummary | null;
      readonly conversationCost?: ConversationCostSummary | null;
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
