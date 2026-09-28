# Phase 0 Implementation Execution Plan

Status: Approved for Phase 0 R&D  
Authority: Decision 038 with subsequent amendments through Decision 067  
Scope: Ordered implementation of the local single-user browser voice-agent baseline  
Depends on: `00-voice-agent-master-plan.md` through `13-dependency-and-version-matrix.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

## 1. Purpose

This document defines the exact implementation order for the approved Phase 0 browser voice agent. It converts the architecture and provider decisions into bounded work packages with prerequisites, deliverables, validation evidence, stop/go gates, and cost controls.

Approval of this plan does not itself install dependencies, create application code, provision cloud resources, or make paid provider calls.

Core rules:

> Complete and verify one bounded milestone before enabling the next external dependency.

> Exercise the provider-independent pipeline with mocks before spending money on provider calls.

> A failed compatibility or acceptance gate stops progression; it does not authorize a silent version, provider, architecture, or security change.

> Every real provider test records configuration identity, latency, usage, cost evidence, and safe failure evidence.

## 2. Authority and conflict order

Implementation follows these documents:

1. `00-voice-agent-master-plan.md` and its decision log;
2. `01-system-contracts.md`;
3. the component-specific approved document;
4. this execution plan;
5. implementation notes and generated test evidence.

If prose from an earlier section conflicts with a later approved numbered decision or detailed document, the later approved decision and detailed contract win. A material conflict is documented and returned for approval before implementation continues.

## 3. Phase 0 completion boundary

Phase 0 is complete only when one browser user can:

- start a session through the FastAPI control API;
- join the approved LiveKit room using a short-lived scoped credential;
- speak Hindi, Hinglish, or English;
- receive an accepted Deepgram final transcript;
- obtain a streamed OpenAI text response under the approved prompt policy;
- hear Sarvam Bulbul v3 voice `priya` in the browser;
- interrupt stale agent speech safely;
- end the session idempotently;
- inspect normalized transcript, event, operation, latency, error, usage, and cost evidence;
- pass the approved Phase 0 evaluation gates.

Knowledge-graph integration, challenger providers, telephony, public deployment, multi-user authentication, production operations, and ordinary audio storage are not Phase 0 completion requirements.

## 4. Canonical repository layout

The initial repository uses two application roots and one documentation/evidence boundary:

```text
backend/
    pyproject.toml
    uv.lock
    .python-version
    src/
        voice_agent/
            control_api/
            agent_worker/
            orchestration/
            speech_activity/
            turn_management/
            response_segmentation/
            contracts/
            domain/
            ports/
            provider_registry/
            transport_adapters/
            stt_adapters/
            conversation_adapters/
            tts_adapters/
            persistence/
            events_and_latency/
            costing/
            privacy_and_retention/
            security/
            maintenance/
            evaluation/
    tests/
        unit/
        contract/
        integration/
        end_to_end/

frontend/
    package.json
    package-lock.json
    .nvmrc
    src/
        api/
        audio/
        components/
        config/
        contracts/
        livekit/
        session/
        telemetry/
    tests/

