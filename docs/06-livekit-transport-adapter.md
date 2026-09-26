# LiveKit Transport Adapter Design

Status: Approved for Phase 0 R&D  
Authority: Decision 030 with Decisions 042, 044, 050, 052, 060, 062, and 067 amendments  
Scope: Browser-based single-user LiveKit media, data, token, reconnect, and cleanup boundary  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `03-backend-module-design.md`, `04-control-api-contract.md`, `05-agent-worker-orchestration.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Initial platform: LiveKit

## 1. Purpose

This document defines the replaceable LiveKit transport boundary used by the FastAPI control API and Python agent worker. LiveKit carries realtime media and transient data, but it does not own conversation state, durable evidence, business rules, or provider orchestration.

The core rule is:

> LiveKit-specific SDK objects stay inside the adapter; the rest of the application sees normalized transport commands, audio frames, events, usage, and errors.

## 2. Adapter split

The LiveKit integration has two internal parts:

1. `control_plane`: explicit agent dispatch, browser join-token creation, dispatch/room inspection, and cleanup.
2. `session_transport`: worker room connection, participant/track handling, microphone intake, agent-audio publication, realtime data, reconnect handling, quality summaries, and close.

The control API may use only the control-plane port. The worker may use only the session-transport port. The browser receives only a scoped room credential and the safe public LiveKit URL.

## 3. Dispatch and room model

Phase 0 uses explicit named-agent dispatch, not automatic dispatch.

- one room represents one voice session;
- one room expects one browser participant and one agent participant;
- one room-level LiveKit job starts the Python worker session;
- room names are opaque and generated as `va-rd-<random-uuid>`;
- participant identities are opaque and cannot contain email, phone, customer name, or other direct PII;
- dispatch is created only after the durable session and immutable configuration reference exist.

Session creation follows this order:

```text
Validate request/configuration
    -> create durable session
    -> allocate opaque room/participant identities
    -> create explicit LiveKit dispatch
    -> issue scoped browser token
    -> return safe session/transport response
