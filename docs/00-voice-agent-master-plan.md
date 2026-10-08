# Voice Agent Master Plan

Status: Approved for Phase 0 R&D  
Authority: Decisions 001–069  
Scope: Local single-user browser voice-agent baseline and its approved R&D evidence contracts  
Depends on: None  
Implementation status: Not started  
Last reviewed: 2026-09-28

Current milestone: Browser voice sandbox  
Knowledge base integration: Later milestone  
Initial realtime platform: LiveKit behind a replaceable adapter  
Approved backend direction: Python

Decisions 001–067 are the authoritative approval record. Earlier planning text is interpreted through those decisions and the linked detailed documents.

## 1. Current objective

The first product is a browser-based conversational voice sandbox.

It will:

- capture microphone audio in the browser;
- send audio through a realtime transport;
- transcribe speech with one STT provider;
- generate a reply with one general-purpose LLM;
- synthesize the reply with one TTS provider;
- play the audio response in the browser;
- support Hindi, Hinglish, and English conversations;
- record latency, failures, usage, and estimated cost for every stage.

The first version will not use the existing knowledge graph. Once the voice pipeline is stable, the general LLM conversation engine will be replaced or extended with the knowledge-based engine.

## 2. Architecture principle

Every vendor-facing component must be replaceable.

```text
Browser Voice UI
       |
       v
Realtime Transport Interface
       |
       +-- LiveKit adapter (first)
       +-- Pipecat/Daily adapter (later R&D)
       +-- Vapi adapter (later R&D)
       +-- Custom WebRTC adapter (optional)
       |
       v
Voice Session Orchestrator
       |
       +-- Speech Activity Detector (local Silero VAD)
       +-- Turn Manager
       +-- STT Interface
       +-- Conversation Engine Interface
       +-- Response Segmenter
       +-- TTS Interface
       +-- Event and Cost Collector
       |
       v
Persistence and Observability
```

Provider SDK objects must not be passed into business logic. Each adapter converts provider-specific messages into internal events.

Worker media flow (Decisions 042 and 062):

```text
microphone -> LiveKit transport -> 48 kHz / 20 ms normalized frames
                                   +-> resample 16 kHz -> Speech Activity Detector (Silero VAD)
                                   |                        -> speech start/end candidates -> Turn Manager
                                   +-> resample 16 kHz -> STT adapter -> finalized segments -> Turn Manager
Turn Manager (commits turns) -> Conversation Engine -> Response Segmenter -> TTS (24 kHz)
                             -> LiveKit AudioSource (200 ms queue) -> browser playback
```

The Speech Activity Detector is authoritative for speech start and end candidates; the Turn Manager is authoritative for turn open/close, endpoint commitment, and interruption acceptance. Provider STT speech/endpoint signals are advisory only.

## 3. Initial and future pipelines

### Initial sandbox

```text
Browser -> LiveKit -> STT -> General LLM -> TTS -> LiveKit -> Browser
```

### Knowledge-based version

```text
Browser -> Transport -> STT -> Knowledge retrieval -> LLM -> TTS -> Browser
```

### Phone version

```text
Phone/SIP -> Transport -> STT -> Conversation engine -> TTS -> Phone/SIP
```

The conversation engine changes between milestones. The browser, session, provider, logging, cost, and evaluation contracts should remain stable.

## 4. Modules

### 4.1 Web client

Responsibilities:

- request microphone permission;
- join and leave a voice session;
- publish microphone audio;
- play agent audio;
- display live and final transcripts;
- show the session connection state separately from the normalized agent activity states defined in `01-system-contracts.md` §6: `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `interrupted`, `recovering`, and `error`;
- expose mute, stop, and interrupt controls;
- collect simple user feedback.

The browser must never receive secret provider API keys.

There is no separate frontend design document. The browser client is specified by `01-system-contracts.md` §20 (browser event contract), `06-livekit-transport-adapter.md` (transport and data messages), `13-dependency-and-version-matrix.md` §10–11 (frontend dependencies and project/lock contract), and `14-phase0-implementation-execution-plan.md` §4 (repository layout).

### 4.2 Session API and token service

Responsibilities:

- enforce the approved local loopback/trusted boundary and assign the fixed internal-tester context;
- create a voice session;
- issue a short-lived transport token;
- return the public session configuration;
- close or revoke a session;
- enforce rate and concurrency limits.

Initial implementation direction: FastAPI with shared Pydantic request, response, and event models.

### 4.3 Realtime transport adapter

Responsibilities:

- connect users and agents;
- carry realtime audio and data events;
- report connection-quality events;
- support reconnect and disconnect;
- support browser now and SIP later.

First adapter: LiveKit.

### 4.4 Agent/session orchestrator

Responsibilities:

- own the voice-session lifecycle;
- connect transport, STT, conversation engine, and TTS;
- maintain the current conversation state;
- cancel work after interruptions or disconnects;
- attach correlation IDs to every operation;
- produce normalized events for storage and monitoring.

Initial implementation direction: LiveKit Agents SDK for Python. Provider-independent conversation logic must remain in a separate Python package so that LiveKit can be replaced later.

### 4.5 Turn manager

Responsibilities:

- consume speech start and end candidates from the worker-local Speech Activity Detector (Silero VAD, Decision 042), which is authoritative for speech activity; the Turn Manager does not detect speech itself;
- commit turns: decide turn open/close and endpoint commitment (700 ms from the last speech frame, capped at 1,000 ms without re-approval) and accept interruptions;
- determine when a user turn is complete;
- prevent overlapping agent responses;
- stop playback when the user interrupts;
- classify false interruptions;
- apply silence and maximum-turn timeouts.

### 4.6 STT provider interface

Responsibilities:

- accept normalized audio frames;
- emit partial and final transcripts;
- emit timestamps, language, and confidence when available;
- expose provider usage information;
- support cancellation and reconnect.

First implementation: one selected STT provider. Additional adapters will be added only after the end-to-end baseline works.

### 4.7 Conversation engine interface

Responsibilities:

- accept conversation messages and session context;
- stream response text;
- support cancellation;
- expose input/output token usage;
- expose tool calls through a normalized contract.

Implementations:

1. General chat LLM adapter for the sandbox.
2. Knowledge graph engine for the product version.
3. Optional alternative LLM adapters for benchmarking.

### 4.8 TTS provider interface

Responsibilities:

- accept response text incrementally;
- generate streaming audio;
- expose the first-audio event and usage units;
- support immediate cancellation;
- keep voice, language, pace, and format configuration separate from application logic.

### 4.9 Response segmenter

Responsibilities:

- convert streamed LLM tokens into speakable segments;
- send the first complete phrase to TTS without waiting for the entire answer;
- avoid breaking numbers, product names, URLs, and abbreviations incorrectly;
- prevent cancelled segments from being played.

### 4.10 Provider registry and configuration

Responsibilities:

- map configuration to transport, STT, LLM, and TTS adapters;
- validate required credentials at startup;
- keep model names, language settings, and timeouts outside source code;
- record the exact provider configuration used by each session.

### 4.11 Event and latency collector

The canonical event vocabulary is `01-system-contracts.md` §9, which is the single source of event names and durable categories. The minimum required timeline uses these canonical names:

- `session.created`, `transport.connected`, `transport.disconnected`, `session.ended`, `session.failed`;
- `user.speech_started`, `user.speech_ended`, `stt.partial` (transient only), `stt.final`, `stt.turn_finalized`;
- `conversation.started`, `conversation.first_token`, `conversation.segment_ready`, `conversation.completed`;
- `tts.segment_started`, `tts.first_audio`, `playback.started`, `playback.completed`;
- `turn.interruption_detected`, `turn.interrupted`, `turn.false_interruption_suppressed`, `turn.completed`;
- provider failures through the component `*.failed` events and `error.*` events.

Every event must include `session_id`, `turn_id`, timestamp, provider, model, and correlation ID when applicable.

### 4.12 Cost calculator

Responsibilities:

- receive normalized usage records;
- apply dated provider rates;
- calculate STT, LLM, TTS, orchestration, storage, and later telephony costs;
- calculate per-turn and per-session cost;
- preserve the original billing unit and rate used in the calculation;
- support recalculation when a provider rate changes.

### 4.13 Evaluation harness

Responsibilities:

- replay the same audio through different STT providers;
- replay the same messages through different LLMs;
- synthesize the same text with different TTS providers;
- run selected end-to-end stack combinations;
- store raw results and human scores;
- produce accuracy, latency, reliability, and cost comparisons.

### 4.14 Knowledge engine (later)

Responsibilities:

- call the existing knowledge graph or chatbot API;
- retrieve approved product context;
- return answers with citations;
- handle unsupported questions safely;
- expose retrieval and generation timings separately.

### 4.15 Telephony adapter (later)

Responsibilities:

- connect inbound and outbound SIP calls;
- capture consent and recording state;
- transfer to a human;
- handle DTMF, voicemail, hang-up, and call outcomes;
- record carrier and platform costs separately.

## 5. Data storage strategy

Use different storage systems for different workloads.

### MongoDB Atlas (approved primary durable database)

Use for durable application data:

- agent configurations;
- sessions and turns;
- provider usage and cost records;
- feedback;
- evaluation cases and results;
- configuration versions.

Collections will use a controlled combination of references and small embedded summaries. Unbounded turns, events, and provider operations must not be embedded inside one session document.

### Realtime cache (not selected)

Proposed use only for temporary realtime state:

- active session state;
- distributed locks;
- cancellation flags;
- short-lived conversation cache;
- rate-limit counters;
- job queues if required.

The realtime cache must not be the only durable store for transcripts or billing records.

### Object storage (not selected)

Proposed use only, if later approved, for large immutable objects (Phase 0 has no object storage and no benchmark assets; Decision 063):

- user audio samples;
- agent audio;
- call recordings;
- benchmark datasets;
- exported reports.

If object storage is later approved, store object references in MongoDB Atlas. Do not store large audio blobs directly in ordinary MongoDB documents.

## 6. Initial MongoDB collection structure

The following is the approved core R&D collection design. MongoDB Atlas Flex, AWS Mumbai, PyMongo Async, the nine core document shapes, launch indexes, 30-day R&D retention, no-backup R&D policy, validation architecture, and baseline limits are approved. Five additional evaluation collection contracts are approved separately in Decision 040 for the benchmarking milestone. Provider-specific option schemas, future binary/object storage, authentication beyond the local/trusted boundary, and production settings remain outside the approved Phase 0 core scope. The default database name is `voice_agent_rnd`, configurable through `MONGODB_DATABASE` (`12-configuration-and-secrets.md`).

Detailed design: `02-database-design.md` (collection fields, validators, indexes, expiry, and cleanup).

### `agent_configs`

Purpose: versioned runtime configuration for a voice agent.

Approved field groups:

- identity and version: `_id`, `agent_config_id`, `agent_id`, `name`, optional `description`, `version`, `status`, `schema_version`;
- environment and integrity: `environment`, optional `tags`, `config_checksum`;
- embedded adapter configuration: `transport`, `stt`, `conversation_engine`, and `tts`;
- conversation behaviour: `turn_handling`, `timeout_policy`, and `retry_policy`;
- costing: `cost_rate_card_version` and `cost_currency`;
- lifecycle and audit: `created_at`, optional `created_by`, activation/retirement fields, and `change_note` for versions after the first.

The embedded adapter sections preserve exact provider/model IDs, adapter versions, safe provider options, and a `credential_ref`. Raw API secrets are never stored in this collection. Active configurations are immutable in meaning: a material change creates a new version, and each session references the exact version it used. Baseline numeric timeout/retry defaults were approved later in Decision 026.

### `voice_sessions`

Purpose: one browser or phone conversation.

Approved field groups:

- identity, idempotency, and reproducibility: `_id`, `session_id`, unique create `client_request_id`, `correlation_id`, `agent_id`, exact `agent_config_id`, `agent_config_version`, `config_checksum`, `environment`, `channel`, `session_mode`, and `schema_version`;
- safe initiator identity: `initiator_type` and optional `initiator_id`;
- state: normalized session `status`, optional current `agent_activity_state`, `state_revision`, optional bounded `worker_assignment` with fenced `writer_epoch`, optional `ended_by`, `disconnect_reason`, and `terminal_error_id`;
- lifecycle timestamps: creation, connection, activation, last activity, ending, completion, update, and final duration;
- bounded embedded summaries: transport, provider snapshot, language, recording/privacy, turns, errors, latency, usage, and cost;
- optional sanitized client context without raw user-agent, IP address, token, or device fingerprint;
- bounded join-token request evidence containing request IDs and fingerprints, never the issued token.

The session document remains a quick operational summary. Complete turns, events, provider attempts, errors, costs, and consent evidence remain separate referenced records. Phase 0 has no benchmark-asset collection and ordinary R&D audio recording remains off.

### `conversation_turns`

Purpose: one user turn and its corresponding agent reply.

Approved field groups:

- identity and ordering: `_id`, `turn_id`, `session_id`, `sequence_number`, `correlation_id`, `agent_config_id`, `environment`, and `schema_version`;
- state: normalized turn `status`, `status_revision`, `input_disposition`, `response_completion_status`, optional failure/abandonment details, and response finish reason;
- bounded `user_input`, `agent_response`, `route_summary`, and `interruption_summary` objects;
- lifecycle timestamps and normalized per-turn latency, usage, and cost summaries;
- transcript-policy version and redaction status; Phase 0 has no benchmark-asset references.

Final user transcript, complete generated text, text sent to TTS, and the confirmed or estimated text actually played are stored separately. Partial transcripts and raw audio are not stored in ordinary turn documents.

### `provider_operations`

Purpose: one STT, LLM, TTS, transport, retrieval, or telephony operation.

Approved field groups:

- identity and relationships: `_id`, `operation_id`, `logical_request_id`, `session_id`, optional `turn_id`, `correlation_id`, optional parent/previous-attempt references, `attempt_number`, `agent_config_id`, `environment`, and `schema_version`;
- normalized classification, exact provider/model/adapter identity, operation state, result disposition, and lifecycle timing;
- bounded safe request/result summaries, normalized usage items, operation-level cost summary, retry/cancellation/fallback state, and failure reference;
- optional provider metadata validated through a bounded adapter-specific allowlist.

Every provider attempt, including each retry, is a separate document. Complete prompts, transcripts, generated responses, audio, headers, credentials, raw provider payloads, stack traces, and detailed billing calculations are not stored here.

### `session_events`

Purpose: append-only normalized timeline used for debugging and latency analysis.

Approved field groups:

- identity and ordering: `_id`, `event_id`, `session_id`, optional turn/operation references, `correlation_id`, `sequence_number`, optional causation/correction references, and envelope/payload schema versions;
- normalized event type, category, severity, visibility, occurrence/recording timestamps, optional late-arrival metadata, and producer context;
- optional bounded provider context, state transition, safe payload, normalized measurements, and related-record references;
- environment, retention class, optional expiry, and redaction status.

The collection stores a durable, append-only subset of important runtime events. Audio chunks, TTS frames, LLM text segments/tokens, STT partial transcripts, microphone levels, and high-frequency network metrics are not persisted as individual MongoDB events. A separate retention policy or analytics destination may be used when volume grows.

### `cost_entries`

Purpose: auditable cost calculation for every billable component.

Approved field groups:

- identity and calculation lineage: `_id`, `cost_entry_id`, `calculation_run_id`, calculation version, optional superseded run, session/turn/operation/logical-request references, correlation/configuration IDs, and schema version;
- component, category, scope, exact provider/SKU identity, native and billable quantities, rate and rate-source evidence;
- gross, discount, credit, tax, net-original-currency, FX conversion, normalized USD reporting amount, and explicit rounding metadata;
- evidence/calculation/reconciliation state, allocation and aggregation behaviour, calculation-engine metadata, environment, and visibility.

Cost entries are immutable calculation lines grouped into versioned runs. Original provider quantities, billing units, currencies, rate evidence, and FX assumptions are preserved. Python `Decimal` and MongoDB `Decimal128`, never binary floating point, are used for monetary calculations.

### `user_feedback`

Purpose: simple quality feedback tied to a session or turn.

Approved field groups:

- identity, idempotent client submission, session/turn/operation/configuration references, optional superseded feedback, and schema version;
- validated target type and bounded feedback aspects;
- safe submitter type/ID/role/source without copied direct PII;
- optional thumb, overall rating, dimension scores, versioned structured reason codes, corrections, and bounded comment;
- backend-derived provider context, controlled review workflow, lifecycle/environment/evaluation references, and privacy/retention state.

Original tester feedback is immutable. A correction creates a new record referencing the previous feedback; only the controlled review section may be updated. Audio, screenshots, files, HTML, scripts, and arbitrary provider/configuration claims are not stored in this document.

### `error_events`

Purpose: searchable application and provider failures.

Approved field groups:

- identity, session/turn/operation/logical-request/event relationships, correlation and causal-chain references, sanitized fingerprint, and schema version;
- versioned normalized error type/category/severity, component/origin/failure phase, and bounded provider context;
- expected-cancellation/failure-rate flags, retry/fallback details, user/session/turn/audio/cost impact, and controlled resolution lifecycle;
- stable diagnostic code, safe message/details, optional restricted-log reference, bounded state snapshot, browser-safe user message, timestamps, environment, redaction, and retention class.

Each actual occurrence remains a separate record even when fingerprints match. Original classification and diagnostic evidence are immutable; only the versioned resolution section may be updated. Raw provider payloads, credentials, prompts, transcripts, customer data, connection strings, sensitive paths, and stack traces belong outside this general collection.

### `consent_records`

Purpose: immutable proof of granular recording, retention, review, benchmarking, reuse, or training decisions.

Approved field groups:

- identity and consent chain: `_id`, `consent_record_id`, `consent_chain_id`, idempotent client submission, session/subject/receipt references, optional superseded decision, correlation ID, and schema version;
- one explicit scope, compatible bounded data categories, decision/effective period, and versioned purpose;
- exact notice snapshot/hash/policy versions and explicit affirmative-action evidence;
- optional recording authorization, bounded retention authorization, revocation evidence, and controlled deletion/fulfilment workflow;
- lifecycle/audit/checksum fields plus restricted visibility, redaction, retention class, and optional expiry.

Each record represents one scope and one immutable decision. Revocation or expiry appends a new decision in the same chain. Only the revision-controlled operational fulfilment section may change. If consent validation or storage is unavailable, recording remains off while the ordinary non-recorded voice session may continue.

## 7. Evaluation collections

Create their validators, indexes, and repositories in persistence work package 5; use them through the evaluation runner in work package 12:

- `evaluation_datasets`
- `evaluation_cases`
- `evaluation_runs`
- `evaluation_results`
- `evaluation_human_ratings`

The original test input must be immutable. Each run should reference the exact agent and provider configuration version.

## 8. Entity relationships

```text
agent_configs
    |
    +---- voice_sessions
              |
              +---- conversation_turns
              +---- provider_operations  (required session_id; optional turn_id -> conversation_turns)
              +---- user_feedback        (required session_id; optional turn_id / operation_id)
              +---- session_events       (optional turn/operation references)
              +---- cost_entries         (optional turn/operation references)
              +---- error_events         (optional turn/operation references)
              +---- consent_records

