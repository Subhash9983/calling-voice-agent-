import { describe, expect, it, vi } from "vitest";
import { ApiError, createControlApiClient, type FetchLike } from "../../src/api";

const BASE = "http://127.0.0.1:8000/api/v1";
const TOKEN = "eyJ.secret-join-token.sig";
const SESSION_ID = "11111111-1111-4111-8111-111111111111";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const transport = {
  provider: "livekit",
  url: "ws://127.0.0.1:7880",
  room_name: "va-rd-abc",
  participant_identity: "br-1",
  join_token: TOKEN,
  token_expires_at: "2026-09-29T10:10:00Z",
};

const createBody = {
  idempotent_replay: false,
  session: {
    session_id: SESSION_ID,
    status: "connecting",
    agent_activity_state: null,
    created_at: "2026-09-29T10:00:00Z",
    maximum_session_ms: 1800000,
  },
  transport,
  configuration: {
    agent_config_id: "c",
    name: "Default",
    version: 1,
    stt: "stt",
    conversation_engine: "llm",
    tts: "tts",
  },
  request_id: "r1",
};

function clientWith(fetchImpl: FetchLike, extra: { timeoutMs?: number } = {}) {
  return createControlApiClient({
    baseUrl: `${BASE}/`,
    fetchImpl,
    newRequestId: () => "generated-id",
    ...extra,
  });
}

function lastCall(mock: ReturnType<typeof vi.fn<FetchLike>>): { url: string; init: RequestInit } {
  const call = mock.mock.calls.at(-1);
  if (call === undefined) {
    throw new Error("fetch was not called");
  }
  return { url: call[0], init: call[1] };
}

