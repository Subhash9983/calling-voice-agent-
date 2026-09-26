# Control API Contract

Status: Approved for Phase 0 R&D  
Authority: Decision 028 with Decisions 043, 044, 050, 053, 059, 060, 063, 065, and 067 amendments  
Scope: Local browser-facing control and diagnostic HTTP API  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `02-database-design.md`, `03-backend-module-design.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Runtime: Python FastAPI  
API base: `/api/v1`  
Access mode: Local/trusted single-tester R&D; no application login in Phase 0

## 1. Scope

The control API owns browser-facing session control, LiveKit join credentials, safe configuration reads, diagnostics, feedback, consent, and health checks. It does not stream audio and does not execute realtime STT, conversation, or TTS work inside HTTP request handlers.

Browser audio and realtime state flow through LiveKit. The browser uses FastAPI to create/reload/end sessions and to read durable state.

## 2. General conventions

- base path: `/api/v1`;
- JSON fields use `snake_case`;
- timestamps use ISO-8601 UTC;
- request/response contracts use strict Pydantic models;
- unknown input fields are rejected;
- APIs use cursor pagination rather than deep offset pagination;
- every request receives a backend-generated `request_id` and propagated `correlation_id`;
- browser-supplied correlation IDs are not trusted;
- persistence documents are never returned directly;
- raw exceptions, provider payloads, secrets, restricted pricing, and internal stack traces are never exposed.

OpenAPI is generated from Pydantic contracts. Local R&D documentation may be enabled; production documentation exposure requires a separate decision.

## 3. Phase 0 access boundary

- one local/internal tester;
- no public user registration, login, or account system;
- FastAPI binds to `127.0.0.1` for Phase 0; any later trusted-network binding requires a separate access/authentication decision;
- the initial frontend origin is exactly `http://127.0.0.1:5173`;
- CORS wildcard `*` is prohibited;
- MongoDB and provider credentials remain backend-only;
- public internet deployment is prohibited until authentication is approved;
- backend assigns `initiator_type = internal_tester` and optional safe tester context.

## 4. Health endpoints

### `GET /health/live`

Purpose: process liveness only.

Response `200`:

```json
{
  "status": "ok"
}
```

This endpoint does not call MongoDB or paid providers.

### `GET /health/ready`

Checks:

- settings validation;
- MongoDB connectivity;
- required repository/collection readiness;
- approved configuration loading;
- LiveKit server configuration presence.

Responses:

- `200` when ready;
- `503` when a required dependency/configuration is unavailable.

STT, conversation, and TTS paid requests are not sent on every readiness probe.

## 5. Agent configuration endpoints

### `GET /api/v1/agent-configs`

Query parameters:

- `status`, initially `active`;
- optional `environment`, defaulting to the running `APP_ENV` (`development` or `rd`; Decision 067).

Safe fields:

- `agent_config_id`, `agent_id`, name, description, version;
- display labels for transport, STT, conversation, and TTS;
- language mode and supported UI features.

Excluded fields:

- system instruction;
- credentials and credential references;
- provider endpoints;
- unrestricted provider options;
- detailed retry/timeout internals.

### `GET /api/v1/agent-configs/{agent_config_id}`

Returns one browser-safe configuration summary.

Phase 0 has no browser/API endpoint for configuration creation or editing. Configuration writes use separately approved maintenance commands.

## 6. Session creation

### `POST /api/v1/sessions`

Request:

```json
{
  "client_request_id": "uuid",
  "agent_config_id": "uuid",
  "channel": "browser",
  "session_mode": "interactive_test",
  "language_mode": "auto"
}
```

Rules:

- `client_request_id` is required and unique;
- an identical retry reuses the existing session, opaque room, explicit dispatch, and participant identity rather than creating duplicates; it issues a fresh short-lived join token only while the session is nonterminal;
- an identical retry against a terminal (`ended` or `failed`) session returns `200` with `idempotent_replay: true`, the terminal session summary, and no join token (the `transport` section omits `join_token` and `token_expires_at`), consistent with §7's no-token-for-terminal-sessions rule;
- the same ID with different request semantics returns `409 IDEMPOTENCY_CONFLICT`;
- only an active approved agent configuration is accepted;
- Phase 0 accepts only `channel = browser` and `session_mode = interactive_test`;
- provider/model/prompt/voice/endpoint selection cannot be supplied by the browser;
- ordinary recording is off;
- the backend uses explicit named-agent room dispatch after the durable session exists;
- no token is returned until dispatch preparation succeeds.

