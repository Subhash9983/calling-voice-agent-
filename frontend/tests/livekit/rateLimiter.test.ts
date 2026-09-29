import { describe, expect, it } from "vitest";
import { ClientSendLimiter } from "../../src/livekit/rateLimiter";

function limiterAt(clock: { t: number }): ClientSendLimiter {
  return new ClientSendLimiter(() => clock.t);
}

describe("ClientSendLimiter", () => {
  it("allows a burst of 40 then rejects further reliable messages", () => {
    const clock = { t: 0 };
    const limiter = limiterAt(clock);

    const decisions = Array.from({ length: 41 }, () => limiter.decide(false));

    expect(decisions.slice(0, 40).every((d) => d === "allow")).toBe(true);
    expect(decisions[40]).toBe("reject");
    expect(limiter.counters().rejectedReliable).toBe(1);
  });

  it("refills at 20 messages per second", () => {
    const clock = { t: 0 };
    const limiter = limiterAt(clock);
    for (let i = 0; i < 40; i += 1) {
      limiter.decide(false);
    }

    clock.t = 500;
    const afterHalfSecond = Array.from({ length: 11 }, () => limiter.decide(false));

    expect(afterHalfSecond.filter((d) => d === "allow")).toHaveLength(10);
    expect(afterHalfSecond[10]).toBe("reject");
  });

  it("caps refill at the burst size after a long idle period", () => {
    const clock = { t: 0 };
    const limiter = limiterAt(clock);

    clock.t = 60_000;
    const decisions = Array.from({ length: 41 }, () => limiter.decide(false));

    expect(decisions.filter((d) => d === "allow")).toHaveLength(40);
  });

  it("limits playback progress to one per 250 ms and counts drops", () => {
    const clock = { t: 1000 };
    const limiter = limiterAt(clock);

    expect(limiter.decide(true)).toBe("allow");
    clock.t = 1100;
    expect(limiter.decide(true)).toBe("drop");
    clock.t = 1249;
    expect(limiter.decide(true)).toBe("drop");
    clock.t = 1250;
    expect(limiter.decide(true)).toBe("allow");
    expect(limiter.counters().droppedProgress).toBe(2);
  });

  it("drops rather than rejects progress when the aggregate bucket is empty", () => {
    const clock = { t: 0 };
    const limiter = limiterAt(clock);
    for (let i = 0; i < 40; i += 1) {
      limiter.decide(false);
    }

    expect(limiter.decide(true)).toBe("drop");
    expect(limiter.counters()).toEqual({ droppedProgress: 1, rejectedReliable: 0 });
  });

  it("does not consume aggregate tokens for progress dropped by the interval limit", () => {
    const clock = { t: 0 };
    const limiter = limiterAt(clock);
    limiter.decide(true);
    for (let i = 0; i < 10; i += 1) {
      clock.t += 10;
      limiter.decide(true);
    }

    const decisions = Array.from({ length: 39 }, () => limiter.decide(false));

    expect(decisions.every((d) => d === "allow")).toBe(true);
  });
});