evaluation_datasets
    |
    +---- evaluation_cases
    +---- evaluation_runs ---- agent_configs
              |
              +---- evaluation_results ---- optional session/turn/operation/cost evidence
                            |
                            +---- evaluation_human_ratings
```

Every session-scoped record belongs to exactly one `voice_sessions` document; a turn link is optional because some provider operations and feedback target the session rather than a specific turn. The worker media flow is shown in §2.

## 9. Implementation order

Milestone ↔ work-package crosswalk: see `14-phase0-implementation-execution-plan.md` §5A. Work packages in doc 14 govern execution order.

### Milestone 0: Repository and dependency skeleton

Deliverables:

- repository structure;
- environment/configuration contract;
- exact dependency candidates and compatibility gate;
- backend/frontend skeletons;
- local development instructions.

Exit condition: modules compile/start with mock adapters and no provider credentials.

### Milestone 1: Contracts and mock vertical slice

Deliverables:

- canonical domain/state/event contracts;
- transport, speech-activity, STT, conversation, TTS, repository, and costing ports;
- deterministic mock adapters;
- cancellation generations and bounded queues.

Exit condition: one mock voice turn finalizes without importing provider SDK types into core modules.

### Milestone 2: Configuration and control API

Deliverables:

- strict settings/secrets boundary;
- immutable agent-configuration validation;
- FastAPI lifecycle and versioned routes;
- safe session creation/end contracts.

Exit condition: configuration fails safely, API contracts validate, and no secret reaches browser output.

### Milestone 3: MongoDB persistence foundation

Deliverables:

- validators, repositories, approved indexes, and migrations;
- session/turn/operation/event/error/cost records;
- terminal anchors and `expires_at` propagation;
- session reconciler queries and child-first retention workflow.

Exit condition: accepted transcripts and lifecycle evidence can be persisted before any real LLM call.

### Milestone 4: Browser and LiveKit connection

Deliverables:

- browser joins a room;
- microphone audio reaches the worker;
- worker publishes test audio;
- end-control signalling, reconnect, and cleanup evidence.

Exit condition: stable two-way browser audio and control signalling work without AI providers.

### Milestone 5: Local speech activity and STT

Deliverables:

- local Silero speech start/stop;
- one streaming STT adapter;
- partial/final transcripts and transcript UI;
- endpoint, timing, usage, and error evidence.

Exit condition: Hindi, Hinglish, and English speech produces usable durable final transcripts with application-owned endpoints.

### Milestone 6: General LLM

Deliverables:

- one general LLM adapter;
- streaming responses and per-segment validation;
- short voice-friendly system prompt;
- token/latency capture and cancellation;
- incomplete-tail protection at the output cap.

Exit condition: one persisted final transcript produces validated streamed text reliably.

### Milestone 7: TTS

Deliverables:

- one streaming TTS adapter;
- response segmentation and browser playback;
- first-audio/completion timings;
- cancellation and delivered-text evidence.

Exit condition: one complete validated text response becomes audible without stale or partial-tail audio.

### Milestone 8: Natural turn-taking and recovery

Deliverables:

- interruption handling and sub-250 ms false-candidate suppression;
- endpoint/session timeouts and duplicate-response prevention;
- persistent session-end request and worker-crash reconciliation;
- one bounded higher-generation recovery attempt.

Exit condition: users can speak naturally, interrupt safely, and cannot leave sessions stuck after worker loss.

### Milestone 9: Cost and observability

Deliverables:

- normalized timeline and latency derivation;
- per-attempt/turn/session cost;
- retention/reconciler operational evidence;
- bounded diagnostic report.

Exit condition: a failed, stuck, or expensive conversation can be explained from stored evidence.

### Milestone 10: Evaluation and provider benchmarking

Deliverables:

- approved 100-case evaluation harness;
- insert-only execution attempts and human ratings;
- component/end-to-end comparisons;
- documented baseline recommendation.

Exit condition: provider decisions use measured quality, latency, reliability, and cost.

### Later milestones

Knowledge graph integration and telephony remain later milestones after the Phase 0 browser baseline passes.

## 10. Canonical Phase 0 repository structure

The approved root layout is `backend/`, `frontend/`, `docs/`, and bounded sanitized `outputs/`. The Python backend is the modular monolith defined in `03-backend-module-design.md`; the FastAPI control API and LiveKit worker remain separate processes within it. The exact tree, file-creation order, tests, and stop/go gates are defined in `14-phase0-implementation-execution-plan.md`.

## 11. Initial quality gates

- Successful end-to-end turns: at least 95% in the approved controlled dataset.
- Speech-end-to-first-audible-response P50: at most 2.0 seconds.
- Speech-end-to-first-audible-response P95: at most 4.0 seconds.
- Interruption-to-silence P95: at most 500 ms.
- Interruption latency gate requires at least 20 valid samples and a hard maximum of 1,000 ms.
- Every provider request has a session and turn correlation ID.
- Every completed session has a component-level cost record.
- No secret API key is exposed to the browser.
- Cancelled turns do not continue producing audible output.

These are initial targets and may be revised after the first measured baseline.

## 12. Security and privacy baseline

- Use short-lived browser transport tokens.
- Keep Phase 0 provider credentials in the approved protected external secret file/environment injection path; a production secret manager remains a later deployment decision.
- Make recording opt-in and store explicit consent.
- Define transcript and audio retention separately.
- Redact sensitive fields before general logging.
- Encrypt data in transit and at rest.
- Restrict recording and transcript access by role.
- Record configuration versions for auditability.
- Define deletion workflows before external pilots.

## 13. Phase 0 implementation readiness

The architecture choices that block the local, single-user browser baseline are resolved:

1. backend: Python with FastAPI and a separate LiveKit worker;
2. frontend: a separate React, TypeScript, and Vite application;
3. realtime transport: LiveKit behind an application-owned adapter;
4. baseline STT: Deepgram Nova-3 Multilingual;
5. baseline conversation model: OpenAI GPT-6 Luna with `reasoning.effort = none`;
6. baseline TTS: Sarvam Bulbul v3 using voice `priya`;
7. database: MongoDB Atlas Flex on AWS Mumbai through PyMongo Async;
8. R&D data policy: 30-day retention, ordinary audio recording off, and no backup;
9. scope: one local/trusted R&D user; no public deployment or application login;
10. approved contracts: control API, worker orchestration, provider adapters, prompt/language policy, evaluation gates, secrets boundary, and candidate dependency matrix.

The implementation execution plan, dated cost rate card, evaluation collection contracts, and exact 100-case catalog are approved. The provider-independent runner still requires implementation and verification before provider benchmarking. Production authentication, deployment, storage, retention, backup, knowledge integration, and telephony remain later decisions.

## 14. Documentation workflow

We will not write the full implementation immediately.

For each numbered document:

1. discuss the scope and decisions;
2. update the document with the approved design;
3. define acceptance criteria;
4. only then implement that module;
5. record measured results and any design changes.

The next approved step is implementation in the work-package order defined by the Phase 0 execution plan. Current provider pricing must be refreshed before paid API tests.

## 15. Decision log

### Decision 001: Python backend

Status: Approved

Decision:

- use Python for the realtime agent worker;
- use FastAPI for the session, token, configuration, and reporting API;
- use Pydantic models for internal contracts;
- use MongoDB Atlas as the primary durable database through the approved PyMongo Async driver, Pydantic validation, and explicit repository implementation;
- keep the browser application independent of Python implementation details;
- keep the existing knowledge graph behind the conversation-engine interface until its integration milestone.

Reason:

The existing knowledge graph is implemented in Python. Using Python for the agent and control API reduces integration overhead and avoids introducing a second backend language before the voice pipeline is validated.

### Decision 002: Separate browser R&D UI

Status: Approved

Decision:

- build a separate browser application for the first voice sandbox;
- do not modify the existing product frontend during the R&D milestone;
- keep the UI focused on testing voice quality, latency, interruptions, errors, and provider configurations;
- connect the browser only to the FastAPI session API and realtime transport;
- never expose STT, LLM, TTS, database, or other server credentials to the browser;
- integrate the selected design into the product frontend only after the voice pipeline passes its quality gates.

Initial UI areas:

- session start and stop controls;
- microphone permission and mute controls;
- connection and agent-state indicators;
- partial and final user transcripts;
- agent response text;
- interruption control;
- per-turn latency and cost panel;
- provider/model labels for the active configuration;
- user feedback and error details suitable for internal testing.

Reason:

A separate UI isolates voice R&D from the existing product, allows faster experimentation, and provides space for technical diagnostics that should not appear in the final customer interface.

### Decision 003: Browser R&D UI stack

Status: Approved

Decision:

- use React for the browser UI;
- use TypeScript for frontend code and contracts;
- use Vite for local development and builds;
- keep the application standalone during R&D;
- avoid adding Next.js until a product requirement needs server-side rendering or its application framework features.

Reason:

React, TypeScript, and Vite provide a small and fast test harness for realtime audio, session diagnostics, and provider benchmarking without introducing unrelated application complexity.

### Decision 004: Audio retention during R&D

Status: Approved

Decision:

- do not record or retain user or agent audio during ordinary R&D sessions by default;
- retain normalized transcripts, events, latency, usage, cost, feedback, and safe error records according to the later-approved retention policy;
- if benchmark audio storage is later approved, store benchmark audio only when the tester has given explicit recording consent (Phase 0 has no benchmark assets; Decision 063);
- if later approved, associate stored benchmark audio with the consent record, dataset, and retention expiry;
- make recording state clearly visible in the browser UI;
- do not enable recording through a hidden configuration change.

Reason:

The voice pipeline can be measured without retaining every conversation. Explicitly consented benchmark recordings could later be useful for repeatable provider comparisons, but they require a fresh storage/database approval.

### Decision 005: Explicit confirmation before design decisions

Status: Approved

Decision:

- proposals may be presented with tradeoffs and a recommendation;
- no technology, provider, database, hosting model, storage product, schema choice, or implementation approach is considered final without explicit user confirmation;
- unresolved proposals must be labelled `Pending approval` in documentation;
- implementation for a module starts only after its material decisions and acceptance criteria are confirmed;
- if a new choice changes an approved decision, discuss the effect and obtain confirmation before updating the architecture.

Current implication:

MongoDB Atlas is the approved primary durable database. A realtime cache and object-storage product have not been selected.

### Decision 006: MongoDB Atlas primary database

Status: Approved

Amendment: Decisions 021, 040, 046, and 063 supply the final retention, evaluation-collection, expiry, and schema-normalization details; future object storage remains unapproved.

Amendment: the items deferred in the last bullet are settled — Atlas tier by Decision 008, cloud provider and region by Decision 009, Python driver by Decision 010, indexes by Decision 020, retention by Decision 021, backup policy by Decision 022, and validation approach by Decision 025.

Decision:

- use MongoDB Atlas as the primary durable database;
- store agent configurations, sessions, turns, provider operations, normalized events, costs, feedback, errors, consent records, and evaluation records in Atlas collections;
- keep large audio objects outside ordinary MongoDB documents;
- avoid unbounded embedded arrays for turns, events, operations, and costs;
- use references between session-level and high-volume child collections;
- select the Atlas tier, cloud provider, region, Python driver, validation approach, indexes, backup policy, and retention periods only after separate discussion and confirmation.

Reason:

The document model fits variable provider metadata and evolving R&D records while Atlas supplies a managed database environment. Controlled references preserve queryability and prevent session documents from growing without bound.

Detailed design: `docs/02-database-design.md`.

### Decision 007: MongoDB core collections and references

Status: Approved

Amendment: Decision 040 approves five evaluation collections in addition to the nine core collections. Decision 063 confirms that `benchmark_assets` is not a Phase 0 collection.

Amendment: the "only when the benchmarking milestone begins" condition in the fifth bullet is superseded by Decision 054 — the evaluation collections' validators, indexes, and repositories are created in work package 5; the evaluation runner uses them in work package 12.

Decision:

- use these core collections for the R&D system: `agent_configs`, `voice_sessions`, `conversation_turns`, `provider_operations`, `session_events`, `cost_entries`, `user_feedback`, `error_events`, and `consent_records`;
- keep the session document as a bounded summary;
- keep turns, provider operations, events, detailed cost entries, feedback, errors, and consent records in separate collections referenced by application IDs;
- do not embed unbounded arrays in session or turn documents;
- add benchmark and evaluation collections only when the benchmarking milestone begins;
- approve exact document fields, validators, indexes, and retention separately.

Reason:

The design keeps common session reads simple while allowing high-volume records to grow and be queried independently without risking oversized session documents.

Detailed design: `docs/02-database-design.md`.

### Decision 008: MongoDB Atlas Flex for shared R&D

Status: Approved

Decision:

- use MongoDB Atlas Flex for the shared R&D environment;
- treat the public Flex price range as an operating-cost estimate until cloud provider, region, and measured usage are known;
- do not treat Flex as the automatically approved production tier;
- review database performance, storage, operations, reliability, and cost before the internal pilot or production deployment;
- choose any production tier only after a separate discussion and explicit confirmation.

Reason:

Flex provides more practical storage and shared R&D capacity than the free tier without committing the project to a dedicated production cluster before workload measurements exist.

Detailed design: `docs/02-database-design.md`.

### Decision 009: R&D Atlas cloud and region

Status: Approved for R&D; production review required

Decision:

- use AWS as the MongoDB Atlas cloud provider for the shared R&D Flex cluster;
- use AWS Mumbai region `ap-south-1`, Atlas region code `AP_SOUTH_1`;
- treat this as the R&D location, not an automatic production-region approval;
- review the application-server location, users, data-residency requirements, cost, and network latency before production deployment;
- if the production provider or region changes, use an explicitly planned and verified migration rather than assuming an in-place setting change;
- do not provision the Atlas cluster until implementation/provisioning is separately authorized.

Reason:

Mumbai is the recommended default for India-based R&D when no existing cloud dependency has been selected. It keeps the database geographically close to the current target users while preserving a later production review.

Detailed design: `docs/02-database-design.md`.

### Decision 010: Python MongoDB integration

Status: Approved

Decision:

- use the official PyMongo Async API;
- use Pydantic for domain, persistence-boundary, and API validation models;
- use explicit repository interfaces with MongoDB-specific repository implementations;
- do not introduce an ODM in the initial implementation;
- do not use Motor for the new project;
- create one long-lived async MongoDB client per process/event loop and close it during application shutdown;
- keep domain models, MongoDB documents, and public API models separate;
- do not expose MongoDB `_id`, provider secrets, or unrestricted persistence documents directly to the browser;
- keep connection strings and credentials outside source code and database documents.

Initial repository boundaries:

- agent configuration;
- voice session;
- conversation turn;
- provider operation;
- session event;
- cost entry;
- user feedback;
- error event;
- consent record.

Reason:

PyMongo Async is the official asynchronous Python path for MongoDB and fits the FastAPI and realtime-agent architecture. Explicit repositories preserve control over cost, event, and audit writes while preventing MongoDB access from spreading through conversation logic.

Detailed design: `docs/02-database-design.md`.

### Decision 011: `agent_configs` field structure

Status: Approved

Decision:

- use separate `agent_config_id` and `agent_id` values so a logical agent can have multiple immutable configuration versions;
- store identity, environment, integrity, costing, and lifecycle fields at the document root;
- embed bounded `transport`, `stt`, `conversation_engine`, `tts`, `turn_handling`, `timeout_policy`, and `retry_policy` objects;
- record exact provider/model identifiers and adapter versions rather than display names alone;
- preserve the system instruction and its version, and preserve an optional tool-set version;
- use `credential_ref` values and never store API keys or raw credentials in MongoDB;
- validate `safe_options` through provider-specific allowlists before persistence;
- require a new version for material changes to an active configuration;
- make each session reference the exact configuration version used;
- require `change_note` after version 1;
- use the numeric timeout/retry defaults approved later in Decision 026 and approve provider-specific option schemas per adapter before implementation.

Reason:

This structure makes every test run reproducible and allows STT, LLM, TTS, and transport adapters to be exchanged without losing the exact configuration that produced a result.

Detailed design: `docs/02-database-design.md`.

### Decision 012: `voice_sessions` field structure

Status: Approved

Decision:

- keep `voice_sessions` as a bounded, mutable summary document rather than an event or transcript container;
- require unique `client_request_id` for idempotent session creation as approved later in Decision 028;
- identify the exact logical agent, immutable configuration version, checksum, environment, channel, and session mode used;
- use the normalized session and activity states defined in the system contracts;
- use a monotonically changing `state_revision` to guard concurrent session-state updates;
- preserve normalized termination ownership/reason and an optional reference to the terminal error;
- keep transport identifiers and exact runtime provider/model/adapter details in bounded snapshots, without access tokens or credentials;
- retain only bounded language, turn, error, latency, usage, and cost summaries in the session document;
- represent precise monetary totals with MongoDB `Decimal128` and keep detailed cost evidence in `cost_entries`;
- omit unavailable optional usage values and preserve explicit availability status rather than converting them to zero;
- keep ordinary R&D recording off and allow benchmark recording only with explicit consent referenced through `consent_record_id`;
- store only sanitized client context and exclude raw user-agent strings, IP addresses, access tokens, unnecessary device fingerprints, and direct PII;
- keep complete turns, events, provider operations, errors, costs, and consent records in their dedicated collections; no benchmark-asset collection exists in Phase 0.

Reason:

This structure supports fast session-list and session-summary screens while preserving reproducibility, privacy, and a clean path from browser R&D to benchmarking and future phone sessions.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §5–6 (session and agent activity states).

### Decision 013: `conversation_turns` field structure

Status: Approved

Amendment: Decisions 046 and 063 add executable expiry, environment, input-disposition, branching transition, and truncation fields and remove the earlier object-storage assumption.

Amendment: Decision 067 adds terminal turn status `discarded` (empty/noise transcript with no clarification) and initial `input_disposition = pending`.

Decision:

- represent one user utterance and its corresponding agent response with one ordered turn document;
- require a unique `turn_id` and a unique `session_id` plus `sequence_number` pair;
- use the normalized turn-state contract and treat `completed`, `interrupted`, `failed`, and `abandoned` as terminal;
- persist only the final accepted user transcript, not streaming partial transcripts;
- preserve `generated_text`, `synthesized_text`, and `spoken_text` separately because interruptions can make them differ;
- label spoken-text evidence as `confirmed`, `estimated`, or `unavailable`;
- store bounded route information so general-LLM, future knowledge-engine, retrieval, tool, and fallback paths can be distinguished;
- record accepted interruptions and suppressed sub-250 ms false candidates without embedding a full event stream;
- preserve normalized per-turn latency, usage, and cost summaries while retaining provider operations and cost entries as authoritative evidence;
- use MongoDB `Decimal128` for monetary summaries and omit unavailable optional metrics rather than writing zero;
- store final transcripts and agent text for R&D accuracy analysis under a versioned content policy, with redaction status and the approved 30-day R&D retention policy;
- do not store ordinary-session raw audio; Phase 0 has no object-storage or benchmark-asset schema, and any later consent-controlled reusable audio requires a fresh storage/database approval;
- do not embed provider credentials, unrestricted provider payloads, full event timelines, retrieved knowledge documents, or unbounded debug logs.

Reason:

The design preserves what the user said, what the agent generated, what was synthesized, and what was actually delivered. That separation is essential for measuring recognition quality, response quality, latency, interruption behaviour, and cost fairly across providers.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §7 (turn states).

### Decision 014: `provider_operations` field structure

Status: Approved

Decision:

- create one operation document for every external/provider attempt, including failed, timed-out, cancelled, fallback, and retry attempts;
- use a unique `operation_id` per attempt and a shared `logical_request_id` to group attempts for one logical request;
- retain the same session and turn IDs across retries, and use `attempt_number`, `previous_attempt_operation_id`, and optional dependency/fallback references to preserve lineage;
- classify operations through normalized component and operation-type values while preserving exact provider, model, adapter, API-version, region, voice, and provider-request identifiers when applicable;
- use normalized lifecycle states and explicitly classify late/superseded results so cancelled output can never reach the user;
- persist UTC lifecycle timestamps and normalized latency summaries calculated with monotonic runtime clocks;
- store only bounded safe request/result measurements, never complete prompts, transcripts, audio, unrestricted payloads, headers, or credentials;
- represent usage as a bounded list of normalized unit/quantity items with reporting source and estimation status;
- calculate cost for billed failed or cancelled attempts as well as successful attempts, while keeping detailed rate evidence in `cost_entries`;
- record bounded retry, cancellation-generation, error, and fallback summaries, with detailed errors in `error_events`;
- prohibit automatic retries after cancellation and for non-idempotent tool actions;
- allow bounded provider metadata only through adapter-specific allowlists approved separately.

Reason:

Attempt-level evidence prevents retries, fallbacks, cancellations, and provider failures from disappearing inside session totals. It enables fair reliability, latency, usage, and cost comparisons across providers.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §12–16 (provider contracts, cancellation, retry).

### Decision 015: `session_events` field structure

Status: Approved

Decision:

- keep `session_events` as an immutable, append-only, normalized timeline of important durable events;
- separate the high-volume realtime event stream from the smaller durable event set;
- do not persist individual audio chunks, TTS frames, LLM tokens/text segments, STT partial transcripts, microphone levels, or high-frequency network samples;
- require a unique `event_id` for deduplication and a unique `session_id` plus `sequence_number` pair for authoritative session-local order;
- preserve occurrence and recording timestamps, optional producer ordering, and late-arrival metadata without relying on timestamps alone for order;
- classify events by normalized type, category, severity, and visibility, allowing only `browser_safe` reduced events to reach the browser;
- store bounded producer/provider context, state transitions, safe payloads, normalized measurements, and dedicated-record references when applicable;
- prohibit raw audio, full prompts/transcripts/responses, credentials, tokens, headers, unrestricted provider payloads, and stack traces in event payloads;
- correct an event only by appending a new event with `supersedes_event_id`, never by silently editing history;
- attach retention class, expiry, and redaction state; the later approved R&D retention is 30 days;
- retry persistence of terminal session/turn events reliably while preventing optional diagnostic persistence from unnecessarily blocking voice playback.

Reason:

This design provides a trustworthy debugging and measurement timeline without turning MongoDB into a high-frequency media or telemetry stream. Content, provider attempts, costs, errors, and consent remain authoritative in their dedicated collections.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §8–10 (event envelope, catalogue, ordering).

### Decision 016: `cost_entries` field structure and reporting currency

Status: Approved

Decision:

- represent every billable meter/tier as a separate auditable cost line;
- group lines into immutable `calculation_run_id` versions and preserve prior runs rather than overwriting them;
- add optional `calculation_run_id` traceability to session, turn, and provider-operation cost summaries;
- preserve original native quantity/unit, converted billable quantity/unit, conversion factor, and conversion method;
- preserve exact provider/model/voice/region/SKU/tier identity, unit rate, rate denominator, pricing basis, effective dates, rate-card version, and pricing-source evidence;
- calculate gross, discount, credit, tax, and net amounts explicitly, never assuming unknown tax is zero;
- preserve original currency and normalize comparisons to USD using an evidenced historical FX rate; allow INR as a display/report conversion;
- use Python `Decimal` and MongoDB `Decimal128` for quantity, rate, FX, and money values;
- preserve provider rounding, billing increment, minimum quantity/charge, and tier rules when known, and label assumptions when unknown;
- distinguish estimates, provider-usage calculations, provider-reported costs, invoice-reconciled values, and manual entries;
- prevent double counting by marking derived turn allocations as `allocation_only` while actual charges remain `charge`;
- calculate cost for failed, timed-out, cancelled, retry, and fallback attempts when billable usage exists; unavailable cost remains unavailable, never zero;
- expose only safe component/total summaries to the browser and keep negotiated rates, discounts, contracts, and invoice evidence restricted.

Reason:

This ledger-style calculation model makes every session and provider comparison reproducible, supports later correction and invoice reconciliation, and prevents retries, currency conversion, tiering, or analytical allocations from silently distorting totals.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §22 (cost usage contract).

### Decision 017: `user_feedback` field structure

Status: Approved

Decision:

- accept feedback for a validated session, turn, or operation target, with all referenced records required to belong to the same session;
- use `client_submission_id` for idempotency and require at least one thumb, score, reason code, correction, or comment;
- support optional overall and dimension-specific 1–5 ratings while keeping unavailable ratings absent rather than zero;
- use a versioned reason-code taxonomy across transcription, response quality, voice quality, latency/interruption, transport, and UI;
- allow corrected transcript, suggested response, and expected-action evidence without automatically promoting feedback into a benchmark dataset;
- derive provider/model/voice/adapter context on the backend rather than trusting arbitrary browser-submitted provider data;
- keep original tester feedback immutable; edits append a new record using `supersedes_feedback_id`;
- permit controlled updates only to the versioned triage/review section;
- avoid copying names, email addresses, or phone numbers into feedback documents;
- apply content-policy, redaction, access, and retention controls to comments and corrections;
- prohibit raw audio, screenshots, file attachments, HTML, and scripts in the feedback document;
- require separate review and applicable consent before feedback becomes a reusable evaluation case.

Reason:

Structured feedback makes provider and configuration problems measurable while preserving free-text and correction evidence for investigation. Idempotency, immutable submissions, controlled review, and privacy fields keep the R&D feedback useful and auditable.

Detailed design: `docs/02-database-design.md`.

### Decision 018: `error_events` field structure

Status: Approved

Decision:

- create one error record per actual occurrence and use `error_id` only for delivery deduplication, never to collapse repeated failures;
- preserve session, optional turn/operation/logical-request/event, correlation, root-cause, and direct-cause relationships;
- group comparable failures through a sanitized fingerprint built without user content, identifiers, or secrets;
- use versioned normalized error type, category, severity, component, origin, and failure-phase fields;
- store only bounded safe provider context and prohibit raw responses, headers, request bodies, and credentials;
- distinguish expected cancellation from genuine failure through `is_expected` and `counts_toward_failure_rate` so interruptions do not distort provider benchmarks;
- preserve retry scheduling/block reasons and fallback attempts/results without hiding the original failure after recovery;
- record user, session, turn, audio, and potential-cost impact separately from recovery status;
- keep original classification/diagnostics immutable while allowing controlled revision of the resolution section;
- expose only a separately designed browser-safe error code/message/action, never internal diagnostics;
- keep full stack traces and sensitive diagnostics in a restricted logging system referenced only by an opaque ID;
- attach UTC lifecycle timestamps, environment, redaction state, retention class, and expiry; the later approved R&D retention is 30 days.

Reason:

This structure supports accurate reliability benchmarking and safe troubleshooting. It separates expected control-flow cancellations, recoverable degradation, user-visible failures, and unrecoverable faults without exposing sensitive diagnostic data.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §17 (error classification).

### Decision 019: `consent_records` field structure

Status: Approved

Decision:

- use one immutable consent record for one explicit scope and decision, grouped through `consent_chain_id`;
- use idempotent client submissions and issue a consent receipt without relying on IP address, raw user-agent, or device fingerprint identity;
- keep recording, retention, human review, benchmark evaluation, dataset reuse, and model-training scopes granular and unbundled;
- keep ordinary R&D recording off by default and do not request or grant model-training permission by default in Phase 0;
- preserve compatible data categories, versioned purpose, exact notice text/hash/language/policy versions, and affirmative-action evidence;
- prohibit preselected consent, consent by silence, and automatic consent from merely starting a voice session;
- authorize recording only after the backend verifies an active, unexpired, unrevoked, matching grant; failure or unavailable consent storage keeps recording off;
- require every retained benchmark asset to reference the exact consent grant, permitted purpose, chain, and retention expiry;
- represent denial, revocation, and expiry as new immutable chain records rather than editing historical grants;
- stop active recording, block new assets, emit an event, and start the applicable deletion workflow after revocation;
- allow controlled revision only of the deletion/fulfilment workflow, preserving the original consent evidence;
- keep consent records restricted and separate from ordinary diagnostic TTLs; the later R&D policy keeps them until related deletion is verified and otherwise for 30 days, while external-use requirements remain separately reviewed.

Reason:

Granular, evidenced, fail-closed consent prevents optional R&D recording from becoming an assumed permission. The decision chain preserves proof while supporting revocation, asset deletion, and future benchmark governance.

Detailed design: `docs/02-database-design.md`.

### Decision 020: MongoDB query patterns and index strategy

Status: Approved

Decision:

- expose a fixed repository/API query catalogue rather than arbitrary browser database filters;
- use cursor pagination with timestamp plus `_id` for recency lists and sequence numbers for turn/event timelines;
- create focused launch indexes for unique application IDs, configuration versions/activation, session lists, ordered turns/events, operation retries, current cost runs, feedback/error work queues, and consent authorization/history;
- enforce unique session-turn and session-event sequence pairs and unique logical-request attempt numbers;
- use partial indexes for optional turn, operation, provider-request, and retention-expiry fields where specified;
- calculate session totals from the latest successful calculation run using only `aggregation_behavior = charge`;
- defer cross-run provider/model analytics, reason-code multikey, authenticated initiator history, and speculative evaluation analytical indexes until real query volume justifies them; the bounded evaluation launch indexes are approved later in Decision 040;
- avoid wildcard, speculative, sensitive full-text, unrestricted provider-metadata, and high-cardinality debug indexes initially;
- do not create TTL indexes until retention periods and required expiry fields are separately approved;
- exclude ordinary diagnostic TTL treatment from cost and consent evidence and keep consent/asset deletion workflow controlled;
- review query/index measurements and Atlas recommendations before adding or removing indexes; do not apply recommendations automatically.

Reason:

The approved plan supports the R&D UI, diagnostics, cost reconciliation, feedback triage, and consent checks without paying the write/storage cost of speculative analytics indexes. Cursor pagination and targeted uniqueness constraints also keep query behaviour predictable as data volume grows.

Detailed design: `docs/02-database-design.md`.

### Decision 021: 30-day R&D retention policy

Status: Approved for R&D only

Decision:

- retain R&D session summaries, transcripts/responses, events, provider operations, errors, feedback, and cost entries for 30 days;
- do not store ordinary-session raw audio;
- if benchmark audio/assets are later approved, retain explicitly consented benchmark audio/assets for a maximum of 30 days and begin deletion immediately after revocation (Phase 0 has no benchmark assets; Decision 063);
- retain R&D evaluation run/result/human-rating evidence for 30 days under the scheduled cleanup policy approved later in Decision 040;
- exempt active/required agent configurations from operational retention deletion;
- make a retired configuration eligible only after its final related session expires and a further 30-day safety period completes;
- retain consent evidence while related assets exist and until deletion is verified; otherwise retain it for 30 days from the applicable decision;
- assign versioned `expires_at` values to retention-eligible records;
- if benchmark assets are separately approved later, delete them before their consent evidence and block consent cleanup when deletion is unverified;
- do not apply a generic diagnostic TTL rule to cost or consent evidence;
- use the bounded scheduled child-first R&D cleanup approved later in Decision 046 and defer only production TTL/orphan-cleanup strategy;
- require a fresh retention decision before production or external-user deployment.

Reason:

A uniform short R&D window limits privacy and storage exposure while preserving enough recent evidence for testing. Asset-first deletion and consent-ordering rules prevent automatic cleanup from destroying proof before retained audio has been removed.

Detailed design: `docs/02-database-design.md`.

### Decision 022: No R&D database backup

Status: Approved for R&D only

Decision:

- do not configure application-managed MongoDB backup, scheduled database export, snapshot retention, point-in-time recovery, or cross-region recovery for R&D;
- treat Atlas R&D records as disposable and accept that accidental deletion, corruption, cluster loss, or operator error can cause permanent data loss;
- provide no R&D recovery-point objective, recovery-time objective, or restore guarantee;
- continue version control for source code, schemas, migration scripts, documentation, and non-secret seed configuration without representing this as an Atlas data backup;
- keep secrets out of exports and source control;
- require explicit approval before introducing any manual export or backup;
- do not treat provider/platform internal replication as a project-controlled backup;
- make a fresh backup, deletion-propagation, and restore-testing decision before production or external-user deployment.

Reason:

The current environment is disposable R&D with a short 30-day data window. Avoiding a backup system keeps operational complexity and cost low while making the accepted data-loss risk explicit.

Detailed design: `docs/02-database-design.md`.

### Decision 023: Single database for development and R&D

Status: Approved for the current R&D phase

Decision:

- use one AWS Mumbai MongoDB Atlas Flex cluster and one MongoDB database for local development and shared R&D activity;
- distinguish development and shared-R&D records with the approved `environment` field;
- require environment-scoped repository queries and cleanup operations where applicable;
- use unique session/run IDs and explicit seed/demo/test labels to prevent collisions and enable narrow cleanup;
- prohibit development cleanup from targeting the entire shared database;
- keep production data, production credentials, and customer exports out of the R&D database;
- use the default database name `voice_agent_rnd`, configurable through `MONGODB_DATABASE` (Decision 067), and do not hard-code it in repositories;
- do not convert the R&D database into production by relabelling it;
- make a fresh decision for production project/cluster/database isolation, credentials, network controls, retention, backup, monitoring, and migration/sanitization.

Trade-off:

The single-database approach reduces R&D setup and cost but weakens isolation. Because R&D has no backup, repository-level environment controls and narrowly scoped cleanup are mandatory to reduce unrecoverable accidental loss.

Detailed design: `docs/02-database-design.md`.

### Decision 024: Single MongoDB database user for R&D

Status: Approved for the current R&D phase

Decision:

- create one dedicated MongoDB database user shared by the Python backend and voice-agent services in development/R&D;
- grant `readWrite` only on the selected R&D database and no cluster-wide administrative role;
- do not create separate runtime, migration, analytics, read-only, or per-developer database users in the current phase;
- never provide MongoDB credentials or direct database connectivity to the browser client;
- load the connection string through environment-based secret configuration and keep secret files out of source control;
- prohibit credentials in documents, code, logs, screenshots, and reports;
- use TLS for Atlas connections and rotate the credential after exposure, relevant offboarding, or a material access-boundary change;
- choose the username and generate the password only during separately authorized provisioning;
- prevent this R&D user from accessing any future production database;
- redesign users, service identities, roles, secret management, network controls, rotation, and auditing before production.

Trade-off:

One credential minimizes R&D setup but cannot provide per-developer database attribution or separation between runtime and migration actions. Application logs and application-level actor IDs provide the available R&D attribution.

Detailed design: `docs/02-database-design.md`.

### Decision 025: Database schema-validation architecture

Status: Approved

Decision:

- use separate strict Pydantic API, domain, persistence, event, adapter, and settings models;
- reject unknown fields and enforce approved enums, IDs, UTC timestamps, nested structures, and required/optional semantics;
- use explicit repositories for cross-document relationships, state transitions, consent gates, retry limits, cost consistency, immutable fields, environment scope, and cleanup safety;
- prohibit generic unrestricted document-update methods and expose named domain updates only;
- create strict MongoDB `$jsonSchema` validators for all nine core collections with `validationAction: error`;
- use unique/partial indexes for approved database-enforceable constraints;
- apply optimistic revision checks to mutable session, turn, review, resolution, and fulfilment state;
- keep Python Decimal/Decimal128, datetime/BSON UTC, enum/string, UUID/string, and ObjectId mapping inside repository codecs;
- omit unavailable optional measurements while preserving their availability/status evidence rather than silently coercing them to zero;
- omit secret-bearing fields from persistence models and redact protected settings in logs/serialization;
- return safe validation/persistence errors without storing or logging unrestricted rejected/provider payloads;
- require schema versions, compatible reader-first deployments, coordinated validator/writer changes, dry-run migrations, and count reconciliation;
- require separate approval for destructive migrations because R&D has no database backup;
- add validation tests for document shape, bounds, secrets, relationships, transitions, revisions, MongoDB validators, and serialization;
- apply the numeric/text/array/payload limits approved later in Decision 026.

Reason:

Layered validation catches unsafe input early while retaining a final database-level guard against malformed direct writes. Explicit repositories and revision checks handle business invariants and realtime races that JSON Schema cannot safely enforce alone.

Detailed design: `docs/02-database-design.md`.

### Decision 026: Baseline R&D limits and runtime defaults

Status: Approved

Amendment: Decision 067 caps endpoint tuning at 1,000 ms; values above 1,000 ms within the original 700–2,000 ms range require latency-budget re-approval.

Decision:

- enforce bounded names, labels, descriptions, notes, provider identifiers, tags, language lists, safe metadata, request/result summaries, event payloads, diagnostics, feedback, consent text, and document sizes as specified in the database design;
- permit up to 50,000 characters for the system instruction, 10,000 for user transcripts, and 20,000 for generated/synthesized/spoken response text;
- limit durable event payloads and provider-safe metadata to 16 KiB, operation request/result summaries to 8 KiB each, and error safe details to 8 KiB;
- limit an R&D session to 30 minutes and one user speech turn to 120 seconds;
- use initial timeouts of 15 seconds browser join, 20 seconds agent join, 60 seconds maximum silence before idle activity, 3 seconds STT finalization, 8 seconds LLM first token, 45 seconds LLM total, 5 seconds TTS first audio, 5 minutes idle session, 20 seconds reconnect, and 10 seconds graceful shutdown;
- allow at most three total attempts per logical provider request with 250 ms initial and 2,000 ms maximum exponential backoff plus jitter;
- enable interruptions with sub-250 ms false-candidate suppression, use 250 ms minimum accepted interruption duration, 700–2,000 ms total endpointing delay measured from the last speech frame (amended by Decision 067: capped at 1,000 ms unless the latency budget is re-approved), never resume stale audio after an accepted interruption, and disable preemptive generation initially;
- enforce per-collection document safety caps from 32 KiB for durable events up to 256 KiB for configurations/turns;
- use finite Decimal/Decimal128 values with up to 12 fractional places for billing evidence and six for percentages, applying provider rounding or final-boundary versioned rounding;
- allow provider/adapter limits to be stricter but never to bypass safety, secret, or allowlist validation;
- require a new approval before increasing the 30-minute session or 120-second user-turn maximum.

Reason:

These defaults are large enough for Hindi/Hinglish R&D while keeping realtime failures bounded, preventing oversized MongoDB documents, and making latency/retry comparisons consistent across providers.

Detailed design: `docs/02-database-design.md` and `docs/01-system-contracts.md` §16 (timeout and retry contract).

### Decision 027: Phase 0 backend module boundaries

Status: Approved

Decision:

- implement the Python backend as one modular-monolith repository with shared packages and two independent long-running processes: FastAPI `control-api` and LiveKit `agent-worker`;
- use an explicit scoped `maintenance` entry point for validators, indexes, seeds, migrations, consistency checks, and retention/deletion jobs;
- separate control API, worker bootstrap, orchestration, turn management, response segmentation, contracts, domain, ports, provider registry, adapters, persistence, events/latency, costing, privacy/retention, security, maintenance, and later evaluation modules;
- keep provider SDK types inside transport/STT/conversation/TTS adapters and make orchestration depend only on normalized ports;
- keep domain modules independent of FastAPI, LiveKit, PyMongo, and provider SDKs;
- route every MongoDB operation through explicit repositories and codecs;
- keep realtime audio/provider work outside HTTP request handlers;
- use one general-LLM conversation adapter initially and allow the existing Python knowledge engine to implement the same port later;
- include mock transport/provider adapters for contract and end-to-end testing before real-provider integration;
- keep provider selections, exact endpoint/method contracts, event backpressure, and production architecture as separately approved decisions;
- do not begin implementation merely because the module design is approved.

Reason:

The two-process modular monolith keeps Phase 0 simple to run while protecting the realtime path from HTTP workloads and preserving clean replacement points for LiveKit, STT, conversation, TTS, persistence, and the future knowledge engine.

Detailed design: `docs/03-backend-module-design.md`.

### Decision 028: Phase 0 FastAPI control API contract

Status: Approved

Decision:

- use versioned `/api/v1` JSON endpoints with strict Pydantic contracts, safe projections, UTC timestamps, request correlation, and cursor pagination;
- run Phase 0 without an application-login system only on the canonical `127.0.0.1` loopback boundary, with the exact approved frontend origin and no wildcard CORS;
- prohibit public deployment until authentication is separately approved;
- provide liveness/readiness, safe configuration reads, session create/read/list/end, join-token refresh, turn/event/operation/error/cost reads, feedback submission, and consent grant/status/revoke routes;
- require unique `voice_sessions.client_request_id` for idempotent session creation and add `uq_session_client_request_id`;
- issue LiveKit join tokens scoped to the room/participant with a 10-minute R&D lifetime and never persist or log them;
- carry realtime audio and transient browser events through LiveKit rather than FastAPI audio WebSocket/SSE endpoints;
- prevent the browser from selecting arbitrary providers, models, prompts, voices, endpoints, credentials, or persistence fields;
- keep agent configuration writes in maintenance tooling rather than Phase 0 browser APIs;
- use default/max page sizes of 25/100 for sessions, 50/100 for turns, and 100/500 for events;
- return standardized safe errors and never expose raw exceptions, provider payloads, stack traces, restricted log references, or restricted pricing;
- keep configuration mutation, public auth, telephony, evaluation, knowledge management, unrestricted export/query, and database-reset endpoints outside Phase 0.

Reason:

This API surface provides everything the separate R&D browser needs while keeping realtime work in the agent worker, making mutations idempotent, and preventing browser access to provider/database internals.

Detailed contract: `docs/04-control-api-contract.md`.

### Decision 029: Agent worker and orchestration flow

Status: Approved

Amendment: the 10-second heartbeat and 30-second lease in the third bullet are superseded by Decision 060 (5-second heartbeat, 15-second lease); Decision 067 adds heartbeat compare-and-set fencing and worker self-fencing.

Decision:

- use one orchestrator command loop as the single writer of session-visible state;
- validate LiveKit job identifiers against the durable session/configuration before starting providers;
- add a bounded `voice_sessions.worker_assignment` with worker/job identity, generation, independent `lease_revision`, claim/heartbeat/lease/release times, 10-second heartbeat, and 30-second R&D lease; heartbeat renewal must not increment business `state_revision`;
- reject duplicate active claims and discard every result from an old worker or cancellation generation;
- use normalized 20 ms mono PCM signed-16 audio frames with explicit sample rate and adapter-owned resampling;
- use bounded per-session tasks/queues: approximately two seconds inbound audio, five response segments, 500 non-terminal persistence events, and ten seconds playback audio;
- never silently drop user audio or terminal evidence;
- use a session-stream-oriented normalized STT lifecycle while allowing adapters to implement provider-specific call patterns;
- persist the final transcript before authorizing conversation generation;
- stream stable conversation segments into TTS while preemptive generation remains disabled;
- build conversation history from accepted user transcripts and delivered/spoken assistant content, not hidden interrupted generations;
- enforce interruption ordering that increments cancellation generation, stops playback, cancels queued/current work, records delivery, and closes the old turn before the next turn (amended: the canonical order is superseded by Decision 067 S9: confirm, fence, cancel LLM/TTS queued-then-active, clear the `AudioSource` queue, notify the browser, record);
- apply the approved three-attempt retry policy with attempt-level usage/error/cost evidence;
- stop playback during browser disconnect and allow only the approved 20-second reconnect window;
- converge normal end, failure, timeout, disconnect, and shutdown through an idempotent finalization path;
- exclude telephony, multi-user rooms, browser provider switching, arbitrary tools, knowledge retrieval, automatic benchmarking, long resume, and sessions over 30 minutes from Phase 0.

Reason:

Single-writer state ownership, bounded backpressure, assignment/cancellation generations, and one finalization path prevent duplicate workers, late provider output, memory growth, and interruption races from corrupting the user experience or evidence.

Detailed design: `docs/05-agent-worker-orchestration.md`.

### Decision 030: LiveKit transport adapter

Status: Approved

Decision:

- split LiveKit integration into a control-plane adapter for explicit dispatch/token/cleanup and a worker session-transport adapter for room media/data/reconnect;
- use explicit named-agent room dispatch, one opaque room per session, one expected browser participant, and one expected agent participant;
- create the durable session before dispatch, keep job metadata to a 2 KiB safe locator, and reload authoritative configuration in the worker;
- issue 10-minute least-privilege browser tokens for the exact room/identity and never persist, log, or place them in URL query parameters;
- make identical session-create retries reuse the session/room/dispatch/identity while issuing a fresh token;
- subscribe/publish audio only, normalize microphone and agent audio at the adapter boundary, and publish one logical `agent-audio` track;
- allow only the versioned `va.*.v1` realtime topics, cap reliable low-frequency application payloads at 8 KiB and lossy high-frequency payloads at 1,200 encoded bytes;
- accept bounded playback acknowledgements for delivery evidence without treating browser claims as security authority;
- stop playback and pause new work during disconnect, enforce the approved 20-second reconnect window, and never replay stale buffered audio;
- fail closed on unexpected participants and converge transport shutdown through bounded idempotent cleanup and best-effort room deletion;
- record transport usage/evidence without hardcoding provider price;
- use standard encrypted LiveKit/WebRTC transport while deferring additional agent E2EE to a separately approved design;
- exclude automatic dispatch, video, multi-user rooms, SIP, recording/egress, arbitrary RPC/files, browser admin grants, and public deployment from Phase 0;
- pin exact mutually compatible LiveKit package/browser SDK versions before implementation.

Reason:

The split keeps credentials and dispatch in the backend, realtime media in the worker, and LiveKit SDK details out of domain logic while preventing duplicate rooms, over-privileged tokens, stale playback, and unbounded transient data.

Detailed design: `docs/06-livekit-transport-adapter.md`.

### Decision 031: STT adapter contract and initial baseline

Status: Approved

Decision:

- use a provider-independent session-stream STT port with normalized audio input, partial/final events, turn-finalization, cancellation, usage, error, and idempotent close behaviours;
- select Deepgram Nova-3 Multilingual (`model=nova-3`, `language=multi`) as the first Phase 0 baseline;
- select Sarvam Saaras realtime/codemix as the first benchmark challenger after the baseline is stable;
- configure Hindi and English expectation with code switching enabled while translation, transliteration, and diarization remain disabled;
- preserve recognized script/language instead of silently translating or transliterating;
- allow a server-owned immutable list of at most 50 validated product keyterms and record any paid keyterm usage/cost separately;
- keep partial transcripts transient/UI-only and prohibit them from conversation history, LLM input, and ordinary turn persistence;
- require an accepted final transcript to be durably stored before conversation generation;
- keep the orchestrator authoritative for turn/endpoint decisions while provider speech/endpoint signals remain advisory;
- apply worker/cancellation generation checks, bounded buffering, distinct retry attempts, and no silent audio loss;
- measure latency, WER, CER, code-switch/product-term/numeric accuracy, endpoint errors, reliability, usage, and cost on representative audio;
- preserve dated native provider rates/quantities/currencies rather than hardcoding permanent price;
- exclude automatic translation/transliteration, browser provider controls, ordinary audio/partial storage, automatic provider fallback, and production compliance/SLA claims from Phase 0.

Reason:

This baseline provides a low-cost streaming multilingual path that fits the existing application-owned turn manager, while Sarvam supplies the most relevant first comparison for Indian code-mixed speech. The normalized contract keeps both replaceable and makes transcript acceptance, latency, accuracy, and cost auditable.

Detailed design: `docs/07-stt-adapter-and-baseline.md`.

### Decision 032: Conversation adapter and general LLM baseline

Status: Approved

Decision:

- use a replaceable text conversation port with normalized request, streaming text, segmentation, completion, cancellation, usage, error, and close behaviours;
- select OpenAI GPT-6 Luna (`gpt-6-luna`) with Responses API `reasoning = {"effort": "none"}` as the Phase 0 general conversation baseline; normalized configuration uses `reasoning.effort`;
- select Grok 4.7 with low reasoning as the first LLM benchmark challenger after the baseline is stable;
- keep independent STT and TTS instead of replacing the pipeline with provider speech-to-speech;
- require accepted durable final STT text before any conversation request;
- use streaming with a 250-output-token cap and provider-default sampling unless a safe option is separately approved;
- target a concise system instruction of about 2,000 tokens or less, 12,000 tokens recent history, and 16,000 total request input;
- truncate oldest complete turn pairs without automatic summarization when the context budget is exceeded;
- build history only from accepted user transcripts and delivered/spoken agent content;
- stream through a bounded speakable response segmenter and keep generated, synthesized, and delivered text separate;
- cancel on the local generation before accepting provider acknowledgement and reject all late output;
- permit guarded retries only before output is delivered and prohibit automatic full-answer retry after partial delivery;
- disable all tools, search, external actions, provider-authoritative memory, and provider conversation storage in Phase 0;
- capture input/cached/cache-write/reasoning/output usage and dated native rates for per-attempt/turn/session cost;
- benchmark latency, cancellation, language quality, brevity, instruction following, hallucination, safety, reliability, tokens, and cost;
- defer final prompt wording, endpoint/SDK version, credentials, challengers, tools, knowledge composition, fallback, and production compliance details.

Reason:

GPT-6 Luna provides a low-cost streaming multilingual baseline for a focused voice conversation, while the normalized adapter protects the future knowledge-engine replacement and prevents provisional input, hidden output, late cancellation data, or provider state from corrupting spoken history.

Detailed design: `docs/08-conversation-adapter-and-llm-baseline.md`.

### Decision 033: TTS adapter and initial voice baseline

Status: Approved

Decision:

- use a replaceable TTS port with normalized open, segment synthesis, streaming audio, cancellation, flush, usage, error, and close behaviours;
- select Sarvam Bulbul v3 (`bulbul:v3`) with voice `priya` as the Phase 0 baseline;
- select ElevenLabs Flash v2.5 with a later-approved Indian multilingual voice as the first challenger;
- synthesize streaming mono 24 kHz signed-16 Linear PCM normalized into recommended 20 ms frames;
- cap provider-facing segments at 500 Unicode characters and retain the five-segment queue bound;
- route Hindi/Hinglish as `hi-IN`, English as `en-IN`, and uncertain mixed text from current language context with `hi-IN` fallback;
- normalize speech-unfriendly formatting/numbers/terms deterministically without changing facts, translation, or meaning;
- allow a server-owned versioned pronunciation dictionary only after its contents are separately approved;
- keep generated, TTS-normalized, synthesized, and delivered/spoken evidence separate;
- generation-check every audio frame and reopen provider connections when a clean post-cancel boundary cannot be proven;
- retry only before delivery and never automatically replay a full segment after partial delivery;
- keep one voice/configuration per session and prohibit silent fallback;
- require AI-generated-voice disclosure;
- disable cloning, uploaded/community voices, and ordinary audio storage;
- preserve dated character/credit/audio usage and native currency for auditable cost;
- benchmark latency, interruption leakage, Hindi/Hinglish/English pronunciation, terms/numbers, consistency, defects, MOS, reliability, and cost;
- later blind-test `priya`, `ishita`, `shubh`, and `ratan` before a production preference.

Reason:

Bulbul v3 provides the strongest initial fit for Indian Hindi/Hinglish/English speech and future telephony formats, while the normalized contract prevents voice, audio, billing, and cancellation behaviour from becoming Sarvam-specific.

Detailed design: `docs/09-tts-adapter-and-voice-baseline.md`.

### Decision 034: Phase 0 prompt, language, greeting, and fallback policy

Status: Approved

Decision:

- use the exact versioned `phase0_general_voice_assistant_v1` system instruction for the initial general-purpose R&D assistant;
- explicitly state that product/company knowledge, live internet, customer records, tools, and business actions are unavailable;
- default to Hinglish, then mirror substantive Hindi, Hinglish, or English input while avoiding language switches caused only by accents, names, fillers, or isolated borrowed words;
- render Hindi in Devanagari, English in Latin script, and Hinglish with Devanagari Hindi plus common English terms in Latin script;
- produce warm, concise, spoken-only responses, normally one to three sentences and preferably below 80 words, with a hard 250-token cap;
- prohibit Markdown, raw URLs, stage directions, model/provider internals, fabricated access, and fabricated actions;
- ask one short clarification question when meaning or language is unclear and preserve important names, numbers, dates, codes, and identifiers;
- keep the exact opening greeting application-controlled, send it directly through the normal TTS path after valid `client.ready`, and play it only once per new session;
- keep unclear-speech, provider-failure, connection-problem, and session-limit messages as versioned deterministic application templates rather than new LLM calls;
- keep product/tool limitation answers model-owned under explicit instructions, allowing language adaptation without changing the limitation;
- validate every streamed candidate segment before TTS and reject stale, empty, structurally invalid, incomplete-tail, or unauthorized-generation output;
- record prompt identity/version/checksum, selected language, usage, latency, delivery, failure, and fallback evidence;
- treat behaviour-changing prompt/template edits as new prompt and agent-configuration versions;
- defer company persona, knowledge/citation prompt composition, tools/actions, human handoff, production wording, extra languages, and prompt-management infrastructure.

Reason:

A fixed, concise, versioned instruction makes the Phase 0 LLM measurable, while deterministic greeting and operational fallbacks remove avoidable model latency, cost, and variation. Explicit language and capability boundaries reduce unintended switching and fabricated product/tool claims before the real knowledge engine is connected.

Detailed design: `docs/10-phase0-prompt-and-language-policy.md`.

### Decision 035: Phase 0 evaluation dataset and acceptance gates

Status: Approved

Decision:

- use a provider-independent local Python evaluation harness rather than depending on a provider-hosted evaluation platform;
- begin with 100 logical scenarios: 60 transcript-to-LLM cases, 30 live browser voice scenarios, and 10 reliability/failure scenarios;
- divide transcript cases across language mirroring, general help, clarification, capability boundaries, safety/injection, names/numbers, and multi-turn context;
- run each transcript case three times per configuration to expose nondeterminism;
- perform live voice scenarios without persisting ordinary session audio and defer reusable benchmark-audio assets to a separately approved consent/storage design;
- use deterministic assertions, calibrated 1-5 human ratings, and measured latency/reliability/usage/cost evidence;
- do not select an LLM-as-judge until its model, rubric, calibration, and cost are separately approved;
- split the dataset into 80 visible development/regression cases and 20 held-out cases, preserving immutable case meaning within a version;
- enforce zero tolerance for critical safety/capability failures, fabricated access, secret disclosure, duplicate greeting, and stale post-interruption output;
- target at least 95% language/instruction compliance, 98% spoken formatting, 90% useful clarification/transcript semantic acceptance, 95% critical-term accuracy and successful end-to-end turns, and at least 4.0/5 overall human quality;
- target speech-end-to-first-audible-response P50 at most 2 seconds and P95 at most 4 seconds, plus interruption-to-silence P95 at most 500 ms;
- measure latency through browser playback evidence rather than provider-only claims;
- preserve native usage/rates/currencies and reconcile itemized/session cost within 1%, including billable retries and failures;
- defer a fixed session-cost ceiling until at least 20 valid baseline sessions establish real usage;
- compare candidate configurations on the same dataset with equivalent repetition/runtime conditions and report quality, latency, reliability, and cost separately;
- exact case content and evaluation database fields were deferred here and later resolved by Decisions 040 and 041; runner implementation, judge model, benchmark audio, production continuous evaluation, knowledge evaluation, and final provider weighting remain deferred.

Reason:

The fixed scenario distribution and explicit gates replace demonstration-driven judgement with repeatable evidence while keeping the harness portable across providers. Human review captures voice nuance, deterministic assertions catch hard requirements, and end-to-end evidence makes latency, reliability, and cost independently auditable.

Detailed design: `docs/11-phase0-evaluation-plan.md`.

### Decision 036: Phase 0 configuration and secrets contract

Status: Approved for R&D

Decision:

- use Pydantic Settings v2 for strict immutable Python bootstrap configuration and fail-safe startup validation;
- separate safe defaults, R&D environment settings, immutable agent behaviour, server secrets, and browser-public configuration;
- because the workspace is inside OneDrive, keep real R&D secrets in a local file under the current user's Local AppData area outside the repository and OneDrive;
- identify the external file through `VOICE_AGENT_SECRETS_FILE` and never create, overwrite, print, sync, upload, or back it up automatically;
- require MongoDB, LiveKit, Deepgram, OpenAI, and Sarvam credentials for the enabled baseline while requiring xAI/ElevenLabs credentials only when their challenger adapters are enabled;
- store provider/model/voice/prompt/language/timeout/retry/queue/rate-card behaviour in immutable versioned `agent_configs`, not environment overrides;
- apply bootstrap precedence of explicit process environment, external secret file, safe environment configuration, then safe application defaults;
- store only allowlisted references such as `env:OPENAI_API_KEY` in MongoDB and resolve values only inside server-side adapter boundaries;
- allow the Vite browser bundle only the API base URL, environment label, and build version, treating every `VITE_*` value as public;
- keep the approved short-lived LiveKit participant token transient and separate from static frontend configuration;
- validate enabled dependencies before readiness and expose only normalized safe failure codes;
- exclude credentials, connection-string secrets, tokens, signed URLs, settings dumps, provider objects, and exception locals from logs/events/errors;
- use project-specific R&D credentials, least privilege, and provider spend/rate controls where available;
- rotate R&D secrets manually through external-file replacement, process restart, safe verification, and old-key revocation, with no hot reload;
- add defence-in-depth Git ignore/scanning checks during implementation while recognizing that ignore rules do not make repository secret placement safe;
- treat Pydantic Settings and the local secret file as having no incremental third-party R&D service charge;
- defer exact dependency versions/file names, setup tooling, provider permission mechanics, production secret manager, CI/CD injection, service identities, and automatic rotation.

Reason:

This boundary keeps synchronized source files and the browser free of credentials while preserving reproducible versioned agent behaviour. The external single-user secret file is a zero-service-cost R&D mechanism, and the strict startup/redaction contract prevents missing configuration or accidental diagnostics from turning into silent failures or credential exposure.

Detailed design: `docs/12-configuration-and-secrets.md`.

### Decision 037: Phase 0 dependency and version matrix

Status: Approved for R&D compatibility validation

Decision:

- use CPython 3.12.14 with uv 0.12.18 and Node.js 24.21.0 LTS with bundled npm 11.19.0; install CPython 3.12.14 through uv-managed builds because python.org publishes no Windows installer for security-only releases;
- manage Python through `pyproject.toml`, `.python-version`, and a committed `uv.lock`, and frontend packages through exact `package.json` versions, `.nvmrc`, and committed `package-lock.json`;
- select the initial backend direct pins FastAPI 0.141.1, Uvicorn 0.53.0, Pydantic 2.13.5, Pydantic Settings 2.15.0, PyMongo 4.18.1, LiveKit Agents 1.8.3, LiveKit API 1.2.1, OpenAI 3.19.2 (amended to 2.54.0 by Decision 068), Deepgram SDK 7.10.0, and SarvamAI 0.1.34;
- use base LiveKit packages for control-plane/worker transport, except the `livekit-agents[silero]` extra approved by Decision 042, while keeping OpenAI, Deepgram, and Sarvam official SDKs inside this project's replaceable provider adapters;
- use the OpenAI Python SDK with the Responses API and HTTP SSE streaming, not the OpenAI Agents SDK or Realtime speech-to-speech;
- use official Deepgram SDK v7 for baseline streaming STT and official stable Sarvam SDK async streaming for baseline TTS;
- use `pymongo.AsyncMongoClient` directly, with no Motor or ODM;
- select React 19.3.0, React DOM 19.3.0, Vite 8.3.0, Vite React plugin 6.1.1, TypeScript 6.0.3, and LiveKit Client 2.22.3 for the browser;
- avoid adding a UI framework, global-state package, router, Axios, chart library, or LiveKit React component library until a demonstrated requirement is approved;
- use pytest/Vitest/Testing Library/Playwright/ESLint-based testing, with exact companion versions accepted only after compatible lock resolution;
- initially install only Playwright Chromium and defer Firefox/WebKit downloads until cross-browser testing is scheduled;
- keep challenger xAI, ElevenLabs, Sarvam STT, and alternative transport SDKs outside the baseline environment until their adapter milestone;
- prohibit prerelease, yanked, mutable Git, ad-hoc/global, and silently auto-updated application dependencies;
- require a compatibility gate covering Windows artifact resolution (including Silero VAD and `onnxruntime` Windows wheels), API/worker startup with Silero VAD model prewarm at worker startup, settings, Atlas, LiveKit, Deepgram, OpenAI, Sarvam, frontend build, browser audio/data, cancellation, redaction, usage, and cost evidence;
- return any incompatible/insecure direct candidate for evidence-backed version approval instead of silently replacing it;
- treat selected open-source libraries as having INR 0 incremental licence/subscription cost while tracking cloud/provider/runtime costs separately.

Reason:

The minimal locked matrix makes the first implementation reproducible while keeping provider SDKs confined to replaceable adapters. Conservative runtime choices, stable-only packages, an explicit compatibility gate, and no silent upgrades reduce integration/security risk without adding a dependency subscription fee.

Detailed design: `docs/13-dependency-and-version-matrix.md`.

### Decision 038: Phase 0 implementation execution plan

Status: Approved

Decision:

- use `backend/`, `frontend/`, `docs/`, and bounded sanitized `outputs/` as the canonical Phase 0 root layout;
- implement a Python modular monolith with independently runnable FastAPI control API and LiveKit worker processes plus a separate React/Vite browser application;
- execute bounded work packages in this order: preflight, locked scaffold, contracts/mocks, configuration, control API, MongoDB, LiveKit/browser transport, Deepgram STT, OpenAI conversation, Sarvam TTS, natural turn-taking, observability/cost, and evaluation;
- require a mock end-to-end vertical slice before any provider-paid test;
- enable and validate one external dependency at a time, capturing sanitized compatibility, latency, usage, failure, and cost evidence;
- apply explicit exit gates and mandatory stop conditions so dependency, provider, schema, security, or scope changes cannot occur silently;
- keep real secrets outside the repository/OneDrive path and prohibit ordinary audio, production data, or unrestricted sensitive evidence in repository artifacts;
- keep API, worker, and browser processes independently startable/restartable with an approved local startup/shutdown sequence;
- require the dated provider rate card and bounded smoke-test allowance before paid calls;
- require the approved evaluation collection contracts (resolved in Decision 040) and the separately approved exact 100-case dataset before the final Phase 0 release gate;
- exclude challenger providers, knowledge integration, telephony, public/multi-user deployment, production operations, object storage, and cross-browser certification from this implementation baseline.

Reason:

The sequence proves contracts with zero-provider-cost mocks first, then introduces each external dependency behind its approved adapter with a measurable stop/go gate. This reduces integration ambiguity, limits spend and secret exposure, preserves replaceability, and ensures that a stable baseline is supported by reproducible evidence rather than a single successful demonstration.

Detailed design: `docs/14-phase0-implementation-execution-plan.md`.

### Decision 039: Phase 0 pricing and cost model

Status: Approved for R&D

Decision:

- use versioned rate card `phase0_rate_card_2026_09_26_v1` and preserve original usage/rate/currency evidence;
- use INR 100 per USD as the conservative planning conversion while recording the dated reference and actual invoice/payment FX separately;
- use LiveKit Build at USD 0/month within its hard free allowances, with the locally hosted Python worker counted through WebRTC participant usage rather than managed-agent deployment minutes;
- budget MongoDB Atlas Flex at USD 8/month for the base workload while recognizing the published USD 30/month maximum;
- budget Deepgram Nova-3 Multilingual streaming at its published regular USD 0.0092/min rate while recording the current promotional USD 0.0058/min only when confirmed applicable;
- cost GPT-6 Luna Standard at USD 0.10/1M input, USD 0.01/1M cached input, USD 0.125/1M cache writes, and USD 0.50/1M output tokens, without assuming caching or tool usage;
- cost Sarvam streaming TTS at INR 3/1,000 synthesized characters;
- use the approved ten-minute planning assumption of ten STT minutes, 60,000 cumulative uncached LLM input tokens, 10 LLM responses of 250 output tokens each (2,500 output tokens; Decision 067), and 6,000 TTS characters; retain 20,000/60,000/120,000+ input-token low/planning/stress scenarios because repeated system instructions and retained history are billable on each turn;
- set the planning variable cost at INR 27.925 (≈ INR 27.93)/session or INR 2.7925/session-minute under those assumptions (STT INR 9.20, LLM input INR 0.60, LLM output INR 0.125, TTS INR 18.00; Deepgram promotional-rate variant INR 24.525); the 20-session loaded budget is INR 1,923.64 base and INR 5,038.84 Atlas-maximum (Decision 067, `docs/15-phase0-pricing-and-cost-model.md`);
- keep marginal provider cost separate from allocated monthly Atlas/platform cost;
- approve an initial set of up to 20 valid ten-minute baseline sessions, an INR 2,000 expected monthly alert, and an INR 5,100 absolute monthly approval boundary;
- calculate a separate 18% tax cash-buffer scenario for planning, but use actual invoice/reverse-charge/accounting treatment for final cost and obtain CA confirmation;
- calculate gross list-price usage before applying free credits, discounts, refunds, tax, FX spread, or payment fees;
- stop rather than silently upgrade a plan, top up credit, enable an add-on, or exceed a free/spend cap;
- refresh and version the rate card before the first paid call, on provider/plan/promotion changes, monthly during paid R&D, and before benchmarking.

Reason:

The model makes per-minute voice cost explainable without confusing marginal provider usage with fixed monthly infrastructure or temporary credits. Conservative FX and regular-rate budgeting limit surprise spend, while separate actual/invoice evidence supports accurate reconciliation and later provider comparison.

Detailed design: `docs/15-phase0-pricing-and-cost-model.md`.

### Decision 040: Evaluation database schema

Status: Approved for Phase 0 R&D benchmarking

Decision:

- add five evaluation collections to the existing R&D Atlas database: `evaluation_datasets`, `evaluation_cases`, `evaluation_runs`, `evaluation_results`, and `evaluation_human_ratings`;
- make frozen dataset versions and their cases immutable, with material changes creating a new dataset version;
- require the initial frozen release dataset to validate exactly 100 logical cases, the 60/30/10 layer split, the 80/20 development/holdout split, and three transcript repetitions;
- store the exact dataset/configuration/prompt/provider/build/dependency-lock/gate/rate-card snapshot on every run;
- allow exactly one result slot per run/case/repetition and preserve failed, cancelled, and invalid samples with their evidence;
- keep deterministic assertion outcomes, normalized metrics, usage/cost evidence, and individual human scorecards separately traceable;
- store human ratings in their own versioned collection, allow only one current submitted rating per result/reviewer/rubric, and never overwrite corrections;
- prohibit ordinary audio, partial transcripts, provider raw payloads, credentials, production/customer data, and direct general-browser evaluation access;
- retain terminal run/result/rating evidence for 30 days from the run terminal anchor using bounded scheduled child-first cleanup and ordinary expiry indexes, not immediate TTL indexes;
- keep active/frozen dataset/case definitions while reusable and delete retired definitions only when unreferenced plus a 30-day safety period;
- use strict Pydantic/repository/MongoDB validators, optimistic lifecycle revisions, explicit document-size caps, and approved unique/query/expiry indexes;
- use the existing single Atlas Flex database and spend boundary; add no second database, cache, object store, or analytics system;
- defer runner package/CLI/default concurrency, LLM judge, multi-reviewer adjudication, benchmark audio, cross-run analytics, and production evaluation; the exact 100-case content is approved in Decision 041.

Reason:

The schema makes every comparison reproducible without coupling evaluation to a provider or overwriting inconvenient samples. Separating definitions, executions, objective checks, and human judgement supports fair benchmarking while preserving the approved 30-day R&D privacy/cost boundary.

Detailed design: `docs/16-evaluation-database-schema.md`.

### Decision 041: Exact Phase 0 evaluation case catalog

Status: Approved

Decision:

- freeze the semantic content for dataset key `phase0_general_voice_assistant`, version 1, with exactly 100 logical cases;
- define exactly 60 transcript-to-LLM cases across the approved 12/10/8/10/10/6/4 category distribution;
- define exactly 30 live browser voice cases across 8 Hindi, 10 Hinglish, 6 English, and 6 fast-speech/pause/correction/noise scenarios;
- define exactly 10 reliability cases across 3 interruption, 2 reconnect, 3 STT/LLM/TTS failure, 1 duplicate-greeting, and 1 maximum-duration scenario;
- use exactly 80 development and 20 operational holdout cases, comprising 12 transcript, 6 live, and 2 reliability holdouts;
- execute transcript cases three times, live cases once, and reliability cases three times per configuration, producing 240 result slots;
- treat holdout as an operational no-tuning release discipline, keep it out of general browser APIs, and disclose when tuning has observed holdout outcomes;
- use exact accepted transcripts/live scripts/fault triggers with semantic response expectations, deterministic assertion codes, critical-term preservation, and approved human-review dimensions;
- require browser playback evidence for live success and prohibit ordinary audio/partial-transcript persistence;
- require exact deterministic fallback/greeting behaviour and generation-aware event ordering for reliability cases;
- keep invalid harness samples visible and never discard provider/application failures to improve reported scores;
- preserve zero tolerance for fabricated access/actions/live data, prompt/secret disclosure, unsafe critical failures, and stale/duplicate audible output;
- require a separate projected-cost approval before running the full paid 240-slot suite because it exceeds the initial 20-session smoke allowance;
- defer runner package/CLI/default concurrency, fixture generation, LLM judge, multiple-reviewer adjudication, reusable audio, later knowledge/telephony catalogs, and provider-weighted winner formulas.

Reason:

The catalog converts broad evaluation goals into reproducible, auditable inputs and pass conditions across Hindi, Hinglish, English, live audio, and failure handling. The explicit split and repetition policy expose nondeterminism while preventing a few successful demos or silently discarded failures from defining the baseline.

Detailed design: `docs/17-phase0-evaluation-case-catalog.md`.

### Decision 042: Speech activity, endpointing, barge-in, and AgentSession ownership

Status: Approved

Decision:

- add a worker-local provider-neutral `SpeechActivityDetector`/`SpeechActivityPort` backed initially by Silero VAD;
- make local Silero VAD authoritative for speech-activity start and stop while keeping the Turn Manager authoritative for turn open/close, endpoint commitment, and interruption acceptance;
- route continuous normalized microphone audio to both local VAD and the Deepgram STT stream, including while agent playback is active;
- configure the initial detector for 16 kHz mono PCM, 0.5 activation threshold, 50 ms minimum speech, 500 ms prefix padding, and approximately 550 ms silence detection;
- measure the total endpoint deadline from the last detected speech frame, initially 700 ms and configurable within 700–1,000 ms (values above 1,000 ms up to the earlier 2,000 ms limit require latency-budget re-approval; Decision 067), so VAD silence and endpoint delay are not added together;
- require 250 ms of continuous local speech from interruption-candidate start before incrementing cancellation generation and stopping playback/TTS/LLM work; candidate start is the VAD speech-onset timestamp on the worker monotonic clock; while agent audio is playing, raise the interruption-candidate activation threshold from 0.5 to 0.7 (`vad.playback_activation_threshold`, Decision 067), with the same 250 ms confirmation;
- state user-perceived interruption time explicitly as VAD onset (about 50–100 ms) + 250 ms confirmation + at most 500 ms acceptance-to-silence ≈ 800–850 ms at P95, while the gate remains acceptance-to-silence P95 at most 500 ms with a 1,000 ms hard maximum (Decision 067);
- suppress shorter candidates without cancelling playback; after an interruption is accepted, never resume stale audio automatically, and use the approved clarification fallback if no usable transcript arrives;
- keep Deepgram authoritative for transcript segments/text while treating `SpeechStarted`, `speech_final`, `UtteranceEnd`, and provider endpoint/finalization signals as advisory diagnostics only;
- do not use LiveKit `AgentSession` or the LiveKit semantic/audio Turn Detector in Phase 0; continue using job/room/audio/data primitives with the custom orchestrator;
- approve `livekit-agents[silero]` as the sole exception to the LiveKit plugin/extras exclusion, keep its types behind the speech-activity adapter, and freeze resolved compatible dependencies through the lock compatibility gate.

Reason:

This removes the missing owner for speech detection and makes barge-in and endpoint latency independently testable. Local detection avoids provider/network timing controlling user-visible cancellation, while application-owned turn commitment preserves provider replaceability and prevents duplicate ownership with LiveKit `AgentSession`.

Detailed design: `docs/03-backend-module-design.md`, `docs/05-agent-worker-orchestration.md`, `docs/06-livekit-transport-adapter.md`, `docs/07-stt-adapter-and-baseline.md`, `docs/13-dependency-and-version-matrix.md`, and `docs/14-phase0-implementation-execution-plan.md`.

### Decision 043: Documentation, event, dependency, cost, and milestone consistency

Status: Approved

Amendment: Decisions 060–066 correct additional recovery, transport, schema, evaluation, latency, and workbook inconsistencies found during the later cross-document audit.

Decision:

- make Decision 035 gates canonical everywhere: speech-end-to-first-audible-response P50 at most 2.0 seconds, P95 at most 4.0 seconds, and interruption-to-silence P95 at most 500 ms;
- use one normalized runtime event vocabulary across system, persistence, and adapter documents: `stt.final`, `tts.audio_frame`, `conversation.segment_ready`, and their approved lifecycle companions;
- preserve local-VAD pre-acceptance false-interruption suppression and remove legacy post-cancellation recovery wording;
- replace TypeScript 7.0.2 with exact candidate TypeScript 6.0.3 because the selected TypeScript ESLint support range is `<6.1.0`;
- use GPT-6 Luna Standard short-context planning rates of USD 0.10 input and USD 0.50 output per 1M tokens in the research workbook (doc 15 governs the cost basis per Decision 067) and preserve Flex prices only when explicitly labelled as a separate tier;
- align the master implementation milestones with persistence before real STT/LLM/TTS work;
- implement/mock-test the bounded Deepgram keyterm option path while keeping the live baseline keyterm list empty and excluding its paid add-on until separate approval.

Reason:

These corrections remove contradictory gates, event names, dependency combinations, cost labels, and implementation ordering without changing the approved provider architecture.

### Decision 044: Durable session termination, state transitions, and worker-crash reconciliation

Status: Approved

Amendment: Decision 060 replaces the original 10/30-second scan/lease timings with 5/15/5-second heartbeat/lease/reconciliation timings and adds writer-epoch fencing plus termination race barriers.

Decision:

- persist an idempotent `termination_request` before sending a targeted reliable `va.control.v1` wake-up packet to the worker;
- treat the LiveKit packet as best-effort acceleration only and MongoDB state as authoritative;
- run one bounded control-API background session reconciler every 5 seconds against indexed due nonterminal sessions;
- permit `created -> connecting|ending|failed`, `connecting -> active|ending|failed`, `active -> ending|failed`, and `ending -> ended|failed`;
- classify connection/start timeout and exhausted worker recovery as `failed`, while explicit user end, idle timeout, and maximum-duration end become `ended` after verified cleanup;
- detect expired 15-second worker leases under writer-epoch fencing, abandon unfinished work, reject stale generations, and allow at most one higher-generation replacement dispatch with a 20-second recovery deadline;
- never replay the opening greeting or stale audio after recovery; terminalize exhausted recovery as `failed`.

Reason:

Durable intent plus a reconciler prevents sessions from remaining indefinitely `ending` or `active` when the realtime packet is lost or the worker disappears.

### Decision 045: Evaluation scoring and insert-only execution-attempt lineage

Status: Approved

Decision:

- add `safety_appropriateness` to the approved human-rating dimensions;
- score calm/natural tone through existing `conversational_naturalness` rather than an undeclared field;
- store every evaluation execution as an immutable attempt with one-based `attempt_index`, current-attempt marker, and supersession link;
- make `(run, case, repetition, attempt)` unique and permit exactly one current attempt per logical slot;
- allow a new attempt only for genuine harness/test-setup invalidation; retain the invalid attempt and prohibit rerunning application/provider failures merely to improve results.

Reason:

The validator now accepts every catalog rubric dimension, and reruns remain auditable without overwriting inconvenient evidence.

### Decision 046: Session evidence expiry fields and child-first R&D cleanup

Status: Approved

Decision:

- use terminal `voice_sessions.ended_at` as the retention anchor and propagate required `expires_at = ended_at + 30 days` to session-scoped R&D records;
- add ordinary expiry indexes for sessions, turns, operations, events, costs, feedback, and errors; do not add immediate TTL indexes;
- run scheduled cleanup at least once per 24 hours in batches of at most 100 sessions;
- delete `user_feedback`, `error_events`, `cost_entries`, `provider_operations`, `session_events`, `conversation_turns`, then `voice_sessions`;
- verify each child collection before parent deletion, stop on failure, preserve the parent, record a safe error, and retry idempotently;
- retain separate protected workflows for consent/assets, retired configurations, and evaluation definitions.

Reason:

The approved 30-day policy is now executable and referentially safe rather than depending on absent fields or an unspecified deletion order.

### Decision 047: Per-segment streaming validation and output-cap truncation

Status: Approved

Decision:

- validate every complete candidate response segment before TTS for generation, content shape, speakability, and the 500-character boundary;
- maintain the rolling 250-token output cap while streaming and retain unfinished trailing text in the segment buffer;
- on normal completion, release the tail only if it is a complete valid speakable unit;
- on maximum-token/length completion, discard the incomplete tail before TTS and exclude it from delivered history;
- when no meaningful complete segment was delivered, speak the exact versioned response-truncated fallback once; otherwise complete the turn with `response_completion_status = truncated_partial` (or `truncated_fallback` when the fallback was spoken) and show a safe UI notice without inventing a closing sentence; the terminal turn status remains `completed` (Decision 067);
- retain final whole-response audit evidence without pretending it can retroactively validate already spoken segments.

Reason:

This preserves low-latency phrase streaming while preventing structurally invalid or half-finished token-limit output from becoming audible.

### Decision 048: Documentation hygiene and Deepgram rate presentation

Status: Approved

Amendment: Decision 066 completes the later stale-text, evaluation-statistics, cost-category, and canonical-workbook alignment.

Decision:

- keep the decision range, collection names, evaluation status, and next-step wording synchronized with the latest approvals;
- distinguish Deepgram Nova-3 Multilingual's currently displayed promotional USD 0.0058/min rate from its published regular USD 0.0092/min rate;
- use the regular rate for the conservative budget and the promotional rate only for dated actual estimates while it remains applicable.

Reason:

Current and ceiling rates answer different questions. Explicit labels prevent temporary promotional pricing and stale documentation from becoming permanent planning assumptions.

### Decision 049: Cumulative LLM token planning

Status: Approved

Decision:

- calculate session input as the sum of every billed turn input, including repeated system instructions, retained history, current user text, and any supplied context;
- use 20,000 input tokens as a low case, 60,000 as the ten-minute planning baseline, and 120,000 or more as a stress case;
- use 10 LLM responses × 250 output tokens = 2,500 output tokens for the approved ten-minute example (superseding the earlier 1,200-token figure; Decision 067) and replace assumptions with measured provider usage when available.

Reason:

Unique conversation text understates billed input because prompt and retained history are resent across turns. Scenario-based cumulative accounting is more reproducible and remains inexpensive enough not to distort the overall stack comparison.

### Decision 050: LiveKit reliable and lossy payload caps

Status: Approved

Decision:

- cap encoded lossy realtime data messages at 1,200 bytes;
- cap encoded reliable low-frequency application messages at 8 KiB;
- compact or truncate oversized transient payloads rather than fragmenting them, while sending final durable-safe state through the reliable path.

Reason:

Small lossy packets reduce congestion and head-of-line risk for frequently replaced UI state without unnecessarily constraining bounded final state and error envelopes.

### Decision 051: Worker heartbeat revision isolation

Status: Approved

Amendment: the ten-second heartbeat referenced in the reason is superseded by Decision 060 (5-second heartbeat, 15-second lease); Decision 067 defines the exact heartbeat compare-and-set match fields and expired-lease rejection.

Decision:

- add an independent `worker_assignment.lease_revision` used with assignment identity/generation for heartbeat compare-and-set updates;
- heartbeat updates only `heartbeat_at`, `lease_expires_at`, and `lease_revision`;
- heartbeat renewal does not increment session `state_revision`, `updated_at`, or `last_activity_at`;
- business lifecycle mutations continue to use and increment `state_revision`.

Reason:

Liveness renewal is not a business state change. Separating revisions prevents a ten-second heartbeat from repeatedly invalidating legitimate API and orchestrator state mutations.

### Decision 052: Atomic session event sequence allocator

Status: Approved

Decision:

- store `event_sequence_counter` on `voice_sessions`, initialized to zero;
- require every API, worker, and maintenance writer of a durable session event to obtain its number through one shared repository allocator using MongoDB `findOneAndUpdate` with `$inc: 1` and the updated value returned;
- retain the unique `(session_id, sequence_number)` index, allow unused gaps after failures, and prohibit writer-local authoritative counters;
- keep Phase 0 allocation per durable event; block reservation is a later measured optimization.

Reason:

A shared atomic allocator gives API and worker events one unambiguous session-local order without relying on timestamps or unsafe independent counters.

### Decision 053: Complete Control API status and error contract

Status: Approved

Decision:

- return `201` for the first successful session, feedback, or consent creation and `200` with `idempotent_replay = true` for an identical replay;
- return `202` with the current `ending` summary when an end request is first accepted and `200` with the current summary plus `idempotent_replay = true` for a replay or already-terminal session;
- require consent revoke body fields `client_submission_id` and `reason = tester_revoked` in Phase 0;
- publish stable uppercase application error codes and their HTTP mappings for validation, access, not-found, payload, revision/state/idempotency conflicts, consent, rate limit, provider/dependency availability, and internal failures.

Reason:

Explicit status, replay, and error contracts let the separate UI distinguish successful replay, asynchronous termination, client correction, and safe retry without guessing from free-form messages.

### Decision 054: Evaluation implementation ownership and cleanup

Status: Approved

Decision:

- create evaluation collection validators, indexes, and repositories in work package 5;
- implement the evaluation runner, scoring integration, and report flow in work package 12;
- add explicit cost controls to work packages 10–12 and require projected-cost approval before a full paid run;
- use `critical` rather than undefined `severity-one` terminology and keep Hinglish-tagged test scripts genuinely Hinglish;
- remove stale text that describes the approved evaluation catalog as a later or pending design.

Reason:

Clear work-package ownership and consistent gate language prevent the approved schema/catalog from being omitted or implemented twice.

### Decision 055: Verified Python dependency lifecycle

Status: Approved

Decision:

- retain `deepgram-sdk == 7.10.0`, published on 2026-09-21, rather than downgrading to 7.9.0;
- record Motor as deprecated since 2025-05-14, with ordinary bug-fix support ending 2026-05-14 and critical-fix support ending 2027-05-14;
- continue using `pymongo.AsyncMongoClient` directly and introduce no Motor dependency.

Reason:

The pins and lifecycle notes now match current package and vendor evidence while keeping the new codebase on the supported PyMongo Async path.

### Decision 056: Standard document-control headers and schema-table completeness

Status: Approved

Decision:

- begin every numbered Phase 0 document with the same ordered control fields: `Status`, `Authority`, `Scope`, `Depends on`, `Implementation status`, and `Last reviewed`;
- retain document-specific provider, model, runtime, dataset, or pricing metadata immediately after the common block;
- require every four-column schema row to include field, type, requirement, and purpose cells, including conditionally required lifecycle timestamps.

Reason:

Consistent document control and complete schema rows make review status, authority, dependency order, and validator requirements quickly comparable across the documentation set.

### Decision 057: Enum casing convention

Status: Approved

Decision:

- encode wire, runtime, and persistence enum values as lowercase `snake_case`;
- encode browser-visible application error codes as uppercase `SNAKE_CASE`;
- retain uppercase `SNAKE_CASE` for environment-variable and secret names;
- normalize the previously uppercase session, agent-activity, turn, provider-operation, and consent-decision values before implementation begins.

Reason:

One value convention prevents case-conversion bugs across Python, TypeScript, JSON, and MongoDB while keeping errors and environment variables visually distinct.

### Decision 058: Canonical Phase 0 loopback hostname

Status: Approved

Decision:

- use `127.0.0.1` consistently for the Phase 0 API bind, Vite development origin, browser URL, and API base URL;
- allow exactly `http://127.0.0.1:5173` as the initial frontend origin and prohibit wildcard CORS;
- do not treat `localhost` as an interchangeable browser origin; an alias change requires an explicit allowlist update.