Response `201` for the first successful creation; an identical idempotent replay returns `200` with the same logical resource and, only for a nonterminal session, a freshly issued join token:

```json
{
  "idempotent_replay": false,
  "session": {
    "session_id": "uuid",
    "status": "connecting",
    "agent_activity_state": null,
    "created_at": "UTC timestamp",
    "maximum_session_ms": 1800000
  },
  "transport": {
    "provider": "livekit",
    "url": "safe LiveKit URL",
    "room_name": "opaque room name",
    "participant_identity": "opaque identity",
    "join_token": "short-lived token",
    "token_expires_at": "UTC timestamp"
  },
  "configuration": {
    "agent_config_id": "uuid",
    "name": "display name",
    "version": 1,
    "stt": "display label",
    "conversation_engine": "display label",
    "tts": "display label"
  },
  "request_id": "uuid"
}
```

Join-token rules:

- room/participant scoped;
- 10-minute R&D issue lifetime;
- never persisted or logged;
- contains no provider or database credential;
- allows exact-room join, microphone publication, subscription, and approved data send/receive only; LiveKit `canSubscribe` is room-wide rather than per-track, and in the Phase 0 single-user room the only subscribable remote audio is the agent's;
- grants no browser room administration, participant removal, recording/egress control, or arbitrary metadata mutation;
- is returned in the response body and never placed in a URL query parameter.

If dispatch fails after the database session exists, no token is issued and the session is finalized as failed. If dispatch succeeds but token/response preparation fails, the backend deletes the dispatch/room best-effort, finalizes the session as failed, and records any cleanup failure separately.

## 7. Join-token refresh

### `POST /api/v1/sessions/{session_id}/join-token`

Request:

```json
{
  "client_request_id": "uuid"
}
```

Use cases:

- the initial token expires before join;
- an approved reconnect needs a new token.

Rules:

- idempotent by request ID;
- both first success and identical replay return `200`; the response includes `idempotent_replay`, and a replay issues a fresh token without creating another room/participant identity;
- no token for terminal sessions;
- enforce maximum session duration and reconnect window;
- backend owns room and participant identity;
- reconnect reuses the same room and participant identity within the approved window;
- the bounded request ID/fingerprint/issue evidence is stored on the session for conflict detection and replay audit;
- token remains scoped, short-lived, unlogged, unpersisted, and absent from URL query parameters.

## 8. Session reads

### `GET /api/v1/sessions/{session_id}`

Returns a browser-safe summary:

- session and agent activity states;
- configuration display labels;
- lifecycle timestamps;
- language summary;
- turn/error counts;
- latency/cost summary;
- recording state;
- safe terminal reason.

Transcripts and provider diagnostics are not included by default.

### `GET /api/v1/sessions`

Filters:

- status;
- `agent_config_id`;
- `created_before`;
- opaque cursor;
- limit.

Pagination:

- default limit 25;
- maximum limit 100;
- newest first using the approved timestamp/ID cursor.

Response:

```json
{
  "items": [],
  "next_cursor": null,
  "request_id": "uuid"
}
```

## 9. Session termination

### `POST /api/v1/sessions/{session_id}/end`

Request:

```json
{
  "client_request_id": "uuid",
  "reason": "user_ended"
}
```

`reason` must be one of the `disconnect_reason` enum values in `02-database-design.md` §6 (Decision 067): `user_ended`, `browser_closed`, `idle_timeout`, `maximum_duration`, `network_lost`, `transport_error`, `provider_error`, `server_shutdown`, or `unknown`. Any other value returns `422 VALIDATION_FAILED`. The browser normally sends `user_ended`.

Behaviour:

