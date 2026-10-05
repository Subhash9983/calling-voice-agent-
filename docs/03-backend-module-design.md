# Backend Module Design

Status: Approved for Phase 0 R&D  
Authority: Decision 027 with Decisions 042, 051, 052, 054, 065, and 067 amendments  
Scope: Python backend module and process boundaries  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `02-database-design.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Backend language: Python  
Runtime style: Modular monolith with two long-running processes  
Primary transport: LiveKit behind a replaceable adapter  
Database: One shared R&D MongoDB Atlas Flex database through explicit PyMongo Async repositories

## 1. Purpose

This document defines the Python backend module boundaries for the browser-based voice-agent R&D sandbox. It does not authorize implementation by itself. Interfaces, workflows, provider selections, and implementation milestones are approved separately before code is written.

The baseline connects a browser voice session to a general conversation LLM. The existing Python knowledge engine can later replace the general LLM through the same conversation-engine port.

## 2. Runtime architecture

```text
React browser UI
      |
      +---- FastAPI control API
      |
      +---- LiveKit room
                 |
                 +---- Python agent worker
                              |
                              +---- transport -> STT -> conversation -> TTS
```

Approved long-running processes:

1. `control-api`: FastAPI control-plane process.
2. `agent-worker`: LiveKit realtime agent process.

Approved non-daemon entry point:

3. `maintenance`: explicit, scoped administrative commands/jobs.

The processes share contracts, domain logic, ports, repositories, provider adapters, event logic, costing, privacy rules, and settings from one Python package. Realtime audio work never runs inside an ordinary FastAPI request handler.

## 3. Architecture style

Phase 0 uses a modular monolith:

- one backend repository;
- one shared Python package;
- two long-running processes;
- explicit module boundaries;
- replaceable provider adapters;
- no independent microservice deployment per component;
- no provider SDK types inside domain or orchestration logic.

This approach keeps R&D setup small while preserving interfaces that allow later extraction or replacement.

## 4. Dependency direction

```text
Control API / Agent Worker entry points
                 |
                 v
        Application + Orchestration
                 |
                 v
        Domain + Contracts + Ports
                 ^
                 |
   Adapters + MongoDB implementations
```

Rules:

- domain modules do not import FastAPI, LiveKit, PyMongo, or provider SDKs;
- orchestration depends on ports, not concrete providers;
- adapters implement ports and translate provider-specific values into internal contracts;
- adapters do not call one another directly;
- the browser connects only to FastAPI and LiveKit;
- MongoDB and provider credentials remain backend-only;
- persistence models are never returned directly to the browser;
- provider-specific SDK objects stop at the adapter boundary.

## 5. `control_api`

Purpose: FastAPI HTTP control plane.

Responsibilities:

- create, read, and end a voice session;
- issue short-lived LiveKit browser credentials;
- list approved agent configurations for R&D;
- read safe session summaries and paginated timelines;
- submit feedback;
- grant, revoke, and read safe consent status;
- map internal errors to browser-safe responses;
- expose health and readiness endpoints.

Background tasks (run inside the `control-api` process, never inside an ordinary request handler):

- `session_reconciler`: every 5 seconds, processes sessions whose `next_reconcile_at` is due (connection/termination deadlines, expired worker leases, expired `recovery_authorization` ownership leases, passed `recovery_authorization.recovery_deadline_at`, and the idle/maximum-duration backstop); it fences expired leases, dispatches at most one higher-epoch worker-crash recovery attempt, and terminalizes `ending` or exhausted sessions (Decision 067; full contract in `05-agent-worker-orchestration.md` §21).

Boundaries:

- does not perform realtime STT, LLM, or TTS work;
- does not expose provider credentials, persistence documents, internal diagnostic details, or restricted pricing;
- accepts only approved Pydantic API contracts;
- uses application services and repository ports rather than direct MongoDB calls from route handlers.

## 6. `agent_worker`

Purpose: LiveKit realtime-agent executable.

Responsibilities:

- accept an authorized LiveKit job/session;
- load the exact session and immutable agent configuration;
- build approved adapters through the provider registry;
- start and stop the session orchestrator;
- propagate shutdown and cancellation;
- ensure terminal session, turn, operation, usage, cost, and error finalization.

Boundaries:

- contains process/bootstrap code, not provider-specific conversation policy;
- does not construct arbitrary adapters from browser input;
- does not bypass repositories or domain state transitions.

## 7. `orchestration`

Purpose: control the end-to-end realtime voice pipeline.

```text
Transport audio
    -> Speech activity detector
    -> Turn manager
    -> STT
    -> Conversation engine
    -> Response segmenter
    -> TTS
    -> Transport playback
