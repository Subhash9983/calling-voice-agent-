/**
 * Typed control-API client (docs/04). Base URL comes from the validated WP3
 * public configuration (it already includes `/api/v1`).
 *
 * Idempotency: every mutation carries a `client_request_id` (UUID) in the
 * body. Callers own the id for a logical operation and pass the same one on
 * retry so the backend replays instead of duplicating (docs/04 §6-§7, §9).
 *
 * Security: responses are `no-store`, no credentials/cookies are sent, and
 * neither the request body nor a join token is ever logged or put in a URL.
 */
import {
  parseAgentConfigList,
  parseCreateSession,
  parseEndSession,
  parseEventList,
  parseJoinToken,
  parseSessionSummary,
  type AgentConfigView,
  type CreateSessionResult,
  type DisconnectReason,
  type EndSessionResult,
  type JoinTokenResult,
  type SessionEventItem,
  type SessionSummary,
} from "../contracts/sessionApi";
import { ContractError } from "../contracts/validate";
import { ApiError, apiErrorFromBody, clientError } from "./apiError";

export const DEFAULT_TIMEOUT_MS = 15_000;

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface ControlApiClientOptions {
  readonly baseUrl: string;
  readonly fetchImpl?: FetchLike;
  readonly timeoutMs?: number;
  readonly newRequestId?: () => string;
}

export interface ControlApiClient {
  listAgentConfigs(): Promise<readonly AgentConfigView[]>;
  createSession(input: {
    readonly agentConfigId: string;
    readonly clientRequestId?: string;
  }): Promise<CreateSessionResult>;
  refreshJoinToken(sessionId: string, clientRequestId?: string): Promise<JoinTokenResult>;
  endSession(
    sessionId: string,
    reason: DisconnectReason,
    clientRequestId?: string,
  ): Promise<EndSessionResult>;
  getSession(sessionId: string): Promise<SessionSummary>;
  listEvents(sessionId: string, limit?: number): Promise<readonly SessionEventItem[]>;
}

const defaultFetch: FetchLike = (input, init) => fetch(input, init);

export function createControlApiClient(options: ControlApiClientOptions): ControlApiClient {
  const base = options.baseUrl.replace(/\/+$/, "");
  const doFetch = options.fetchImpl ?? defaultFetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const newId = options.newRequestId ?? ((): string => crypto.randomUUID());

  async function request<T>(
    method: "GET" | "POST",
    path: string,
    body: unknown,
    parse: (raw: unknown) => T,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, timeoutMs);
    try {
      const init: RequestInit = {
        method,
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        cache: "no-store",
        credentials: "omit",
        signal: controller.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      };
      const response = await doFetch(`${base}${path}`, init);
      return await handle(response, parse);
    } catch (error) {
      throw toApiError(error, controller.signal.aborted);
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    listAgentConfigs: () => request("GET", "/agent-configs", undefined, parseAgentConfigList),
    createSession: ({ agentConfigId, clientRequestId }) =>
      request(
        "POST",
        "/sessions",
        {
          client_request_id: clientRequestId ?? newId(),
          agent_config_id: agentConfigId,
          channel: "browser",
          session_mode: "interactive_test",
          language_mode: "auto",
        },
        parseCreateSession,
      ),
    refreshJoinToken: (sessionId, clientRequestId) =>
      request(
        "POST",
        `/sessions/${encodeURIComponent(sessionId)}/join-token`,
        { client_request_id: clientRequestId ?? newId() },
        parseJoinToken,
      ),
    endSession: (sessionId, reason, clientRequestId) =>
      request(
        "POST",
        `/sessions/${encodeURIComponent(sessionId)}/end`,
        { client_request_id: clientRequestId ?? newId(), reason },
        parseEndSession,
      ),
    getSession: (sessionId) =>
      request("GET", `/sessions/${encodeURIComponent(sessionId)}`, undefined, parseSessionSummary),
    listEvents: (sessionId, limit = 100) =>
      request(
        "GET",
        `/sessions/${encodeURIComponent(sessionId)}/events?limit=${String(limit)}`,
        undefined,
        parseEventList,
      ),
  };
}

async function handle<T>(response: Response, parse: (raw: unknown) => T): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    throw apiErrorFromBody(response.status, body);
  }
  if (body === undefined) {
    throw clientError("INVALID_RESPONSE", "The server returned an unreadable response.", false);
  }
  return parse(body);
}

function toApiError(error: unknown, timedOut: boolean): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof ContractError) {
    return clientError("INVALID_RESPONSE", "The server returned an unexpected response shape.", false);
  }
  if (timedOut) {
    return clientError("TIMEOUT", "The server did not respond in time.", true);
  }
  return clientError("NETWORK_ERROR", "The server could not be reached.", true);
}
