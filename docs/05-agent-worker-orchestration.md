# Agent Worker and Orchestration Design

Status: Approved for Phase 0 R&D  
Authority: Decision 029 with Decisions 042, 044, 051, 052, 060, 061, 062, 065, 067, and 069 amendments  
Scope: Worker ownership, queues, turn orchestration, recovery, and finalization  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `02-database-design.md`, `03-backend-module-design.md`, `04-control-api-contract.md`  
Implementation status: Not started  
Last reviewed: 2026-09-28

Runtime: Python LiveKit agent worker  
Architecture: One orchestrator and one user-visible state writer per voice session

## 1. Purpose

This document defines how a LiveKit job becomes an active browser voice session and how the Python worker coordinates transport, STT, conversation generation, response segmentation, TTS, playback, interruptions, retries, persistence, costing, and cleanup.

The core invariant is:

> One session's user-visible state is controlled by its orchestrator, never directly by an adapter.

## 2. Worker process lifecycle

```text
Process starts
    -> settings validate
    -> MongoDB client/repositories ready
    -> provider registry loads approved adapters
    -> LiveKit worker registers
    -> worker READY
    -> LiveKit job received
    -> session validated and claimed
    -> transport joined
    -> conversation active
    -> session end/cancel/failure
    -> usage/cost/terminal evidence finalized
    -> resources closed
    -> worker assignment released
```

Worker readiness does not send paid STT, conversation, or TTS requests. A worker with invalid settings, unavailable required database access, or failed LiveKit registration does not report ready.

## 3. LiveKit job validation

Minimum safe job metadata:

- metadata schema version;
- `session_id`;
- `correlation_id`;
- expected `agent_config_id`;
- `environment`.

Encoded job metadata is limited to 2 KiB and contains no prompt, transcript, direct PII, credential, secret, or full configuration.

Job metadata is a locator, not the authoritative configuration. Before starting providers, the worker reloads the durable session and exact immutable configuration. Claim validation is mode-specific:

- session exists;
- an initial claim requires session state `connecting`, generation `1`, and no unexpired assignment;
- a recovery claim requires session state `active` with activity `recovering`, a `recovery_authorization.recovery_dispatch_id` matching the job's explicit dispatch, `recovery_authorization.recovery_deadline_at > $$NOW`, and no `termination_request` (the reconciler ownership lease `recovery_authorization.expires_at` does not gate the claim) (see §21, Decision 067);
- maximum duration has not expired;
- configuration is usable and matches the job;
- channel is `browser`;
- recording is off unless an active consent authorization explicitly permits it;
- no valid conflicting worker assignment exists, and an initial claim finds no `recovery_authorization`;
- the durable `termination_request` is absent both at claim time and at the startup barrier immediately before providers are opened.

A mismatch fails safely before starting the provider pipeline.

The approved explicit-dispatch, participant, token, audio, realtime-data, reconnect, and cleanup rules are defined in `docs/06-livekit-transport-adapter.md`.

## 4. Worker assignment and generation

Each session may contain bounded `worker_assignment`:

- `worker_instance_id`;
- `livekit_job_id`;
- integer `generation`;
- integer `writer_epoch`;
- integer `lease_revision`;
- `claimed_at`;
- `heartbeat_at`;
- `lease_expires_at`;
- optional `released_at`.

There is no `assignment_id`. An assignment is identified by the tuple (`worker_assignment.generation`, `worker_instance_id`, `livekit_job_id`).

R&D timing (Decision 060):

- heartbeat every 5 seconds;
- assignment lease expires after 15 seconds without a successful heartbeat;
- the reconciler scans due work every 5 seconds.

Rules:

- initial claim uses an atomic `state_revision`-checked session update;
- heartbeat renewal is a compare-and-set that matches `session_id`, `worker_assignment.generation`, `worker_instance_id`, `livekit_job_id`, `writer_epoch`, and `lease_revision`, and also requires `lease_expires_at > $$NOW` (server time);
- a successful renewal sets `heartbeat_at`, sets `lease_expires_at = now + 15 s`, and increments `lease_revision`; it does not increment session `state_revision` or update session `updated_at`/`last_activity_at`;
- an expired lease cannot be renewed, so a late heartbeat from an old worker fails;
- the reconciler fence increments both `writer_epoch` and `lease_revision`, so every later write or renewal by the fenced worker fails;
- one unexpired active assignment is allowed;
- duplicate active claims are rejected;
- a stale assignment may be recovered only through the stored `recovery_authorization` and a higher writer epoch (§21);
- every asynchronous result carries/checks the worker generation;
- output from an old generation becomes `discarded_late`;
- hostnames, process commands, and machine secrets are not stored.

