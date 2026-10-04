import { describe, expect, it } from "vitest";
import {
  MAX_LOSSY_BYTES,
  MAX_RELIABLE_BYTES,
  decodeInbound,
  encodeClientEvent,
  playbackPayload,
} from "../../src/contracts/realtime";

const IDENT = { worker_generation: 2, cancellation_generation: 0, segment_id: "seg-1" };
const encode = (value: unknown): Uint8Array => new TextEncoder().encode(JSON.stringify(value));

function envelope(payload: unknown, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    event_id: "e1",
    session_id: "s1",
    event_type: "x.y",
    sequence_number: 3,
    occurred_at: "2026-09-29T10:00:00Z",
    payload,
    ...overrides,
  };
}

describe("decodeInbound", () => {
  it("decodes an agent state message", () => {
    const result = decodeInbound("va.state.v1", encode(envelope({ state: "speaking" })), true);

    expect(result.ok && result.message.topic === "va.state.v1" && result.message.state).toBe(
      "speaking",
    );
  });

  it("decodes partial and final transcripts", () => {
    const partial = decodeInbound(
      "va.transcript.v1",
      encode(envelope({ text: "hel", is_final: false })),
      false,
    );

    expect(partial.ok && partial.message.topic === "va.transcript.v1" && partial.message.isFinal).toBe(
      false,
    );
  });

  it("decodes response, playback, error and metrics topics", () => {
    const response = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "hi", is_final: true })),
      true,
    );
    expect(
      response.ok && response.message.topic === "va.response.v1" && response.message.completionStatus,
    ).toBeNull();
    const playback = decodeInbound("va.playback.v1", encode(envelope({ state: "started", ...IDENT })), true);
    const error = decodeInbound(
      "va.error.v1",
      encode(envelope({ code: "stt_failed", message: "Try again", retryable: true })),
      true,
    );
    const metrics = decodeInbound("va.metrics.v1", encode(envelope({})), false);

    expect([response.ok, playback.ok, error.ok, metrics.ok]).toEqual([true, true, true, true]);
    expect(error.ok && error.message.topic === "va.error.v1" && error.message.retryable).toBe(true);
  });

  it("exposes the playback ack identity from va.playback.v1", () => {
    const result = decodeInbound(
      "va.playback.v1",
      encode(envelope({ state: "completed", ...IDENT })),
      true,
    );

    expect(result.ok && result.message.topic === "va.playback.v1" && result.message.identity).toEqual({
      workerGeneration: 2,
      cancellationGeneration: 0,
      segmentId: "seg-1",
    });
  });

  it("rejects playback without a valid identity and payloads using non-canonical aliases", () => {
    const missing = decodeInbound("va.playback.v1", encode(envelope({ state: "started" })), true);
    const zeroGeneration = decodeInbound(
      "va.playback.v1",
      encode(envelope({ state: "started", ...IDENT, worker_generation: 0 })),
      true,
    );
    const alias = decodeInbound("va.transcript.v1", encode(envelope({ transcript: "x", is_final: true })), true);
    const noFinality = decodeInbound("va.transcript.v1", encode(envelope({ text: "x" }, { event_type: "stt.final" })), true);
    const aliasState = decodeInbound("va.state.v1", encode(envelope({ agent_activity_state: "idle" })), true);
    const noRetryable = decodeInbound("va.error.v1", encode(envelope({ code: "c", message: "m" })), true);

    for (const result of [missing, zeroGeneration, alias, noFinality, aliasState, noRetryable]) {
      expect(result).toEqual({ ok: false, reason: "invalid_message" });
    }
  });

  it("reads an optional response_completion_status on va.response.v1, tolerating unknown future values", () => {
    const truncated = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "partial answer", is_final: true, response_completion_status: "truncated_partial" })),
      true,
    );
    const unknownFutureValue = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "hi", is_final: true, response_completion_status: "some_future_status" })),
      true,
    );

    expect(
      truncated.ok && truncated.message.topic === "va.response.v1" && truncated.message.completionStatus,
    ).toBe("truncated_partial");
    expect(
      unknownFutureValue.ok &&
        unknownFutureValue.message.topic === "va.response.v1" &&
        unknownFutureValue.message.completionStatus,
    ).toBeNull();
  });

  it("reads an optional fallback_template_id on va.response.v1, defaulting to null when absent", () => {
    const withTemplate = decodeInbound(
      "va.response.v1",
      encode(
        envelope({
          text: "Sorry, the response was cut short.",
          is_final: true,
          response_completion_status: "truncated_fallback",
          fallback_template_id: "fallback.response_truncated.v1",
        }),
      ),
      true,
    );
    const withoutTemplate = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "hi", is_final: true })),
      true,
    );

    expect(
      withTemplate.ok && withTemplate.message.topic === "va.response.v1" && withTemplate.message.fallbackTemplateId,
    ).toBe("fallback.response_truncated.v1");
    expect(
      withoutTemplate.ok &&
        withoutTemplate.message.topic === "va.response.v1" &&
        withoutTemplate.message.fallbackTemplateId,
    ).toBeNull();
  });

  it("reads an optional segment_sequence on a provisional va.response.v1 segment", () => {
    const withSequence = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "Namaste", is_final: false, segment_sequence: 0 })),
      true,
    );
    const withoutSequence = decodeInbound(
      "va.response.v1",
      encode(envelope({ text: "hi", is_final: true })),
      true,
    );

    expect(
      withSequence.ok && withSequence.message.topic === "va.response.v1" && withSequence.message.segmentSequence,
    ).toBe(0);
    expect(
      withoutSequence.ok &&
        withoutSequence.message.topic === "va.response.v1" &&
        withoutSequence.message.segmentSequence,
    ).toBeNull();
  });

  it("decodes the lossy metrics heartbeat", () => {
    const result = decodeInbound(
      "va.metrics.v1",
      encode(envelope({ lease_valid_for_ms: 9000, mic_frames: 50, mic_peak: 0.2, playback_bursts: 0, playback_acks: 0 }, { event_type: "transport.quality_updated" })),
      false,
    );

    expect(result.ok && result.message.topic).toBe("va.metrics.v1");
  });

  it("ignores unknown or missing topics", () => {
    expect(decodeInbound("va.other.v1", encode(envelope({})), true)).toEqual({
      ok: false,
      reason: "unknown_topic",
    });
    expect(decodeInbound(undefined, encode(envelope({})), true)).toEqual({
      ok: false,
      reason: "unknown_topic",
    });
    expect(decodeInbound("va.client.v1", encode(envelope({})), true)).toEqual({
      ok: false,
      reason: "unknown_topic",
    });
  });

  it("rejects unsupported schema versions", () => {
    const result = decodeInbound(
      "va.state.v1",
      encode(envelope({ state: "idle" }, { schema_version: 2 })),
      true,
    );

    expect(result).toEqual({ ok: false, reason: "unsupported_schema_version" });
  });

  it("rejects malformed json and invalid payloads without echoing content", () => {
    expect(decodeInbound("va.state.v1", new TextEncoder().encode("{nope"), true)).toEqual({
      ok: false,
      reason: "invalid_json",
    });
    expect(decodeInbound("va.state.v1", encode(envelope({ state: "dancing" })), true)).toEqual({
      ok: false,
      reason: "invalid_message",
    });
    expect(decodeInbound("va.transcript.v1", encode(envelope({ text: 5 })), true)).toEqual({
      ok: false,
      reason: "invalid_message",
    });
    expect(decodeInbound("va.state.v1", encode([1, 2]), true)).toEqual({
      ok: false,
      reason: "invalid_message",
    });
  });

  it("enforces 8 KiB reliable and 1200 byte lossy limits", () => {
    const big = new Uint8Array(MAX_LOSSY_BYTES + 1);
    const huge = new Uint8Array(MAX_RELIABLE_BYTES + 1);

    expect(decodeInbound("va.state.v1", big, false)).toEqual({ ok: false, reason: "oversized" });
    expect(decodeInbound("va.state.v1", huge, true)).toEqual({ ok: false, reason: "oversized" });
    // 1201 bytes is acceptable size-wise for reliable delivery (fails later on JSON).
    expect(decodeInbound("va.state.v1", big, true)).toEqual({ ok: false, reason: "invalid_json" });
  });
});