- idempotent;
- atomically create/reuse a bounded durable `termination_request` and transition `created`, `connecting`, or `active` to `ending`;
- set `termination_request.requested_by = anonymous_user` for a browser request (the other allowed values, `system_timeout`, `system_reconciler`, and `system_evaluation`, are used only by backend writers);
- emit the durable `session.end_requested` event (category `session`, 01 §9) when the `termination_request` is first recorded; a replay does not emit it again;
- block new turns;
- when a valid worker lease exists, send a targeted reliable `va.control.v1` `session.end_requested` packet as a low-latency wake-up;
- treat that LiveKit packet as best-effort acceleration only; the durable request remains authoritative and the worker also reloads it during heartbeat;
- allow the worker to cancel providers and close/finalize resources;
- when no valid worker lease exists, let the control-API session reconciler perform room/dispatch cleanup and terminalize without waiting for a worker;
- recovery dispatch and replacement startup both re-read the durable request; an end accepted during recovery sets `ending`, so the replacement claim is rejected (it requires `active`), the dispatched job exits at its claim or startup barrier without starting providers, and the reconciler finalizes the session and cleans up the dispatch;
- finish as `ended` or controlled `failed` when cleanup is unrecoverable;
- return the current terminal state (same schema as the `202` body) when already terminal.

The stored request contains `client_request_id`, reason, requester class, requested time, and revision. Reusing the same client request is idempotent; a later request cannot reopen or overwrite a terminal session.

First accepted response `202`:

```json
{
  "data": {
    "session_id": "uuid",
    "status": "ending",
    "revision": 1,
    "termination_request_revision": 1,
    "disconnect_reason": null
  },
  "idempotent_replay": false,
  "request_id": "uuid"
}
```

`revision` is the session `state_revision` after the write. `disconnect_reason` is `null` until the session is terminal.

An identical replay, an already accepted request, or an already-terminal session returns `200` with the same `data` schema as the `202` body, carrying the current values (for example `status: "ended"`, the current `revision`, and the recorded `disconnect_reason`), and `idempotent_replay: true`. It never returns `202` merely because the original asynchronous cleanup is still running.

Bounded completion rules:

- explicit user end, idle timeout, or maximum-duration end becomes `ended` after verified bounded cleanup;
- connection/start timeout before `active`, exhausted worker-crash recovery, or unrecoverable cleanup becomes `failed` with a normalized reason;
- the reconciler prevents `ending` from remaining nonterminal indefinitely.

## 10. Turn endpoints

### `GET /api/v1/sessions/{session_id}/turns`

Returns ordered turns with:

- turn ID and sequence;
- status;
- final transcript;
- generated, synthesized, and spoken text;
- language;
- interruption summary;
- latency/cost summary;
- timestamps.

Pagination:

- default limit 50;
- maximum limit 100;
- sequence-number cursor.

### `GET /api/v1/sessions/{session_id}/turns/{turn_id}`

Returns one browser-safe detailed turn. Provider payloads, hidden instructions, and restricted diagnostics are excluded.

## 11. Event timeline

### `GET /api/v1/sessions/{session_id}/events`

Optional filters:

- `category`: one of the `session_events.category` values in `02-database-design.md` §9 — `session`, `transport`, `speech`, `stt`, `conversation`, `tts`, `playback`, `turn`, `usage`, `cost`, `error`, `consent`, or `worker` (Decision 067);
- `severity`: one of the `session_events.severity` values in `02-database-design.md` §9 — `debug`, `info`, `warning`, `error`, or `critical`;
- cursor;
- limit.

An unknown `category` or `severity` value returns `422 VALIDATION_FAILED`.

Pagination:

- default limit 100;
- maximum limit 500.

Only safe durable projections are returned. An event's internal storage visibility does not automatically authorize browser exposure.

## 12. Provider-operation diagnostics

### `GET /api/v1/sessions/{session_id}/operations`

Optional filters:

- `turn_id`;
- component;
- status;
- cursor;
- limit.

Safe output:

- component/provider/model labels;
- attempt number and state;
- timing and normalized usage;
- estimated cost;
- retry/fallback state;
- safe error code.

Excluded:

- raw request/response bodies;
- credentials and headers;
- duplicated prompt/transcript content;
- restricted pricing.

## 13. Error diagnostics

### `GET /api/v1/sessions/{session_id}/errors`

Safe output:

- diagnostic code, component, type, and severity;
- retry/fallback/recovery state;
- user impact;
- safe message;
- timestamps.