Claim/update by session uses the unique session ID. Reconciler scans use the approved partial due-session and lease-expiry indexes so they never require a full collection scan.

### Worker self-fencing

The worker keeps a local lease deadline: the `lease_expires_at` returned by the last successful renewal, measured conservatively from the time that renewal request was sent, minus a 3-second safety margin.

The worker self-fences when either of these happens:

- the local lease deadline passes without a successful renewal;
- any write returns a `writer_epoch` or generation mismatch.

Self-fence steps, in order:

1. revoke output authorization;
2. cancel LLM and TTS work;
3. call `AudioSource.clear_queue()`;
4. unpublish the agent audio track;
5. disconnect from the room;
6. emit a best-effort `worker.self_fenced` diagnostic when the database is reachable;
7. exit the job.

A self-fenced or evicted worker never rejoins the room and never retries the claim. Because the worker fences before its lease expires and the reconciler acts only after expiry plus its scan interval, the old worker has stopped at least about 3 seconds before any replacement can start.

## 5. Orchestrator ownership

The session orchestrator owns:

- pipeline lifecycle;
- current active turn;
- provider-operation creation;
- cancellation generation;
- retries/fallback coordination;
- session-local durable event order;
- user-visible output authorization;
- state transitions;
- final cleanup/finalization.

Adapters return normalized data/events only. An adapter cannot directly change session state, publish arbitrary browser events, write MongoDB, or call another adapter.

## 6. Single-writer command loop

```text
Transport task ----+
Local VAD task ----+
STT task ----------+
Conversation task -+--> Orchestrator command loop --> state/events/output
TTS task ----------+
Playback task -----+
Timers/persistence-+
```

Supporting tasks submit bounded commands/events. During a healthy assignment, the orchestrator command loop is the only writer for user-visible conversational state, active-turn ownership, and cancellation-generation decisions. Every such write is fenced by the current `writer_epoch` and worker generation. After lease expiry, the reconciler must atomically fence the old epoch and write the stored `recovery_authorization` (§21) before it may transition recovery/terminal state or abandon an open turn; a replacement worker then claims under that authorization. Thus one fenced authority, rather than one permanently fixed process, exists at a time.

## 7. Internal audio contract

Normalized audio frames contain:

- signed 16-bit little-endian PCM;
- mono audio;
- explicit `sample_rate_hz`;
- explicit frame duration;
- monotonic capture/playback timestamp;
- safe session/track identity;
- no provider SDK object.

Recommended frame duration is 20 ms.

The LiveKit adapter normalizes incoming frames. The STT adapter performs provider-required resampling internally. TTS emits normalized frames with an explicit sample rate; the transport adapter handles playback-compatible conversion.

## 8. Per-session task group

One active session owns bounded tasks for:

- transport event reading;
- user audio intake;
- speech/turn event processing;
- STT stream management;
- active conversation request;
- response segmentation;
- TTS generation;
- playback publishing;
- event/metric persistence;
- worker-assignment heartbeat;
- timeout and reconnect monitoring.

Session cancellation closes every child task. Child failure is reported to the orchestrator rather than independently mutating shared state.

## 9. Bounded queues and backpressure

Initial R&D limits:

- inbound audio: approximately 2 seconds;
- response segments: 5;
- non-terminal persistence events: 500;
- generated playback audio: approximately 10 seconds.

Rules:

- unbounded queues are prohibited;
- user audio is never silently dropped;
- audio overflow creates `realtime_overload` evidence and fails/recovers the affected turn;
- terminal events are never intentionally dropped;
- optional diagnostic events may aggregate/drop under pressure with a recorded counter;
- segmentation waits when the segment queue is full;
- accepted interruption immediately clears/cancels queued playback and TTS work.

Queue item counts may vary with actual frame size, but the time-based caps remain authoritative.

