import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionScreen } from "../../src/components";
import type { PublicConfig } from "../../src/config";
import type { AgentAudioStatus } from "../../src/audio/agentAudio";
import type { MicrophoneResult } from "../../src/audio/microphone";
import { ApiError } from "../../src/api";
import { fakeDeps, ACTIVE_REPORT, type FakeDeps } from "../support/fakeDeps";

const CONFIG: PublicConfig = {
  apiBaseUrl: "http://127.0.0.1:8000/api/v1",
  appEnv: "development",
  buildVersion: "test",
};

afterEach(cleanup);

function renderScreen(fake: FakeDeps = fakeDeps()): FakeDeps {
  render(<SessionScreen config={CONFIG} controllerDeps={fake.deps} />);
  return fake;
}

async function startSession(fake: FakeDeps): Promise<void> {
  const start = await screen.findByRole("button", { name: /start session/i });
  await waitFor(() => {
    expect((start as HTMLButtonElement).disabled).toBe(false);
  });
  fireEvent.click(start);
  await waitFor(() => {
    expect(screen.getAllByRole("status")[0]?.textContent).toContain("Connected");
  });
  expect(fake.api.createSession).toHaveBeenCalled();
}

function button(name: RegExp): HTMLButtonElement {
  return screen.getByRole<HTMLButtonElement>("button", { name });
}

