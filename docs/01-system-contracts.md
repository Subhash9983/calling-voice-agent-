# Voice Agent System Contracts

Status: Approved for Phase 0 R&D  
Authority: Decisions 001–067  
Scope: Cross-component browser, API, worker, provider, event, and persistence contracts  
Depends on: `00-voice-agent-master-plan.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

## 1. Approved technical direction

- Browser R&D UI: React, TypeScript, and Vite.
- Agent worker: Python.
- Control API: FastAPI.
- Shared backend models: Pydantic.
- First realtime transport: LiveKit behind a replaceable adapter.
- First conversation engine: a general-purpose chat LLM.
- Existing Python knowledge graph: later conversation-engine implementation.
- Primary durable database: MongoDB Atlas.

## 2. Purpose of this document

This document defines the contracts between the browser, control API, realtime transport, agent worker, AI providers, and persistence layer.

The contracts must remain stable enough that we can replace:

- LiveKit with another realtime platform;
- one STT provider with another;
- the general LLM with another LLM or the knowledge engine;
- one TTS provider with another;
- cloud deployment with a self-hosted deployment.

This document does not select the first STT, LLM, or TTS model. It also does not define database indexes or implementation code.

Contract casing rule: JSON fields and all runtime/persistence enum values use lowercase `snake_case`; browser-visible application error codes and environment-variable names use uppercase `SNAKE_CASE`.

## 3. Service boundaries

### Browser R&D UI

The browser is responsible for:

- obtaining microphone permission;
- requesting a voice session from FastAPI;
- connecting to the selected realtime transport with a short-lived token;
- publishing microphone audio;
- playing agent audio;
- showing normalized session and agent events;
- collecting internal tester feedback.

The browser is not responsible for:

- holding provider secret keys;
- calling STT, LLM, or TTS providers directly;
- calculating authoritative costs;
- owning conversation memory;
- making knowledge or safety decisions.

### FastAPI control API

The control API is responsible for:

- enforcing the approved local loopback/trusted boundary and assigning the fixed `internal_tester` context;
- creating and closing sessions;
- loading the approved agent configuration;
- issuing short-lived transport credentials;
- returning session summaries and diagnostic records;
- enforcing application rate and concurrency limits.

The control API does not process the realtime audio path.

### Python agent worker

The agent worker is responsible for:

- joining the realtime session;
- running the voice pipeline;
- coordinating STT, conversation engine, and TTS;
- handling turns, interruptions, cancellations, and timeouts;
- emitting normalized events, usage, cost inputs, and errors.

### Provider adapters

Each adapter is responsible for translating between an external provider SDK/API and the internal contract. Provider-specific response objects must stop at the adapter boundary.

### Persistence and observability

MongoDB Atlas stores durable business and diagnostic records. The observability layer handles operational logs, metrics, and traces. A successful database or log write must not be required for the audio conversation to continue. Realtime cache and object-storage products remain pending.

## 4. Identifier contract

Every operation must be traceable with stable identifiers.

| Identifier | Created by | Lifetime | Purpose |
| --- | --- | --- | --- |
| `session_id` | Control API | Whole conversation | Links every record in one browser or phone session |
| `turn_id` | Agent worker | One user-to-agent exchange | Links transcript, LLM, TTS, latency, and interruption data |
| `operation_id` | Agent worker | One provider request | Identifies an STT, LLM, TTS, retrieval, or transport operation |
| `event_id` | Event producer | One event | Supports deduplication and audit |
| `correlation_id` | Entry-point service | Request chain | Connects API, worker, provider, and persistence logs |
| `provider_request_id` | External provider | Provider request | Supports vendor troubleshooting and invoice reconciliation |

Identifiers generated internally use one canonical UUID string representation but are opaque and must not be used for chronological ordering. Sequence numbers and explicit timestamps provide order. Provider identifiers remain external strings and must not replace internal IDs.

## 5. Session state contract

### Session states

```text
created
   +--> connecting --> active --> ending --> ended
   |         |           |          |
   |         +-----------+----------+--> failed
   +---------------------> ending
   +--------------------------------> failed