## 10. Activation flow

```text
1. Claim session atomically
2. Join LiveKit transport
3. Verify browser participant/track
4. Prepare STT stream
5. Session -> active
6. Agent activity -> listening
7. Publish normalized ready state
```

After activation the agent activity state is `listening` (01 §6: user audio is being accepted). The same state is used after a successful browser reconnect and after a worker-crash recovery claim.

Activation failure/timeout cancels opened operations, records a safe error, marks the session failed, closes resources, and releases the assignment.

## 11. Speech activity and STT lifecycle

The worker owns a provider-neutral `SpeechActivityDetector` backed initially by local Silero VAD. WebRTC microphone intake is normalized as 48 kHz mono 20 ms PCM frames with a stream epoch, frame sequence, and monotonic capture interval. Frames are routed continuously to both consumers and resampled to 16 kHz mono Linear16 for local Silero VAD and the Deepgram baseline. Neither consumer depends on provider endpoint events for application turn ownership.

Authority is explicit:

- local Silero VAD is authoritative for speech activity start/stop;
- the Turn Manager is authoritative for application turn open/close, endpoint commitment, and interruption acceptance;
- Deepgram is authoritative for transcript segments/text only;
- Deepgram `SpeechStarted`, `speech_final`, and `UtteranceEnd` signals are advisory diagnostics and may assist an STT flush, but cannot close a turn or interrupt playback;
- LiveKit `AgentSession` and the LiveKit semantic/audio Turn Detector are not used in Phase 0.

The normalized STT port is session-stream oriented:

- an STT stream may open near session activation;
- audio frames flow continuously while listening;
- partial transcripts are realtime/UI-only and cannot enter conversation history or start generation;
- final transcripts become durable turn intent;
- provider reconnect/retry creates a new operation attempt;
- one provider stream may produce multiple turns;
- one turn may contain multiple finalized STT segments; arrival time never decides turn ownership;
- finalized segments are associated by provider audio interval mapped to the authoritative capture timeline and accepted turn speech window;
- a turn references the relevant STT operation;
- provider usage remains attached to its actual connection/operation;
- derived per-turn allocation is explicitly estimated/allocation-only.

An adapter may use turn-scoped calls internally, but it must preserve the same normalized behaviour.

The initial adapter is Deepgram Nova-3 Multilingual with Hindi/English code switching. Exact normalized events, finalization, retry, keyterm, language, cost, and benchmark rules are defined in `docs/07-stt-adapter-and-baseline.md`.

## 12. User turn flow

```text
Local VAD accepts speech start
    -> turn open
    -> audio flows to STT
Local VAD accepts speech stop
    -> Turn Manager waits until total endpoint deadline
    -> request STT finalization/flush
    -> wait up to 3 seconds for final transcript
Final transcript
    -> turn transcript_final
    -> conversation request starts
Stable text segment
    -> deterministic segment validation
    -> TTS starts
Audio arrives
    -> playback starts
Playback completes
    -> turn completed
```

Rules:

- the endpoint deadline is measured from the last detected speech frame, not added after the VAD silence window;
- initial local VAD silence detection is approximately 550 ms and the initial total endpoint deadline is 700 ms, leaving only the remaining interval before finalization;
- the endpoint deadline is configurable from 700 ms up to a 1,000 ms cap; a value above 1,000 ms (the former 2,000 ms upper bound) requires re-approval of the latency budget (Decision 067);
- a turn opens with `input_disposition = pending`;
- local VAD speech start during agent playback opens a turn only after the interruption candidate is confirmed (≥250 ms); a suppressed false-interruption candidate never creates a turn;
- empty final transcript does not create a conversation request; an empty/noise turn without a clarification fallback ends `discarded`, not `failed`;
- preemptive generation is disabled initially;
- only one conversation generation is active per turn;
- multiple ordered TTS-segment operations may exist per turn;
- unfinished output-cap tails are discarded before TTS and never enter delivered history;
- only one user-visible agent playback stream is active per session.

## 13. Conversation history

History reflects what was accepted/heard, not hidden generated content:

