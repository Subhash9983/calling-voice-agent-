import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../src/api";
import type { CostBreakdown } from "../../src/contracts/diagnosticsApi";
import { VoiceSessionController } from "../../src/session/controller";
import { fakeDeps, type FakeDeps } from "../support/fakeDeps";

/** Default outcome once `ended()` stops a session with reason "user_ended" and no errors are reported. */
const CLEAN_OUTCOME = { status: "ended", disconnectReason: "user_ended", errorCount: 0, errorCodes: [] };

const LATENCY_NOT_AVAILABLE = [
  { key: "sttFinalization", label: "STT finalization", metric: null },
  { key: "llmFirstToken", label: "LLM first token", metric: null },
  { key: "ttsFirstAudio", label: "TTS first audio", metric: null },
  { key: "firstAudibleResponse", label: "First audible response", metric: null },
  { key: "completeTurn", label: "Complete turn", metric: null },
  { key: "interruption", label: "Interruption", metric: null },
];

const STT_OP = {
  operationId: "op-1",
  component: "stt",
  provider: "deepgram",
  status: "succeeded",
  usage: [{ unit: "transcribed_audio_seconds", quantity: 7.25 }],
};

const CONVERSATION_OP = {
  operationId: "op-2",
  component: "conversation_engine",
  provider: "openai",
  status: "succeeded",
  usage: [
    { unit: "input_tokens", quantity: 120 },
    { unit: "output_tokens", quantity: 45 },
  ],
};

const TTS_OP = {
  operationId: "op-3",
  component: "tts",
  provider: "sarvam",
  status: "succeeded",
  usage: [{ unit: "synthesized_characters", quantity: 42 }],
};

function mockListOperations(
  fake: FakeDeps,
  byComponent: Readonly<Record<string, readonly unknown[]>>,
): void {
  vi.mocked(fake.api.listOperations).mockImplementation((_sessionId, options) => {
    const component = options?.component ?? "";
    return Promise.resolve((byComponent[component] ?? []) as never);
  });
}

async function ended(configure: (fake: FakeDeps) => void): Promise<VoiceSessionController> {
  const fake = fakeDeps();
  configure(fake);
  const controller = new VoiceSessionController(fake.deps);
  await controller.start("cfg-1");
  await controller.stop("user_ended");
  return controller;
}