Reason:

Browsers treat `localhost` and `127.0.0.1` as different origins. One canonical loopback hostname removes an avoidable CORS mismatch during local R&D.

### Decision 059: Explicit no-login Phase 0 access wording

Status: Approved

Decision:

- implement no user registration, account, login, session cookie, or application authentication flow in Phase 0;
- bind the API to the approved loopback boundary, enforce the exact frontend origin, and assign the fixed `internal_tester` context server-side;
- reserve `AUTHENTICATION_REQUIRED` for a later authenticated deployment and do not present local Phase 0 users as authenticated users;
- retain provider-credential authentication and future production-authentication references where those meanings are explicit.

Reason:

The wording now matches the actual single-user local boundary without weakening provider credential checks or implying that public deployment is safe.

### Decision 060: Fenced worker recovery and termination race closure

Status: Approved

Decision:

- distinguish initial claims (`connecting`) from authorized recovery claims (`active` plus `recovering`);
- fence every conversational-state write by worker generation and `writer_epoch`, allowing the reconciler temporary ownership only after atomically fencing an expired lease;
- use 5-second heartbeats, 15-second leases, and 5-second reconciliation scans;
- recheck durable termination intent before replacement dispatch, after dispatch, and at the replacement worker startup barrier;
- show browser recovery promptly on agent-participant loss and permit only one bounded higher-generation recovery.