```

An identical idempotent session-create retry reuses the same session, room, dispatch, and browser participant identity, but issues a fresh short-lived token. It never creates a second room or dispatch.

## 4. Dispatch metadata

Dispatch metadata is an untrusted locator, not authoritative session configuration.

Maximum encoded metadata size inside the application: 2 KiB.

Allowed fields:

- metadata schema version;
- `session_id`;
- `correlation_id`;
- `agent_config_id`;
- `environment`.

Prohibited fields:

- prompt or conversation history;
- transcript or generated response;
- user/customer PII;
- credentials, token, connection string, or secret;
- arbitrary browser-provided metadata;
- full agent configuration.

The worker reloads and validates the durable session and exact immutable configuration before starting any paid provider.

## 5. Browser access token policy

The backend generates the browser token with server-held LiveKit credentials. The token:

- is valid for the exact room and opaque browser participant identity;
- expires 10 minutes after issue in R&D;
- permits room join;
- permits microphone audio publication;
- permits subscription to agent audio;
- permits approved realtime data send/receive;
- does not grant room administration, participant removal, recording/egress control, or arbitrary room-metadata mutation;
- is never stored in MongoDB, ordinary logs, analytics, browser storage, or URL query parameters;
- is returned only in the HTTPS or approved `127.0.0.1` loopback JSON response body.

The browser never receives LiveKit API secret/server credentials. Token claims are constructed from backend-owned values, not copied from browser input.

## 6. Control-plane lifecycle and compensation

The control plane owns:

- explicit named-agent dispatch;
- scoped browser token generation;
- safe dispatch/room status lookup;
- dispatch cleanup where supported;
- best-effort room deletion after terminal finalization.

Failure compensation:

- dispatch failure: do not issue a token; finalize the durable session as `failed` with safe evidence;
- dispatch succeeds but token creation/response preparation fails: delete the dispatch/room best-effort, finalize the session as `failed`, and record cleanup failure separately if needed;
- cleanup failure: do not undo the terminal application state; record a bounded retryable transport-cleanup error.

No token is returned unless dispatch preparation has succeeded.

## 7. Worker connection

The worker accepts only the approved named room-level job. Its request handler sets the exact opaque `agent_participant_id` as the LiveKit participant identity during `req.accept(...)`; bounded job metadata supplies only the locator and the subsequent durable reload verifies it before paid providers start.

Connection sequence:

1. validate bounded job metadata;
2. reload durable session/configuration;
3. claim the worker assignment generation;
4. register room/participant/track listeners before connecting;
5. connect with audio-only auto-subscription;
6. verify the expected browser participant identity and microphone publication;
7. expose normalized transport-ready/audio events to the orchestrator.

Video is neither subscribed to nor published. Paid STT, conversation, and TTS work cannot start before durable validation and assignment claim succeed.

Replacement workers (Decision 067): a recovery replacement joins with the same `agent_participant_id` identity. LiveKit evicts any still-connected old participant with that identity; this eviction is expected. A worker that is evicted, or that self-fences because its local lease deadline passed or a write returned a `writer_epoch`/generation mismatch, follows the self-fence steps in the worker design (revoke output authorization, cancel LLM/TTS, `AudioSource.clear_queue()`, unpublish `agent-audio`, disconnect, best-effort `worker.self_fenced`, exit the job). It never rejoins the room or retries the claim.

## 8. Participant security

Expected membership is exactly one configured browser identity and one configured agent identity.

If an unexpected participant joins:

- raise a normalized transport-security error;
- stop current/new agent playback authorization;
- block new turns;
- enter the orchestrator's guarded terminal cleanup path;
- delete the room best-effort after evidence is persisted.

A replacement worker joining with the same configured agent identity, and the resulting LiveKit eviction of the old agent participant, is not an unexpected participant and must not trigger this path.

The adapter does not treat participant metadata as authorization. The durable session and token claims are authoritative.

## 9. Browser microphone intake

The adapter consumes the expected remote microphone audio track through LiveKit's audio stream API and converts frames to the approved internal audio contract:

- signed 16-bit little-endian PCM;
- mono;
- 48 kHz capture sample rate;
- recommended 20 ms frame duration;
- monotonic capture timestamp;
- safe track/session identity;
- no LiveKit SDK object outside the adapter.

Mute or temporary unpublish pauses listening and emits normalized track state; the browser reports explicit user mute/unmute with the transient `client.mic_muted` / `client.mic_unmuted` client events (section 13). It does not immediately end the session. Queue and overload behaviour remains owned by the orchestrator contract.

The browser requests microphone capture with `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: true`, and mono/channel-count `1`. Unsupported constraints are detected and evidenced; they are not silently treated as active. Browser AEC is defence-in-depth, not proof that a frame contains user speech.

The worker resamples each normalized 48 kHz microphone frame to 16 kHz mono Linear16 exactly once and fans the same 16 kHz frame to two independent consumers, avoiding double resampling:

1. the local `SpeechActivityDetector` for Silero VAD;
2. the baseline Deepgram STT adapter.

Silero VAD evaluates fixed 512-sample (32 ms at 16 kHz) windows, while the transport delivers 20 ms (320-sample) frames. The `SpeechActivityDetector` therefore rebuffers the 320-sample frames into 512-sample windows, carrying the remainder into the next window and preserving monotonic capture timestamps for each window. The STT consumer receives the 20 ms frames unchanged.

The microphone path remains active while agent audio is playing so local VAD can detect barge-in. WebRTC echo cancellation may reduce playback echo, but it is not treated as proof of user speech; echo/noise false-start behaviour is measured explicitly. While agent audio is playing, the Silero activation threshold for interruption candidates rises from 0.5 to 0.7 (configurable as `vad.playback_activation_threshold`, Decision 067); the 250 ms confirmation still applies. The transport adapter itself does not decide speech start, speech stop, endpointing, or interruption acceptance.

## 10. Agent audio publication

The agent publishes one continuous logical audio track named `agent-audio` for the session. The baseline publication source is 24 kHz mono Linear16 and uses `AudioSource(queue_size_ms=200)` rather than the SDK's one-second default.

- TTS audio is converted to the publication format inside the adapter;
- the orchestrator authorizes every playback segment/generation;
- accepted interruption follows the canonical order (Decision 067): confirm the candidate (≥250 ms of VAD speech at the playback threshold), increment the in-memory cancellation generation, cancel LLM streaming and TTS (queued segments first, then the active segment), clear the application playback queue and call `AudioSource.clear_queue()`, notify the browser with the interruption state message, then record events and evidence;
- cancelled or stale generations cannot publish further audio;
- reconnect does not authorize replay of old buffered speech;
- close stops the audio source and track before room disconnect.

The browser plays agent audio only through the LiveKit-attached WebRTC audio element for the subscribed `agent-audio` track. Custom Web Audio playback (for example decoding and scheduling frames through an `AudioContext`) is prohibited, because browser echo cancellation needs the WebRTC playout as its reference signal.

The transport adapter never starts TTS or decides what content may be spoken.

## 11. Realtime data topics

Approved agent-to-browser topics:

- `va.state.v1`;
- `va.transcript.v1`;
- `va.response.v1`;
- `va.playback.v1`;
- `va.error.v1`;
- `va.metrics.v1`.

Approved browser-to-agent topic:

- `va.client.v1`.

Approved server/control-plane-to-agent topic:

- `va.control.v1`.

Approved server/control-plane-to-browser message:

- the reconciler's best-effort `agent.recovering` state message, sent through the LiveKit server API (`RoomService.SendData`) on the existing `va.state.v1` topic to the expected browser identity after it fences an expired worker lease (Decision 067). It is transient, not durable, and carries only safe state fields.

`va.control.v1` initially permits only the bounded `session.end_requested` envelope targeted to the expected agent identity. The durable `session.end_requested` event (category `session`) is emitted by the API when it records the `termination_request`; the control envelope only notifies the worker. It uses reliable packet delivery but is not durable or authoritative. The worker validates session ID, request revision, correlation ID, and current assignment generation against MongoDB before acting. The durable `voice_sessions.termination_request` and session reconciler remain the recovery path when the participant is absent or packet delivery fails.

Every message uses a strict versioned envelope containing:

- `schema_version`;
- `event_id`;
- `session_id`;
- optional `turn_id` and `operation_id`;
- `event_type`;
- monotonically increasing session-local `sequence_number` where applicable; durable-event numbers come from the shared atomic persistence allocator rather than independent transport/API counters;
- UTC occurrence time;
- bounded topic-specific safe payload.

Maximum encoded reliable application payload: 8 KiB. Maximum encoded lossy application payload: 1,200 bytes. Oversized transient state is compacted or truncated to its newest safe representation and is not fragmented; an oversized reliable message is rejected and the browser reloads durable state through the control API. Unknown topic, schema version, field, or event type is rejected or ignored safely and recorded only when operationally useful. Transcripts, responses, and errors are sanitized before browser publication.

## 12. Reliable and lossy delivery

Use reliable data delivery for low-frequency state that the current UI should receive:

- session/agent state changes;
- final transcript and final response text;
- accepted interruption and playback cancellation;
- safe error/terminal state;
- turn/session summary.

Use lossy delivery for high-frequency state where freshness matters more than replay:

- partial transcript;
- transient listening/thinking indicators;
- playback progress;
- transient quality updates.

MongoDB remains the durable source for the approved event subset. LiveKit reliable data does not replace durable persistence or imply unlimited replay.

## 13. Browser playback acknowledgement

The browser may send these strict `va.client.v1` events:

- `client.ready`;
- `playback.started`;
- `playback.progress`;
- `playback.completed`;
- `playback.failed`;
- `client.mic_muted`;
- `client.mic_unmuted`;
- `client.latency_sample` (bounded per-turn browser playout span and RTT/2 estimate, §15).

These client events are transient and not durable unless 01 §9 / doc 02 lists them as stored.

Playback progress is rate-limited to at most one event every 250 ms. Acknowledgements include only the approved ack identity — (`worker_assignment.generation`, cancellation generation, segment id) — plus bounded timing/position values. The cancellation generation is in-memory worker state only; an acknowledgement whose worker generation does not match the current assignment is stale (Decision 067).

All browser-to-agent application messages share a participant-level token bucket of 20 messages/second with a burst of 40. The stricter playback-progress limit still applies. Excess lossy progress is dropped with a counter; an excess reliable message is rejected with safe `realtime_overload` evidence, and sustained abuse blocks further client events without expanding payload or log volume.

They support delivered/spoken-text evidence but are not security authority. Invalid, stale, impossible, or mismatched acknowledgements are ignored and may create safe diagnostic evidence. Confirmed, estimated, and unavailable spoken-text accuracy retain the definitions in the worker design.

## 14. Reconnect behaviour

The LiveKit SDK handles low-level transport reconnection. The adapter converts its lifecycle into normalized events.

On disconnect/reconnect start:

- stop or clear current playback;
- pause new turns and output authorization;
- set the orchestrator activity to `recovering`;
- begin the approved 20-second application reconnect window.

If the agent participant disappears while the browser remains connected, the browser immediately renders `recovering` from the normalized transport event instead of retaining stale `speaking`; durable ownership/recovery still comes only from the fenced lease/reconciler contract.

During worker recovery (Decision 067), the browser also:

- handles the reconciler's server-sent `agent.recovering` message on `va.state.v1` by showing "reconnecting agent…" and clearing any "speaking" state;
- falls back to "reconnecting" on its own if no agent audio or state message arrives for 5 s after the last lease-timing hint, in case the best-effort server message is lost;
- expects the replacement agent to appear with the same agent identity; the resulting eviction of the old agent participant is not an error.

An approved browser reconnect uses the same session, room, and browser participant identity with a refreshed scoped token. After success, the browser reloads durable state through FastAPI and the worker returns to listening without replaying stale audio. Expiry of the window enters terminal finalization.

## 15. Quality and transport measurements

The adapter exposes bounded aggregate measurements, not high-frequency raw network telemetry:

- connection and reconnection timestamps/durations;
- reconnect count;
- connected transport duration;
- participant/track join timing;
- available aggregate connection-quality state;
- approved audio publication/subscription failure counts.

Only state changes and useful aggregates are persisted. Packet-level data, microphone levels, and continuous quality samples are not stored as ordinary MongoDB events.

For response latency (Decision 067), the measurement adds these parts and never subtracts wall clocks across machines:

- worker side: the worker monotonic time from the last VAD speech frame of the committed turn to the first TTS frame written to `AudioSource`;
- browser side: from the first agent audio packet received to playout, read from WebRTC receiver stats for the `agent-audio` track (`jitterBufferDelay` / playout delay);
- a one-way network estimate of RTT/2 from WebRTC stats, recorded with its uncertainty.

The browser computes its span and the network estimate as bounded per-turn aggregates; raw stats samples are not stored. They are sent as the non-durable `client.latency_sample` browser event (01 §9) on `va.client.v1`, rate-limited to one per turn.

## 16. Cleanup

Terminal cleanup is idempotent and ordered:

1. block new input/output authorization;
2. stop and clear playback;
3. close audio source/stream and publications;
4. disconnect the worker from the room;
5. finalize available transport usage/errors/events;
6. finalize the application session through the orchestrator;
7. delete the room best-effort through the control plane.

Close operations are bounded. One failed close step does not prevent later cleanup steps. Room deletion failure is evidence for bounded retry, not a reason to return the application session to active state.

## 17. Normalized control-plane port

The replaceable control-plane port supports these behaviours without exposing LiveKit SDK types:

- prepare opaque room and participant identities;
- create explicit agent dispatch with bounded metadata;
- issue a scoped browser join credential;
- read safe dispatch/room status;
- delete dispatch where applicable;
- delete room;
- close control-plane resources.

Inputs and outputs are strict application models. Provider request IDs and status values are normalized before leaving the adapter.

## 18. Normalized worker transport port

The replaceable worker transport port supports:

- connect to the assigned session;
- verify expected participant and microphone track;
- receive normalized audio frames;
- publish authorized normalized agent audio;
- send approved reliable/lossy realtime events;
- receive approved client events;
- stop/clear playback;
- expose normalized connection/track/quality events and usage;
- close idempotently.

The orchestration port must remain implementable by a future Daily/Pipecat, Vapi, or custom WebRTC adapter.

## 19. Error normalization

LiveKit-specific failures map to stable application categories such as:

- authentication/authorization failed;
- dispatch failed;
- room connection failed;
- participant verification failed;
- microphone track unavailable;
- publish/subscribe failed;
- data send/receive invalid;
- reconnect exhausted;
- room/dispatch cleanup failed;
- transport rate-limited;
- transport unavailable;
- transport internal error.

Normalized errors include retryability, safe diagnostic code, operation/room context, and provider request ID when safely available. Raw tokens, metadata, payloads, exceptions, network addresses, and credentials are never exposed to the browser or ordinary database error documents.

## 20. Package/version policy

Implementation must pin an exact mutually compatible LiveKit Python/agent package set and browser SDK version. Floating `latest` or unbounded dependency ranges are prohibited.

An SDK upgrade requires adapter contract tests for dispatch, token claims, connection, audio intake/publication, reliable/lossy data, reconnect, cancellation, and cleanup before adoption.

## 21. Database mapping

`voice_sessions.transport` uses the existing approved bounded fields:

- `provider = livekit`;
- `adapter_version` = internal adapter version;
- optional `region_label` = safe configured/observed region label;
- `external_room_id` = opaque LiveKit room name/ID used by this session;
- `external_session_id` = explicit LiveKit dispatch ID;
- `browser_participant_id` = opaque expected browser identity;
- `agent_participant_id` = opaque expected agent identity.

The transport summary stores no join token, API key/secret, WebSocket credential, dispatch metadata blob, or participant access token. Detailed attempts and usage belong in `provider_operations`; durable lifecycle evidence belongs in `session_events` and `error_events`.

## 22. Cost evidence

The adapter records quantities and evidence, not hardcoded provider price:

- connected transport seconds;
- dispatch/room operation evidence where billable;
- reconnect count/duration;
- provider/SKU and region evidence when safely available;
- pricing/evidence timestamp and rate-card reference through the cost engine.

The cost calculator applies the approved dated rate card. Missing provider billing evidence remains unavailable/estimated, never silently zero.

## 23. Encryption and recording

Phase 0 uses LiveKit/WebRTC encrypted transport and keeps ordinary recording/egress disabled.

Additional LiveKit end-to-end encryption for agents is not enabled in Phase 0 because key distribution, recovery, browser compatibility, and operational support require a separate approved design. This does not authorize unencrypted media transport.

Recording, egress, SIP, and benchmark audio capture remain subject to their separate consent/storage decisions.

## 24. Phase 0 exclusions

- automatic agent dispatch;
- video publishing/subscription;
- multi-user or multi-agent rooms;
- SIP/telephony;
- recording or egress;
- arbitrary RPC methods, byte streams, files, or unrestricted data topics;
- browser room-administration grants;
- public deployment;
- adapter-owned conversation/business logic;
- additional agent E2EE;
- hardcoded provider pricing.

## 25. Acceptance criteria

- session creation produces one durable session and one explicit dispatch;
- identical create retries never duplicate room/dispatch and return a fresh scoped token;
- token lifetime and grants are least-privilege and the token is never persisted/logged;
- only the expected browser and agent identities are accepted;
- microphone and agent audio cross the adapter only as normalized frames;
- normalized microphone frames reach both local speech-activity detection and STT without LiveKit types escaping the adapter;
- reliable/lossy topics and payload sizes are allowlisted and bounded;
- stale cancellation/worker generations cannot publish audio or browser state;
- agent audio plays only through the LiveKit-attached WebRTC audio element, and the playback VAD threshold is 0.7;
- a replacement worker's same-identity eviction of the old agent does not trigger unexpected-participant termination, and an evicted or self-fenced worker never rejoins;
- disconnect immediately stops playback and the 20-second reconnect policy is enforced;
- cleanup and failure compensation are idempotent and produce safe evidence;
- LiveKit SDK types do not escape either adapter port;
- exact compatible SDK versions are pinned before implementation;
- no recording, video, SIP, multi-user flow, or public access is introduced.

## 26. Official LiveKit references

Implementation must be checked against the pinned SDK versions and current official documentation:

- [Agent dispatch](https://docs.livekit.io/agents/server/agent-dispatch/)
- [Agent jobs](https://docs.livekit.io/agents/server/job/)
- [Agent server options](https://docs.livekit.io/agents/server/options/)
- [Access-token generation](https://docs.livekit.io/home/server/generating-tokens)
- [Frontend authentication endpoint](https://docs.livekit.io/frontends/build/authentication/endpoint/)
- [Python access-token API](https://docs.livekit.io/reference/python/livekit/api/access_token.html)
- [Python agent-dispatch API](https://docs.livekit.io/reference/python/livekit/api/agent_dispatch_service.html)
- [Rooms, participants, and tracks](https://docs.livekit.io/intro/basics/rooms-participants-tracks/participants/)
- [Frontend media and data](https://docs.livekit.io/frontends/build/media-data/)
- [Realtime data transport](https://docs.livekit.io/transport/data/)
- [Agent encryption](https://docs.livekit.io/transport/encryption/agents/)
- [LiveKit Silero VAD](https://docs.livekit.io/agents/logic/turns/vad/)