```

Responsibilities:

- own the session pipeline lifecycle;
- coordinate the active turn and provider operations;
- apply configured timeouts and retry limits;
- own hierarchical cancellation and cancellation generations;
- reject late or superseded asynchronous results;
- initiate approved fallback behaviour;
- coordinate state transitions;
- record operation, event, error, usage, latency, and cost evidence;
- finalize resources idempotently.

Boundaries:

- uses ports only;
- does not import provider SDK classes;
- does not contain HTTP-route logic;
- does not perform unrestricted MongoDB queries.

## 8. `speech_activity` and `turn_management`

Purpose: detect local microphone speech activity and own user/agent turn behaviour without giving a provider or transport framework control of application turns.

Responsibilities:

- run local Silero VAD behind a provider-neutral `SpeechActivityPort`;
- emit authoritative speech-start/speech-stop activity with monotonic audio timing;
- keep Silero and LiveKit plugin types inside the adapter;
- use Deepgram speech/endpoint signals only as advisory diagnostics;
- apply endpointing decisions;
- open and finalize one turn per user utterance;
- accept only final transcripts as durable user intent;
- manage barge-in and playback cancellation;
- suppress sub-250 ms false interruption candidates without cancelling playback;
- track agent activity state;
- preserve generated, synthesized, and spoken text separately;
- create a new turn for a new utterance after an accepted interruption.

The Turn Manager, not Silero, Deepgram, or LiveKit, remains authoritative for opening/closing turns and accepting interruptions. The initial R&D defaults are: 16 kHz mono VAD input, 0.5 activation threshold, 50 ms minimum speech, 500 ms prefix padding, approximately 550 ms local silence detection, 250 ms minimum accepted interruption, a 0.7 activation threshold for interruption candidates while agent audio is playing (`vad.playback_activation_threshold`), and a total endpoint deadline of 700 ms measured from the last detected speech frame, tunable up to a 1,000 ms cap (values above 1,000 ms require latency-budget re-approval, Decision 067). An accepted interruption never resumes stale audio automatically; an accepted interruption with no usable transcript follows the approved clarification fallback. Preemptive generation is disabled.

## 9. `response_segmentation`

Purpose: transform streamed conversation output into speakable TTS segments.

Responsibilities:

- buffer incomplete streamed text;
- detect sentence and phrase boundaries;
- remove or transform non-speakable Markdown/symbols;
- handle numbers and abbreviations through versioned rules;
- support Hindi/Hinglish-friendly segmentation;
- validate each complete candidate and emit the canonical `conversation.segment_ready` event; conversation-provider adapters emit text deltas/completion but never this event;
- preserve text submitted to TTS separately from raw generated text;
- cancel queued segments after interruption or turn/session cancellation.

Boundaries:

- does not synthesize audio;
- does not call the conversation provider;
- does not own playback.

## 10. `contracts`

Purpose: shared strict Pydantic contracts.

Contains:

- API requests and responses;
- session, activity, turn, and operation states;
- normalized event envelopes;
- provider-port inputs and outputs;
- normalized usage units;
- error taxonomy;
- cost contracts;
- consent and retention contracts.

Contract rules:

- forbid unknown fields at trust boundaries;
- preserve schema versions;
- use canonical IDs, UTC timestamps, and approved enums;
- keep browser, domain, adapter, and persistence contracts separate.

## 11. `domain`

Purpose: provider-independent entities and business rules.

Contains domain representations for:

- agent configuration;
- voice session;
- conversation turn;
- provider operation;
- session event;
- cost entry;
- user feedback;
- error event;
- consent record.

The domain has no dependency on FastAPI, LiveKit, MongoDB, or provider SDKs.

## 12. `ports`

Purpose: provider and infrastructure interfaces used by application/core modules.

Initial ports:

- `TransportPort`;
- `SpeechActivityPort`;
- `STTPort`;
- `ConversationEnginePort`;
- `TTSPort`;
- repository interfaces for all nine core collections and five evaluation collections;
- shared `EventSequenceAllocator` repository port for atomic durable-event ordering;
- normalized event writer;
- cost calculator and rate lookup;
- clock and ID generator;
- consent authorization;
- retention/deletion interfaces.

Ports define normalized behaviour and cancellation semantics. They do not expose external SDK types.

## 13. `transport_adapters`

Initial structure:

```text
transport_adapters/
    livekit/
