/**
 * WP10: deterministic initial greeting, clarification fallback, and
 * delivered-only conversation history (docs/14 §16, docs/01 §6-§7,
 * docs/08 §13). The backend sends the greeting and the clarification
 * fallback through the same `va.response.v1`/`va.playback.v1` flow as any
 * other agent response (docs/01 §6: no distinct "greeting" activity state
 * exists; it reuses `speaking`), so these tests prove the existing reducer
 * and rendering need no special-casing, while locking in the exact
 * ordering/dedup/no-stale-text guarantees WP10 requires.
 */
import { describe, expect, it } from "vitest";
import type { Envelope, InboundMessage } from "../../src/contracts/realtime";
import { INITIAL_SESSION_STATE, sessionReducer, type SessionAction } from "../../src/session/sessionState";

const IDENTITY = { workerGeneration: 1, cancellationGeneration: 0, segmentId: "seg-greeting" } as const;

function envelope(overrides: Partial<Envelope> = {}): Envelope {
  return {
    eventId: "e1",
    sessionId: "s",
    turnId: null,
    eventType: "x",
    sequenceNumber: null,
    occurredAt: "t",
    payload: {},
    ...overrides,
  };
}

function run(actions: readonly SessionAction[]): ReturnType<typeof sessionReducer> {
  return actions.reduce(sessionReducer, INITIAL_SESSION_STATE);
}

describe("deterministic initial greeting", () => {
  it("renders as the first agent-response entry after client.ready, via the normal response/playback flow", () => {
    const greetingResponse: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "greet-1" }),
      text: "Namaste! How can I help you today?",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: null,
      segmentSequence: null,
    };
    const greetingPlayback: InboundMessage = {
      topic: "va.playback.v1",
      envelope: envelope({ eventId: "greet-1-playback" }),
      state: "completed",
      identity: IDENTITY,
    };

    const state = run([
      { type: "message", message: greetingResponse },
      { type: "message", message: greetingPlayback },
    ]);

    expect(state.agentResponse).toHaveLength(1);
    expect(state.agentResponse[0]).toMatchObject({
      text: "Namaste! How can I help you today?",
      isFinal: true,
    });
    expect(state.userTranscript).toHaveLength(0);
  });

  it("is delivered exactly once: a duplicate delivery of the same greeting event is deduplicated", () => {
    const greetingResponse: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "greet-1" }),
      text: "Namaste! How can I help you today?",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: null,
      segmentSequence: null,
    };

    const state = run([
      { type: "message", message: greetingResponse },
      { type: "message", message: greetingResponse },
    ]);

    expect(state.agentResponse).toHaveLength(1);
  });

  it("remains the first entry once a real user turn follows", () => {
    const greetingResponse: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "greet-1" }),
      text: "Namaste! How can I help you today?",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: null,
      segmentSequence: null,
    };
    const turnReply: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "reply-1", turnId: "t1" }),
      text: "Sure, here is the weather.",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: null,
      segmentSequence: null,
    };

    const state = run([
      { type: "message", message: greetingResponse },
      { type: "message", message: turnReply },
    ]);

    expect(state.agentResponse.map((line) => line.text)).toEqual([
      "Namaste! How can I help you today?",
      "Sure, here is the weather.",
    ]);
  });
});

describe("clarification fallback", () => {
  it("renders identically to a normal delivered response when an interruption lands with an unusable transcript", () => {
    const clarification: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "clarify-1", turnId: "t2" }),
      text: "Sorry, I didn't catch that. Could you say it again?",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: "fallback.clarification.v1",
      segmentSequence: null,
    };

    const state = run([{ type: "message", message: clarification }]);

    expect(state.agentResponse).toHaveLength(1);
    expect(state.agentResponse[0]).toMatchObject({
      text: "Sorry, I didn't catch that. Could you say it again?",
      isFinal: true,
    });
    // No distinct marker beyond the normal delivered-line shape (docs/01 §7:
    // the clarification fallback follows the same completed turn contract).
    expect(state.agentResponse[0]?.truncated).toBeUndefined();
  });

  it("never fabricates a reply: an empty/unusable turn without the fallback stays discarded (no agent-response line at all)", () => {
    // A `discarded` turn (docs/01 §7) produces no va.response.v1 message in
    // the first place; the reducer must not invent one from a bare
    // transcript/state transition.
    const unusableTranscript: InboundMessage = {
      topic: "va.transcript.v1",
      envelope: envelope({ eventId: "noise-1", turnId: "t3" }),
      text: "",
      isFinal: true,
    };
    const idleState: InboundMessage = { topic: "va.state.v1", envelope: envelope(), state: "idle" };

    const state = run([
      { type: "message", message: unusableTranscript },
      { type: "message", message: idleState },
    ]);

    expect(state.agentResponse).toHaveLength(0);
    expect(state.userTranscript).toHaveLength(0);
  });
});

describe("delivered-only conversation history", () => {
  it("never shows text that was generated but cancelled before it was ever played", () => {
    const partial: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "gen-1", turnId: "t4" }),
      text: "Generated but never delivered",
      isFinal: false,
      completionStatus: null,
      fallbackTemplateId: null,
      segmentSequence: null,
    };
    const cancelled: InboundMessage = {
      topic: "va.playback.v1",
      envelope: envelope({ turnId: "t4" }),
      state: "cancelled",
      identity: IDENTITY,
    };
    const nextTurnReply: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "gen-2", turnId: "t5" }),
      text: "This is the real delivered answer.",
      isFinal: true,
      completionStatus: "completed",
      fallbackTemplateId: null,
      segmentSequence: null,
    };

    const state = run([
      { type: "message", message: partial },
      { type: "message", message: cancelled },
      { type: "message", message: nextTurnReply },
    ]);

    expect(state.agentResponse.map((line) => line.text)).toEqual(["This is the real delivered answer."]);
    expect(state.agentResponse.some((line) => line.text.includes("never delivered"))).toBe(false);
  });

  it("keeps a truncated-but-delivered line (distinct from a cancelled, never-played line)", () => {
    const truncatedDelivered: InboundMessage = {
      topic: "va.response.v1",
      envelope: envelope({ eventId: "trunc-1", turnId: "t6" }),
      text: "This answer was cut sh",
      isFinal: true,
      completionStatus: "truncated_partial",
      fallbackTemplateId: null,
      segmentSequence: null,
    };

    const state = run([{ type: "message", message: truncatedDelivered }]);

    expect(state.agentResponse).toHaveLength(1);
    expect(state.agentResponse[0]?.truncated).toBe(true);
    expect(state.agentResponse[0]?.text).toBe("This answer was cut sh");
  });
});
