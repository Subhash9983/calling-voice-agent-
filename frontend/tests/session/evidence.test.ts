import { describe, expect, it } from "vitest";
import {
  summarizeConversationCost,
  summarizeConversationOperations,
  summarizeCost,
  summarizeOperations,
} from "../../src/session/evidence";

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

const conversationOp = (count: { input?: number; output?: number }) => ({
  operationId: `op-${String(count.input ?? 0)}-${String(count.output ?? 0)}`,
  component: "conversation_engine",
  provider: "openai",
  status: "succeeded",
  usage: [
    ...(count.input === undefined ? [] : [{ unit: "input_tokens", quantity: count.input }]),
    ...(count.output === undefined ? [] : [{ unit: "output_tokens", quantity: count.output }]),
  ],
});

describe("summarizeConversationOperations", () => {
  it("counts only conversation-engine operations and sums input/output tokens", () => {
    expect(
      summarizeConversationOperations([
        conversationOp({ input: 100, output: 20 }),
        conversationOp({ input: 50, output: 10 }),
        { operationId: "stt-1", component: "stt", provider: "p", status: "succeeded", usage: [] },
      ]),
    ).toEqual({ count: 2, inputTokens: 150, outputTokens: 30 });
  });

  it("reports unavailable (never zero) token usage when it is absent", () => {
    expect(summarizeConversationOperations([conversationOp({})])).toEqual({
      count: 1,
      inputTokens: null,
      outputTokens: null,
    });
  });

  it("returns null when there is no conversation-engine operation", () => {
    expect(summarizeConversationOperations([])).toBeNull();
    expect(
      summarizeConversationOperations([{ operationId: "s", component: "stt", provider: "p", status: "succeeded", usage: [] }]),
    ).toBeNull();
  });
});

describe("summarizeConversationCost", () => {
  it("picks the conversation-engine component amount", () => {
    const cost = summarizeConversationCost({
      calculationStatus: "final",
      totalUsd: "1.00",
      components: [
        { component: "stt", label: "S", amountUsd: "0.1" },
        { component: "conversation_engine", label: "LLM", amountUsd: "0.9" },
      ],
    });
    expect(cost).toEqual({ conversationUsd: "0.9", calculationStatus: "final" });
  });

  it("has no conversation-engine amount when the component is missing", () => {
    expect(
      summarizeConversationCost({ calculationStatus: "partial", totalUsd: "0", components: [] }).conversationUsd,
    ).toBeNull();
  });
});
