/**
 * Per-session STT evidence summary (docs/04 §12, §14). Pure functions so the
 * controller and the panel stay trivial. "Not available" is a neutral state:
 * a missing or not-ready read model is never an error.
 */
import {
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

export type EvidenceState =
  | { readonly status: "idle" }
  | {
      readonly status: "ready";
      readonly operations: SttOperationsSummary | null;
      readonly cost: SttCostSummary | null;
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