- completed turn: final delivered assistant response;
- interrupted turn: spoken/delivered portion only;
- failed unplayed response: excluded from assistant history;
- abandoned turn: excluded unless separately finalized;
- discarded (empty/noise) turn: excluded;
- user history: accepted final transcripts only.

The future knowledge engine receives this same normalized history contract.

For Phase 0, the conversation adapter is OpenAI GPT-6 Luna with reasoning `none`, streaming, and a 250-output-token cap. Context targets are approximately 2,000 system-instruction tokens, 12,000 recent-history tokens, and 16,000 total input tokens; excess history removes oldest complete turn pairs without automatic summarization. All tools/search/provider-authoritative memory remain disabled. Full rules are in `docs/08-conversation-adapter-and-llm-baseline.md`.

## 14. Conversation-to-TTS streaming

```text
Final transcript
    -> conversation generation
    -> stable phrase/sentence
    -> response segment emitted
    -> TTS begins
    -> later conversation segments continue
```

Each segment has:

- `segment_id`;
- sequence number;
- normalized speakable text;
- language;
- associated conversation operation;
- associated TTS operation;
- cancellation generation.

Playback authorization always rechecks the current generation, even after TTS succeeds.

For Phase 0, TTS uses Sarvam Bulbul v3 with `priya`, 24 kHz mono Linear16 streaming output, and provider-facing segments capped at 500 Unicode characters. Hindi/Hinglish maps to `hi-IN`, English to `en-IN`; cloning, ordinary audio storage, and silent fallback remain disabled. Full rules are in `docs/09-tts-adapter-and-voice-baseline.md`.

Conversation-stream retry is allowed only while no response portion has been delivered. Once browser/TTS delivery has begun, the worker ends the affected response with the delivered portion (`response_completion_status = failed`, or `truncated_partial`/`truncated_fallback` for a length limit with turn status `completed`) rather than restarting and speaking a duplicate full answer.

TTS retry follows the same delivery boundary: before first published/played audio a guarded retry may occur; after delivery begins, the complete segment is not automatically replayed.

## 15. Spoken-content evidence

Playback emits segment-level evidence:

- playback started;
- played duration/position where available;
- playback completed or cancelled.

Classification:

- full acknowledged segment: `spoken_text_accuracy = confirmed`;
- partial playback without exact text/audio alignment: `estimated`;
- insufficient evidence: `unavailable`.

Generated, synthesized, and spoken/delivered text remain separate.

## 16. Interruption ordering

Accepted barge-in follows the canonical interruption order (Decision 067; identical in 01 §15, 06, 08, and 09):

```text
1. Confirm the candidate: ≥250 ms of continuous local-VAD speech from candidate start
2. Increment the in-memory cancellation generation (the fence)
3. Cancel LLM streaming and TTS: queued segments first, then the active segment
4. AudioSource.clear_queue() (queue bounded at queue_size_ms = 200)
5. Notify the browser (interruption state message)
6. Record events and evidence
```

Step 6 covers persisting delivered-content evidence and marking the turn `interrupted`. From step 2 onward every old-generation frame/event is rejected. The next utterance then opens a new turn.

While agent audio is playing, the Silero activation threshold for interruption candidates rises from 0.5 to 0.7 (`vad.playback_activation_threshold`, Decision 067); the 250 ms confirmation still applies.

The same candidate rule applies while the agent is thinking (LLM streaming or TTS synthesis started, no agent audio played yet), at the normal 0.5 threshold: a confirmed candidate runs the canonical order above and cancels the stale response, and the new utterance becomes the next turn (Decision 069).

Playback acknowledgements are identified by (`worker_assignment.generation`, cancellation generation, `segment_id`). The cancellation generation is in-memory only; there is no durable cancellation-generation field, and cross-process fencing uses `writer_epoch` and the worker generation.

Every asynchronous result checks:

- session ID;
- turn ID;
- operation ID;
- worker generation;
- cancellation generation.

Mismatch results are marked `discarded_late` and cannot affect playback, browser state, or conversation history. A candidate shorter than 250 ms is classified as a suppressed false interruption; it does not cancel or pause playback and never creates a turn. After an interruption is accepted, stale audio never resumes automatically; an empty/unusable transcript follows the approved clarification fallback.

## 17. Retry policy

The values below are the versioned-configuration defaults from `02-database-design.md` §24 "Retry defaults"; configuration may tune them within validation bounds.