```

LiveKit adapter responsibilities:

- separate backend control-plane dispatch/token/cleanup from worker session transport;
- use explicit named-agent room dispatch and manage participants, tracks, and publications internally;
- receive user audio and publish agent audio;
- send and receive approved bounded versioned data events;
- stop playback;
- normalize connection/reconnection/quality events;
- expose transport usage required for costing;
- issue least-privilege short-lived browser tokens without persisting/logging them;
- compensate partial session-creation failures and perform idempotent cleanup;
- prevent LiveKit SDK types from escaping the adapter ports.

The exact approved contract, topic policy, reconnect behaviour, database mapping, and Phase 0 exclusions are defined in `docs/06-livekit-transport-adapter.md`.

Later R&D adapters may implement Pipecat/Daily, Vapi, or custom WebRTC without changing orchestration contracts.

## 14. `stt_adapters`

Initial structure:

```text
stt_adapters/
    mock/
    deepgram/
    sarvam/             # first later benchmark challenger
```

Responsibilities:

- accept internal audio frames and approved language/audio configuration;
- perform provider-specific audio conversion inside the adapter;
- normalize partial and final transcripts;
- normalize language, timestamps, confidence, usage, request IDs, and errors;
- respond to cancellation;
- prevent provider SDK objects from escaping.

The first real adapter is Deepgram Nova-3 Multilingual. Sarvam Saaras realtime/codemix is the first later benchmark challenger. A mock adapter must pass the same lifecycle/event contract before paid integration. The approved language, transcript, keyterm, endpointing, retry, measurement, cost, security, and exclusion rules are defined in `docs/07-stt-adapter-and-baseline.md`.

## 15. `conversation_adapters`

Initial structure:

```text
conversation_adapters/
    mock/
    openai/
    grok/                # first later benchmark challenger
    knowledge_engine/    # later
```

Responsibilities:

- accept normalized conversation history and final user intent;
- apply the approved system-instruction/tool-set version;
- stream normalized text segments;
- normalize tool requests, finish reason, usage, request IDs, cancellation, and errors;
- keep provider message classes inside the adapter.

Phase 0 uses OpenAI GPT-6 Luna with reasoning `none`, streaming, a 250-output-token cap, and no tools/search/provider conversation storage. Grok 4.7 low is the first later benchmark challenger. The existing Python knowledge system later implements or composes behind the same `ConversationEnginePort`. The exact approved request/history/context/segmentation/cancellation/retry/cost/security rules are defined in `docs/08-conversation-adapter-and-llm-baseline.md`.

## 16. `tts_adapters`

Initial structure:

```text
tts_adapters/
    mock/
    sarvam/
    elevenlabs/          # first later benchmark challenger