docs/
outputs/
```

Rules:

- `backend` and `frontend` have independent process lifecycles;
- Python provider SDKs never enter browser code or shared browser contracts;
- generated environments, caches, build output, coverage, browser traces, and local evidence are ignored unless a sanitized artifact is intentionally retained;
- real secrets never enter any repository directory, including `outputs`;
- `outputs` contains only sanitized, bounded R&D reports/evidence and is not a durable production store;
- no additional service/package root is introduced without a demonstrated need and approval.

## 5. Standard milestone workflow

Every work package follows the same cycle:

1. confirm its approved inputs and exclusions;
2. create only the required files/dependencies;
3. implement the smallest provider-independent vertical slice;
4. run unit and contract tests without external credentials;
5. run the package/runtime compatibility checks;
6. enable one real external dependency when authorized;
7. capture sanitized evidence, measured latency, usage, and cost;
8. compare results with the exit gate;
9. stop on failure or record completion before starting the next package.

An implementation change is not accepted merely because the application starts. Its tests, evidence, security boundary, and documented exit gate must also pass.

## 5A. Milestone and work-package crosswalk

This table is the single mapping between the milestones in `00-voice-agent-master-plan.md` §9 and the work packages in this plan.

| Milestone (doc 00 §9) | Work package(s) |
|---|---|
| M0 Repository and dependency skeleton | WP0 preflight and repository safety; WP1 scaffold, runtimes, and exact locks |
| M1 Contracts and mock vertical slice | WP2 contracts and mock vertical slice |
| M2 Configuration and control API | WP3 configuration and secret validation; WP4 FastAPI control-plane skeleton |
| M3 MongoDB persistence foundation | WP5 MongoDB Atlas persistence |
| M4 Browser and LiveKit connection | WP6 LiveKit browser transport |
| M5 Local speech activity and STT | WP7 Deepgram streaming STT (with local Silero VAD) |
| M6 General LLM | WP8 OpenAI conversation adapter |
| M7 TTS | WP9 Sarvam streaming TTS |
| M8 Natural turn-taking and recovery | WP10 end-to-end orchestration and natural turn-taking |
| M9 Cost and observability | WP11 durable observability, latency, and cost |
| M10 Evaluation and provider benchmarking | WP12 evaluation harness and Phase 0 release gate |

WP12 covers only the baseline evaluation and release gate. The challenger-provider benchmarking listed under M10 is outside WP12 and deferred (see §23); it needs its own approval and budget.

## 6. Work package 0 — preflight and repository safety

### Deliverables

- inventory existing repository files and preserve unrelated/user-owned changes;
- verify that no real secret exists inside the repository or OneDrive project path;
- establish ignore rules for environments, caches, builds, logs, coverage, Playwright artifacts, and external secret files;
- record the approved runtime/provider/configuration matrix;
- verify the external secret-file path can be referenced without reading values into logs;
- define sanitized evidence naming by milestone and timestamp.

### Validation

- repository status is captured before changes;
- secret-path checks report only presence/missing state, never values;
- no destructive cleanup is run;
- ignored local artifacts cannot be accidentally staged by ordinary workflows.

### Exit gate

Proceed only when the workspace boundary is known, existing user work is preserved, and secret placement complies with `12-configuration-and-secrets.md`.

Cost: INR 0 incremental software/API cost.

## 7. Work package 1 — scaffold, runtimes, and exact locks

### Deliverables

- create the canonical `backend` and `frontend` skeletons;
- pin CPython, uv, Node.js, npm, and the approved direct application dependencies;
- resolve and commit `uv.lock` and `package-lock.json`;
- record exact compatible companion lint/test versions and transitive evidence;
- add minimal lint, type-check, unit-test, and build command surfaces;
- create empty importable modules without provider behaviour.

### Planned validation commands

```text
uv lock --check
uv sync --locked
uv run ruff check .
uv run mypy src
uv run pytest
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

Exact script names become authoritative in the committed manifests. They must retain these capabilities even if a platform-specific command spelling requires evidence-backed adjustment.

### Exit gate

- dependency resolution passes on the R&D Windows host;
- the `livekit-agents[silero]` VAD dependencies, including `onnxruntime`, resolve to prebuilt Windows wheels for CPython 3.12 and the Silero model loads locally without a source build;
- no unapproved source-build toolchain, prerelease, yanked package, mutable Git dependency, or incompatible licence appears;
- backend imports and frontend production build pass;
- lockfiles reproduce the environment.

Any incompatible approved direct pin is returned with evidence for a version decision. It is not silently changed.

Cost: INR 0 package licence/subscription cost; network transfer and local disk usage are operational resources.

## 8. Work package 2 — contracts and mock vertical slice

### Deliverables

