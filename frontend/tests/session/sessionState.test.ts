import { describe, expect, it } from "vitest";
import type { Envelope, InboundMessage } from "../../src/contracts/realtime";
import {
  INITIAL_SESSION_STATE,
  sessionReducer,
  type SessionAction,
  type SessionViewState,
} from "../../src/session/sessionState";

const IDENTITY = { workerGeneration: 1, cancellationGeneration: 0, segmentId: "seg" } as const;

function envelope(overrides: Partial<Envelope> = {}): Envelope {
  return {
    eventId: "e1",
    sessionId: "s",
    turnId: "t1",
    eventType: "x",
    sequenceNumber: null,
    occurredAt: "t",
    payload: {},
    ...overrides,
  };
}

function run(actions: readonly SessionAction[], from: SessionViewState = INITIAL_SESSION_STATE): SessionViewState {
  return actions.reduce(sessionReducer, from);
}

const live = (): SessionViewState => run([{ type: "phase", phase: "live" }]);

describe("sessionReducer", () => {
  it("never mutates the previous state", () => {
    const before = INITIAL_SESSION_STATE;
    const snapshot = JSON.stringify(before);

    sessionReducer(before, { type: "phase", phase: "live" });

    expect(JSON.stringify(before)).toBe(snapshot);
  });

  it("replaces a partial transcript with the final one for the same turn", () => {
    const partial: InboundMessage = { topic: "va.transcript.v1", envelope: envelope({ eventId: "a" }), text: "hel", isFinal: false };
    const final: InboundMessage = { topic: "va.transcript.v1", envelope: envelope({ eventId: "b" }), text: "hello", isFinal: true };

    const state = run([{ type: "message", message: partial }, { type: "message", message: final }]);

    expect(state.userTranscript).toEqual([{ id: "b", turnId: "t1", text: "hello", isFinal: true }]);
  });

  it("appends a new turn after a final line", () => {
    const first: InboundMessage = { topic: "va.response.v1", envelope: envelope({ eventId: "a" }), text: "one", isFinal: true };
    const second: InboundMessage = { topic: "va.response.v1", envelope: envelope({ eventId: "b", turnId: "t2" }), text: "two", isFinal: false };

    const state = run([{ type: "message", message: first }, { type: "message", message: second }]);

    expect(state.agentResponse.map((line) => line.text)).toEqual(["one", "two"]);
  });

  it("ignores stale or duplicate state messages by sequence number", () => {
    const newer: InboundMessage = { topic: "va.state.v1", envelope: envelope({ sequenceNumber: 5 }), state: "thinking" };
    const stale: InboundMessage = { topic: "va.state.v1", envelope: envelope({ sequenceNumber: 4 }), state: "speaking" };

    const state = run([{ type: "message", message: newer }, { type: "message", message: stale }]);

    expect(state.agentState).toBe("thinking");
    expect(state.lastStateSequence).toBe(5);
  });

  it("interrupted playback replaces speaking; cancelled leaves state unchanged", () => {
    const speaking = run([{ type: "agent_state", state: "speaking" }]);
    const interrupted: InboundMessage = { topic: "va.playback.v1", envelope: envelope(), state: "interrupted", identity: IDENTITY };
    const cancelled: InboundMessage = { topic: "va.playback.v1", envelope: envelope(), state: "cancelled", identity: IDENTITY };

    expect(sessionReducer(speaking, { type: "message", message: interrupted }).agentState).toBe("interrupted");
    expect(sessionReducer(speaking, { type: "message", message: cancelled }).agentState).toBe("speaking");
  });

  it("records agent errors and counts rejected messages", () => {
    const error: InboundMessage = { topic: "va.error.v1", envelope: envelope(), code: "c", message: "Try again", retryable: true };

    const state = run([{ type: "message", message: error }, { type: "message_rejected" }, { type: "message_rejected" }]);

    expect(state.lastAgentError).toBe("Try again");
    expect(state.rejectedMessages).toBe(2);
  });

  it("latches audio verification once audible audio was received", () => {
    const state = run([
      { type: "agent_audio", status: { phase: "playing", receiving: true, audible: true } },
      { type: "agent_audio", status: { phase: "playing", receiving: true, audible: false } },
    ]);

    expect(state.audioVerified).toBe(true);
  });

  it("only downgrades agent state on presence loss while live", () => {
    expect(sessionReducer(INITIAL_SESSION_STATE, { type: "agent_presence", present: false }).agentState).toBeNull();
    expect(sessionReducer(live(), { type: "agent_presence", present: false }).agentState).toBe("recovering");
    expect(sessionReducer(live(), { type: "agent_presence", present: true }).agentState).toBeNull();
  });

  it("tracks microphone lifecycle and mute", () => {
    const state = run([
      { type: "mic_requesting" },
      { type: "mic_active", report: { statuses: { echoCancellation: "active", noiseSuppression: "active", autoGainControl: "active", channelCount: "active" }, degraded: [] } },
      { type: "mic_muted", muted: true },
    ]);

    expect(state.mic).toMatchObject({ status: "active", muted: true });
  });

  it("resets to the initial state and clears a previous error on start", () => {
    const failed = run([{ type: "failed", error: { message: "x", retryable: true } }]);

    expect(sessionReducer(failed, { type: "starting" }).error).toBeNull();
    expect(sessionReducer(failed, { type: "reset" })).toEqual(INITIAL_SESSION_STATE);
  });

  it("records the session, quality, transport, and timeline", () => {
    const state = run([
      { type: "session_created", sessionId: "s", configuration: { name: "n", version: 1, stt: "a", conversationEngine: "b", tts: "c" } },
      { type: "quality", quality: "good" },
      { type: "transport", state: "connected" },
      { type: "events_loaded", events: [{ eventId: "e", eventType: "session.ended", severity: "info", sequenceNumber: 1, occurredAt: "t" }] },
    ]);

    expect(state).toMatchObject({ phase: "connecting", sessionId: "s", quality: "good", transport: "connected" });
    expect(state.events).toHaveLength(1);
  });
});
