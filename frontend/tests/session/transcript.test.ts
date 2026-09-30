import { describe, expect, it } from "vitest";
import type { InboundMessage } from "../../src/contracts/realtime";
import { INITIAL_SESSION_STATE, sessionReducer, type SessionViewState } from "../../src/session/sessionState";
import { MAX_FINAL_LINES } from "../../src/session/transcript";

let counter = 0;
function transcript(
  text: string,
  isFinal: boolean,
  opts: { turnId?: string | null; sequence?: number | null; eventId?: string } = {},
): InboundMessage {
  counter += 1;
  return {
    topic: "va.transcript.v1",
    text,
    isFinal,
    envelope: {
      eventId: opts.eventId ?? `evt-${String(counter)}`,
      sessionId: "s",
      turnId: opts.turnId === undefined ? "turn-1" : opts.turnId,
      eventType: isFinal ? "stt.final" : "stt.partial",
      sequenceNumber: opts.sequence ?? null,
      occurredAt: "2026-09-30T00:00:00Z",
      payload: {},
    },
  };
}

function feed(messages: readonly InboundMessage[]): SessionViewState {
  return messages.reduce<SessionViewState>(
    (state, message) => sessionReducer(state, { type: "message", message }),
    INITIAL_SESSION_STATE,
  );
}

const texts = (state: SessionViewState): readonly string[] => state.userTranscript.map((l) => l.text);

describe("user transcript reducer", () => {
  it("replaces a partial in place with the next partial", () => {
    const state = feed([transcript("hel", false), transcript("hello the", false)]);
    expect(texts(state)).toEqual(["hello the"]);
    expect(state.userTranscript[0]?.isFinal).toBe(false);
  });

  it("commits a final over the partial of the same turn", () => {
    const state = feed([transcript("hello the", false), transcript("hello there", true)]);
    expect(texts(state)).toEqual(["hello there"]);
    expect(state.userTranscript[0]?.isFinal).toBe(true);
  });

  it("never promotes a partial to final by itself", () => {
    const state = feed([transcript("hello", true, { turnId: "t1" }), transcript("how are", false, { turnId: "t2" })]);
    expect(state.userTranscript.map((l) => l.isFinal)).toEqual([true, false]);
  });

  it("drops a stale provisional line when a later turn commits", () => {
    const state = feed([transcript("abandoned", false, { turnId: "t1" }), transcript("next", true, { turnId: "t2" })]);
    expect(texts(state)).toEqual(["next"]);
  });

  it("ignores a late partial for a turn that is already final", () => {
    const state = feed([transcript("done", true, { turnId: "t1" }), transcript("do", false, { turnId: "t1" })]);
    expect(state.userTranscript).toHaveLength(1);
    expect(state.userTranscript[0]?.isFinal).toBe(true);
  });

  it("keeps finals in arrival order", () => {
    const state = feed([
      transcript("one", true, { turnId: "t1" }),
      transcript("two", true, { turnId: "t2" }),
      transcript("three", true, { turnId: "t3" }),
    ]);
    expect(texts(state)).toEqual(["one", "two", "three"]);
  });

  it("drops out-of-order transcript messages when sequence numbers are present", () => {
    const state = feed([
      transcript("second", false, { sequence: 5 }),
      transcript("first", false, { sequence: 4 }),
    ]);
    expect(texts(state)).toEqual(["second"]);
  });

  it("ignores a duplicate delivery of the same final", () => {
    const state = feed([
      transcript("hi", true, { eventId: "dup", turnId: "t1" }),
      transcript("hi", true, { eventId: "dup", turnId: "t1" }),
    ]);
    expect(texts(state)).toEqual(["hi"]);
  });

  it("bounds the history to the most recent finals", () => {
    const messages = Array.from({ length: MAX_FINAL_LINES + 10 }, (_, i) =>
      transcript(`line ${String(i)}`, true, { turnId: `t${String(i)}` }),
    );
    const state = feed([...messages, transcript("live", false, { turnId: "tx" })]);
    const finals = state.userTranscript.filter((l) => l.isFinal);
    expect(finals).toHaveLength(MAX_FINAL_LINES);
    expect(finals[0]?.text).toBe("line 10");
    expect(state.userTranscript.at(-1)?.text).toBe("live");
  });

  it("ignores empty and whitespace-only transcripts", () => {
    const state = feed([transcript("", false), transcript("   ", true), transcript("\n", false)]);
    expect(state.userTranscript).toEqual([]);
  });

  it("clears a pending partial when the turn finalises with no text", () => {
    const state = feed([transcript("um", false, { turnId: "t1" }), transcript("", true, { turnId: "t1" })]);
    expect(state.userTranscript).toEqual([]);
  });

  it("stores Devanagari, romanized Hinglish and mixed script literally", () => {
    const mixed = 'मेरा order number 4521 है <b>ok</b> & "quotes"';
    const state = feed([
      transcript("नमस्ते, आप कैसे हैं?", true, { turnId: "t1" }),
      transcript("mujhe kal ka appointment chahiye", true, { turnId: "t2" }),
      transcript(mixed, true, { turnId: "t3" }),
    ]);
    expect(texts(state)).toEqual(["नमस्ते, आप कैसे हैं?", "mujhe kal ka appointment chahiye", mixed]);
  });

  it("clears transcripts when a new session starts", () => {
    const state = sessionReducer(feed([transcript("hi", true)]), { type: "starting" });
    expect(state.userTranscript).toEqual([]);
  });
});