describe("createControlApiClient", () => {
  it("creates a session with the approved strict body and maps the response", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(201, createBody));
    const client = clientWith(fetchMock);

    const result = await client.createSession({ agentConfigId: "cfg-1" });

    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(`${BASE}/sessions`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
    expect(JSON.parse(init.body as string)).toEqual({
      client_request_id: "generated-id",
      agent_config_id: "cfg-1",
      channel: "browser",
      session_mode: "interactive_test",
      language_mode: "auto",
    });
    expect(result.session.sessionId).toBe(SESSION_ID);
    expect(result.transport.joinToken).toBe(TOKEN);
    expect(result.transport.url).toBe("ws://127.0.0.1:7880");
    expect(result.configuration.conversationEngine).toBe("llm");
  });

  it("reuses a caller-supplied client_request_id for idempotent retries", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockImplementation(() => Promise.resolve(jsonResponse(200, createBody)));
    const client = clientWith(fetchMock);

    await client.createSession({ agentConfigId: "cfg-1", clientRequestId: "same-id" });
    await client.createSession({ agentConfigId: "cfg-1", clientRequestId: "same-id" });

    const bodies = fetchMock.mock.calls.map(
      (call) => (JSON.parse(call[1].body as string) as { client_request_id: string }).client_request_id,
    );
    expect(bodies).toEqual(["same-id", "same-id"]);
  });

  it("handles a terminal replay response that omits the token", async () => {
    const noToken = { provider: "livekit", url: transport.url, room_name: "va-rd-abc", participant_identity: "br-1" };
    const body = { ...createBody, idempotent_replay: true, transport: noToken };
    const client = clientWith(vi.fn<FetchLike>().mockResolvedValue(jsonResponse(200, body)));

    const result = await client.createSession({ agentConfigId: "cfg-1" });

    expect(result.idempotentReplay).toBe(true);
    expect(result.transport.joinToken).toBeNull();
  });

  it("refreshes a join token through the mutation envelope", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse(200, {
        data: { session_id: SESSION_ID, transport },
        idempotent_replay: true,
        request_id: "r2",
      }),
    );

    const result = await clientWith(fetchMock).refreshJoinToken(SESSION_ID, "rid-9");

    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(`${BASE}/sessions/${SESSION_ID}/join-token`);
    expect(JSON.parse(init.body as string)).toEqual({ client_request_id: "rid-9" });
    expect(result.transport.joinToken).toBe(TOKEN);
    expect(result.idempotentReplay).toBe(true);
  });

  it("ends a session with a reason and accepts 202", async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse(202, {
        data: {
          session_id: SESSION_ID,
          status: "ending",
          revision: 3,
          termination_request_revision: 1,
          disconnect_reason: "user_ended",
        },
        idempotent_replay: false,
        request_id: "r3",
      }),
    );

    const result = await clientWith(fetchMock).endSession(SESSION_ID, "user_ended");

    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(`${BASE}/sessions/${SESSION_ID}/end`);
    expect(JSON.parse(init.body as string)).toEqual({
      client_request_id: "generated-id",
      reason: "user_ended",
    });
    expect(result.status).toBe("ending");
    expect(result.disconnectReason).toBe("user_ended");
  });

  it("reads session summary and events", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(
        jsonResponse(200, {
          data: {
            session_id: SESSION_ID,
            status: "active",
            agent_activity_state: "listening",
            disconnect_reason: null,
          },
          request_id: "r",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [
            {
              event_id: "e1",
              event_type: "session.active",
              severity: "info",
              sequence_number: 4,
              occurred_at: "2026-09-29T10:00:01Z",
              payload: {},
            },
          ],
          next_cursor: null,
          request_id: "r",
        }),
      );
    const client = clientWith(fetchMock);

    const summary = await client.getSession(SESSION_ID);
    const events = await client.listEvents(SESSION_ID, 50);

    expect(summary.agentActivityState).toBe("listening");
    expect(events[0]?.eventType).toBe("session.active");
    expect(lastCall(fetchMock).url).toBe(`${BASE}/sessions/${SESSION_ID}/events?limit=50`);
  });

  it("lists agent configs", async () => {
    const client = clientWith(
      vi.fn<FetchLike>().mockResolvedValue(
        jsonResponse(200, {
          items: [{ agent_config_id: "cfg-1", name: "Default", version: 2, description: null }],
          next_cursor: null,
          request_id: "r",
        }),
      ),
    );

    const configs = await client.listAgentConfigs();

    expect(configs).toEqual([
      { agentConfigId: "cfg-1", name: "Default", version: 2, description: null },
    ]);
  });

  it("maps the approved error envelope to ApiError without echoing input", async () => {
    const client = clientWith(
      vi.fn<FetchLike>().mockResolvedValue(
        jsonResponse(409, {
          error: {
            code: "IDEMPOTENCY_CONFLICT",
            message: "The request ID was already used for a different request.",
            retryable: false,
            suggested_action: null,
            field_errors: [{ field: "body.client_request_id", code: "conflict" }],
          },
          request_id: "req-7",
        }),
      ),
    );

    const error = await client.createSession({ agentConfigId: "x" }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.code).toBe("IDEMPOTENCY_CONFLICT");
    expect(apiError.status).toBe(409);
    expect(apiError.retryable).toBe(false);
    expect(apiError.requestId).toBe("req-7");
    expect(apiError.fieldErrors).toEqual([{ field: "body.client_request_id", code: "conflict" }]);
  });

  it("reports retryable network failure without leaking the raw exception text", async () => {
    const client = clientWith(
      vi.fn<FetchLike>().mockRejectedValue(new TypeError("connect ECONNREFUSED 10.0.0.1")),
    );

    const error = (await client.listAgentConfigs().catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("NETWORK_ERROR");
    expect(error.retryable).toBe(true);
    expect(error.message).not.toContain("10.0.0.1");
  });

  it("times out slow requests as a retryable TIMEOUT", async () => {
    vi.useFakeTimers();
    const hanging: FetchLike = (_url, init) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    const client = clientWith(hanging, { timeoutMs: 100 });

    const pending = client.listAgentConfigs().catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(150);
    const error = (await pending) as ApiError;
    vi.useRealTimers();

    expect(error.code).toBe("TIMEOUT");
    expect(error.retryable).toBe(true);
  });

  it("rejects an unexpected success shape as INVALID_RESPONSE", async () => {
    const client = clientWith(
      vi.fn<FetchLike>().mockResolvedValue(jsonResponse(201, { session: {} })),
    );

    const error = (await client.createSession({ agentConfigId: "x" }).catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("INVALID_RESPONSE");
  });

  it("treats a non-JSON error body as a generic INVALID_RESPONSE error", async () => {
    const client = clientWith(
      vi.fn<FetchLike>().mockResolvedValue(new Response("<html>oops</html>", { status: 502 })),
    );

    const error = (await client.listAgentConfigs().catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("INVALID_RESPONSE");
    expect(error.status).toBe(502);
    expect(error.retryable).toBe(true);
  });

  it("never puts the join token in a URL or a log", async () => {
    const logSpies = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => undefined),
    );
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(201, createBody));

    await clientWith(fetchMock).createSession({ agentConfigId: "cfg-1" });

    expect(lastCall(fetchMock).url).not.toContain(TOKEN);
    for (const spy of logSpies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});