- maximum three total attempts per logical request;
- initial attempt plus two retries;
- initial backoff 250 ms;
- maximum backoff 2,000 ms;
- exponential backoff with jitter;
- only allowlisted transient errors retry;
- cancellation/deadline stops retries;
- non-idempotent tools do not retry automatically.

Every retry uses a new operation ID, preserves the logical request and applicable turn IDs, and records separate latency, usage, error, and cost evidence.

## 18. Component failure behaviour

### Transport

- enter activity `recovering`;
- stop/clear playback;
- allow 20-second reconnect;
- return to activity `listening` after success;
- terminate with normalized network reason after expiry.

### STT

- apply approved retry/fallback;
- fail the affected turn when unrecoverable;
- session may remain active if STT recovers;
- no final transcript means no conversation request.

### Conversation engine

- apply approved retry/fallback;
- fail the current turn when unrecoverable;
- normally keep the session available for a later turn;
- never fabricate a response.

### TTS

- preserve safe text for the UI when available;
- record failed audio/delivery evidence;
- mark the turn `failed` when no audio was delivered, or record the delivered portion under the turn's actual terminal status and `response_completion_status` when some audio was delivered;
- allow later turns when the component recovers.

### Persistence

- optional diagnostic persistence cannot block audio indefinitely;
- terminal evidence receives bounded reliable retry;
- rejected documents are not dumped to general logs;
- unrecoverable critical write creates `persistence_failed` evidence.

## 19. Browser disconnect

- set agent activity to `recovering`;
- stop playback and pause new turns;
- keep the worker alive for the 20-second reconnect window;
- reload durable state after reconnect;
- return to activity `listening` after success;
- after expiry, terminate as `ended` with `disconnect_reason = browser_closed` (or `network_lost` when the transport reported a network loss), per 01 §5;
- never continue speaking while the browser is absent.

## 20. Explicit session end

After the worker receives the reliable fast signal or observes the durable `termination_request` during heartbeat/reload, it:

1. blocks new turns;
2. increments cancellation generation;
3. stops playback;
4. cancels conversation/TTS work;
5. closes STT;
6. closes transport;
7. finalizes an open turn as interrupted/abandoned;
8. records final available usage/cost;
9. writes terminal event/session summary;
10. releases assignment;
11. marks the session `ended`.

Repeated end requests remain idempotent. The worker acknowledges the request revision durably before terminalization; failure to receive the LiveKit packet does not lose the request.

## 21. Worker shutdown

Graceful shutdown:

- stop accepting jobs;
- notify active orchestrators;
- allow at most 10 seconds;
- stop playback and cancel operations;
- persist terminal evidence where possible;
- close providers and transport;
- release assignments;
- close MongoDB after session tasks finish.

Unexpected death leaves a stale heartbeat/lease. The session reconciler detects it and applies the bounded recovery contract below.

### Session reconciler and crash recovery

The control-API process owns one bounded background `session_reconciler` task (Decision 067); it is not part of the non-daemon `maintenance` entry point and does not run inside an ordinary request handler. Every 5 seconds it scans indexed nonterminal sessions whose `next_reconcile_at` is due.

`next_reconcile_at` equals the minimum of the session's active deadlines: connect deadline, termination deadline, `worker_assignment.lease_expires_at`, `recovery_authorization.expires_at`, `recovery_authorization.recovery_deadline_at`, idle deadline, and maximum-duration deadline. The worker is the primary enforcer of idle timeout and maximum duration; the reconciler is the backstop.

Stored `voice_sessions.recovery_authorization` (absent when no recovery is in progress) contains `owner_instance_id`, `acquired_at`, `expires_at` (ownership lease, `acquired_at + 10 s`, renewed every 5 s by the owning reconciler while it works), `recovery_deadline_at` (first acquisition + 20 s; never reset by a takeover), `writer_epoch`, `recovery_dispatch_id`, and `owner_generation` (incremented on each takeover).

For an expired worker lease:

