/**
 * Defense-in-depth (docs/02 §restricted fields, docs/04 §12-§13, §19): every
 * evidence decoder reads an explicit allowlist of fields. These tests inject
 * restricted-looking values (raw provider payloads, prompts, credentials,
 * stack traces) alongside the documented safe fields and assert the parsed
 * view model never carries them, even if a future backend bug emitted them.
 */
import { describe, expect, it } from "vitest";
import {
  parseCostBreakdown,
  parseErrorList,
  parseOperationList,
} from "../../src/contracts/diagnosticsApi";
import { parseEventList, parseSessionSummary } from "../../src/contracts/sessionApi";

const RESTRICTED = {
  prompt: "system instructions should never reach the browser",
  system_instructions: "do not leak",
  api_key: "sk-should-not-leak",
  credentials: { token: "secret" },
  headers: { authorization: "Bearer secret" },
  raw_response: { choices: ["leaked completion"] },
  stack_trace: "Traceback (most recent call last): ...",
  provider_payload: { internal: true },
};

function keysOf(value: object): readonly string[] {
  return Object.keys(value);
}

describe("restricted-field absence: operations", () => {
  it("never surfaces fields outside the documented operation allowlist", () => {
    const [operation] = parseOperationList({
      items: [
        {
          operation_id: "op-1",
          component: "stt",
          provider: "deepgram",
          status: "succeeded",
          usage: { items: [] },
          ...RESTRICTED,
        },
      ],
    });
    expect(keysOf(operation as object)).toEqual(["operationId", "component", "provider", "status", "usage"]);
  });
});

describe("restricted-field absence: costs", () => {
  it("never surfaces fields outside the documented cost allowlist", () => {
    const breakdown = parseCostBreakdown({
      data: {
        calculation_status: "final",
        total_usd: "0.01",
        total_inr_display: "0.85",
        components: [
          { component: "stt", label: "Deepgram", amount_usd: "0.01", amount_inr_display: "0.85", ...RESTRICTED },
        ],
        ...RESTRICTED,
      },
    });
    expect(keysOf(breakdown as object)).toEqual(["calculationStatus", "totalUsd", "totalInrDisplay", "components"]);
    expect(keysOf(breakdown.components[0] as object)).toEqual([
      "component",
      "label",
      "amountUsd",
      "amountInrDisplay",
    ]);
  });
});

describe("restricted-field absence: errors", () => {
  it("never surfaces fields outside the documented safe-error allowlist", () => {
    const [error] = parseErrorList({
      items: [
        {
          error_id: "err-1",
          component: "stt",
          error_type: "provider_timeout",
          category: "transient",
          severity: "error",
          retryable: true,
          recovered: true,
          user_affected: false,
          safe_message: "safe",
          occurred_at: "2026-09-29T10:00:00Z",
          ...RESTRICTED,
        },
      ],
    });
    expect(keysOf(error as object)).toEqual([
      "errorId",
      "component",
      "errorType",
      "category",
      "severity",
      "retryable",
      "recovered",
      "userAffected",
      "safeMessage",
      "occurredAt",
    ]);
  });
});

describe("restricted-field absence: events", () => {
  it("never surfaces fields outside the documented safe-event allowlist", () => {
    const [event] = parseEventList({
      items: [
        {
          event_id: "e1",
          event_type: "session.active",
          severity: "info",
          sequence_number: 1,
          occurred_at: "2026-09-29T10:00:00Z",
          payload: { internal: "leaked" },
          ...RESTRICTED,
        },
      ],
    });
    expect(keysOf(event as object)).toEqual([
      "eventId",
      "eventType",
      "severity",
      "sequenceNumber",
      "occurredAt",
    ]);
  });
});

describe("restricted-field absence: session summary and latency", () => {
  it("never surfaces fields outside the documented session-summary allowlist", () => {
    const summary = parseSessionSummary({
      data: {
        session_id: "s1",
        status: "ended",
        agent_activity_state: null,
        disconnect_reason: "user_ended",
        latency_summary: {
          complete_turn: { sample_count: 1, average_ms: 100, p50_ms: 100, p95_ms: 100, maximum_ms: 100, ...RESTRICTED },
          ...RESTRICTED,
        },
        ...RESTRICTED,
      },
    });
    expect(keysOf(summary as object)).toEqual([
      "sessionId",
      "status",
      "agentActivityState",
      "disconnectReason",
      "latencySummary",
      "createdAt",
      "endedAt",
    ]);
    const completeTurn = summary.latencySummary?.completeTurn;
    expect(completeTurn === null || completeTurn === undefined).toBe(false);
    expect(keysOf(completeTurn as object)).toEqual([
      "sampleCount",
      "averageMs",
      "p50Ms",
      "p95Ms",
      "maximumMs",
    ]);
  });
});