Raw exceptions, stack traces, restricted-log references, and sensitive provider details are excluded.

## 14. Cost breakdown

### `GET /api/v1/sessions/{session_id}/costs`

Returns the latest successful calculation run:

- calculation-run ID;
- estimated/actual evidence status;
- normalized USD total;
- optional INR display conversion;
- component and provider/model display breakdown;
- retry/failure-related cost;
- calculation timestamp.

Only `aggregation_behavior = charge` contributes to the session total. Allocation-only rows are labelled and never double-counted. Negotiated rates, contracts, and restricted pricing evidence are excluded.

## 15. Feedback

### `POST /api/v1/sessions/{session_id}/feedback`

Request supports:

- `client_submission_id`;
- optional turn/operation target;
- target type and aspects;
- optional thumb, overall rating, and dimension scores;
- reason codes;
- optional correction and comment.

Rules:

- all targets belong to the session;
- at least one feedback signal is required;
- duplicate client submission is idempotent;
- provider/configuration context is derived by the backend;
- comment limit is 4,000 characters.

Response `201` contains a safe feedback receipt for the first submission. An identical `client_submission_id` replay returns `200` with the same receipt and `idempotent_replay: true`.

## 16. Consent

Phase 0 recording remains off, but contracts are defined for later consent-controlled benchmark recording.

### `POST /api/v1/sessions/{session_id}/consents`

One scope and decision per request. Input includes:

- `client_submission_id`;
- scope and data categories;
- decision;
- purpose/version;
- approved notice/version/hash;
- affirmation evidence.

The browser cannot submit arbitrary consent wording; notice versions come from a server-approved catalogue.

The first accepted consent submission returns `201`; an identical `client_submission_id` replay returns `200` with the same receipt and `idempotent_replay: true`.

### `GET /api/v1/sessions/{session_id}/consents/status`

Optional scope filter. Returns safe current status and receipt, not the restricted consent document.

### `POST /api/v1/consent-chains/{consent_chain_id}/revoke`

Request:

```json
{
  "client_submission_id": "uuid",
  "reason": "tester_revoked"
}
```

`reason` permits only `tester_revoked` in Phase 0. The first accepted request returns `201` with a safe revocation receipt. An identical replay returns `200` with that receipt and `idempotent_replay: true`. The operation appends a revocation, stops active recording, blocks new assets, and queues required deletion fulfilment.

## 17. Realtime browser events

FastAPI does not expose an initial audio WebSocket or SSE stream. LiveKit carries:

- connection and agent activity;
- partial/final transcript;
- streamed/final agent text;
- playback/interruption state;
- safe errors;
- turn latency/cost summaries.

Approved LiveKit topics are `va.state.v1`, `va.transcript.v1`, `va.response.v1`, `va.playback.v1`, `va.error.v1`, `va.metrics.v1`, browser-to-agent `va.client.v1`, and targeted control-plane-to-agent `va.control.v1`. Encoded reliable low-frequency application payloads are limited to 8 KiB; encoded lossy high-frequency payloads are limited to 1,200 bytes. Durable/final state uses reliable delivery; high-frequency partial/progress/quality updates use lossy delivery. Full rules are in `docs/06-livekit-transport-adapter.md`.

After reconnect, the browser reloads current durable session/turn state through FastAPI. Unlimited realtime replay is not assumed.

## 18. Response envelopes

Single resource:

```json
{
  "data": {},
  "request_id": "uuid"
}
```

List:

```json
{
  "items": [],
  "next_cursor": null,
  "request_id": "uuid"
}
```

The session-create response uses named `session`, `transport`, and `configuration` sections plus top-level `idempotent_replay` and `request_id`. Every idempotent mutation response includes Boolean `idempotent_replay`; it is `false` on the first accepted mutation and `true` when the same request/submission ID reuses the existing logical result.

