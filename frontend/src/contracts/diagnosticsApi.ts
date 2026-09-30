/**
 * Browser-safe provider-operation and cost read models (docs/04 §12, §14).
 *
 * Parsers are deliberately tolerant of additive fields and of new enum
 * values (units, statuses): only the fields the evidence panel reads are
 * validated, and everything is bounded.
 */
import { readArray, readRecord, readString, type JsonRecord } from "./validate";

export const STT_COMPONENT = "stt";
export const TRANSCRIBED_AUDIO_SECONDS = "transcribed_audio_seconds";

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
}

export interface CostBreakdown {
  readonly calculationStatus: string;
  readonly totalUsd: string;
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

export function parseCostBreakdown(raw: unknown): CostBreakdown {
  const envelope = readRecord(raw, "response");
  const data = readRecord(envelope["data"], "data");
  return {
    calculationStatus: readString(data, "calculation_status", "data", 32),
    totalUsd: readString(data, "total_usd", "data", 32),
    components: readArray(data["components"], "data.components").map((item, index) => {
      const path = `data.components[${String(index)}]`;
      const source = readRecord(item, path);
      return {
        component: readString(source, "component", path, 64),
        label: readString(source, "label", path, 128),
        amountUsd: readString(source, "amount_usd", path, 32),
      };
    }),
  };
}