1. in one atomic update, conditioned on `status = active`, an expired lease (`lease_expires_at <= $$NOW`), no `termination_request`, no `recovery_authorization` present, and `worker_recovery_count` below the limit: increment `writer_epoch` and `lease_revision`, increment `worker_recovery_count`, set `agent_activity_state = recovering`, and write `recovery_authorization` (with `recovery_deadline_at = now + 20 s` and `owner_generation = 1`) including a newly generated `recovery_dispatch_id` stored before any dispatch is created;
2. send a best-effort `agent.recovering` state message to the browser participant through the LiveKit server API (`RoomService.SendData`) on the existing agent-to-browser `va.state.v1` topic, so the browser shows "reconnecting agent…" and stops showing "speaking"; independently, the browser falls back to "reconnecting" when no agent audio or state arrives for 5 seconds after the last lease-timing hint;
3. under the fenced ownership, mark any open turn `abandoned` and inspect/clean the stale dispatch without replaying buffered speech;
4. re-read `termination_request`; only when the browser remains present and no termination is requested may it create the replacement explicit dispatch, idempotently: it checks whether a dispatch with the stored `recovery_dispatch_id` exists and creates it only if it does not, so a retry after a reconciler crash never creates a second dispatch;
5. after dispatch, re-read `termination_request`; if one arrived, the replacement claim will be rejected (the session is no longer `active`), the dispatched job exits at its claim or startup barrier, and the reconciler finalizes the session and cleans up the dispatch;
6. the replacement worker joins with the same agent participant identity; LiveKit evicts any still-connected old participant with that identity, and this expected eviction does not trigger unexpected-participant termination (the evicted worker self-fences per §4 and never rejoins);
7. the replacement claim requires `status = active`, a matching `recovery_authorization.recovery_dispatch_id`, `recovery_authorization.recovery_deadline_at > $$NOW`, and no `termination_request` (not an unexpired ownership lease); a successful claim writes the new assignment and unsets `recovery_authorization`;
8. the replacement worker re-checks `termination_request` and `status` immediately after the claim and again at its startup barrier, reloads durable state, and resumes in activity `listening` without replaying the greeting;
9. if `recovery_authorization.recovery_deadline_at` (20 seconds after first acquisition) passes, no browser remains, or a second worker-crash recovery would be required, clean up and mark the session `failed`.

If the 10 s ownership lease `recovery_authorization.expires_at` passes before `recovery_deadline_at` (for example, the owning control-API process crashed or restarted with a new instance ID), a later reconciler pass may take over with a fencing update that increments `writer_epoch`, replaces `owner_instance_id`, `acquired_at` and `expires_at`, increments `owner_generation`, and reuses the stored `recovery_dispatch_id`. A takeover continues the same recovery: it does not increment `worker_recovery_count`, does not reset `recovery_deadline_at`, and is not blocked by the recovery limit. Once `recovery_authorization.recovery_deadline_at` passes without a successful replacement claim, any reconciler instance, whether or not it owns the authorization, takes the fenced finalize path (increment `writer_epoch`, abandon any open turn, clean up the dispatch, mark the session `failed`). The owning reconciler renews its ownership lease from an in-process task every 5 s with a compare-and-set matching `owner_instance_id` and `owner_generation` and requiring `expires_at > $$NOW`, which sets `expires_at = now + 10 s`; renewal stops when `recovery_authorization` is unset (successful claim or finalization) or when a renewal fails, after which that instance stops acting as owner. The ownership lease governs only which reconciler may run recovery steps; it never gates the replacement worker's claim. `recovery_authorization` is unset when the replacement claims successfully or when the session is finalized.

End during recovery: once End sets the session to `ending`, the recovery claim fails because it requires `active`, and the reconciler finalizes the session.