describe("encodeClientEvent", () => {
  const base = { sessionId: "s1", eventId: "e1", occurredAt: "2026-09-29T10:00:00Z" } as const;

  it("encodes reliable client events on va.client.v1", () => {
    const encoded = encodeClientEvent({ ...base, eventType: "client.mic_muted" });

    expect(encoded.topic).toBe("va.client.v1");
    expect(encoded.reliable).toBe(true);
    const body = JSON.parse(new TextDecoder().decode(encoded.bytes)) as Record<string, unknown>;
    expect(body).toMatchObject({
      schema_version: 1,
      event_type: "client.mic_muted",
      session_id: "s1",
      payload: {},
    });
  });

  it("sends playback progress lossy with the approved ack identity", () => {
    const encoded = encodeClientEvent({
      ...base,
      eventType: "playback.progress",
      payload: playbackPayload(
        { workerGeneration: 2, cancellationGeneration: 5, segmentId: "seg" },
        120.6,
      ),
    });

    expect(encoded.reliable).toBe(false);
    const body = JSON.parse(new TextDecoder().decode(encoded.bytes)) as { payload: unknown };
    expect(body.payload).toEqual({
      worker_generation: 2,
      cancellation_generation: 5,
      segment_id: "seg",
      position_ms: 121,
    });
  });

  it("rejects an oversized client event", () => {
    expect(() =>
      encodeClientEvent({
        ...base,
        eventType: "playback.progress",
        payload: { segment_id: "x".repeat(MAX_LOSSY_BYTES) },
      }),
    ).toThrow(/exceeds/);
  });
});
