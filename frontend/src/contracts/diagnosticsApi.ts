/**
 * Browser-safe provider-operation and cost read models (docs/04 §12, §14).
 *
 * Parsers are deliberately tolerant of additive fields and of new enum
 * values (units, statuses): only the fields the evidence panel reads are
 * validated, and everything is bounded.
 */
import {
  readArray,
  readBoolean,
  readOptionalBoolean,
  readOptionalString,
  readRecord,
  readString,
  type JsonRecord,
} from "./validate";

export const STT_COMPONENT = "stt";
export const TRANSCRIBED_AUDIO_SECONDS = "transcribed_audio_seconds";

/** Conversation-engine (LLM) component and usage units (docs/02 §9-§10). */
export const CONVERSATION_COMPONENT = "conversation_engine";
export const INPUT_TOKENS = "input_tokens";
export const OUTPUT_TOKENS = "output_tokens";

/** TTS component and usage units (docs/09 §9-§10). */
export const TTS_COMPONENT = "tts";
export const SYNTHESIZED_CHARACTERS = "synthesized_characters";
/**
 * Optional first-audio latency usage unit. Not part of the current backend
 * fixtures; tolerated as an additive unit so the summary stays accurate if a
 * future operation row reports it (docs/09 §12 first-audio timing).
 */
export const FIRST_AUDIO_MS = "first_audio_ms";

export interface OperationUsageItem {
  readonly unit: string;
  readonly quantity: number;
}

export interface OperationView {
  readonly operationId: string;
  readonly component: string;
  readonly provider: string;
  readonly status: string;
  readonly usage: readonly OperationUsageItem[];
}

export interface CostComponentView {
  readonly component: string;
  readonly label: string;
  readonly amountUsd: string;
  /** Absent/null when this component's own FX conversion is unavailable (never fabricated). */
  readonly amountInrDisplay?: string | null;
}

export interface CostBreakdown {
  readonly calculationStatus: string;
  readonly totalUsd: string;
  /** Display-only INR conversion of `totalUsd` (Decision 069); absent/null when unavailable. */
  readonly totalInrDisplay?: string | null;
  readonly components: readonly CostComponentView[];
}

function parseUsageItems(usage: unknown, path: string): readonly OperationUsageItem[] {
  const record = readRecord(usage, `${path}.usage`);
  return readArray(record["items"], `${path}.usage.items`).flatMap((raw, index) => {
    const item = readRecord(raw, `${path}.usage.items[${String(index)}]`);
    const quantity = Number.parseFloat(readString(item, "quantity", path, 64));
    return Number.isFinite(quantity)
      ? [{ unit: readString(item, "unit", path, 64), quantity }]
      : [];
  });
}

function parseOperation(raw: unknown, index: number): OperationView {
  const path = `items[${String(index)}]`;
  const source: JsonRecord = readRecord(raw, path);
  return {
    operationId: readString(source, "operation_id", path, 64),
    component: readString(source, "component", path, 64),
    provider: readString(source, "provider", path, 128),
    status: readString(source, "status", path, 32),
    usage: parseUsageItems(source["usage"], path),
  };
}

export function parseOperationList(raw: unknown): readonly OperationView[] {
  const envelope = readRecord(raw, "response");
  return readArray(envelope["items"], "response.items").map(parseOperation);
}

/**
 * Safe error diagnostic (docs/04 §13). `safeMessage` is the backend-bounded
 * ``safe_message`` field, never a raw provider exception or stack trace.
 */
export interface ErrorItemView {
  readonly errorId: string;
  readonly component: string;
  readonly errorType: string;
  readonly category: string;
  readonly severity: string;
  readonly retryable: boolean;
  readonly recovered: boolean | null;
  readonly userAffected: boolean;
  readonly safeMessage: string;
  readonly occurredAt: string;
}

export function parseErrorList(raw: unknown): readonly ErrorItemView[] {
  const envelope = readRecord(raw, "response");
  return readArray(envelope["items"], "response.items").map((item, index) => {
    const path = `items[${String(index)}]`;
    const source = readRecord(item, path);
    return {
      errorId: readString(source, "error_id", path, 64),
      component: readString(source, "component", path, 64),
      errorType: readString(source, "error_type", path, 64),
      category: readString(source, "category", path, 64),
      severity: readString(source, "severity", path, 16),
      retryable: readBoolean(source, "retryable", path),
      recovered: readOptionalBoolean(source, "recovered", path),
      userAffected: readBoolean(source, "user_affected", path),
      safeMessage: readString(source, "safe_message", path, 500),
      occurredAt: readString(source, "occurred_at", path, 64),
    };
  });
}

export function parseCostBreakdown(raw: unknown): CostBreakdown {
  const envelope = readRecord(raw, "response");
  const data = readRecord(envelope["data"], "data");
  return {
    calculationStatus: readString(data, "calculation_status", "data", 32),
    totalUsd: readString(data, "total_usd", "data", 32),
    totalInrDisplay: readOptionalString(data, "total_inr_display", "data", 32),
    components: readArray(data["components"], "data.components").map((item, index) => {
      const path = `data.components[${String(index)}]`;
      const source = readRecord(item, path);
      return {
        component: readString(source, "component", path, 64),
        label: readString(source, "label", path, 128),
        amountUsd: readString(source, "amount_usd", path, 32),
        amountInrDisplay: readOptionalString(source, "amount_inr_display", path, 32),
      };
    }),
  };
}