describe("SessionScreen accessibility and states", () => {
  it("uses semantic landmarks and labelled sections", async () => {
    renderScreen();
    await screen.findByRole("option", { name: /default agent/i });

    expect(screen.getByRole("banner")).toBeDefined();
    expect(screen.getByRole("main")).toBeDefined();
    for (const name of ["Session", "Live state", "Conversation", "Audio check", "Microphone"]) {
      expect(screen.getByRole("region", { name })).toBeDefined();
    }
    expect(screen.getByLabelText<HTMLSelectElement>("Agent configuration").value).toBe("cfg-1");
  });

  it("disables end and mute until a session is live, and announces status politely", async () => {
    renderScreen();
    await screen.findByRole("option", { name: /default agent/i });

    expect(button(/end session/i).disabled).toBe(true);
    expect(button(/mute microphone/i).disabled).toBe(true);
    const status = screen.getAllByRole("status")[0];
    expect(status?.getAttribute("aria-live")).toBe("polite");
    expect(status?.textContent).toContain("Ready to start");
  });

  it("starts a session and shows live state, configuration labels and mic constraints", async () => {
    const fake = renderScreen();
    await startSession(fake);

    expect(button(/end session/i).disabled).toBe(false);
    expect(button(/start session/i).disabled).toBe(true);
    expect(screen.getByText(/STT stt-x, LLM llm-y, TTS tts-z/)).toBeDefined();
    expect(screen.getByText("Echo cancellation: Active")).toBeDefined();
    expect(screen.getByText("Mono capture: Active")).toBeDefined();
  });

  it("toggles mute with aria-pressed", async () => {
    const fake = renderScreen();
    await startSession(fake);

    fireEvent.click(button(/mute microphone/i));

    await waitFor(() => {
      expect(button(/unmute microphone/i).getAttribute("aria-pressed")).toBe("true");
    });
    expect(screen.getByText("Muted")).toBeDefined();
  });

  it("ends the session and shows the terminal state", async () => {
    const fake = renderScreen();
    await startSession(fake);

    fireEvent.click(button(/end session/i));

    await waitFor(() => {
      expect(screen.getAllByRole("status")[0]?.textContent).toContain("Session ended");
    });
    expect(fake.api.endSession).toHaveBeenCalled();
    expect(button(/start session/i).disabled).toBe(false);
  });

  it("renders realtime state, transcript and agent text as plain text", async () => {
    const fake = renderScreen();
    await startSession(fake);
    const envelope = { eventId: "e1", sessionId: "s", turnId: "t1", eventType: "x", sequenceNumber: 1, occurredAt: "t", payload: {} } as const;

    act(() => {
      fake.handlers().onMessage({ topic: "va.state.v1", envelope, state: "speaking" });
      fake.handlers().onMessage({ topic: "va.transcript.v1", envelope: { ...envelope, eventId: "e2" }, text: "<b>hi</b>", isFinal: true });
    });

    expect(screen.getAllByText("Agent speaking").length).toBeGreaterThan(0);
    const log = screen.getByRole("log", { name: "Your transcript" });
    expect(within(log).getByText("<b>hi</b>")).toBeDefined();
    expect(log.querySelector("b")).toBeNull();
  });

  it("shows a reconnecting notice and never a stale speaking state", async () => {
    const fake = renderScreen();
    await startSession(fake);

    act(() => {
      fake.handlers().onState("reconnecting", null);
    });

    expect(screen.getByText(/Reconnecting within 20 seconds/)).toBeDefined();
    expect(screen.queryByText("Agent speaking")).toBeNull();
    expect(screen.getAllByText("Reconnecting agent").length).toBeGreaterThan(0);
  });

  it("reports quality and a blocked-autoplay recovery button", async () => {
    const fake = renderScreen();
    await startSession(fake);
    const blocked: AgentAudioStatus = { phase: "blocked", receiving: false, audible: false };

    act(() => {
      fake.handlers().onQuality("poor");
      fake.handlers().onAgentAudio(blocked);
    });
    fireEvent.click(button(/enable audio playback/i));

    expect(screen.getByText("Poor")).toBeDefined();
    await waitFor(() => {
      expect(fake.transport.startAudio).toHaveBeenCalled();
    });
  });

  it("verifies the backend test tone once audible audio is received", async () => {
    const fake = renderScreen();
    await startSession(fake);
    expect(screen.getByText("Not yet verified")).toBeDefined();

    act(() => {
      fake.handlers().onAgentAudio({ phase: "playing", receiving: true, audible: true });
    });

    expect(screen.getByText("Tone verified")).toBeDefined();
    expect(screen.getByText(/played through the WebRTC audio element/)).toBeDefined();
  });

  it("shows an alert when the microphone permission is denied and no session is created", async () => {
    const fake = renderScreen(
      fakeDeps({ ok: false, error: { kind: "permission_denied", message: "Microphone access was blocked. Allow it." } }),
    );
    const start = await screen.findByRole("button", { name: /start session/i });
    await waitFor(() => {
      expect((start as HTMLButtonElement).disabled).toBe(false);
    });

    fireEvent.click(start);

    await waitFor(() => {
      expect(screen.getAllByRole("alert").some((el) => /blocked/.test(el.textContent ?? ""))).toBe(true);
    });
    expect(screen.getByText("Permission denied")).toBeDefined();
    expect(fake.api.createSession).not.toHaveBeenCalled();
  });

  it("warns when capture processing is not verifiably active", async () => {
    const degraded = {
      statuses: { ...ACTIVE_REPORT.statuses, noiseSuppression: "unsupported" },
      degraded: ["noiseSuppression"],
    } as const;
    const fake = fakeDeps();
    const track = Object.assign(new EventTarget(), { stop: vi.fn() }) as unknown as MediaStreamTrack;
    renderScreen({ ...fake, deps: { ...fake.deps, acquireMicrophone: () => Promise.resolve({ ok: true, track, report: degraded }) } });
    const start = await screen.findByRole("button", { name: /start session/i });
    await waitFor(() => {
      expect((start as HTMLButtonElement).disabled).toBe(false);
    });

    fireEvent.click(start);

    await waitFor(() => {
      expect(screen.getByText(/not verifiably active/)).toBeDefined();
    });
    expect(screen.getByText("Noise suppression: Unsupported by this browser")).toBeDefined();
  });

  it("shows an alert with the API message when session creation fails", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.createSession).mockRejectedValue(
      new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "A required dependency is unavailable.", status: 503, retryable: false }),
    );
    renderScreen(fake);
    const start = await screen.findByRole("button", { name: /start session/i });
    await waitFor(() => {
      expect((start as HTMLButtonElement).disabled).toBe(false);
    });

    fireEvent.click(start);

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("A required dependency is unavailable.");
    });
    expect(button(/start session/i).disabled).toBe(false);
  });

  it("shows a retry alert when agent configurations cannot be loaded", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listAgentConfigs).mockRejectedValueOnce(
      new ApiError({ code: "NETWORK_ERROR", message: "The server could not be reached.", status: null, retryable: true }),
    );
    renderScreen(fake);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The server could not be reached.");
    fireEvent.click(within(alert).getByRole("button", { name: /retry/i }));

    expect(await screen.findByRole("option", { name: /default agent/i })).toBeDefined();
  });

  it("explains an empty configuration list", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listAgentConfigs).mockResolvedValue([]);
    renderScreen(fake);

    expect((await screen.findByRole("alert")).textContent).toContain("No active agent configuration");
    expect(button(/start session/i).disabled).toBe(true);
  });

  it("reports device loss as an alert and ends the session when the mic cannot return", async () => {
    const fake = fakeDeps();
    const acquire = vi
      .fn<() => Promise<MicrophoneResult>>()
      .mockResolvedValueOnce({ ok: true, track: fake.track, report: ACTIVE_REPORT })
      .mockResolvedValue({ ok: false, error: { kind: "no_device", message: "No microphone was found." } });
    renderScreen({ ...fake, deps: { ...fake.deps, acquireMicrophone: acquire } });
    await startSession(fake);
    expect(screen.getByText("Capturing")).toBeDefined();

    act(() => {
      fake.track.dispatchEvent(new Event("ended"));
    });

    await waitFor(() => {
      expect(screen.getByText("Device lost")).toBeDefined();
    });
    expect(screen.getAllByRole("alert").some((el) => /could not be restored/.test(el.textContent ?? ""))).toBe(true);
    await waitFor(() => {
      expect(fake.api.endSession).toHaveBeenCalled();
    });
  });

  it("counts invalid realtime messages without displaying their content", async () => {
    const fake = renderScreen();
    await startSession(fake);

    act(() => {
      fake.handlers().onRejected("invalid_message");
    });

    expect(screen.getByText(/Ignored 1 invalid realtime message/)).toBeDefined();
  });
});