- implement approved Pydantic domain/contracts without provider SDK types;
- implement ports for transport, STT, conversation, TTS, repositories, events, and costing;
- implement `SpeechActivityPort` and deterministic mock speech-start/speech-stop/interruption events;
- implement deterministic mock adapters;
- implement bounded queues, generation IDs, cancellation primitives, and normalized failures;
- run an in-process mock flow from synthetic audio event through transcript, response text, synthesized test audio, playback acknowledgement, and finalization.

### Tests

- serialization/versioning and restricted-field tests;
- state-transition and invalid-transition tests;
- cancellation and late-result rejection tests;
- queue/backpressure tests;
- response segmentation tests for Hindi, Hinglish, and English;
- retry classification, usage normalization, and cost arithmetic fixtures;
- secret/redaction fixtures.

### Exit gate

The complete pipeline works with mock adapters, stale generations cannot publish output, and no core/domain module imports a provider SDK.

Cost: INR 0 provider cost.

## 9. Work package 3 — configuration and secret validation

### Deliverables

- implement Pydantic Settings v2 bootstrap settings;
- read the secret-file location only through `VOICE_AGENT_SECRETS_FILE`;
- enforce the approved precedence and public/private configuration split;
- implement safe readiness validation and redacted configuration diagnostics;
- implement immutable versioned `agent_config` validation;
- reject browser/provider overrides outside the approved allowlist.

### Tests

- missing file, missing key, malformed URI, extra field, and disabled-provider cases;
- redaction snapshots proving values cannot appear in logs/errors/models;
- browser bundle inspection proving no secret-bearing setting is included;
- readiness failure exposes only normalized safe codes.

### Exit gate

Processes fail safely for invalid enabled configuration, start with mock configuration, and never disclose secret values.

Cost: INR 0 incremental configuration-management service cost.

## 10. Work package 4 — FastAPI control-plane skeleton

### Deliverables

- independent FastAPI application lifecycle;
- `/health/live` and `/health/ready`;
- versioned `/api/v1` routing and approved error envelope;
- request/correlation ID middleware and safe structured logging;
- approved route schemas with repository/LiveKit operations initially mocked;
- local/trusted access guard that prevents accidental public-mode startup.

### Tests

- API schema/validation and safe error responses;
- idempotency-conflict behaviour with mock repositories;
- restricted fields cannot be submitted or returned;
- no audio/provider execution occurs inside request handlers;
- liveness remains distinct from dependency readiness.

### Exit gate

The API starts independently, contract tests pass, and every approved route has either bounded working behaviour or an explicit safe not-ready dependency state.

Cost: INR 0 provider cost.

## 11. Work package 5 — MongoDB Atlas persistence

### Deliverables

- PyMongo Async connection lifecycle with approved timeouts;
- Pydantic validation boundary and explicit repositories;
- migrations/bootstrap for the nine approved core collections and five approved evaluation collections;
- approved MongoDB validators and launch indexes, including the evaluation validators/indexes from `16-evaluation-database-schema.md`;
- explicit evaluation repositories for dataset, case, run, result-attempt, and human-rating persistence; runner/scoring integration remains in work package 12;
- repository idempotency, optimistic revision, pagination, safe projections, and reference checks;
- shared atomic `EventSequenceAllocator` and unique session-event ordering tests across API, worker, and maintenance writers;
- independent worker `lease_revision` heartbeat compare-and-set path that cannot bump business `state_revision`;
- terminal retention-anchor propagation, required `expires_at` fields/indexes, and bounded daily child-first cleanup in batches of at most 100 sessions;
- nonterminal session reconciliation indexes and repository operations;
- health readiness integration and persistence-failure normalization.

### Tests

- repository contract tests for create/read/update/finalize paths;
- unique/idempotency/index tests;
- concurrent event-allocation tests prove unique monotonic session-local numbers with allowed gaps and no writer-local counters;
- concurrent heartbeat/state tests prove lease renewal cannot cause a business-state revision conflict;
- cursor pagination and bounded-query tests;
- validation rejection and safe projection tests;
- transient database failure proves the realtime conversation is not synchronously dependent on every diagnostic write;
- destructive tests use only uniquely scoped R&D fixtures.
- cleanup dry-run, child-count verification, partial failure, idempotent retry, and parent-preservation tests.