```

Approved transitions:

- `created -> connecting | ending | failed`;
- `connecting -> active | ending | failed`;
- `active -> ending | failed`;
- `ending -> ended | failed`.

Transition triggers:

- `created -> connecting` happens during session creation, when the control API has successfully created the explicit named-agent dispatch for the durable session (04 §6). The create response therefore already reports `connecting`. If dispatch creation fails, the session goes `created -> failed` and no join token is issued.
- `connecting -> active` happens when the worker completes activation (05 §10).

| State | Meaning |
| --- | --- |
| `created` | Database session exists; explicit agent dispatch not yet created, so no transport credentials are issued |
| `connecting` | Dispatch exists; browser and agent are joining the realtime session |
| `active` | Browser and agent are connected; conversation can proceed |
| `ending` | New turns are blocked; current resources are being closed |
| `ended` | Session closed normally and final usage was recorded |
| `failed` | Session could not start or continue because of an unrecoverable error |

Rules:

- `ended` and `failed` are terminal.
- A disconnect does not immediately mean `failed`; the reconnect policy receives a bounded opportunity first.
- browser/agent connection timeout before `active`, exhausted worker recovery, or unrecoverable runtime failure ends as `failed`;
- explicit user end, idle timeout, or maximum-duration end proceeds through `ending` to `ended` when bounded cleanup succeeds;
- a user-side disconnect whose 20-second reconnect window expires (the browser closed or lost its network and did not return) proceeds through `ending` to `ended`, with `disconnect_reason = browser_closed` or `network_lost` (02 §6); it is not a system fault;
- an agent-side or infrastructure fault (worker crash beyond the recovery budget, `transport_error`, `provider_error`, `server_shutdown`) ends as `failed`;
- an end request received in `created` or `connecting` is valid and proceeds through `ending` without waiting for a worker;
- Closing an already closed session is idempotent.
- The authoritative session state is stored by the backend, not inferred only from the browser screen.

## 6. Agent activity state contract

Agent activity is separate from session state.

| State | Meaning |
| --- | --- |
| `idle` | Session is active, but no turn is in progress |
| `listening` | User audio is being accepted |
| `transcribing` | User audio is being finalized into text |
| `thinking` | Conversation engine is generating a response |
| `speaking` | Agent audio is being generated or played |
| `interrupted` | Current agent response was cancelled by a user interruption |
| `recovering` | A temporary provider or connection failure is being retried, or a worker-lease recovery is in progress (Decision 067) |
| `error` | Current turn cannot continue |

The browser uses these normalized values. It must not derive UI state from provider-specific event names.

## 7. Turn state contract

| State | Meaning |
| --- | --- |
| `open` | User turn has started |
| `transcript_final` | Final user transcript is available |
| `response_streaming` | LLM text is being generated |
| `audio_streaming` | TTS audio is being generated or played |
| `completed` | Agent response finished normally |
| `interrupted` | Turn was stopped by barge-in or explicit cancellation |
| `failed` | Turn ended because of an unrecoverable error |
| `abandoned` | Session ended before the turn could complete |
| `discarded` | Final transcript was empty or noise and no clarification fallback was sent |

Rules:

- Each user turn has one primary `turn_id`.
- A retry of one provider operation keeps the same `turn_id` but receives a new `operation_id`.
- A new user utterance after interruption creates a new turn.
- Audio from an accepted interrupted turn never resumes automatically. A sub-250 ms candidate may be classified as a suppressed false interruption before cancellation, allowing uninterrupted playback to continue.
- Allowed transitions are branching rather than linear: `open -> transcript_final|audio_streaming|interrupted|failed|abandoned|discarded`; `transcript_final -> response_streaming|audio_streaming|interrupted|failed|abandoned`; `response_streaming -> audio_streaming|completed|interrupted|failed|abandoned`; and `audio_streaming -> completed|interrupted|failed|abandoned`.
- `completed`, `interrupted`, `failed`, `abandoned`, and `discarded` are terminal.
- A turn opens with `input_disposition = pending`, before any transcript is available. The disposition then becomes `accepted`, `empty`, `unusable`, or `timed_out`.
- An empty, whitespace-only, timed-out, or otherwise unusable transcript cannot enter `transcript_final` or authorize an LLM request. The turn records its input disposition and may follow `open -> audio_streaming -> completed` only for the versioned application-owned clarification fallback. Without that fallback, an empty or noise turn ends `discarded`; it is not recorded as `failed`.
- Precedence while stages overlap:
  - once the first audio for a turn is authorized, the turn status is `audio_streaming`, even while LLM text is still streaming;
  - `response_streaming -> completed` without audio is allowed only when every segment was suppressed or the response had no speakable text;
  - `transcript_final -> audio_streaming` is allowed only for the deterministic fallback/clarification phrase.
- Output stopped by a model length limit is represented only by `response_completion_status = truncated_partial` or `truncated_fallback`; the turn's terminal status is `completed`, and no separate truncated status exists. The normalized finish reason `maximum_tokens` maps from the Responses API `status = incomplete` with `incomplete_details.reason = "max_output_tokens"`. Incomplete trailing text is never spoken or added to delivered history.
- Local VAD speech start during agent playback opens a turn only after the interruption candidate is confirmed (≥250 ms). A suppressed false-interruption candidate never creates a turn.

## 8. Normalized event envelope

Every internal event carries:

- schema version;
- event ID;
- event type;
- event timestamp in UTC;
- session ID;
- optional turn ID;
- optional operation ID;
- correlation ID;
- component name;
- optional provider and model;
- producer service;
- non-sensitive event payload.

Event timestamps use UTC. Latency calculations should prefer a monotonic process clock while persisting UTC timestamps for cross-service analysis.

## 9. Event catalogue

### Session and transport

- `session.created`
- `session.connecting`
- `session.active`
- `session.ending`
- `session.ended`
- `session.failed`
- `session.end_requested` (durable, category `session`; emitted by the control API when it records a `termination_request`)
- `transport.connected`
- `transport.disconnected`
- `transport.reconnecting`
- `transport.reconnected`
- `transport.quality_updated`

### User speech and STT

- `user.speech_started`
- `user.speech_ended`
- `stt.stream_started`
- `stt.partial`
- `stt.final`
- `stt.turn_finalized`
- `stt.usage`
- `stt.warning`
- `stt.failed`
- `stt.stream_closed`

### Conversation engine

- `conversation.started`
- `conversation.first_token`
- `conversation.text_delta`
- `conversation.segment_ready`
- `conversation.completed`
- `conversation.usage`
- `conversation.cancelled`
- `conversation.failed`

### TTS and playback

- `tts.session_started`
- `tts.segment_started`
- `tts.first_audio`
- `tts.audio_frame`
- `tts.segment_completed`
- `tts.usage`
- `tts.cancelled`
- `tts.failed`
- `tts.session_closed`
- `playback.started`
- `playback.progress`
- `playback.completed`
- `playback.cancelled`
- `playback.failed`

### Client control

- `client.ready`
- `client.message_rejected`
- `client.mic_muted`
- `client.mic_unmuted`
- `client.latency_sample`

Server-to-browser state events:

- `agent.recovering` (sent by the reconciler through `RoomService.SendData` on `va.state.v1`; non-durable; the durable record is `worker.recovery_started`)

Browser client events (`client.ready`, `playback.progress`, `playback.failed`, `client.mic_muted`, `client.mic_unmuted`, `client.latency_sample`) arrive on the browser-to-agent `va.client.v1` topic. `client.latency_sample` carries the bounded per-turn browser playout span and RTT/2 estimate used for response latency (`06-livekit-transport-adapter.md` §15). They are non-durable unless the durable catalogue in `02-database-design.md` §9 lists them as stored. The targeted control-plane-to-agent `va.control.v1` topic carries the `session.end_requested` wake-up; the durable `termination_request` remains authoritative.

### Worker and recovery

All of these are durable, with category `worker`:

- `worker.lease_expired`
- `worker.recovery_started`
- `worker.recovery_claimed`
- `worker.recovery_failed`
- `worker.self_fenced`

High-volume audio chunks should not be written individually to MongoDB Atlas. Aggregate timing and usage events are persisted; detailed media telemetry belongs in operational metrics when required.

The names above are the canonical normalized runtime event names. Persistence stores only the approved durable subset; it does not rename runtime events. Provider-native names remain inside adapters.

`conversation.segment_ready` is created only by the orchestrator-owned `ResponseSegmenter` after a complete candidate passes normalization, policy validation, and generation checks. Conversation-provider adapters emit `conversation.text_delta` and completion/usage/failure events but do not create speakable segments.

### Turn control

- `turn.opened`
- `turn.completed`
- `turn.interruption_detected`
- `turn.interrupted`
- `turn.failed`
- `turn.abandoned`
- `turn.discarded`
- `turn.false_interruption_suppressed`

### Usage and cost

- `usage.recorded`
- `cost.calculated`
- `cost.recalculated`

### Consent and recording control

- `consent.requested`
- `consent.granted`
- `consent.denied`
- `consent.revoked`
- `consent.expired`
- `recording.started`
- `recording.stopped`
- `consent.fulfilment_started`
- `consent.fulfilment_completed`
- `consent.fulfilment_failed`

### Errors

- `error.retry_scheduled`
- `error.recovered`
- `error.unrecoverable`

## 10. Event ordering and delivery rules

- Events are ordered within a session by a backend sequence number.
- Every durable-event writer obtains that number from the shared repository `EventSequenceAllocator`, which uses MongoDB `findOneAndUpdate` to increment `voice_sessions.event_sequence_counter` from zero and return the updated value; API, worker, and maintenance writers do not maintain independent authoritative counters.
- Sequence gaps after an allocated write fails are valid, but duplicate `(session_id, sequence_number)` values are rejected.
- UTC timestamps alone are not used to determine exact order.
- Consumers must tolerate duplicate events.
- Event IDs provide deduplication.
- Missing optional diagnostic events must not break the voice session.
- Terminal session and turn events must be persisted reliably.
- Late provider usage events may update cost after the audible turn has completed.
- Browser reconnect must request the current state instead of assuming that all missed events will be replayed.

## 11. Realtime transport contract

The internal transport capability must support:

- create/join session;
- publish user audio;
- receive agent audio;
- send and receive small data events;
- expose connection state and quality;
- stop current playback;
- reconnect within a bounded window;
- close session;
- expose transport usage required for cost calculation.

LiveKit-specific rooms, participants, tracks, publications, and tokens remain inside the LiveKit adapter and token service.

## 12. STT contract

Inputs:

- session and turn identifiers;
- audio frames in the internal audio format;
- source sample rate and channel information;
- language mode;
- optional product vocabulary;
- cancellation signal.

Outputs:

- partial transcript text;
- final transcript text;
- detected language when available;
- word timestamps when available;
- confidence when available;
- normalized provider usage;
- provider request ID;
- recoverable or unrecoverable error.

Rules:

- Partial transcripts are provisional and must not be treated as durable user intent.
- Only a final transcript can normally start the conversation engine.
- Empty final transcripts do not create an LLM request.
- Provider confidence is stored but cannot be compared directly across vendors without calibration.

## 13. Conversation engine contract

Inputs:

- session and turn identifiers;
- final user transcript;
- normalized conversation history;
- system-instruction version;
- language preference;
- approved tool definitions;
- cancellation signal.

Outputs:

- streamed text segments;
- optional tool requests;
- final response text;
- finish reason;
- normalized input, cached-input, output, and reasoning usage when available;
- provider request ID;
- recoverable or unrecoverable error.

Rules:

- The sandbox engine uses a general LLM.
- The future knowledge engine must implement the same high-level contract while adding retrieval and citation metadata.
- Provider-specific message classes cannot enter conversation-core history.
- The orchestrator owns cancellation; adapters must respond to its cancellation signal.
- Text sent to TTS is stored separately from raw streamed tokens so spoken content can be audited.

## 14. TTS contract

Inputs:

- session, turn, and segment identifiers;
- speakable text segment;
- language;
- voice configuration;
- output audio format;
- cancellation signal.

Outputs:

- streaming audio frames;
- first-audio timestamp;
- completion status;
- normalized character, token, or audio-duration usage;
- provider request ID;
- recoverable or unrecoverable error.

Rules:

- TTS must support cancelling unplayed audio.
- A cancelled segment cannot be placed back into the playback queue.
- Text segmentation is owned by conversation core, not a provider adapter.
- Provider voices are referenced through internal voice configuration, with provider IDs stored as adapter settings.

## 15. Cancellation and interruption contract

Cancellation is hierarchical:

```text
Session cancellation
  -> cancels every active turn and provider operation