Reason:

Replacement workers can now claim validly, the reconciler cannot race an old writer, and a concurrent end request cannot be lost during recovery.

### Decision 061: Finalized STT segment assembly and audio-time turn binding

Status: Approved

Decision:

- treat Deepgram `is_final` as a finalized segment rather than a complete utterance;
- assemble multiple non-overlapping final segments in audio-time order and deduplicate only identical provider IDs or interval/text fingerprints;
- map provider-relative timing through a stream epoch to capture timestamps and bind results to accepted turn speech windows by audio overlap, never callback arrival time;
- keep Silero/Turn Manager authoritative for speech and endpoint commitment and prevent playback echo, late finals, and sub-threshold non-speech from leaking into the next turn.

Reason:

Streaming STT may finalize multiple chunks before one application turn ends; explicit audio-time lineage preserves every word without cross-turn contamination.

### Decision 062: Browser audio processing, queue flushing, and realtime limits

Status: Approved

Decision:

- request browser echo cancellation, noise suppression, automatic gain control, and mono capture;
- normalize WebRTC intake at 48 kHz/20 ms and resample the Silero/Deepgram baseline to 16 kHz mono Linear16; publish TTS at 24 kHz mono Linear16;
- use a 200 ms LiveKit `AudioSource` queue and call `clear_queue()` after producers are fenced/cancelled during interruption;
- explicitly set the opaque agent participant identity at job acceptance;
- enforce 20 browser messages/second with burst 40 and retain the stricter four-per-second playback-progress limit.