### Exit gate

All nine core repositories and five evaluation repositories satisfy their contracts against the R&D database, validators/indexes match the approved design, and no ODM/Motor/raw browser database access exists.

Cost: Atlas Flex and network usage are measured external costs; no backup cost is approved for R&D.

## 12. Work package 6 — LiveKit browser transport

### Deliverables

- LiveKit control-plane adapter for room/token/dispatch/cleanup;
- worker session-transport adapter;
- short-lived room/participant-scoped browser token path;
- separate React browser UI with microphone permission, connect/disconnect, session state, transcript/status surfaces, and audio playback;
- browser microphone constraints for echo cancellation, noise suppression, automatic gain control, and mono capture, with 48 kHz/20 ms normalized worker intake;
- explicit opaque agent identity at job acceptance, 20-message/second browser aggregate limit with burst 40, and four-per-second playback-progress limit;
- 24 kHz mono agent publication through a 200 ms `AudioSource` queue with tested `clear_queue()` interruption behaviour;
- normalized `va.*.v1` data topics and payload limits;
- durable termination request plus targeted reliable `va.control.v1` fast signal;
- test-tone/two-way media verification without AI providers;
- reconnect, terminal disconnect, and cleanup evidence.

### Tests

- token scope/expiry and non-persistence;
- duplicate dispatch/worker ownership protection;
- browser microphone denial and device loss;
- connect, reconnect, disconnect, cleanup, and stale-participant behaviour;
- missing end packet/no worker still reaches a terminal session through reconciliation;
- audio/data smoke test and browser build/security inspection.
- echo/self-interruption suppression, aggregate browser-message throttling, and buffered-audio flush after accepted interruption.

### Exit gate

Browser microphone audio reaches the worker and worker test audio plays in the browser reliably without STT, LLM, or TTS credentials.

Cost: LiveKit usage is metered and recorded according to the dated rate card before real cloud testing.

## 13. Work package 7 — Deepgram streaming STT

### Deliverables

- local Silero VAD isolated behind `SpeechActivityPort`, using 16 kHz mono frames, with the Silero model prewarmed (loaded once) at worker startup so that the first session does not pay model-load latency;
- authoritative local speech-start/speech-stop events and Turn Manager endpoint deadline;
- official Deepgram SDK v7 isolated in the STT adapter;
- Nova-3 multilingual streaming configuration;
- normalized partial/final/speech events;
- transcript acceptance, bounded keyterm-capability handling, timeout, retry, cancellation, and close behaviour;
- transcript UI and operation/timing/usage/cost evidence.

### Tests

- adapter contract with recorded non-sensitive fixtures/mocks;
- Hindi, Hinglish, and English live samples;
- names, numbers, silence, noise, empty transcript, provider timeout, cancellation, and late event cases;
- verify that Deepgram speech/endpoint signals remain advisory and cannot close a turn or interrupt playback;
- verify the approximately 550 ms VAD silence window plus remaining endpoint wait produces a 700 ms total initial deadline rather than 1,250 ms;
- only an accepted durable final transcript can authorize conversation generation.
- baseline live configuration keeps the keyterm list empty; keyterm mapping is tested with mocks/fixtures and no paid keyterm feature is enabled without separate approval.

### Exit gate

Approved language samples produce usable final transcripts, failure/cancellation behaviour is safe, and STT latency/usage/cost are explainable.

Cost: no live STT call is made until the dated per-minute rate and test budget are recorded.

## 14. Work package 8 — OpenAI conversation adapter

### Deliverables

- official OpenAI SDK isolated in the conversation adapter;
- Responses API HTTP SSE streaming;
- approved `phase0_general_voice_assistant_v1` instruction and checksum;
- bounded history/input/output, per-segment pre-TTS validation, incomplete-tail buffering, cancellation, timeout, and normalized usage/cost;
- deterministic greeting and operational fallbacks remain application-owned.

### Tests