## 19. Error contract

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "The requested session was not found.",
    "retryable": false,
    "suggested_action": null,
    "field_errors": []
  },
  "request_id": "uuid"
}
```

Stable application codes and HTTP mapping:

| HTTP | Application code | Meaning |
|---:|---|---|
| `400` | `INVALID_REQUEST` | Request semantics are unsupported or internally inconsistent. |
| `401` | `AUTHENTICATION_REQUIRED` | Reserved for a later authenticated deployment; unused by the Phase 0 no-login loopback API. |
| `403` | `ACCESS_FORBIDDEN` | The request is outside the approved access scope; this does not imply that Phase 0 implements user login. |
| `404` | `RESOURCE_NOT_FOUND` | The requested session, turn, consent chain, or other resource does not exist in the caller's scope. |
| `409` | `INVALID_STATE` | The resource exists but its lifecycle state prohibits the operation. |
| `409` | `REVISION_CONFLICT` | The supplied/observed optimistic revision is stale. |
| `409` | `IDEMPOTENCY_CONFLICT` | A request/submission ID was reused with different semantics. |
| `413` | `PAYLOAD_TOO_LARGE` | The encoded request exceeds its approved bound. |
| `422` | `VALIDATION_FAILED` | One or more strict field validations failed; bounded `field_errors` identify them. |
| `422` | `CONSENT_REQUIRED` | The requested recording/data operation lacks an active matching consent grant. |
| `429` | `RATE_LIMITED` | A safe application/provider rate limit prevented acceptance. |
| `502` | `PROVIDER_UNAVAILABLE` | A required upstream provider failed before the operation could be accepted. |
| `503` | `DEPENDENCY_UNAVAILABLE` | A required local/database/transport dependency is temporarily unavailable. |
| `500` | `INTERNAL_ERROR` | Unexpected internal failure; response remains generic and non-sensitive. |

No endpoint invents a new browser-visible error code without updating this allowlist and its generated OpenAPI contract.

## 20. Request correlation and timeout behaviour

- backend creates request/correlation IDs;
- safe structured logs include related session/turn/operation IDs;
- HTTP handlers do not wait for a complete realtime conversation response;
- API database/dependency timeouts are bounded;
- realtime timeouts remain in immutable agent configuration;
- long maintenance/evaluation work never runs in an ordinary request handler.

## 21. Phase 0 exclusions

- browser-based configuration creation/editing;
- arbitrary provider/model/voice/prompt switching;
- database query console;
- public registration/login;
- production administration API;
- outbound phone-call API;
- evaluation-run API;
- knowledge-base management API;
- unrestricted export;
- delete-all/reset-database endpoint.

## 22. Persistence implication

Add required `voice_sessions.client_request_id`:

- canonical UUID string;
- unique;
- preserves session-create idempotency;
- repeated ID with different create semantics returns `409 IDEMPOTENCY_CONFLICT`.

Add unique index:

```text
uq_session_client_request_id
{client_request_id: 1}
```

Join-token, session-end, feedback, and consent mutations also require their approved client request/submission IDs for idempotent handling.

## 23. Approved Phase 0 endpoint catalogue

```text
GET  /health/live
GET  /health/ready

GET  /api/v1/agent-configs
GET  /api/v1/agent-configs/{agent_config_id}

POST /api/v1/sessions
GET  /api/v1/sessions
GET  /api/v1/sessions/{session_id}
POST /api/v1/sessions/{session_id}/join-token
POST /api/v1/sessions/{session_id}/end

GET  /api/v1/sessions/{session_id}/turns
GET  /api/v1/sessions/{session_id}/turns/{turn_id}
GET  /api/v1/sessions/{session_id}/events
GET  /api/v1/sessions/{session_id}/operations
GET  /api/v1/sessions/{session_id}/errors
GET  /api/v1/sessions/{session_id}/costs

POST /api/v1/sessions/{session_id}/feedback

POST /api/v1/sessions/{session_id}/consents
GET  /api/v1/sessions/{session_id}/consents/status
POST /api/v1/consent-chains/{consent_chain_id}/revoke
```

## 24. Acceptance criteria

- audio/realtime provider work does not run through FastAPI;
- session creation is idempotent;
- only active server-approved configuration can create a session;
- LiveKit tokens are scoped, expire after 10 minutes, and are never stored/logged;
- browser cannot set provider credentials, prompts, models, endpoints, or persistence fields;
- all reads use safe projections and approved pagination;
- session end and other mutations are idempotent;
- internal errors and restricted data never enter browser responses;
- Phase 0 remains local/trusted and cannot be deployed publicly without an authentication decision.