Write conflicts: on any `state_revision` conflict (for example, End racing the reconciler's turn-abandon write), the reconciler re-reads and re-evaluates, retrying at most 3 times. If the session is now `ending`, it follows the finalize path. It never leaves an open turn behind: finalization abandons any open turn.

Recovery evidence uses the durable `worker` event category: `worker.lease_expired`, `worker.recovery_started`, `worker.recovery_claimed`, `worker.recovery_failed`, and `worker.self_fenced` (01 §9).

The reconciler also:

- marks connection/start timeouts before `active` as `failed`;
- completes `ending` sessions when no valid worker exists;
- finalizes explicit user/idle/maximum-duration ends as `ended` after verified cleanup;
- records unavailable final usage as unavailable rather than zero;
- uses `state_revision`, `writer_epoch`, and generation conditions so a recovered worker and reconciler cannot both terminalize or publish output.

## 22. Idempotent finalization

Every session has logical `finalize_once` behaviour:

- normal end, failure, timeout, disconnect, and shutdown share the same guarded path;
- repeated callbacks return the existing terminal outcome;
- resource-close steps continue safely when one close operation fails;
- terminal state cannot return to active;
- missing usage remains unavailable rather than zero.

## 23. Persistence ordering

```text
Create/claim operation
    -> call provider
    -> store usage/result/error evidence
    -> update turn/session summaries
```

Rules:

- operation exists before its provider call;
- final transcript is durable before conversation generation is authorized;
- interruption/generation change is authoritative before late output can be accepted;
- terminal evidence receives bounded reliable persistence;
- cost may finalize after playback while preserving calculation-run traceability.

## 24. Normalized internal events

The command loop consumes events for:

- transport connect/disconnect/reconnect;
- authoritative local-VAD speech start/end and interruption candidates;
- advisory provider speech/endpoint signals;
- STT partial/final/failure;
- conversation first token/segment/completion/failure;
- TTS first audio/audio-frame/completion/failure;
- playback start/completion/cancellation;
- interruption candidate/detect/accept/suppress;
- retry/fallback scheduling;
- session end/shutdown.

Audio chunks, partial transcripts, token segments, and TTS frames remain non-durable high-frequency events.

Browser-facing LiveKit data uses only the approved versioned `va.*.v1` topics. Encoded reliable low-frequency application messages are capped at 8 KiB; encoded lossy high-frequency messages are capped at 1,200 bytes. The adapter decides delivery class according to `docs/06-livekit-transport-adapter.md`; durable event selection remains owned by the persistence contract.

## 25. Phase 0 exclusions

- outbound telephony;
- multi-user rooms;
- multiple active agents in one session;
- browser-controlled provider switching;
- arbitrary side-effecting tools;
- knowledge retrieval;
- automatic benchmarking;
- resume after the approved reconnect window or worker-recovery budget/deadline is exhausted;
- sessions longer than 30 minutes;
- automatic LiveKit dispatch, video, recording/egress, arbitrary RPC/files, and browser room-administration grants.

## 26. Database implication

Add these `voice_sessions` fields:

- bounded `worker_assignment` (`worker_instance_id`, `livekit_job_id`, `generation`, `writer_epoch`, independent `lease_revision`, `claimed_at`, `heartbeat_at`, `lease_expires_at`, optional `released_at`; no `assignment_id`);
- `writer_epoch` (inside `worker_assignment`, as in 02 §6) as the fencing counter checked by every worker and reconciler write and incremented by every reconciler fence;
- optional `recovery_authorization` (`owner_instance_id`, `acquired_at`, `expires_at`, `recovery_deadline_at`, `writer_epoch`, `recovery_dispatch_id`, `owner_generation`); there is no root-level `recovery_deadline_at`;
- `termination_request`, `connect_deadline_at`, `termination_deadline_at`, and `next_reconcile_at`;
- `worker_recovery_count`;
- zero-initialized `event_sequence_counter`.

The heartbeat/lease/reconciliation timings are 5/15/5 seconds. There is no durable cancellation-generation field. A partial environment-scoped reconciliation index is required for due nonterminal sessions and expired leases.

## 27. Acceptance criteria

- exactly one fenced authority owns session-visible state for the current writer epoch;
- duplicate workers cannot both authorize output;
- a durable end request reaches terminal state even when no worker is connected;
- expired worker leases are detected, receive at most one higher-generation recovery attempt, and cannot leave a session indefinitely active;
- every late result is generation checked;
- internal queues are bounded;
- no audio is silently dropped;
- final transcript is durable before generation;
- local VAD owns speech activity while the Turn Manager owns endpoint/turn commitment;
- Deepgram speech/endpoint events cannot independently close turns or stop playback;
- interruptions stop playback before old output can resume;
- conversation history reflects delivered content;
- retries and fallbacks retain attempt-level cost/error evidence;
- browser disconnect stops speech;
- all end/failure paths converge on idempotent finalization.