- transcript-to-response cases across Hindi, Hinglish, and English;
- capability honesty, injection resistance, no fabricated tools/knowledge, and no secret disclosure;
- stream cancellation and late token rejection;
- normal final-tail release, maximum-token incomplete-tail discard, exact `fallback.response_truncated.v1` when nothing meaningful was delivered, and no fabricated closure after partial delivery;
- exactly one authorized response generation per accepted user turn;
- generated versus speakable/delivered text evidence remains separated.

### Exit gate

Accepted transcripts reliably yield compliant streamed speakable text, cancellation works, and token/latency/cost evidence reconciles.

Cost: no live LLM call is made until dated input/output token rates and a bounded test budget are recorded.

## 15. Work package 9 — Sarvam streaming TTS

### Deliverables

- official Sarvam SDK isolated in the TTS adapter;
- Bulbul v3 with approved voice `priya`;
- safe text normalization and bounded streaming segments;
- normalized audio frames, first-audio/completion timing, cancellation, close, usage, and cost evidence;
- browser audio playback acknowledgements.

### Tests

- Hindi, Hinglish, and Indian-English pronunciation samples;
- numbers, abbreviations, punctuation, empty/oversized segments, timeout, failure, and cancellation;
- cancelled/stale synthesis cannot reach playback;
- generated, normalized, synthesized, and delivered evidence stays distinct.

### Exit gate

The browser plays intelligible approved-language output with safe cancellation and explainable first-audio, completion, usage, and cost evidence.

Cost: no live synthesis occurs until the dated character/time rate and a bounded test budget are recorded.

## 16. Work package 10 — end-to-end orchestration and natural turn-taking

### Deliverables

- one authoritative session orchestrator and worker lease/heartbeat;
- local Silero speech activity, Turn Manager endpoint commitment (capped at 1,000 ms), response segmentation, playback state, barge-in, and false-interruption suppression, with the interruption-candidate activation threshold raised to 0.7 during agent playback (`vad.playback_activation_threshold`);
- no LiveKit `AgentSession` or semantic/audio Turn Detector;
- generation-aware cancellation across conversation, TTS, and playback;
- approved silence, turn, reconnect, idle, and maximum-session timeouts;
- retry/fallback orchestration and idempotent finalization;
- 5-second session reconciler (Decision 060 timings), persistent end requests, due connection/termination deadlines, and at most one higher-generation worker-crash recovery attempt;
- deterministic initial greeting after `client.ready` exactly once.

### Tests

- happy path and rapid multi-turn conversation;
- user interruption during first audio and mid-response;
- sub-250 ms noise/echo does not cancel playback; 250 ms continuous speech accepts barge-in;
- accepted interruption with an empty/unusable transcript uses clarification fallback and never resumes stale audio;
- repeated/late provider events and duplicate browser messages;
- disconnect/reconnect, provider timeout, quota/rate limit, persistence degradation, and shutdown;
- end requested in `created`/`connecting`, lost LiveKit end signal, expired worker lease, successful replacement claim, and exhausted recovery to `failed`;
- no stale or duplicate response becomes audible;
- conversation history reflects delivered content rather than hidden/cancelled text.

### Exit gate

Complete Hindi/Hinglish/English browser conversations work naturally and all end/failure paths converge on one idempotent final state. Work-package completion requires at least 20 valid interruption samples, interruption-to-silence P95 at most 500 ms, and maximum at most 1,000 ms; no percentile is claimed from one conversation. The samples come from the `INT-LIVE` protocol (`17-phase0-evaluation-case-catalog.md` §19A: `INT-LIVE-S` speakers-only and `INT-LIVE-H` headphones, 12 scripted barge-ins each) plus any valid live barge-in samples. Mock reliability cases `REL-091`/`REL-092` are correctness checks, not latency samples.

Cost: mock orchestration tests are INR 0 provider cost. Any live end-to-end session consumes the already approved bounded STT/LLM/TTS/LiveKit budget and records actual usage; this package authorizes no additional service or rate. `INT-LIVE` costs 2 × ₹27.925 ≈ ₹55.85 variable and needs approval together with the live batch.

