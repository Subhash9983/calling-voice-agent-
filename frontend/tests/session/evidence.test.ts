import { describe, expect, it } from "vitest";
import type { ErrorItemView } from "../../src/contracts/diagnosticsApi";
import type { LatencySummaryView } from "../../src/contracts/sessionApi";
import {
  summarizeConversationCost,
  summarizeConversationOperations,
  summarizeCost,
  summarizeLatency,
  summarizeOperations,
  summarizeOutcome,
  summarizeTtsCost,
  summarizeTtsOperations,
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

const ttsOp = (count: { characters?: number; firstAudioMs?: number }) => ({
  operationId: `op-tts-${String(count.characters ?? 0)}-${String(count.firstAudioMs ?? 0)}`,
  component: "tts",
  provider: "sarvam",
  status: "succeeded",
  usage: [
    ...(count.characters === undefined ? [] : [{ unit: "synthesized_characters", quantity: count.characters }]),
    ...(count.firstAudioMs === undefined ? [] : [{ unit: "first_audio_ms", quantity: count.firstAudioMs }]),
  ],
});

describe("summarizeTtsOperations", () => {
  it("counts only TTS operations, sums characters, and averages first-audio timing across segments", () => {
    expect(
      summarizeTtsOperations([
        ttsOp({ characters: 13, firstAudioMs: 400 }),
        ttsOp({ characters: 41, firstAudioMs: 600 }),
        { operationId: "stt-1", component: "stt", provider: "p", status: "succeeded", usage: [] },
      ]),
    ).toEqual({ count: 2, charactersSynthesized: 54, firstAudioMs: 500 });
  });

  it("reports unavailable (never zero) usage when it is absent", () => {
    expect(summarizeTtsOperations([ttsOp({})])).toEqual({
      count: 1,
      charactersSynthesized: null,
      firstAudioMs: null,
    });
  });

  it("returns null when there is no TTS operation", () => {
    expect(summarizeTtsOperations([])).toBeNull();
    expect(
      summarizeTtsOperations([{ operationId: "s", component: "stt", provider: "p", status: "succeeded", usage: [] }]),
    ).toBeNull();
  });
});

describe("summarizeTtsCost", () => {
  it("picks the TTS component amount", () => {
    const cost = summarizeTtsCost({
      calculationStatus: "final",
      totalUsd: "1.00",
      components: [
        { component: "stt", label: "S", amountUsd: "0.1" },
        { component: "tts", label: "T", amountUsd: "0.9" },
      ],
    });
    expect(cost).toEqual({ ttsUsd: "0.9", calculationStatus: "final" });
  });

  it("has no TTS amount when the component is missing", () => {
    expect(summarizeTtsCost({ calculationStatus: "partial", totalUsd: "0", components: [] }).ttsUsd).toBeNull();
  });
});

const errorItem = (component: string, errorType: string): ErrorItemView => ({
  errorId: `err-${component}-${errorType}`,
  component,
  errorType,
  category: "transient",
  severity: "error",
  retryable: true,
  recovered: true,
  userAffected: false,
  safeMessage: "safe",
  occurredAt: "2026-09-29T10:00:00Z",
});

describe("summarizeOutcome", () => {
  it("reports a clean end with zero errors, never null, when the errors fetch succeeded", () => {
    const outcome = summarizeOutcome({ status: "ended", disconnectReason: "user_ended" }, []);
    expect(outcome).toEqual({
      status: "ended",
      disconnectReason: "user_ended",
      errorCount: 0,
      errorCodes: [],
    });
  });

  it("groups errors by component and safe error type", () => {
    const outcome = summarizeOutcome(
      { status: "failed", disconnectReason: "provider_error" },
      [errorItem("stt", "provider_timeout"), errorItem("stt", "provider_timeout"), errorItem("tts", "quota_exceeded")],
    );
    expect(outcome.errorCount).toBe(3);
    expect(outcome.errorCodes).toEqual(
      expect.arrayContaining([
        { component: "stt", errorType: "provider_timeout", count: 2 },
        { component: "tts", errorType: "quota_exceeded", count: 1 },
      ]),
    );
  });

  it("reports errorCount as null (not zero) when the errors fetch is unavailable", () => {
    const outcome = summarizeOutcome({ status: "ended", disconnectReason: "user_ended" }, null);
    expect(outcome.errorCount).toBeNull();
    expect(outcome.errorCodes).toEqual([]);
  });
});

const METRIC = { sampleCount: 2, averageMs: 100, p50Ms: 95, p95Ms: 150, maximumMs: 160 };

describe("summarizeLatency", () => {
  it("returns every known stage as not-available when the summary is absent", () => {
    const stages = summarizeLatency(null);
    expect(stages).toHaveLength(6);
    expect(stages.every((stage) => stage.metric === null)).toBe(true);
    expect(stages.map((stage) => stage.label)).toContain("Complete turn");
  });

  it("surfaces available stages and keeps missing ones null", () => {
    const latency: LatencySummaryView = {
      sttFinalization: METRIC,
      llmFirstToken: null,
      ttsFirstAudio: null,
      firstAudibleResponse: null,
      completeTurn: METRIC,
      interruption: null,
    };
    const stages = summarizeLatency(latency);
    expect(stages.find((stage) => stage.key === "sttFinalization")?.metric).toEqual(METRIC);
    expect(stages.find((stage) => stage.key === "llmFirstToken")?.metric).toBeNull();
    expect(stages.find((stage) => stage.key === "completeTurn")?.metric).toEqual(METRIC);
  });
});
