import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionScreen } from "../../src/components";
import type { PublicConfig } from "../../src/config";
import type { Envelope } from "../../src/contracts/realtime";
import { fakeDeps, type FakeDeps } from "../support/fakeDeps";

const CONFIG: PublicConfig = { apiBaseUrl: "http://127.0.0.1:8000/api/v1", appEnv: "development", buildVersion: "test" };

afterEach(cleanup);

let counter = 0;
function envelope(turnId: string | null = "t1"): Envelope {
  counter += 1;
  return { eventId: `e${String(counter)}`, sessionId: "s", turnId, eventType: "x", sequenceNumber: null, occurredAt: "t", payload: {} };
}

async function live(fake: FakeDeps = fakeDeps()): Promise<FakeDeps> {
  render(<SessionScreen config={CONFIG} controllerDeps={fake.deps} />);
  const start = await screen.findByRole("button", { name: /start session/i });
  await waitFor(() => {
    expect((start as HTMLButtonElement).disabled).toBe(false);
  });
  fireEvent.click(start);
  await waitFor(() => {
    expect(screen.getAllByRole("status")[0]?.textContent).toContain("Connected");
  });
  return fake;
}

function sendTranscript(fake: FakeDeps, text: string, isFinal: boolean, turnId = "t1"): void {
  act(() => {
    fake.handlers().onMessage({ topic: "va.transcript.v1", envelope: envelope(turnId), text, isFinal });
  });
}

function sendState(fake: FakeDeps, state: "listening" | "transcribing" | "speaking"): void {
  act(() => {
    fake.handlers().onMessage({ topic: "va.state.v1", envelope: envelope(null), state });
  });
}

function indicator(): HTMLElement {
  const conversation = screen.getByRole("region", { name: "Conversation" });
  const found = conversation.querySelector<HTMLElement>(".listening");
  if (found === null) {
    throw new Error("indicator missing");
  }
  return found;
}

describe("transcript panel", () => {
  it("shows a partial as provisional outside the live log, then commits the final", async () => {
    const fake = await live();
    const log = screen.getByRole("log", { name: "Your transcript" });

    sendTranscript(fake, "hello the", false);
    expect(screen.getByTestId("provisional-line").textContent).toContain("hello the");
    expect(within(log).queryByText("hello the")).toBeNull();

    sendTranscript(fake, "hello there", true);
    expect(screen.queryByTestId("provisional-line")).toBeNull();
    expect(within(log).getByText("hello there")).toBeDefined();
  });

  it("uses a polite additions-only log for finals", async () => {
    await live();
    const log = screen.getByRole("log", { name: "Your transcript" });
    expect(log.getAttribute("aria-live")).toBe("polite");
    expect(log.getAttribute("aria-relevant")).toBe("additions");
  });

  it("keeps partial text out of any live region", async () => {
    const fake = await live();
    sendTranscript(fake, "partial words", false);
    const provisional = screen.getByTestId("provisional-line");
    expect(provisional.closest("[aria-live='polite']")).toBeNull();
    expect(provisional.closest("[role='log']")).toBeNull();
  });

  it("renders Devanagari, romanized Hinglish and mixed text literally", async () => {
    const fake = await live();
    const mixed = "मेरा order number 4521 है <i>ok</i>";
    sendTranscript(fake, "नमस्ते, आप कैसे हैं?", true, "t1");
    sendTranscript(fake, "mujhe kal ka appointment chahiye", true, "t2");
    sendTranscript(fake, mixed, true, "t3");

    const log = screen.getByRole("log", { name: "Your transcript" });
    expect(within(log).getByText("नमस्ते, आप कैसे हैं?")).toBeDefined();
    expect(within(log).getByText("mujhe kal ka appointment chahiye")).toBeDefined();
    expect(within(log).getByText(mixed)).toBeDefined();
    expect(log.querySelector("i")).toBeNull();
    expect(log.querySelector("li")?.getAttribute("dir")).toBe("auto");
    expect(log.querySelector("li")?.className).toContain("transcript-text");
  });

  it("ignores empty transcripts", async () => {
    const fake = await live();
    sendTranscript(fake, "   ", true);
    sendTranscript(fake, "", false);
    expect(screen.queryByTestId("provisional-line")).toBeNull();
    expect(screen.getByRole("log", { name: "Your transcript" }).querySelector("li")).toBeNull();
  });

  it("drives the listening indicator from the canonical state", async () => {
    const fake = await live();
    expect(indicator().textContent).toBe("Not listening");

    sendState(fake, "listening");
    expect(indicator().textContent).toBe("Listening");
    expect(indicator().getAttribute("data-state")).toBe("listening");
    expect(indicator().getAttribute("role")).toBe("status");
    expect(indicator().getAttribute("aria-live")).toBe("off");

    sendState(fake, "transcribing");
    expect(indicator().textContent).toBe("Hearing you");

    sendState(fake, "speaking");
    expect(indicator().textContent).toBe("Not listening");
  });
});

describe("microphone release", () => {
  it("shows a released state and stops the track when the user ends the session", async () => {
    const fake = await live();
    expect(screen.getByText("Capturing")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /end session/i }));

    await waitFor(() => {
      expect(screen.getByText("Released (microphone off)")).toBeDefined();
    });
    expect(screen.queryByText("Capturing")).toBeNull();
    expect(fake.track.stop).toHaveBeenCalled();
  });

  it("ends the session and releases the microphone on pagehide", async () => {
    const fake = await live();

    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });

    await waitFor(() => {
      expect(fake.track.stop).toHaveBeenCalled();
    });
    expect(fake.api.endSession).toHaveBeenCalledWith("sess-1", "browser_closed", expect.any(String));
  });
});

describe("speech recognition summary", () => {
  it("is neutral before the session ends", async () => {
    await live();
    const panel = screen.getByRole("region", { name: "Speech recognition summary" });
    expect(panel.textContent).toContain("Shown after the session ends");
  });

  it("shows operation count, audio seconds and cost after the session ends", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listOperations).mockResolvedValue([
      { operationId: "o1", component: "stt", provider: "deepgram", status: "succeeded", usage: [{ unit: "transcribed_audio_seconds", quantity: 12.34 }] },
    ]);
    vi.mocked(fake.api.getCosts).mockResolvedValue({
      calculationStatus: "final",
      totalUsd: "0.01",
      components: [{ component: "stt", label: "Deepgram", amountUsd: "0.0053" }],
    });
    await live(fake);
    fireEvent.click(screen.getByRole("button", { name: /end session/i }));

    const panel = screen.getByRole("region", { name: "Speech recognition summary" });
    await waitFor(() => {
      expect(panel.textContent).toContain("12.3 s");
    });
    expect(panel.textContent).toContain("$0.0053 (final)");
    expect(within(panel).getByText("STT operations").nextElementSibling?.textContent).toBe("1");
  });

  it("shows a neutral not-available state when costs are not ready", async () => {
    const fake = await live();
    fireEvent.click(screen.getByRole("button", { name: /end session/i }));

    const panel = screen.getByRole("region", { name: "Speech recognition summary" });
    await waitFor(() => {
      expect(panel.textContent).toContain("Not available yet");
    });
    expect(within(panel).queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
    expect(fake.api.getCosts).toHaveBeenCalled();
  });
});