Turn cancellation
  -> cancels current conversation and TTS operations

Operation cancellation
  -> cancels only one provider request
```

When a genuine user interruption occurs, the canonical order (Decision 067; 05, 06, 08, and 09 match it) is:

1. confirm the candidate (≥250 ms of continuous VAD speech; during agent playback the activation threshold is 0.7, `vad.playback_activation_threshold`);
2. increment the in-memory cancellation generation (the fence);
3. cancel LLM streaming and TTS: queued segments first, then the active segment;
4. call `AudioSource.clear_queue()` (queue bounded at `queue_size_ms = 200`);
5. notify the browser (the interruption state message);
6. record events and evidence, including the text and audio portion actually delivered, and mark the turn `interrupted`.

From step 2 onward, late data from cancelled operations cannot reach playback. The next user utterance opens a new turn.

Every asynchronous result must confirm that its session, turn, operation, and cancellation generation are still active before changing user-visible state.

Local Silero VAD is authoritative for speech-activity start/stop, while the Turn Manager is authoritative for accepting the interruption. Continuous speech must reach 250 ms from candidate start before cancellation. Deepgram speech/endpoint signals are advisory only. An accepted interruption with no usable transcript follows the clarification fallback and cannot restore stale audio.

The approved worker/orchestration implementation also validates the active worker-assignment generation. Detailed worker claiming, bounded queues, session-stream STT lifecycle, single-writer command loop, interruption ordering, disconnect handling, and idempotent finalization are defined in `docs/05-agent-worker-orchestration.md`.

## 16. Timeout and retry contract

Timeout values belong to versioned agent configuration, not provider adapters.

Required timeout categories:

- browser join timeout;
- agent join timeout;
- maximum silence before an idle status;
- maximum user-turn duration;
- STT finalization timeout;
- conversation first-token timeout;
- conversation total timeout;
- TTS first-audio timeout;
- session idle timeout;
- reconnect window;
- graceful shutdown timeout;
- maximum session duration timeout.

Retry rules:

- retry only errors explicitly classified as transient;
- use bounded attempts and backoff;
- do not replay a non-idempotent tool action automatically;
- create a new operation ID for each retry;
- retain the same turn ID;
- record every attempt for cost and reliability analysis;
- stop retrying immediately after cancellation.

## 17. Error classification

Each normalized error includes:

- component;
- provider;
- internal error type;
- safe message;
- retryable flag;
- HTTP, WebSocket, or provider status when applicable;
- session, turn, and operation identifiers;
- occurrence time;
- whether the user was affected;
- whether fallback succeeded.

Initial internal error types:

- `authentication_failed`
- `permission_denied`
- `configuration_invalid`
- `rate_limited`
- `quota_exhausted`
- `connection_failed`
- `connection_lost`
- `provider_timeout`
- `provider_unavailable`
- `invalid_audio`
- `empty_transcript`
- `content_rejected`
- `cancellation`
- `realtime_overload`
- `persistence_failed`
- `unknown_provider_error`
- `internal_error`

Each internal error type maps to one `error_events.category` in `02-database-design.md` §12. `realtime_overload` maps to category `capacity` (Decision 067).

Raw secrets, authorization headers, full provider payloads, and sensitive user data must not be placed in the general error record.

## 18. Configuration boundaries

### Public session configuration

The browser may receive:

- session ID;
- transport URL;
- short-lived transport token;
- enabled UI features;
- display labels for provider/model combination;
- audio input constraints;
- non-sensitive timeout information;
- event schema version.

### Server runtime configuration

Only backend services may receive:

- provider API credentials;
- provider endpoints;
- exact model IDs;
- prompts and tool definitions;
- retry policies;
- full turn-detection configuration;
- storage credentials;
- cost rate cards;
- internal feature flags.

### Versioning

Every session records:

- agent configuration ID and version;
- event-schema version;
- system-instruction version;
- transport adapter and version;
- STT provider and model;
- conversation engine provider and model;
- TTS provider, model, and voice;
- cost-calculation version.

## 19. FastAPI boundary

Initial API capabilities:

- create a session;
- read current session state;
- close a session;
- read a session summary;
- read a session event timeline for internal diagnostics;
- submit user feedback;
- list approved agent configurations for internal testers.

The exact approved Phase 0 route, payload, pagination, idempotency, response, error, token, and local-access contracts are defined in `docs/04-control-api-contract.md`. Session creation requires unique `client_request_id`; LiveKit join tokens are room/participant scoped, valid for 10 minutes in R&D, and never persisted or logged. Phase 0 has no public application login and cannot be deployed beyond a local/trusted boundary until authentication is separately approved.

The browser must not be allowed to submit arbitrary provider credentials, prompts, model names, or server endpoints.

## 20. Browser event contract

The browser receives a reduced set of normalized events:

- connection state;
- agent activity state;
- partial user transcript;
- final user transcript;
- streamed or final agent text;
- playback state;
- interruption state;
- safe error message;
- per-turn latency summary;
- estimated per-turn cost after calculation;
- active configuration display labels.

Debug details visible in the R&D UI must still be safe for the approved internal-tester context.

## 21. Latency definitions

To keep provider comparisons consistent:

- STT finalization latency: user speech end to final transcript.
- LLM first-token latency: conversation request start to first response token.
- TTS first-audio latency: TTS request start to first playable audio.
- First audible response: user speech end to browser playback start.
- Turn completion latency: user speech end to final agent playback completion.
- Interruption latency: accepted interruption detection to playback stopped.

P50, P95, and failure rate should be calculated from the same event definitions across providers.

## 22. Cost usage contract

Adapters report usage in the provider's native billable units. The cost engine performs pricing.

Examples of normalized units:

- connected audio seconds;
- transcribed audio seconds;
- input tokens;
- cached input tokens;
- output tokens;
- reasoning tokens;
- synthesized characters;
- generated audio seconds;
- transport session seconds;
- recorded audio seconds;
- telephony connected seconds.

Adapters must not silently convert an unavailable usage value into zero. Missing usage remains unavailable until resolved or estimated under an explicitly labelled calculation method.

## 23. Contract compatibility rules

- Additive optional fields are allowed within one schema version.
- Removing or changing field meaning requires a new schema version.
- Unknown event types must be ignored safely and logged.
- The browser should support the current and immediately previous event-schema versions during R&D.
- Persist original provider/model identifiers even if display names change.
- Store raw rate units so historical cost can be reproduced.

## 24. Acceptance criteria for system contracts

The contracts are ready for implementation when:

- browser, API, worker, and adapter ownership is unambiguous;
- session, activity, and turn state transitions are agreed;
- every important latency metric maps to named events;
- interruption and cancellation ordering is agreed;
- retryable and non-retryable failures are distinguishable;
- no browser contract exposes provider secrets;
- usage units support component-level cost calculation;
- LiveKit-specific objects remain inside the transport integration;
- future knowledge-engine and alternative-provider implementations fit the same boundaries.

## 25. Approval status and remaining boundaries

Resolved for the Phase 0 local/trusted R&D baseline:

1. STT: Deepgram Nova-3 Multilingual;
2. conversation model: OpenAI GPT-6 Luna with no reasoning effort;
3. TTS: Sarvam Bulbul v3 using voice `priya`;
4. audio retention: ordinary recording off; separately consented benchmark audio only after storage approval;
5. access scope: one local/trusted R&D user with no public application login;
6. timing: 300-second idle timeout and 30-minute maximum session duration;
7. concurrency scope: single-user R&D baseline;
8. browser evidence and cost summaries: only normalized safe application contracts, never raw provider objects or secrets;
9. persistence: the approved nine-collection MongoDB Atlas Flex core model, launch indexes, validators, 30-day retention, and no-backup R&D policy through PyMongo Async repositories.

Still outside this contract: public/internal multi-user authentication, production concurrency, production database/deployment settings, object storage, benchmark/evaluation collection details, and UI presentation refinements that do not change the normalized contracts.

## 26. Approved documentation process

- No unresolved proposal in this document is a final architecture decision.
- Before selecting a database, storage service, provider, deployment platform, or major implementation approach, present the options and obtain explicit confirmation.
- Clearly label recommendations as recommendations until approved.
- Do not begin module implementation merely because an approved design document exists; implementation starts only when it is explicitly started under `14-phase0-implementation-execution-plan.md`.