Reason:

The contract now addresses echo defence, sample-rate ownership, buffered stale audio, participant identity, and bounded browser traffic directly.

### Decision 063: MongoDB schema normalization and reference-safe expiry

Status: Approved

Decision:

- add turn environment, input disposition, response completion/truncation, worker epoch/recovery authorization, bounded join-token request evidence, consent events, and recording-authorization lookup;
- remove duplicate consent subject identity and standardize `anonymous_user`, redaction status, and calculation status semantics;
- require BSON `Decimal128` for monetary/precise decimal values and omit unavailable optional MongoDB fields rather than alternating with `null`;
- prefix environment-scoped operational indexes while leaving global identity and parent-scoped indexes selective and collision-safe;
- keep active configurations unexpired and expire retired configurations only after 30 days and all references are gone;
- keep `benchmark_assets` and object-storage schema outside Phase 0.

Reason:

The approved database now has executable, internally consistent validation, idempotency, indexing, and cleanup rules without silently adding a tenth core collection.

### Decision 064: Complete evaluation definition and repetition contract

Status: Approved

Decision:

- define dataset retention state and reference-safe retired-definition expiry;
- require `updated_at` on mutable draft datasets/cases and freeze their meaning afterward;
- define persisted language label `mixed` explicitly;
- require repetitions `3/1/3` for transcript/live/reliability layers and exactly 240 logical result slots per complete initial configuration.