## 17. Work package 11 — durable observability, latency, and cost

### Deliverables

- durable event selection and normalized operation/error records;
- stage and end-to-end latency derivation;
- usage normalization and versioned dated rate-card lookup;
- attempt-level, turn-level, and session-level cost calculation without retry double counting;
- safe session/report API projections;
- 30-day retention evidence and bounded local operational report.

### Tests

- every provider request has session/turn/logical-request correlation;
- retries remain separate billable evidence;
- cost lines reconcile with totals within 1% after documented rounding;
- unavailable usage/rates remain unavailable or estimated, never silently zero;
- persistence and reporting cannot leak restricted fields;
- a failed or expensive session can be reconstructed from stored evidence.

### Exit gate

Every completed test session has a coherent timeline, latency summary, component usage, cost status, errors, and finalization evidence.

Cost: no new provider rate is introduced. Atlas and LiveKit usage stays within the approved session budget, and observability must not enable paid recording, egress, or external monitoring services.

## 18. Work package 12 — evaluation harness and Phase 0 release gate

### Resolved prerequisite decisions

The required design approvals are complete:

- evaluation collection field/index contracts — resolved by Decision 040 and `16-evaluation-database-schema.md`;
- exact content of the 100-case dataset — resolved by Decision 041 and `17-phase0-evaluation-case-catalog.md`;
- current provider rate card and initial bounded spend ceiling — resolved by Decision 039 and `15-phase0-pricing-and-cost-model.md`.

### Deliverables

- provider-independent local Python evaluation runner;
- versioned 60 transcript, 30 live voice, and 10 failure/reliability scenarios (3/1/3 repetitions, 240 result slots);
- the `INT-LIVE` live interruption protocol, outside the 100-case catalog, as the interruption-latency sample source;
- automated assertions, human-rating capture, latency/reliability/cost output, and holdout isolation;
- configuration/run evidence stored through approved evaluation repositories;
- final baseline report with failures and unresolved limitations.

### Cost control

- local fixture, mock, and report-generation runs have INR 0 provider cost;
- before a full paid evaluation run, calculate projected cost from selected cases/repetitions and the current dated rate card and obtain separate approval; the current planning projection in `15-phase0-pricing-and-cost-model.md` §8A is ₹900.35 variable (transcript ≈₹7, live ₹837.75, `INT-LIVE` ₹55.85, mock reliability ₹0) and ₹2,407.70 loaded with the Atlas base month;
- a full run plus the 20 smoke-test sessions in one billing month (₹3,198.53 loaded, Atlas base) exceeds the ₹2,000 alert but stays under the ₹5,100 ceiling; with the Atlas maximum it would exceed the ceiling and must be split across months or separately approved;
- abort rather than expand concurrency, repetitions, providers, or spend beyond the approved run projection.

### Exit gate

- zero critical violations, fabricated capability, secret disclosure, and stale audible output;
- language/instruction compliance at least 95%;
- formatting compliance at least 98%;
- clarification/transcript acceptance at least 90%;
- names/numbers/end-to-end success at least 95%;
- interruption-to-silence P95 at most 500 ms after at least 20 valid samples, with maximum at most 1,000 ms;
- speech-end-to-audio P50 at most 2 seconds and P95 at most 4 seconds;
- human overall rating at least 4/5 and each approved dimension at least 3.5/5;
- cost reconciliation within 1%;
- no unresolved critical defect.

Only a passing, documented configuration becomes the Phase 0 baseline.

## 19. Mandatory stop conditions

Stop the affected milestone and return evidence for a decision when:

- an approved direct dependency is unavailable, insecure, incompatible, or requires an unapproved source toolchain;
- a provider API/SDK cannot satisfy cancellation, usage, latency, or isolation contracts;
- secrets or restricted data appear in browser output, logs, fixtures, traces, or committed files;
- the requested action would require public exposure, production data, ordinary audio recording, destructive shared-data cleanup, or broader permissions;
- measured cost cannot be attributed or bounded;
- provider/model/voice/version behaviour differs materially from the approved contract;
- a test needs a new service, framework, storage product, browser family, challenger provider, or architectural component;
- existing user changes conflict with the required implementation.