describe("STT evidence after the session ends", () => {
  it("summarises STT operations and cost", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
      vi.mocked(fake.api.getCosts).mockResolvedValue({
        calculationStatus: "final",
        totalUsd: "0.01",
        components: [{ component: "stt", label: "Deepgram", amountUsd: "0.0031" }],
      });
    });

    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: { count: 1, audioSeconds: 7.25 },
      cost: { sttUsd: "0.0031", sttInrDisplay: null, calculationStatus: "final" },
      conversation: null,
      conversationCost: { conversationUsd: null, conversationInrDisplay: null, calculationStatus: "final" },
      tts: null,
      ttsCost: { ttsUsd: null, ttsInrDisplay: null, calculationStatus: "final" },
      overallCost: {
        totalUsd: "0.01",
        totalInrDisplay: null,
        calculationStatus: "final",
        costPerMinuteUsd: null,
        costPerMinuteInrDisplay: null,
      },
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
  });

  it("asks for STT, conversation-engine and TTS operations", async () => {
    const fake = fakeDeps();
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");
    expect(fake.api.listOperations).toHaveBeenCalledWith("sess-1", { component: "stt", limit: 100 });
    expect(fake.api.listOperations).toHaveBeenCalledWith("sess-1", {
      component: "conversation_engine",
      limit: 100,
    });
    expect(fake.api.listOperations).toHaveBeenCalledWith("sess-1", { component: "tts", limit: 100 });
  });

  it("treats a 503 not-ready costs response as neutral, not an error", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
      vi.mocked(fake.api.getCosts).mockRejectedValue(
        new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "x", status: 503, retryable: false }),
      );
    });

    const state = controller.getState();
    expect(state.evidence).toEqual({
      status: "ready",
      operations: { count: 1, audioSeconds: 7.25 },
      cost: null,
      conversation: null,
      conversationCost: null,
      tts: null,
      ttsCost: null,
      overallCost: null,
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
    expect(state.phase).toBe("ended");
    expect(state.error).toBeNull();
  });

  it("does not block the ended phase on the cost retry, then patches cost in once it resolves (worker finalize race)", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
    let resolveRetry!: (costs: CostBreakdown) => void;
    const retryAttempt = new Promise<CostBreakdown>((resolve) => {
      resolveRetry = resolve;
    });
    vi.mocked(fake.api.getCosts)
      .mockRejectedValueOnce(
        new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "x", status: 503, retryable: true }),
      )
      // Deliberately left pending: proves stop() does not await the retry chain
      // (sleep + this call), not merely that it happens to resolve fast.
      .mockReturnValueOnce(retryAttempt);
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended"); // resolves despite the still-pending retry attempt above

    expect(controller.getState().phase).toBe("ended");
    expect(controller.getState().evidence).toMatchObject({ cost: null, overallCost: null });

    // The background retry patches the real figures in once it resolves.
    resolveRetry({ calculationStatus: "partial", totalUsd: "0.01", components: [] });
    await vi.waitFor(() => {
      expect(controller.getState().evidence).toMatchObject({
        cost: { sttUsd: null, calculationStatus: "partial" },
        overallCost: { totalUsd: "0.01", calculationStatus: "partial", costPerMinuteUsd: null },
      });
    });
    expect(fake.api.getCosts).toHaveBeenCalledTimes(2);
  });

  it("gives up after repeated retryable failures, staying neutral rather than erroring", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.getCosts).mockRejectedValue(
      new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "x", status: 503, retryable: true }),
    );
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");

    expect(controller.getState().evidence).toMatchObject({ cost: null, overallCost: null });
    expect(controller.getState().error).toBeNull();
    await vi.waitFor(() => {
      expect(fake.api.getCosts).toHaveBeenCalledTimes(4);
    });
    expect(controller.getState().evidence).toMatchObject({ cost: null, overallCost: null });
    expect(controller.getState().error).toBeNull();
  });

  it("discards a late cost retry once a newer session has replaced it (never backdates evidence)", async () => {
    const fake = fakeDeps();
    let resolveStaleRetry!: (costs: CostBreakdown) => void;
    const staleRetry = new Promise<CostBreakdown>((resolve) => {
      resolveStaleRetry = resolve;
    });
    vi.mocked(fake.api.getCosts)
      // Session A: first attempt fails (retryable), its background retry's one
      // follow-up attempt is held open deliberately, to resolve only later.
      .mockRejectedValueOnce(
        new ApiError({ code: "DEPENDENCY_UNAVAILABLE", message: "x", status: 503, retryable: true }),
      )
      .mockReturnValueOnce(staleRetry)
      // Session B: succeeds on its one and only attempt.
      .mockResolvedValueOnce({ calculationStatus: "final", totalUsd: "2.50", components: [] });
    const controller = new VoiceSessionController(fake.deps);

    await controller.start("cfg-1"); // Session A
    await controller.stop("user_ended");
    expect(controller.getState().evidence).toMatchObject({ cost: null }); // first attempt failed
    await vi.waitFor(() => {
      expect(fake.api.getCosts).toHaveBeenCalledTimes(2); // its background retry is now in flight, stuck on staleRetry
    });

    await controller.start("cfg-1"); // Session B starts and ends while A's retry is still pending
    await controller.stop("user_ended");
    expect(controller.getState().evidence).toMatchObject({
      cost: { sttUsd: null, calculationStatus: "final" },
    }); // B's own cost, fetched cleanly on its first try

    resolveStaleRetry({ calculationStatus: "final", totalUsd: "999.99", components: [] });
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the now-resolved retry's microtasks run
    // A's stale $999.99 must never overwrite B's real evidence.
    expect(controller.getState().evidence).toMatchObject({
      cost: { sttUsd: null, calculationStatus: "final" },
      overallCost: { totalUsd: "2.50" },
    });
  });

  it("is neutral when both diagnostics fail", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockRejectedValue(new Error("boom"));
    });
    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: null,
      cost: null,
      conversation: null,
      conversationCost: null,
      tts: null,
      ttsCost: null,
      overallCost: null,
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
    expect(controller.getState().error).toBeNull();
  });

  it("summarises conversation-engine (LLM) operations and cost", async () => {
    const controller = await ended((fake) => {
      mockListOperations(fake, { stt: [STT_OP], conversation_engine: [CONVERSATION_OP] });
      vi.mocked(fake.api.getCosts).mockResolvedValue({
        calculationStatus: "final",
        totalUsd: "0.02",
        components: [
          { component: "stt", label: "Deepgram", amountUsd: "0.0031" },
          { component: "conversation_engine", label: "OpenAI", amountUsd: "0.0120" },
        ],
      });
    });

    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: { count: 1, audioSeconds: 7.25 },
      cost: { sttUsd: "0.0031", sttInrDisplay: null, calculationStatus: "final" },
      conversation: { count: 1, inputTokens: 120, outputTokens: 45 },
      conversationCost: { conversationUsd: "0.0120", conversationInrDisplay: null, calculationStatus: "final" },
      tts: null,
      ttsCost: { ttsUsd: null, ttsInrDisplay: null, calculationStatus: "final" },
      overallCost: {
        totalUsd: "0.02",
        totalInrDisplay: null,
        calculationStatus: "final",
        costPerMinuteUsd: null,
        costPerMinuteInrDisplay: null,
      },
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
  });

  it("is neutral for conversation-engine evidence when operations fail", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockRejectedValue(new Error("boom"));
      vi.mocked(fake.api.getCosts).mockResolvedValue({ calculationStatus: "partial", totalUsd: "0", components: [] });
    });

    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: null,
      cost: { sttUsd: null, sttInrDisplay: null, calculationStatus: "partial" },
      conversation: null,
      conversationCost: { conversationUsd: null, conversationInrDisplay: null, calculationStatus: "partial" },
      tts: null,
      ttsCost: { ttsUsd: null, ttsInrDisplay: null, calculationStatus: "partial" },
      overallCost: {
        totalUsd: "0",
        totalInrDisplay: null,
        calculationStatus: "partial",
        costPerMinuteUsd: null,
        costPerMinuteInrDisplay: null,
      },
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
  });

  it("summarises TTS (synthesis) operations and cost", async () => {
    const controller = await ended((fake) => {
      mockListOperations(fake, { stt: [STT_OP], tts: [TTS_OP] });
      vi.mocked(fake.api.getCosts).mockResolvedValue({
        calculationStatus: "final",
        totalUsd: "0.03",
        components: [
          { component: "stt", label: "Deepgram", amountUsd: "0.0031" },
          { component: "tts", label: "Sarvam", amountUsd: "0.0013" },
        ],
      });
    });

    expect(controller.getState().evidence).toEqual({
      status: "ready",
      operations: { count: 1, audioSeconds: 7.25 },
      cost: { sttUsd: "0.0031", sttInrDisplay: null, calculationStatus: "final" },
      conversation: null,
      conversationCost: { conversationUsd: null, conversationInrDisplay: null, calculationStatus: "final" },
      tts: { count: 1, charactersSynthesized: 42, firstAudioMs: null },
      ttsCost: { ttsUsd: "0.0013", ttsInrDisplay: null, calculationStatus: "final" },
      overallCost: {
        totalUsd: "0.03",
        totalInrDisplay: null,
        calculationStatus: "final",
        costPerMinuteUsd: null,
        costPerMinuteInrDisplay: null,
      },
      outcome: CLEAN_OUTCOME,
      latency: LATENCY_NOT_AVAILABLE,
    });
  });

  it("derives a cost-per-minute rate from the session's own start/end timestamps", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.getCosts).mockResolvedValue({ calculationStatus: "final", totalUsd: "0.06", components: [] });
      vi.mocked(fake.api.getSession).mockResolvedValue({
        sessionId: "sess-1",
        status: "ended",
        agentActivityState: null,
        disconnectReason: "user_ended",
        createdAt: "2026-10-09T04:13:34.894Z",
        endedAt: "2026-10-09T04:16:34.894Z",
      });
    });

    expect(controller.getState().evidence).toMatchObject({
      overallCost: { totalUsd: "0.06", calculationStatus: "final", costPerMinuteUsd: "0.020000" },
    });
  });

  it("is neutral for TTS evidence when operations fail", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listOperations).mockRejectedValue(new Error("boom"));
      vi.mocked(fake.api.getCosts).mockResolvedValue({ calculationStatus: "partial", totalUsd: "0", components: [] });
    });

    expect(controller.getState().evidence).toMatchObject({
      tts: null,
      ttsCost: { ttsUsd: null, calculationStatus: "partial" },
    });
  });

  it("loads evidence when the agent ended the session first", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.listOperations).mockResolvedValue([STT_OP]);
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    vi.mocked(fake.api.getSession).mockResolvedValue({ sessionId: "sess-1", status: "ended", agentActivityState: null, disconnectReason: "server_shutdown" });
    fake.handlers().onState("disconnected", "server");
    await vi.waitFor(() => {
      expect(controller.getState().phase).toBe("ended");
    });
    expect(controller.getState().evidence.status).toBe("ready");
  });

  it("counts and groups safe error codes by component and type", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listErrors).mockResolvedValue([
        {
          errorId: "e1",
          component: "stt",
          errorType: "provider_timeout",
          category: "transient",
          severity: "error",
          retryable: true,
          recovered: true,
          userAffected: false,
          safeMessage: "The speech recognizer timed out and recovered.",
          occurredAt: "2026-09-29T10:00:02Z",
        },
        {
          errorId: "e2",
          component: "stt",
          errorType: "provider_timeout",
          category: "transient",
          severity: "error",
          retryable: true,
          recovered: true,
          userAffected: false,
          safeMessage: "The speech recognizer timed out and recovered.",
          occurredAt: "2026-09-29T10:00:05Z",
        },
      ]);
    });

    expect(controller.getState().evidence).toMatchObject({
      outcome: {
        status: "ended",
        disconnectReason: "user_ended",
        errorCount: 2,
        errorCodes: [{ component: "stt", errorType: "provider_timeout", count: 2 }],
      },
    });
  });

  it("reports errorCount as not available (never zero) when the errors fetch fails", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.listErrors).mockRejectedValue(new Error("boom"));
    });

    expect(controller.getState().evidence).toMatchObject({
      outcome: { status: "ended", disconnectReason: "user_ended", errorCount: null, errorCodes: [] },
    });
  });

  it("reports a failed outcome when the end request could not be confirmed", async () => {
    const fake = fakeDeps();
    vi.mocked(fake.api.endSession).mockRejectedValue(new ApiError({ code: "NETWORK_ERROR", message: "x", status: 0, retryable: true }));
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");

    expect(controller.getState().evidence).toMatchObject({
      outcome: { status: "failed", disconnectReason: null },
    });
  });

  it("includes the latency summary once the session reports it", async () => {
    const controller = await ended((fake) => {
      vi.mocked(fake.api.getSession).mockResolvedValue({
        sessionId: "sess-1",
        status: "ended",
        agentActivityState: null,
        disconnectReason: "user_ended",
        latencySummary: {
          sttFinalization: null,
          llmFirstToken: null,
          ttsFirstAudio: null,
          firstAudibleResponse: null,
          completeTurn: { sampleCount: 1, averageMs: 900, p50Ms: 900, p95Ms: 900, maximumMs: 900 },
          interruption: null,
        },
      });
    });

    const evidence = controller.getState().evidence;
    if (evidence.status !== "ready") {
      throw new Error("expected ready evidence");
    }
    expect(evidence.latency.find((stage) => stage.key === "completeTurn")?.metric).toEqual({
      sampleCount: 1,
      averageMs: 900,
      p50Ms: 900,
      p95Ms: 900,
      maximumMs: 900,
    });
  });

  it("clears previous evidence when a new session starts", async () => {
    const fake = fakeDeps();
    const controller = new VoiceSessionController(fake.deps);
    await controller.start("cfg-1");
    await controller.stop("user_ended");
    expect(controller.getState().evidence.status).toBe("ready");
    await controller.start("cfg-1");
    expect(controller.getState().evidence.status).toBe("idle");
  });
});