Reason:

The evaluation validator can now reproduce the approved catalog counts and safely manage mutable drafts, frozen definitions, and retirement.

### Decision 065: Canonical turn, event, segmentation, and API syntax contracts

Status: Approved

Decision:

- define branching turn transitions, unusable-input clarification, explicit truncated response disposition, `realtime_overload`, missing client/playback events, maximum-session timeout, and `va.control.v1`;
- treat application IDs as opaque rather than sortable;
- make the orchestrator-owned `ResponseSegmenter` the sole producer of `conversation.segment_ready`;
- normalize `speaking_rate` and map it to Sarvam `pace` only inside the adapter;
- use OpenAI Responses syntax `reasoning = {"effort": "none"}` and normalized configuration path `reasoning.effort`;
- update diagrams and stale authorization/storage/security wording; the §2 architecture diagram (Speech Activity Detector, media flow) and §8 entity relationships were completed under Decision 067.

Reason:

One owner and one canonical name now exist for every cross-module state, event, segment, timeout, and provider option.

### Decision 066: Latency statistics and cost-model alignment

Status: Approved

Decision:

- use diagnostic stage budgets of 700/1,200 ms endpointing, 200/600 ms STT-final, 50/150 ms persistence, 350/900 ms first speakable segment, 300/800 ms TTS first audio, and 100/350 ms transport/browser for P50/P95 respectively, while keeping end-to-end P50/P95 2/4 seconds authoritative;
- claim interruption P95 only after at least 20 valid samples, require P95 at most 500 ms and hard maximum at most 1,000 ms, and never infer a percentile from one conversation;
- align evaluation cost categories with marginal versus fully allocated cost in the pricing model;
- treat LLM output tokens and 6,000 TTS characters as independent conservative ten-minute assumptions, not a conversion (Decision 067 sets output at 10 responses × 250 = 2,500 tokens);
- use the ten-minute, 60,000-input-token, 2,500-output-token, 6,000-character, INR 100/USD planning scenario, clearly separating any five-minute market-comparison scenario.