Fixes that preserve an approved contract may be implemented normally. Any material provider, version, schema, security, or scope change requires explicit approval.

## 20. Evidence produced per milestone

Each completed milestone records:

- application and configuration version/checksum;
- dependency lock checksum when relevant;
- tests executed and pass/fail counts;
- sanitized compatibility/health results;
- known limitations and deferred failures;
- real provider/model/region identity when used;
- latency summary;
- normalized usage and cost, including zero only when evidence supports zero;
- date and R&D environment label.

Raw secrets, full provider payloads, ordinary audio, unrestricted transcripts, signed URLs, or database connection strings are never included in evidence.

## 21. Local process/run contract

The local baseline has three independently restartable processes:

1. FastAPI control API;
2. Python LiveKit agent worker;
3. Vite browser development server.

MongoDB Atlas and LiveKit are external dependencies. STT, conversation, and TTS providers are enabled only for the milestone that needs them.

Startup order:

1. validate the external secret-file reference and safe bootstrap settings;
2. start the API and verify liveness/readiness;
3. start the worker and verify registration/health evidence;
4. start the browser UI;
5. create a session and join only after required dependencies are ready.

Shutdown order:

1. stop new session creation;
2. end/finalize the active R&D session;
3. stop the browser session;
4. drain and stop the worker;
5. stop the API.

Unexpected shutdown must not authorize stale provider output after restart or silently reopen a terminal session.

## 22. Cost-control sequence

Cost control is progressive:

1. work packages 0–4 use mocks/local checks and target INR 0 provider usage;
2. Atlas connectivity is enabled with the approved Flex plan and monitored separately;
3. LiveKit, Deepgram, OpenAI, and Sarvam are enabled one at a time only after their dated rates and bounded smoke-test allowance are recorded; the 20-session smoke allowance is ₹558.50 variable (₹27.925 per ten-minute session) and ₹1,923.64 loaded in the Atlas base case per `15-phase0-pricing-and-cost-model.md`;
4. end-to-end tests use explicit session/time/request limits;
5. every retry remains visible as potential billable work;
6. a fixed session-cost ceiling is approved only after at least 20 valid measured baseline sessions;
7. challenger/provider benchmark spend is separately approved.

Tax, foreign exchange, network, storage, minimum platform charges, and provider billing increments remain explicit rather than being hidden inside a nominal per-minute estimate.

## 23. Deferred beyond this plan

- Grok, Sarvam STT, ElevenLabs, and alternative transport adapters;
- challenger-provider benchmarking (the Milestone 10 remainder outside WP12; see §5A), with separate approval and budget;
- production authentication/authorization and multi-user concurrency;
- containers, CI/CD, hosted application infrastructure, autoscaling, and production monitoring;
- production database tier, backup, recovery, retention, deletion SLA, and compliance controls;
- benchmark audio object storage;
- knowledge graph/retrieval/citation behaviour;
- inbound/outbound telephony, consent scripts, recording, and human transfer;
- continuous production evaluation and LLM-as-judge;
- Firefox/WebKit certification and mobile-browser support.

## 24. Acceptance criteria

This execution plan is satisfied when:

- implementation follows the approved work-package order;
- mocks prove contracts before real provider spend;
- each external dependency is enabled and measured separately;
- every milestone has reproducible tests and sanitized evidence;
- stop conditions prevent silent architecture/version/security changes;
- no secret, ordinary audio, or production data enters the repository;
- core provider types stay inside adapters;
- API, worker, and browser lifecycles remain independent;
- latency, usage, retries, failures, and cost remain explainable;
- the 100-case release gate passes before declaring the baseline stable;
- later knowledge, benchmarking, production, and telephony scope remains unimplemented until separately approved.
