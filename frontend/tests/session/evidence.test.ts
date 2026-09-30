import { describe, expect, it } from "vitest";
import { summarizeCost, summarizeOperations } from "../../src/session/evidence";

const op = (component: string, seconds?: number) => ({
  operationId: `op-${component}-${String(seconds ?? 0)}`,
  component,
  provider: "p",
  status: "succeeded",
  usage: seconds === undefined ? [] : [{ unit: "transcribed_audio_seconds", quantity: seconds }],
});

describe("summarizeOperations", () => {
  it("counts only STT operations and sums transcribed audio seconds", () => {
    expect(summarizeOperations([op("stt", 4.5), op("stt", 5.5), op("tts", 99)])).toEqual({ count: 2, audioSeconds: 10 });
  });

  it("reports unknown audio seconds when usage is absent", () => {
    expect(summarizeOperations([op("stt")])).toEqual({ count: 1, audioSeconds: null });
  });

  it("returns null when there is no STT operation", () => {
    expect(summarizeOperations([op("tts", 1)])).toBeNull();
    expect(summarizeOperations([])).toBeNull();
  });
});

describe("summarizeCost", () => {
  it("picks the STT component amount", () => {
    const cost = summarizeCost({
      calculationStatus: "final",
      totalUsd: "1.00",
      components: [
        { component: "tts", label: "T", amountUsd: "0.9" },
        { component: "stt", label: "S", amountUsd: "0.1" },
      ],
    });
    expect(cost).toEqual({ sttUsd: "0.1", calculationStatus: "final" });
  });

  it("has no STT amount when the component is missing", () => {
    expect(summarizeCost({ calculationStatus: "partial", totalUsd: "0", components: [] }).sttUsd).toBeNull();
  });
});