Amendment (Decision 067): `docs/15-phase0-pricing-and-cost-model.md` governs the cost basis. The R&D workbook in `outputs/` is a research snapshot, not an aligned authority; it carries a supersession note pointing to doc 15.

Reason:

Latency gates now have valid sample semantics and diagnostic budgets, while doc 15 carries the single explicit cost basis.

### Decision 067: Round-3 review corrections

Status: Approved

Decision:

- **S1 lease fencing:** there is no `assignment_id`; an assignment is identified by (`worker_assignment.generation`, `worker_instance_id`, `livekit_job_id`). Heartbeat renewal is a compare-and-set on `session_id`, generation, `worker_instance_id`, `livekit_job_id`, `writer_epoch`, and `lease_revision`, requiring `lease_expires_at > $$NOW`; success sets `lease_expires_at = now + 15 s` and increments `lease_revision` only. An expired lease cannot be renewed. The reconciler fence increments `writer_epoch` and `lease_revision`. Decision 060 timings (5 s heartbeat, 15 s lease) stand;
- **S2 worker self-fencing:** the worker keeps a local lease deadline (last renewed `lease_expires_at`, measured from request send, minus a 3 s safety margin) and self-fences when it passes or when any write returns an epoch/generation mismatch: revoke output authorization, cancel LLM/TTS, `AudioSource.clear_queue()`, unpublish agent audio, disconnect, emit best-effort `worker.self_fenced`, exit. A self-fenced or evicted worker never rejoins or retries the claim;
- **S3 stored recovery authorization:** `voice_sessions.recovery_authorization` (`owner_instance_id`, `acquired_at`, `expires_at` = 10 s ownership lease renewed every 5 s by the owning reconciler, `recovery_deadline_at` = first acquisition + 20 s, `writer_epoch`, `recovery_dispatch_id`, `owner_generation`) is written in one atomic step-1 update that also fences, increments `worker_recovery_count`, sets `recovering`, and stores `recovery_dispatch_id` before dispatch; retries are idempotent; it is cleared on successful replacement claim or finalization. If the ownership lease expires before the recovery deadline (crashed or restarted reconciler owner), a later pass takes over with a fencing update that increments `writer_epoch` and `owner_generation`, not `worker_recovery_count`, and does not reset the deadline; once the 20 s `recovery_deadline_at` passes without a claim, any reconciler instance takes the fenced finalize path and marks the session `failed`. The owner renews the ownership lease every 5 s by compare-and-set on `owner_instance_id` and `owner_generation`; the lease gates only reconciler ownership. The replacement claim requires `active`, matching dispatch ID, `recovery_deadline_at > $$NOW`, and no `termination_request`; first acquisition requires that no `recovery_authorization` is present. End during recovery makes the claim fail and the reconciler finalizes; revision conflicts re-read and retry at most 3 times and never leave an open turn. The reconciler runs inside the control-API process; `next_reconcile_at` is the minimum active deadline;
- **S4 browser recovery notice:** after step 1 the reconciler sends a best-effort `agent.recovering` state message via `RoomService.SendData`; the browser also falls back to "reconnecting" after 5 s without agent audio or state;
- **S5 replacement identity:** the replacement joins with the same agent identity; LiveKit's eviction of the old participant is expected and is not an unexpected-participant termination;
- **S6 playback ack identity:** (`worker_assignment.generation`, in-memory cancellation generation, segment id); there is no durable cancellation-generation field;
- **S7 events:** `01-system-contracts.md` §9 is the single vocabulary; adds durable `worker.lease_expired`, `worker.recovery_started`, `worker.recovery_claimed`, `worker.recovery_failed`, `worker.self_fenced` (category `worker`), durable `session.end_requested` (category `session`), browser `client.ready`, `playback.progress`, `playback.failed`, `client.mic_muted`, `client.mic_unmuted`, and maps `realtime_overload` to error category `capacity`;
- **S8 turn states:** adds `input_disposition = pending` and terminal status `discarded`; first authorized audio sets `audio_streaming`; truncation is expressed only by `response_completion_status ∈ {truncated_partial, truncated_fallback}` with terminal status `completed`; `maximum_tokens` maps from Responses `status = incomplete` with `incomplete_details.reason = "max_output_tokens"`; a turn opens during playback only after a confirmed interruption candidate;
- **S9 interruption order:** confirm (≥250 ms), increment in-memory cancellation generation, cancel LLM/TTS (queued then active), `AudioSource.clear_queue()` (200 ms queue), notify browser, record evidence;
- **S10 echo:** browser echo cancellation/noise suppression/AGC on; agent audio plays only through the LiveKit-attached WebRTC audio element; playback interruption threshold 0.7 (`vad.playback_activation_threshold`);
- **S11 latency:** response latency runs from the worker-monotonic last VAD speech frame of the committed turn to first audible browser audio, composed from worker span + browser playout span + RTT/2 estimate with recorded uncertainty and no cross-machine wall-clock subtraction; endpoint tuning is capped at 1,000 ms without re-approval; user-perceived interruption ≈ 800–850 ms P95 while the gate stays acceptance-to-silence P95 ≤ 500 ms, hard max 1,000 ms;
- **S12 `INT-LIVE`:** two live sessions (`INT-LIVE-S` speakers, `INT-LIVE-H` headphones) of 12 scripted barge-ins each (≥24 samples) outside the 100-case catalog; the interruption P95 uses these plus valid live barge-ins (minimum 20); REL-091/092 stay mock correctness checks; cost about INR 55.85, approved with the live batch;
- **S13 cost basis:** doc 15 governs; 10 responses × 250 = 2,500 output tokens, 60,000 input tokens, 6,000 TTS characters; INR 27.925 (≈ 27.93)/session, INR 2.7925/min, promo variant INR 24.525; 20-session budget INR 1,923.64 base and INR 5,038.84 Atlas-maximum; alert INR 2,000, ceiling INR 5,100; the `outputs/` workbook is a research snapshot carrying a supersession note;
- **S14 environment:** `APP_ENV` is `development` or `rd` in Phase 0 (`production` reserved in schema enums but rejected at startup); agent-config list filter defaults to the running `APP_ENV`; default database `voice_agent_rnd`, configurable through `MONGODB_DATABASE`;
- **S15 enums:** actor type `anonymous_user`/`system`/`system_evaluation`/`internal_reviewer`; consent `subject.type = anonymous_user`; `termination_request.requested_by` `anonymous_user`/`system_timeout`/`system_reconciler`/`system_evaluation`; End `reason` uses the doc 02 `disconnect_reason` enum; all enums lowercase `snake_case`;
- **S16 crosswalk:** doc 14 owns the milestone ↔ work-package crosswalk; §9 only points to it; challenger benchmarking under Milestone 10 is outside work package 12 and deferred.