```

Responsibilities:

- accept speakable text segments and approved voice/audio settings;
- stream normalized output audio;
- emit first-audio and completion evidence;
- normalize usage, request IDs, cancellation, and errors;
- cancel unplayed audio promptly.

The Phase 0 baseline is Sarvam Bulbul v3 with `priya`, streaming mono 24 kHz Linear16 PCM, a 500-character segment cap, and Hindi/Hinglish/English routing. ElevenLabs Flash v2.5 is the first challenger. A mock adapter supports contract/orchestration tests. Full rules are in `docs/09-tts-adapter-and-voice-baseline.md`.

## 17. `provider_registry`

Purpose: construct only approved adapter combinations.

Responsibilities:

- map immutable agent configuration to transport, STT, conversation, and TTS adapters;
- validate exact provider/model/voice identifiers;
- validate adapter versions and provider-specific allowlisted options;
- select mock versus real adapters by approved server configuration;
- reject unsupported or incomplete combinations;
- expose safe adapter identity for evidence and UI labels.

Large provider-selection `if/else` chains are prohibited in orchestration logic.

## 18. `persistence`

Initial structure:

```text
persistence/
    mongodb/
        client/
        codecs/
        validators/
        indexes/
        repositories/
```

Responsibilities:

- own the long-lived PyMongo Async client lifecycle;
- map Pydantic persistence documents to/from BSON;
- isolate Decimal128, ObjectId, UUID-string, enum-string, and UTC conversions;
- implement nine core and five evaluation repository interfaces;
- implement `EventSequenceAllocator` as `findOneAndUpdate` with `$inc: {event_sequence_counter: 1}` and return the updated value;
- isolate worker lease compare-and-set updates (matching assignment generation, `worker_instance_id`, `livekit_job_id`, `writer_epoch`, and `lease_revision`, and requiring `lease_expires_at > $$NOW`) from business `state_revision` updates;
- apply optimistic revision checks;
- execute only approved queries and index definitions;
- apply development/R&D environment filters;
- provision strict collection validators after separate provisioning approval;
- calculate approved R&D `expires_at` values.

Conversation/orchestration modules never issue direct MongoDB queries.

## 19. `events_and_latency`

Purpose: normalize realtime/durable events and measurements.

Responsibilities:

- generate durable event IDs and request authoritative session sequence numbers from the shared persistence allocator;
- publish browser-safe realtime events;
- persist only the approved durable event subset;
- exclude audio chunks, partial transcripts, LLM token segments, and TTS frames from MongoDB;
- measure latency using a monotonic runtime clock;
- persist UTC event evidence;
- aggregate turn/session latency summaries;
- use bounded non-blocking persistence behaviour while reliably retrying terminal evidence.

No external observability vendor is selected by this module decision.

## 20. `costing`

Purpose: reproduce component/provider/session costs.

Responsibilities:

- normalize provider-native usage;
- perform Decimal/Decimal128 calculations;
- apply versioned rate-card and FX evidence;
- create immutable calculation runs;
- calculate successful, failed, cancelled, retry, and fallback cost when usage is known;
- distinguish estimated, reported, and reconciled evidence;
- maintain turn/session/operation cost summaries;
- prevent allocation-only rows from double-counting session totals.

Pricing definitions are centralized and do not appear as constants across provider/orchestration modules.

## 21. `privacy_and_retention`

Purpose: consent, redaction, and R&D lifecycle controls.

Responsibilities:

- verify active granular consent before recording;
- keep recording off when consent cannot be verified;
- calculate versioned 30-day R&D expiries;
- trigger immediate benchmark-asset deletion after revocation;
- verify asset deletion before consent evidence becomes deletion eligible;
- apply approved redaction helpers;
- protect active agent configurations from retention cleanup.

Phase 0 uses required root `expires_at` fields plus ordinary expiry indexes and a bounded scheduled child-first cleanup job, not MongoDB TTL indexes. It runs at least daily, handles at most 100 sessions per batch, verifies child deletion before deleting the parent session, and retries idempotently.

## 22. `security`

Purpose: backend-only secrets and safe data exposure.

Responsibilities:

- load environment-based settings;
- resolve approved credential references;
- redact secrets from serialization and logs;
- map internal errors to browser-safe messages;
- restrict persistence/internal/pricing fields from API responses;
- prevent MongoDB/provider credentials from entering the React bundle.

The approved R&D setup uses one dedicated MongoDB database user with `readWrite` on the selected database only.

## 23. `maintenance`

Purpose: explicit scoped R&D administration.

Commands/jobs may:

- create/update approved MongoDB validators;
- create approved indexes;
- seed non-secret agent configurations;
- verify schema versions and data consistency;
- dry-run versioned migrations;
- execute scoped retention/deletion verification;
- produce a bounded, read-only local operational report (`report`, default 20 sessions,
  max 100, optional `--session-id`) and a retention-evidence report (`retention-report`,
  what is scheduled, what is due, and any child record still missing an `expires_at`),
  both WP11 deliverables; `--output` writes a new file and never overwrites, and no
  report data leaves the machine.

The session reconciler is not a `maintenance` command; it runs as a `control_api` background task (§5).

Safety rules:

- no command may default to the complete shared database;
- environment and target scope are mandatory;
- destructive migrations require separate approval;
- R&D has no database backup or restore guarantee;
- an unrestricted drop-database command is not supplied.

## 24. `evaluation`

Purpose: implement the approved provider-independent evaluation catalog and benchmark providers/configurations after a stable baseline exists.

Responsibilities:

- immutable datasets and cases;
- evaluation runs/results;
- WER, latency, cost, reliability, and human-rating metrics;
- provider matrix execution;
- reproducible reports.

The collection contracts and exact 100-case catalog are approved. Work package 5 creates their validators, indexes, and repositories; work package 12 implements the runner, scoring integration, and reports. The runner is not yet implemented.

## 25. Repository structure

The canonical repository layout is `14-phase0-implementation-execution-plan.md` §4; this document does not keep a separate copy. The backend package `voice_agent/` contains one package per module in §5–§24, including `speech_activity/` (§8) alongside `turn_management/`.

The implementation plan is approved in `14-phase0-implementation-execution-plan.md`; creating application directories/code still begins only when implementation work is explicitly started, and must follow that plan.

## 26. Testing boundaries

- unit tests cover domain, segmentation, state transitions, costing, retention, redaction, and repository mapping;
- contract tests run every provider adapter against the same port expectations;
- integration tests cover MongoDB repositories/validators and LiveKit/provider sandboxes where configured;
- end-to-end tests cover browser-to-agent flows with mock adapters before real-provider tests;
- no test requires production credentials or production data;
- shared-R&D destructive cleanup is always narrowly scoped.

## 27. Deferred decisions

- credential provisioning and provider-console permission mechanics;
- provider-specific safe-option/metadata schemas beyond the approved baseline fields;
- exact pronunciation dictionary contents and later blind voice-test results;
- implementation details that exceed the approved FastAPI, LiveKit, worker queue, and backpressure contracts;
- Atlas network/IP access provisioning;
- production topology, users, authentication, security, backups, retention, and scaling;
- full knowledge-engine and telephony implementation;
- challenger provider SDKs and alternative transport implementations.

Each deferred decision requires discussion and explicit approval before implementation.

## 28. Acceptance criteria

The module design is ready to guide implementation planning when:

- every responsibility has one clear owner;
- provider SDK types cannot cross adapter boundaries;
- API and worker lifecycles are independent;
- orchestration owns cancellation and late-result rejection;
- turn management owns interruption and delivery state;
- repositories are the only MongoDB access path;
- browser-visible contracts exclude internal/restricted data;
- mock adapters can exercise the full pipeline;
- future knowledge-engine and transport/provider alternatives fit the approved ports;
- deferred choices remain unimplemented until approved.
