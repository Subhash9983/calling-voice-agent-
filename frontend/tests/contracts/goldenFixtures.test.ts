import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  decodeInbound,
  encodeClientEvent,
  playbackPayload,
  type ClientEventType,
  type InboundMessage,
} from "../../src/contracts/realtime";
import { isRecord, readRecord, readString, type JsonRecord } from "../../src/contracts/validate";

/**
 * Shared golden fixtures owned by the backend (read-only here):
 * backend/tests/fixtures/realtime_wire/. The Python decoder accepts the
 * browser_to_agent envelopes; this decoder must accept agent_to_browser.
 */
const FIXTURE_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "backend",
  "tests",
  "fixtures",
  "realtime_wire",
);

function load(name: string): readonly JsonRecord[] {
  const parsed: unknown = JSON.parse(readFileSync(join(FIXTURE_DIR, name), "utf8"));
  if (!Array.isArray(parsed)) {
    throw new Error(`${name} must be a JSON array`);
  }
  return (parsed as readonly unknown[]).map((entry, index) => readRecord(entry, `${name}[${String(index)}]`));
}

function decodedView(message: InboundMessage): Record<string, unknown> {
  const eventType = message.envelope.eventType;
  switch (message.topic) {
    case "va.state.v1":
      return { event_type: eventType, state: message.state };
    case "va.playback.v1":
      return {
        event_type: eventType,
        state: message.state,
        worker_generation: message.identity.workerGeneration,
        cancellation_generation: message.identity.cancellationGeneration,
        segment_id: message.identity.segmentId,
      };
    case "va.transcript.v1":
    case "va.response.v1":
      return { event_type: eventType, text: message.text, is_final: message.isFinal };
    case "va.error.v1":
      return {
        event_type: eventType,
        code: message.code,
        message: message.message,
        retryable: message.retryable,
      };
    case "va.metrics.v1":
      return { event_type: eventType, ...message.envelope.payload };
  }
}

describe("golden fixtures: agent to browser", () => {
  const entries = load("agent_to_browser.json");

  it("has fixtures to check", () => {
    expect(entries.length).toBeGreaterThan(0);
  });

  it.each(entries.map((entry) => [readString(entry, "name", "fixture"), entry] as const))(
    "decodes %s to the expected view",
    (_name, entry) => {
      const topic = readString(entry, "topic", "fixture");
      const bytes = new TextEncoder().encode(JSON.stringify(entry["envelope"]));

      const result = decodeInbound(topic, bytes, entry["reliable"] === true);

      if (topic === "va.control.v1") {
        // Control packets target the worker; the browser never handles them.
        expect(result).toEqual({ ok: false, reason: "unknown_topic" });
        return;
      }
      expect(result.ok).toBe(true);
      if (result.ok) {
        expect(decodedView(result.message)).toEqual(entry["expected"]);
      }
    },
  );
});

const NORMALIZED = {
  event_id: "00000000-0000-4000-8000-0000000000ff",
  occurred_at: "2026-01-01T00:00:00.000Z",
  session_id: "00000000-0000-4000-8000-0000000000fe",
} as const;

function normalize(envelope: JsonRecord): JsonRecord {
  return { ...envelope, ...NORMALIZED };
}

describe("golden fixtures: browser to agent", () => {
  const entries = load("browser_to_agent.json");

  it("has fixtures to check", () => {
    expect(entries.length).toBeGreaterThan(0);
  });

  it.each(entries.map((entry) => [readString(entry, "name", "fixture"), entry] as const))(
    "encodes %s exactly as the Python decoder expects",
    (_name, entry) => {
      const envelope = readRecord(entry["envelope"], "envelope");
      const payload = readRecord(entry["payload"], "payload");
      const eventType = readString(envelope, "event_type", "envelope") as ClientEventType;
      const isPlayback = eventType.startsWith("playback.");
      const position = payload["position_ms"];

      const encoded = encodeClientEvent({
        eventType,
        sessionId: readString(envelope, "session_id", "envelope"),
        eventId: readString(envelope, "event_id", "envelope"),
        occurredAt: readString(envelope, "occurred_at", "envelope"),
        ...(isPlayback
          ? {
              payload: playbackPayload(
                {
                  workerGeneration: Number(payload["worker_generation"]),
                  cancellationGeneration: Number(payload["cancellation_generation"]),
                  segmentId: readString(payload, "segment_id", "payload"),
                },
                typeof position === "number" ? position : undefined,
              ),
            }
          : {}),
      });

      const produced: unknown = JSON.parse(new TextDecoder().decode(encoded.bytes));
      expect(isRecord(produced)).toBe(true);
      expect(normalize(readRecord(produced, "produced"))).toEqual(normalize(envelope));
      expect(encoded.topic).toBe(entry["topic"]);
      expect(encoded.reliable).toBe(entry["reliable"]);
    },
  );

  it("never encodes an event type the Python decoder rejects", () => {
    const invalid = load("browser_to_agent_invalid.json");
    const accepted = new Set(entries.map((entry) => readString(readRecord(entry["envelope"], "e"), "event_type", "e")));

    for (const entry of invalid) {
      const eventType = readString(readRecord(entry["envelope"], "e"), "event_type", "e");
      if (entry["reason"] === "unknown_event_type") {
        expect(accepted.has(eventType)).toBe(false);
      }
    }
  });
});