Reason:

The round-3 review found fencing gaps that could let a late worker or crashed reconciler produce duplicate audio or lose an end request, plus inconsistent event, turn-state, latency, cost, environment, and enum definitions across documents. These corrections give each rule one owner and one canonical wording.

Detailed design: `docs/01-system-contracts.md` (S7, S8, S9, S15), `docs/02-database-design.md` (S1, S3, S7, S8, S14, S15), `docs/03-backend-module-design.md` (S1, S3, S11), `docs/04-control-api-contract.md` (S3, S14, S15), `docs/05-agent-worker-orchestration.md` (S1–S6, S8–S11), `docs/06-livekit-transport-adapter.md` (S4, S5, S6, S9, S10), `docs/07-stt-adapter-and-baseline.md` (S10, S11), `docs/08-conversation-adapter-and-llm-baseline.md` (S8, S9), `docs/09-tts-adapter-and-voice-baseline.md` (S9), `docs/11-phase0-evaluation-plan.md` (S11, S12), `docs/12-configuration-and-secrets.md` (S14), `docs/14-phase0-implementation-execution-plan.md` (S16), `docs/15-phase0-pricing-and-cost-model.md` (S13), `docs/16-evaluation-database-schema.md` (S14), and `docs/17-phase0-evaluation-case-catalog.md` (S12).

### Decision 068: WP1 lock-resolution corrections

Status: Approved (user, 2026-09-28)

Decision:

- pin `openai==2.54.0` instead of `3.19.2`: every `livekit-agents` 1.8.x release requires `openai>=2.50,<3` as a base dependency, so the approved pair was unsatisfiable. 2.54.0 is the latest stable 2.x; GPT-6 Luna Responses streaming, usage, and cancellation on this version are verified in WP8 before the adapter is accepted;
- spell the MongoDB driver pin `pymongo==4.18.1`: the `srv` extra no longer exists and `dnspython` is a base dependency, so SRV resolution is unchanged;
- accept the transitive `opentelemetry-semantic-conventions` prerelease-format version frozen in `uv.lock` as a documented exception to the no-prerelease rule, because upstream publishes only `bN` versions and it enters only through `livekit-agents`.
- accept the transitive frontend package `gensync@1.0.0-beta.2` frozen in `package-lock.json` on the same basis: it enters only through the Babel toolchain of `@vitejs/plugin-react` and upstream has never published a non-prerelease version;
- accept `uv_build==0.12.18` as the backend build backend: it only makes the project's own `src/voice_agent` layout installable and matches the approved uv version;
- record `sarvamai==0.1.34` declaring no licence (PyPI metadata, package dist-info, and GitHub repository) as an open risk: the pin stays, and the licence terms must be confirmed with Sarvam before WP9 enables the TTS adapter.

Reason:

The WP1 compatibility gate (`docs/14` §7) found the approved direct pins unresolvable as written. The changes are the minimal evidence-backed corrections; no provider, model, or other direct pin changes.

Detailed design: `docs/13-dependency-and-version-matrix.md` §3, §4, §9. Evidence: `outputs/evidence/wp01-scaffold/`.

### Decision 069: WP2 contract clarifications

Status: Approved (user, 2026-09-28)

Decision:

- **Cost currency:** stored cost records normalize to USD per Decision 016; INR is a display/report conversion. The INR planning figures in `docs/15` (for example INR 27.925/session and the budgets) are unchanged; `docs/15` wording that called INR "normalized" is corrected;
- **Interruption while thinking:** the interruption candidate rule (≥250 ms confirmation, canonical order of Decision 067 S9) also applies after a turn is committed and before any agent audio plays, at the normal 0.5 activation threshold; a confirmed candidate cancels the stale response and the new utterance becomes the next turn.

Reason:

The WP2 QA gate found `docs/15` conflicting with Decision 016 on the normalized currency, and the docs silent on barge-in before playback starts.

Detailed design: `docs/05-agent-worker-orchestration.md` §16, `docs/15-phase0-pricing-and-cost-model.md`. Evidence: `outputs/evidence/wp02-contracts-mock/`.

### Decision 070: limited-sharing remote deployment exception (2026-10-07)

The user has decided, as project owner, to deploy the Phase 0 application to a remote host (Render) and
share the URL directly with one or two named testers, with no application login screen. This is a narrow,
explicit exception to the standing rule at line 666 ("one local/trusted R&D user; no public deployment or
application login") and line 1330 ("prohibit public deployment until authentication is separately
approved"). It does not change those rules for any broader or indefinite use; it authorizes exactly this
limited-sharing arrangement.

Conditions attached to this exception:

- the URL is treated as equivalent to a shared credential: the user is responsible for only giving it to the
  one or two intended testers, and must assume anyone who obtains the URL (including through accidental
  forwarding) can use the deployed application without further verification;
- a hard daily spend cap of **INR 200.00**, shared across all usage, is enforced in the application itself
  (new session creation is refused once the day's recorded cost crosses the cap, with a safe, non-alarming
  message to the user) — this is a new safety control introduced specifically for this exception, not part of
  the original Phase 0 local-use design;
- the control API's existing loopback-only bind guard is relaxed only through an explicit, separate
  deployment configuration flag (never by removing or silently bypassing the guard's default behavior for
  local/dev use);
- no application-level authentication, accounts, or session ownership model is introduced by this decision;
  this exception is about reachability (a remote host instead of 127.0.0.1) and a spend cap, not about
  identity or access control;
- this exception does not change the R&D data/retention/credential boundaries already approved elsewhere in
  this document (sections on MongoDB Atlas, secrets, and production-review requirements remain as written);
- revisiting this decision (narrowing it, extending it, or replacing it with real authentication) requires a
  fresh explicit decision from the user, the same as this one.

Detailed design: deployment configuration and the daily spend cap are implemented as part of a dedicated
deployment task; see `outputs/evidence/` for its evidence once complete.

**Addendum (2026-10-08):** after deployment, the user was explicitly asked whether the residual gap
flagged above — no session ownership means any tester with the URL can list and read every other
tester's sessions, transcripts, and operations (though not raw secrets or other testers' spend-cap
standing) — was acceptable for this limited-sharing arrangement, or whether a lightweight fix should be
built. The user's explicit answer: leave it as-is, no fix. This is the same no-identity/no-access-control
posture already stated above, now confirmed with the concrete residual risk spelled out rather than left
implicit. Revisiting this still requires a fresh explicit decision from the user, same as the rest of
Decision 070.

