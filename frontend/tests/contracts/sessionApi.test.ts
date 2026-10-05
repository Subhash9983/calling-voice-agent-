import { describe, expect, it } from "vitest";
import { parseLatencySummary, parseSessionSummary } from "../../src/contracts/sessionApi";
import { ContractError } from "../../src/contracts/validate";

const METRIC = {
  sample_count: 4,
  average_ms: 120.5,
  p50_ms: 110,
  p95_ms: 190,
  maximum_ms: 210,
};

describe("parseLatencySummary", () => {
  it("returns null when the field is absent or null", () => {
    expect(parseLatencySummary(undefined)).toBeNull();
    expect(parseLatencySummary(null)).toBeNull();
  });

  it("parses every known stage when present", () => {
    const result = parseLatencySummary({
      stt_finalization: METRIC,
      llm_first_token: METRIC,
      tts_first_audio: METRIC,
      first_audible_response: METRIC,
      complete_turn: METRIC,
      interruption: METRIC,
    });
    expect(result?.sttFinalization).toEqual({
      sampleCount: 4,
      averageMs: 120.5,
      p50Ms: 110,
      p95Ms: 190,
      maximumMs: 210,
    });
    expect(result?.interruption).not.toBeNull();
  });

  it("treats a stage as unavailable when it is absent, and never half-populates a metric", () => {
    const result = parseLatencySummary({
      stt_finalization: METRIC,
      llm_first_token: { sample_count: 1, average_ms: 50 },
    });
    expect(result?.sttFinalization).not.toBeNull();
    expect(result?.llmFirstToken).toBeNull();
    expect(result?.ttsFirstAudio).toBeNull();
  });
});

describe("parseSessionSummary", () => {
  it("reads the session fields the controller needs, with latency null until populated", () => {
    const result = parseSessionSummary({
      data: {
        session_id: "s1",
        status: "ended",
        agent_activity_state: null,
        disconnect_reason: "user_ended",
      },
      request_id: "r",
    });
    expect(result).toEqual({
      sessionId: "s1",
      status: "ended",
      agentActivityState: null,
      disconnectReason: "user_ended",
      latencySummary: null,
    });
  });

  it("parses a populated latency summary", () => {
    const result = parseSessionSummary({
      data: {
        session_id: "s1",
        status: "ended",
        agent_activity_state: null,
        disconnect_reason: "user_ended",
        latency_summary: { complete_turn: METRIC },
      },
      request_id: "r",
    });
    expect(result.latencySummary?.completeTurn).toEqual({
      sampleCount: 4,
      averageMs: 120.5,
      p50Ms: 110,
      p95Ms: 190,
      maximumMs: 210,
    });
  });

  it("rejects a malformed session summary without echoing values", () => {
    expect(() => parseSessionSummary({ data: { status: "bogus" } })).toThrow(ContractError);
  });
});
