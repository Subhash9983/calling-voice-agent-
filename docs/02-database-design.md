# MongoDB Atlas Database Design

Status: Approved for Phase 0 R&D; later-phase extensions deferred  
Authority: Decisions 006–026, 029, 040, 044, 046, 051, 052, 054, 057, 063, 064, and 067  
Scope: Core and evaluation persistence contracts in the single R&D Atlas database  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Database product: MongoDB Atlas

## 1. Scope and approval boundary

This document defines the approved Phase 0 MongoDB Atlas data model for the browser voice sandbox and the approved Phase 0 evaluation collections; later provider benchmarking extensions remain deferred.

Approved:

- MongoDB Atlas is the primary durable database.
- MongoDB Atlas Flex is the shared R&D tier.
- AWS Mumbai (`ap-south-1`, Atlas `AP_SOUTH_1`) is the approved R&D provider and region.
- PyMongo Async with Pydantic and explicit repositories is the approved Python integration approach; no ODM or Motor.
- Ordinary R&D audio recording is OFF by default.
- Consented benchmark audio may be stored later outside ordinary MongoDB documents.
- The core collection list and reference strategy in Sections 3 and 16 are approved.

Pending explicit approval or milestone evidence:

- production Atlas tier, cloud provider, region, retention, backup, and point-in-time recovery;
- final transitive dependency resolution and any direct-version change forced by compatibility testing;
- provider-specific safe option/metadata schemas;
- production TTL/orphan-cleanup strategy beyond the approved R&D scheduled workflow;
- object-storage product for separately consented benchmark audio;
- optional realtime cache;
- public or multi-user authentication and authorization.

Evaluation implementation and migration timing is settled by Decision 054: the five evaluation collections are created in WP5 and the evaluation harness is built in WP12.

No deferred item should be implemented merely because it is described for future context in this approved design.

## 2. Data-modelling principles

### Embed only small, bounded summaries

Small session-level values that are normally read together may be embedded:

- active provider display labels;
- final latency summary;
- final usage and cost summary;
- consent snapshot;
- disconnect summary.

### Reference high-volume records

Store these in separate collections with `session_id` and, where applicable, `turn_id` references:

- conversation turns;
- provider operations;
- session events;
- cost entries;
- errors;
- feedback;
- evaluation results.

### Avoid unbounded arrays

Do not append every turn, event, provider operation, token, audio chunk, or error into one `voice_sessions` document.

### Normalize cross-provider fields

Normalized fields support comparison. Bounded provider-specific metadata may be retained only after removing secrets and unnecessary sensitive payloads.

### Preserve calculation evidence

Session totals are summaries. Detailed usage and cost entries remain the evidence used to reproduce those totals.

## 3. Approved core collection set

Core R&D collections:

1. `agent_configs`
2. `voice_sessions`
3. `conversation_turns`
4. `provider_operations`
5. `session_events`
6. `cost_entries`
7. `user_feedback`
8. `error_events`
9. `consent_records`

Collections added during benchmarking:

10. `evaluation_datasets`
11. `evaluation_cases`
12. `evaluation_runs`
13. `evaluation_results`
14. `evaluation_human_ratings`

The nine core collection names, five evaluation collection names/contracts, reference split, core field structures, launch index plan, 30-day R&D retention and deletion policy, validation architecture, and baseline R&D limits/defaults are approved. Provider-specific option schemas and production settings remain pending.

## 4. Common document fields

Where applicable, durable documents contain:

- internal MongoDB `_id`;
- application ID such as `session_id`, `turn_id`, or `operation_id`;
- `schema_version`;
- `created_at` in UTC;
- `updated_at` in UTC when mutable;
- `environment` label: `development`, `rd`, or `production`; `production` is reserved in the schema enums for the future, but Phase 0 startup rejects `APP_ENV = production` (Decision 067), so Phase 0 writes only `development` or `rd`;
- correlation identifiers;
- safe producer/source information.

Application IDs remain stable during migrations and exports. Optional MongoDB fields are omitted when unavailable unless a field's schema explicitly requires a value plus an availability/status enum. Durable documents do not alternate arbitrarily between missing and `null`; API projections may use `null` only where their response schema requires a stable key. Money, rates, precise billing quantities, and decimal measurements always use BSON `Decimal128`.

All persisted enum values in this document are lowercase `snake_case` (01 §2, Decision 057). Actor enums use the canonical values `anonymous_user`, `system`, `system_evaluation`, and `internal_reviewer` (plus `internal_tester` and `authenticated_user` where a field lists them); the bare value `anonymous` is never used.

## 5. `agent_configs`

Purpose: versioned runtime configuration for one voice-agent setup.

Status: **Field structure and baseline R&D timeout/retry limits approved.**

Root fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity; never exposed as the public ID. |
| `agent_config_id` | UUID string | Required, unique | Stable application identity for this exact configuration version. |
| `agent_id` | UUID string | Required | Groups all versions of one logical agent. |
| `name` | String | Required | Human-readable configuration name. |
| `description` | String | Optional | Short purpose or test description. |
| `version` | Integer | Required | Monotonically increasing version within an `agent_id`. |
| `status` | Enum | Required | `draft`, `active`, or `retired`. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development`, `rd`, or `production`. |
| `tags` | Array of strings | Optional, bounded | Search and experiment labels. |
| `config_checksum` | String | Required | Detects configuration drift and supports reproducibility. |
| `transport` | Object | Required | Approved transport adapter configuration. |
| `stt` | Object | Required | Approved speech-to-text adapter configuration. |
| `conversation_engine` | Object | Required | Approved LLM/conversation-engine configuration. |
| `tts` | Object | Required | Approved text-to-speech adapter configuration. |
| `turn_handling` | Object | Required | Interruption and endpointing behaviour. |
| `timeout_policy` | Object | Required | Bounded component and session timeouts. |
| `retry_policy` | Object | Required | Bounded retry behaviour. |
| `cost_rate_card_version` | String | Required | Rate-card version used for estimates. |
| `cost_currency` | String | Required | ISO 4217 currency code used by estimates. |
| `created_at` | UTC datetime | Required | Creation timestamp. |
| `updated_at` | UTC datetime | Required | Last permitted lifecycle change (activation, retirement, or expiry marking). |
| `revision` | Integer | Required | Optimistic-concurrency revision for lifecycle changes; incremented by every permitted update. |
| `created_by` | String | Optional until authentication is finalized | Creator identity. |
| `activated_at`, `activated_by` | UTC datetime, string | Optional | Activation audit fields. |
| `retired_at`, `retired_by` | UTC datetime, string | Optional | Retirement audit fields. |
| `expires_at` | UTC datetime | Required only for a retired configuration after reference-safe expiry is known | Earliest permitted R&D cleanup time. |
| `change_note` | String | Required when `version > 1` | Reason for the new version. |

Embedded `transport` fields:

- required `provider`, `adapter_version`, and `credential_ref`;
- optional `region_label` and validated `safe_options`.

Embedded `stt` fields:

- required `provider`, `model`, `adapter_version`, `language_mode`, `sample_rate_hz`, `audio_encoding`, `partial_transcripts`, and `credential_ref`;
- optional validated `safe_options`;
- for the Phase 0 baseline, persist `provider = deepgram`, `model = nova-3`, `language_mode = auto`, and safe options for expected `hi`/`en`, code switching, disabled translation/transliteration/diarization, punctuation/smart formatting, and at most 50 validated server-owned keyterms;
- never store the Deepgram/Sarvam API key or arbitrary browser/provider options.

Embedded `conversation_engine` fields:

- required `provider`, `model`, `adapter_version`, `max_output_tokens`, `prompt_id`, `system_instruction`, `system_instruction_version` (the prompt version), `prompt_checksum` (canonical prompt checksum, doc 10 §9), `safe_options`, and `credential_ref`;
- optional `temperature` and `tool_set_version`;
- for the Phase 0 baseline, persist `provider = openai`, `model = gpt-6-luna`, `max_output_tokens = 250`, an empty Phase 0 tool-set reference, and validated safe options for reasoning `none`, streaming, disabled tools/search/provider conversation storage, and provider-default sampling;
- store the exact separately approved prompt text/version in immutable configuration, never a browser-provided prompt or provider credential.

Embedded `tts` fields:

- required `provider`, `model`, `adapter_version`, `voice_id`, `language_mode`, `audio_encoding`, `sample_rate_hz`, `safe_options`, and `credential_ref`;
- optional `speaking_rate`;
- for Phase 0, persist `provider = sarvam`, `model = bulbul:v3`, `voice_id = priya`, 24 kHz mono `linear16`, `speaking_rate = 1.0`, `hi-IN`/`en-IN` routing, and validated streaming/500-character/no-cloning/no-storage/no-fallback options;
- an optional pronunciation-dictionary reference is allowed only after its contents are approved;
- never store API keys, raw audio, arbitrary browser voice options, or unapproved dictionary content.

Embedded `turn_handling` fields:

- `mode`;
- `interruptions_enabled` and `interruption_mode`;
- `minimum_interruption_ms`;
- `minimum_endpointing_ms` and `maximum_endpointing_ms`;
- `false_interruption_suppression`;
- `preemptive_generation`.

Embedded `timeout_policy` fields:

- `browser_join_ms` and `agent_join_ms`;
- `maximum_silence_ms` and `maximum_user_turn_ms`;
- `stt_finalize_ms`;
- `llm_first_token_ms` and `llm_total_ms`;
- `tts_first_audio_ms`;
- `idle_session_ms`, `maximum_session_ms`, `reconnect_window_ms`, and `graceful_shutdown_ms`.

Embedded `retry_policy` fields:

- `maximum_attempts`;
- `initial_backoff_ms` and `maximum_backoff_ms`;
- bounded `retryable_error_types`.

Rules:

- Never store provider secrets; `credential_ref` points to external secret configuration.
- Store exact provider/model IDs, not display labels alone.
- Validate `safe_options` using an allowlist for the selected adapter; never persist arbitrary credentials, headers, or unrestricted provider payloads.
- Preserve the system instruction and its version so a run can be reproduced.
- Every session references the exact configuration version used.
- Changing active configuration creates a new version rather than altering historical meaning.
- Do not permit silent edits to an active version; only lifecycle fields (`status`, activation/retirement audit fields, `expires_at`, `updated_at`, and `revision`) change after creation, through revision-checked operations.
- Active configurations never expire. For a retired R&D configuration, `expires_at` is no earlier than `retired_at + 30 days` and no earlier than the latest expiry of every referencing session/evaluation run; cleanup also verifies that no live reference remains.
- Baseline R&D timeout/retry values are approved in Section 24; provider-specific option schemas remain separately approved per adapter.

## 6. `voice_sessions`

Purpose: one browser or future phone conversation.

Status: **Field structure approved.**

Identity and configuration fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `session_id` | UUID string | Required, unique | Public application session identity. |
| `client_request_id` | UUID string | Required, unique | Idempotency key for the session-create request. |
| `correlation_id` | String | Required | Connects API, worker, adapter, event, and log activity. |
| `agent_id` | UUID string | Required | Logical agent used by the session. |
| `agent_config_id` | UUID string | Required | Exact immutable configuration used. |
| `agent_config_version` | Integer | Required | Configuration version for quick inspection. |
| `config_checksum` | String | Required | Detects configuration drift. |
| `environment` | Enum | Required | `development`, `rd`, or `production`. |
| `channel` | Enum | Required | `browser` initially; `phone` is reserved for the future. |
| `session_mode` | Enum | Required | `interactive_test`, `benchmark`, or `live`. |
| `worker_assignment` | Object | Optional | Current bounded worker claim, generation, heartbeat, lease, and release evidence. |
| `termination_request` | Object | Optional | Durable idempotent request to end the session. |
| `recovery_authorization` | Object | Optional; absent when no recovery is in progress | Stored reconciler recovery ownership and idempotent dispatch identity (Decision 067). |
| `join_token_requests` | Object array | Required, bounded | Recent idempotency IDs/fingerprints and issue evidence; never contains a token. |
| `worker_recovery_count` | Integer | Required | Bounded automatic crash-recovery attempts; Phase 0 maximum is one. |
| `event_sequence_counter` | Integer | Required | Last allocated durable event number, initially `0`; atomic increment-and-return yields the next one-based number. The increment changes neither `state_revision` nor `updated_at`. |
| `schema_version` | Integer | Required | Persistence-schema version. |

Initiator fields:

- required `initiator_type`: `internal_tester`, `authenticated_user`, `anonymous_user`, or `system`;
- optional `initiator_id`, populated when a safe application identity is available;
- do not copy names, email addresses, phone numbers, or other direct PII into the session summary.

State fields:

- required `status`: `created`, `connecting`, `active`, `ending`, `ended`, or `failed`;
- optional `agent_activity_state`: `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `interrupted`, `recovering`, or `error`;
- required integer `state_revision` for safe concurrent updates;
- optional `ended_by`: `user`, `browser`, `agent`, `server`, `transport`, or `system`;
- optional `disconnect_reason`: `user_ended`, `browser_closed`, `idle_timeout`, `maximum_duration`, `network_lost`, `transport_error`, `provider_error`, `server_shutdown`, or `unknown`;
- optional `terminal_error_id` referencing the relevant `error_events` document.

Optional embedded `termination_request`:

- required when present: `client_request_id`, `reason`, `requested_by`, `requested_at`, and integer `revision`;
- `requested_by` is `anonymous_user`, `system_timeout`, `system_reconciler`, or `system_evaluation`;
- `reason` is one of the `disconnect_reason` values listed under state fields;
- recording the request emits the durable `session.end_requested` event (category `session`);
- optional `worker_acknowledged_at`, `worker_acknowledged_generation`, and `completed_at`;
- it is authoritative over LiveKit control packets and cannot be cleared after acceptance;
- duplicate requests with the same client request ID reuse the existing request/result.

Optional embedded `worker_assignment`:

- required while claimed: `worker_instance_id`, `livekit_job_id`, integer `generation`, integer `writer_epoch`, integer `lease_revision`, `claimed_at`, `heartbeat_at`, and `lease_expires_at`;
- there is no `assignment_id`; an assignment is identified by the tuple (`generation`, `worker_instance_id`, `livekit_job_id`);
- optional `released_at` after clean release;
- initial claim and business lifecycle transitions use `state_revision`;
- heartbeat renewal is a compare-and-set that matches `session_id`, `worker_assignment.generation`, `worker_assignment.worker_instance_id`, `worker_assignment.livekit_job_id`, `worker_assignment.writer_epoch`, and `worker_assignment.lease_revision`, and requires `worker_assignment.lease_expires_at > $$NOW`; an expired lease cannot be renewed, so a late heartbeat from an old worker fails;
- a successful renewal sets `lease_expires_at = now + 15 s`, updates `heartbeat_at`, and increments `lease_revision` only; it does not change `state_revision`, session `updated_at`, or `last_activity_at`;
- the reconciler fence increments both `writer_epoch` and `lease_revision`;
- R&D heartbeat interval is 5 seconds, lease expiry is 15 seconds, and reconciliation interval is 5 seconds;
- old worker generations cannot authorize output;
- hostnames, process commands, and machine secrets are prohibited;
- due lease/reconciliation scans use the approved partial reconciliation indexes rather than collection scans.

Optional root `recovery_authorization` (Decision 067), absent when no recovery is in progress:

- required when present: `owner_instance_id`, `acquired_at`, `expires_at` (ownership lease, `acquired_at + 10 s`, renewed every 5 s by the owning reconciler while it works), `recovery_deadline_at` (first acquisition + 20 s; never reset by a takeover), integer `writer_epoch`, `recovery_dispatch_id`, and integer `owner_generation` (1 at first acquisition, incremented on each takeover);
- written by one atomic reconciler update conditioned on `status = active`, an expired lease, no `termination_request`, no `recovery_authorization` present, and `worker_recovery_count` below the limit; the same update increments `worker_assignment.writer_epoch` and `worker_assignment.lease_revision`, increments `worker_recovery_count`, sets `agent_activity_state = recovering`, and stores the generated `recovery_dispatch_id` before the dispatch is created;
- a retry after a reconciler crash reuses the stored `recovery_dispatch_id` and creates the dispatch only if it does not already exist;
- if the ownership lease `expires_at` passes before `recovery_deadline_at` (crashed or restarted reconciler owner), a later reconciler pass may take over with a fencing update that increments `writer_epoch`, replaces `owner_instance_id`, `acquired_at` and `expires_at`, increments `owner_generation`, and reuses the stored `recovery_dispatch_id`; a takeover does not increment `worker_recovery_count`, does not reset `recovery_deadline_at`, and is not blocked by the recovery limit;
- once `recovery_deadline_at` passes without a successful replacement claim, any reconciler instance, whether or not it owns the authorization, takes the fenced finalize path (increment `writer_epoch`, abandon any open turn, clean up the dispatch, mark the session `failed`);
- the replacement claim requires `status = active`, a matching `recovery_dispatch_id`, `recovery_deadline_at > $$NOW`, and no `termination_request`; the ownership lease `expires_at` does not gate the claim;
- the owning reconciler renews its ownership lease from an in-process task every 5 s with a compare-and-set matching `owner_instance_id` and `owner_generation` and requiring `expires_at > $$NOW`, which sets `expires_at = now + 10 s`; renewal stops when `recovery_authorization` is unset (successful claim or finalization) or when a renewal fails, after which that instance stops acting as owner. The ownership lease governs only which reconciler may run recovery steps; it never gates the replacement worker's claim;
- it is unset when the replacement worker claims successfully or when the session is finalized;
- `recovery_authorization.expires_at` (reconciler ownership) and `recovery_authorization.recovery_deadline_at` (replacement-claim deadline) are the only stored recovery deadlines; there is no root-level `recovery_deadline_at` field.

Bounded `join_token_requests` keeps at most the 10 most recent entries; a `client_request_id` evicted from the list is treated as a new request if it is presented again. Each entry contains `client_request_id`, canonical request fingerprint, `first_requested_at`, `last_issued_at`, and `issue_count`. An identical replay may issue a fresh scoped token; a reused ID with a different fingerprint is rejected. The join token itself is never persisted or logged.

Lifecycle fields:

- required `created_at` and `updated_at` UTC datetimes;
- optional `connecting_at`, `active_at`, `last_activity_at`, and `ending_at` UTC datetimes; terminal `ended_at` is required for both `ended` and `failed`;
- optional `connect_deadline_at`, `termination_deadline_at`, `idle_deadline_at`, and `maximum_duration_deadline_at`; the worker is the primary enforcer of idle and maximum-duration limits and the reconciler is the backstop;
- required nonterminal `next_reconcile_at`, equal to the minimum of the active deadlines: `connect_deadline_at`, `termination_deadline_at`, `worker_assignment.lease_expires_at`, `recovery_authorization.expires_at`, `recovery_authorization.recovery_deadline_at`, `idle_deadline_at`, and `maximum_duration_deadline_at`;
- optional `expires_at`, required after terminal `ended_at` is known and equal to `ended_at + 30 days` in R&D;
- optional final `duration_ms`, populated when the session terminates.

Embedded `transport` summary:

- required `provider` and `adapter_version`;
- optional `region_label`, `external_room_id`, `external_session_id`, `browser_participant_id`, and `agent_participant_id`;
- for LiveKit, map `external_room_id` to the opaque room name/ID and `external_session_id` to the explicit dispatch ID;
- use only opaque browser/agent participant identities without direct PII;
- never store room tokens, access tokens, API keys, credentials, or the dispatch metadata blob.

Embedded `provider_snapshot`:

- bounded `transport`, `stt`, `conversation_engine`, and `tts` component objects;
- preserve exact provider, model where applicable, adapter version, and TTS voice ID;
- use this snapshot for quick comparison; `provider_operations` remains the detailed operational evidence.

Embedded `language_summary`:

- required configured `language_mode`;
- optional `primary_detected_language`;
- optional bounded unique `detected_languages`;
- optional `language_switch_count`.

Embedded `recording` and privacy fields:

- required recording `mode`: `off` or `benchmark_with_consent`;
- required recording `status`: `not_requested`, `consent_pending`, `active`, `stopped`, or `failed`;
- optional `consent_record_id`, `started_at`, and `stopped_at`;
- required `privacy_policy_version` and `retention_policy_version`;
- ordinary R&D sessions use recording mode `off`;
- no audio blob or object-storage path is stored directly in the session summary.

Embedded `turn_summary` counters:

- `total`, `completed`, `interrupted`, `failed`, `abandoned`, and `discarded`.

Embedded `error_summary` counters:

- `total`, `recoverable`, and `unrecoverable`;
- optional `last_error_id`.

Embedded `latency_summary` metrics:

- STT finalization;
- LLM first token;
- TTS first audio;
- first audible response;
- complete turn;
- interruption latency.

Each available latency metric uses a bounded object containing `sample_count`, `average_ms`, `p50_ms`, `p95_ms`, and `maximum_ms`.

Embedded `usage_summary` fields:

- `connected_audio_seconds`, `transcribed_audio_seconds`, `input_tokens`, `cached_input_tokens`, `output_tokens`, and `reasoning_tokens`;
- `synthesized_characters`, `generated_audio_seconds`, `transport_session_seconds`, and `recorded_audio_seconds`;
- an unavailable optional value is omitted and is never silently written as zero.

Embedded `cost_summary` fields:

- required `currency`, `rate_card_version`, and `calculation_status`;
- optional `calculation_run_id` identifying the current calculation evidence;
- `calculation_status` is `pending`, `partial`, `final`, `failed`, or `unavailable`; `failed` means calculation was attempted and failed, while `unavailable` means required usage/rate evidence does not exist;
- `estimated_total` always uses MongoDB `Decimal128`;
- optional `provider_reported_total` and `finalized_at`;
- detailed quantities, unit rates, and calculations remain in `cost_entries`.

Optional sanitized `client_context` fields:

- UI application version;
- browser family;
- operating-system family;
- device class;
- microphone sample rate;
- exclude raw user-agent strings, IP addresses, access tokens, and unnecessary device fingerprints.

Rules:

- This is a quick mutable summary document, not the authoritative timeline.
- It does not contain complete arrays of turns, events, operations, costs, errors, or consent evidence.
- `ended` and `failed` are terminal and cannot transition back to an active state.
- allowed transitions are `created -> connecting|ending|failed`, `connecting -> active|ending|failed`, `active -> ending|failed`, and `ending -> ended|failed`;
- connection/start or exhausted recovery timeout ends `failed`; explicit user/idle/maximum-duration end completes `ended` after verified cleanup;
- Reconnection follows the bounded reconnect policy before a disconnect becomes terminal.
- Summary values can be rebuilt from their authoritative child records when required.

## 7. `conversation_turns`

Purpose: one user utterance and corresponding agent response.

Status: **Field structure approved.**

Identity and ordering fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `turn_id` | UUID string | Required, unique | Application turn identity. |
| `session_id` | UUID string | Required | Parent voice session. |
| `sequence_number` | Integer | Required | One-based order within the session. |
| `correlation_id` | String | Required | Connects the turn to events, operations, costs, and logs. |
| `agent_config_id` | UUID string | Required | Exact configuration used by the turn. |
| `environment` | Enum | Required | `development`, `rd`, or `production`. |
| `schema_version` | Integer | Required | Persistence-schema version. |

State fields:

- required `status`: `open`, `transcript_final`, `response_streaming`, `audio_streaming`, `completed`, `interrupted`, `failed`, `abandoned`, or `discarded`;
- required integer `status_revision` for safe concurrent updates;
- optional `failure_error_id` referencing `error_events`;
- optional `abandonment_reason`;
- optional `response_finish_reason`: `completed`, `maximum_tokens`, `content_filtered`, `cancelled`, `tool_call`, or `error`; `maximum_tokens` maps from an OpenAI Responses API result with `status = incomplete` and `incomplete_details.reason = "max_output_tokens"`;
- required `input_disposition`: `pending`, `accepted`, `empty`, `unusable`, or `timed_out`; `pending` is the initial value when the turn opens, before any transcript is available;
- required `response_completion_status`: `not_started`, `completed`, `truncated_partial`, `truncated_fallback`, `interrupted`, or `failed`.

Embedded `user_input`:

- required `input_mode`: `voice` or `text`;
- `final_transcript`, `language`, and `transcript_source` are required only when `input_disposition = accepted`;
- `transcript_source` is `stt`, `typed`, or `imported`;
- optional `stt_confidence`, `audio_duration_ms`, `word_count`, and `primary_stt_operation_id`;
- partial streaming transcripts are not persisted as durable turn content;
- empty/whitespace-only text is never stored as an accepted final transcript and never authorizes an LLM request;
- unavailable confidence is omitted rather than written as zero or arbitrary `null`.

Embedded `agent_response`:

- `generated_text`: complete conversation-engine output;
- `synthesized_text`: normalized text submitted to TTS;
- `spoken_text`: portion confirmed or estimated to have been played to the user;
- required response `language`;
- optional `primary_llm_operation_id` and `primary_tts_operation_id`;
- required `spoken_text_accuracy`: `confirmed`, `estimated`, or `unavailable`.

Generated, synthesized, and spoken text remain separate because text can be normalized before TTS and an interruption can stop delivery before the full response is heard.

Embedded `route_summary`:

- required `engine_type`: `general_llm`, `knowledge_engine`, or `fallback`;
- required `fallback_used`, `retrieval_used`, and `tool_call_count`;
- optional `fallback_reason`;
- retrieved documents and detailed tool/provider payloads are not embedded here.

Embedded `interruption_summary`:

- required `detected_count`, `accepted`, and `false_interruption_suppressed_count`;
- optional `accepted_at`;
- optional `phase`: `thinking`, `synthesizing`, or `speaking`;
- optional `reason`: `user_barge_in`, `explicit_cancel`, `session_end`, or `system_cancel`;
- optional `playback_stopped_at`, `audio_played_ms`, and `interruption_latency_ms`.

An accepted interruption terminates the current turn as `interrupted`; a subsequent user utterance receives a new `turn_id`. A sub-250 ms candidate may be recorded as a suppressed false interruption without cancelling playback or closing the current turn. Once accepted, an interruption never resumes stale audio automatically.

Lifecycle timestamps:

- required `created_at` and `updated_at` UTC datetimes;
- optional `user_speech_started_at`, `user_speech_ended_at`, and `transcript_final_at`;
- optional `response_generation_started_at` and `response_text_completed_at`;
- optional `playback_started_at`, `playback_ended_at`, and `completed_at`;
- optional `expires_at`, required after the parent session terminal retention anchor is known;
- `session_events` remains the authoritative fine-grained timeline.

Embedded `latency_summary`:

- `user_speech_duration_ms`;
- `stt_finalization_ms`;
- `llm_first_token_ms` and `llm_total_ms`;
- `tts_first_audio_ms`;
- `first_audible_response_ms`;
- `turn_completion_ms`;
- optional `interruption_latency_ms`.

Embedded `usage_summary`:

- `transcribed_audio_seconds`;
- `llm_input_tokens`, `llm_cached_input_tokens`, `llm_output_tokens`, and `llm_reasoning_tokens`;
- `synthesized_characters` and `generated_audio_seconds`;
- unavailable optional values are omitted rather than written as zero;
- detailed original usage remains in `provider_operations`.

Embedded `cost_summary`:

- required `currency`, `rate_card_version`, and `calculation_status`;
- optional `calculation_run_id` identifying the current calculation evidence;
- `calculation_status` is `pending`, `partial`, `final`, `failed`, or `unavailable`;
- `stt_cost`, `llm_cost`, `tts_cost`, optional `transport_allocated_cost`, and `total_cost` always use MongoDB `Decimal128`;
- detailed quantities, unit rates, and calculations remain in `cost_entries`.

Content-policy fields:

- required `content_policy_version`;
- required `redaction_status`: `not_required`, `pending`, `redacted`, or `failed`;
- no benchmark-asset references are permitted in Phase 0; a later storage approval must add its own versioned reference contract;
- final transcripts and agent text are retained for R&D accuracy analysis for 30 days in the R&D environment;
- ordinary-session raw audio is not stored in the turn document or retained elsewhere.

Rules:

- `session_id` plus `sequence_number` is unique.
- `completed`, `interrupted`, `failed`, `abandoned`, and `discarded` are terminal states.
- Allowed transitions are `open -> transcript_final|audio_streaming|interrupted|failed|abandoned|discarded`, `transcript_final -> response_streaming|audio_streaming|interrupted|failed|abandoned`, `response_streaming -> audio_streaming|completed|interrupted|failed|abandoned`, and `audio_streaming -> completed|interrupted|failed|abandoned`.
- `discarded` is allowed only from `open` (an empty or noise transcript never enters `transcript_final`) when the final transcript is empty or noise and no clarification fallback is sent; a noise turn is never recorded as `failed`.
- Once the first audio for a turn is authorized, the status is `audio_streaming` even while LLM text is still streaming; `response_streaming -> completed` without audio is allowed only when every segment was suppressed or the response had no speakable text; `transcript_final -> audio_streaming` is allowed only for the deterministic fallback/clarification phrase.
- A VAD speech start during agent playback opens a turn only after the interruption candidate is confirmed (at least 250 ms); a suppressed false-interruption candidate never creates a turn.
- `input_disposition = empty|unusable|timed_out` prohibits `transcript_final` and any LLM operation; a versioned clarification fallback may take the turn directly from `open` to `audio_streaming` and then `completed`.
- `response_completion_status = truncated_partial|truncated_fallback` is required when the provider length limit prevents a normal complete response; this field is the only representation of truncation, and the turn's terminal status is `completed`. No turn status represents truncation.
- Do not store raw audio, unbounded partial transcripts, provider credentials, unrestricted provider payloads, full event timelines, retrieved knowledge documents, or unbounded debug logs.
- Provider attempts, retries, errors, and cost evidence remain in their dedicated child collections.

## 8. `provider_operations`

Purpose: one STT, LLM, TTS, transport, retrieval, or future telephony attempt.

Status: **Field structure approved. Exact provider-specific metadata schemas remain pending.**

Identity and relationship fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `operation_id` | UUID string | Required, unique | Identity of this exact attempt. |
| `logical_request_id` | UUID string | Required | Groups an original request and all of its attempts. |
| `session_id` | UUID string | Required | Parent session. |
| `turn_id` | UUID string | Optional | Parent turn when the operation belongs to a turn. |
| `correlation_id` | String | Required | Connects events, logs, errors, usage, and costs. |
| `parent_operation_id` | UUID string | Optional | Dependency or parent operation. |
| `previous_attempt_operation_id` | UUID string | Optional | Previous attempt in the retry chain. |
| `attempt_number` | Integer | Required | One-based attempt number within the logical request. |
| `agent_config_id` | UUID string | Required | Exact runtime configuration used. |
| `environment` | Enum | Required | `development`, `rd`, or `production`. |
| `schema_version` | Integer | Required | Persistence-schema version. |

Classification fields:

- required `component`: `transport`, `stt`, `conversation_engine`, `tts`, `retrieval`, `tool`, or future `telephony`;
- required normalized `operation_type`, such as `connect`, `reconnect`, `transcribe_stream`, `generate_response`, `synthesize_stream`, `retrieve_context`, `invoke_tool`, or `place_call`;
- required Boolean `streaming`;
- provider-specific operation names do not replace normalized classification.

Embedded `provider_identity`:

- required `provider` and `adapter_version`;
- optional exact `model`, `provider_api_version`, `region_label`, `voice_id`, and `provider_request_id`;
- display names do not replace exact provider identifiers;
- credentials, authorization headers, signed URLs, and access tokens are prohibited.

State fields:

- required `status`: `created`, `queued`, `started`, `streaming`, `succeeded`, `failed`, `timed_out`, or `cancelled`;
- required integer `status_revision`;
- required `result_disposition`: `used`, `discarded_late`, `superseded_by_retry`, `superseded_by_fallback`, or `not_applicable`;
- `succeeded`, `failed`, `timed_out`, and `cancelled` are terminal;
- a cancelled operation's late result is `discarded_late` and cannot affect user-visible output.

Lifecycle and timing fields:

- required `created_at` and `updated_at` UTC datetimes;
- optional `queued_at`, `started_at`, `first_result_at`, `completed_at`, and `cancelled_at` UTC datetimes;
- optional `expires_at`, required after the parent session terminal retention anchor is known;
- optional `queue_duration_ms`, `time_to_first_result_ms`, `provider_duration_ms`, and `total_duration_ms`;
- runtime duration measurement uses a monotonic clock; persisted timestamps use UTC;
- first result means the first STT result, LLM token, playable TTS audio, or successful transport connection as applicable.

Embedded bounded `request_summary` may contain:

- input language;
- audio encoding and sample rate;
- input audio duration;
- token estimate or message count;
- input character count;
- requested output format and maximum tokens;
- streaming mode.

Embedded bounded `result_summary` may contain:

- detected language and optional confidence;
- partial-result and text-segment counts;
- output character count;
- output audio duration and format;
- finish reason and optional tool-call count;
- whether the output was empty.

Request/result summaries contain only allowlisted measurements and configuration evidence. They do not contain full prompts, transcript duplicates, generated-response duplicates, audio bytes, headers, or unrestricted provider JSON.

Embedded `usage`:

- required `reporting_status`: `provider_reported`, `measured`, `estimated`, or `unavailable`;
- bounded `items` array, with a recommended maximum of 20 items;
- each item contains normalized `unit`, `quantity` as `Decimal128`, `source`, and Boolean `estimated`;
- approved units are `connected_audio_seconds`, `transcribed_audio_seconds`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `synthesized_characters`, `generated_audio_seconds`, `transport_session_seconds`, `recorded_audio_seconds`, and `requests`;
- unavailable quantities are omitted and represented by `reporting_status = unavailable`, never written as zero.

Embedded `cost_summary`:

- required `currency`, `rate_card_version`, and `calculation_status`;
- optional `calculation_run_id` identifying the current calculation evidence;
- `calculation_status` is `pending`, `partial`, `final`, `failed`, or `unavailable`;
- optional `estimated_total` and `provider_reported_total` use MongoDB `Decimal128`;
- optional `reconciliation_status`: `not_checked`, `matched`, `mismatch`, or `unavailable`;
- optional `calculated_at`;
- a billed failed or cancelled operation receives a cost summary;
- detailed unit-rate evidence remains in `cost_entries`.

Embedded `retry`:

- required `is_retry`, `retryable`, and optional `retry_reason`, `backoff_ms`, and `next_operation_id`;
- retry only explicitly transient failures and remain within the configured attempt limit;
- stop retries immediately after cancellation;
- do not retry non-idempotent tool actions automatically.

Embedded `cancellation`:

- required `requested` and integer `worker_generation` (the `worker_assignment.generation` that issued the operation);
- optional `requested_at`, `reason`, and `acknowledged_at`;
- optional `requested_by`: `session`, `turn`, `user_interruption`, `timeout`, or `system`;
- the cancellation generation is in-memory only and is never persisted (Decision 067); the in-memory cancellation generation, `worker_generation`, and `writer_epoch` prevent late asynchronous output from an inactive operation from becoming visible.

Embedded `failure_summary`:

- optional `error_id` referencing `error_events`;
- normalized `error_type` and Boolean `retryable`;
- optional safe `provider_status_code`;
- `user_affected` and `fallback_succeeded`;
- full safe diagnostic details remain in `error_events`; raw provider errors and stack traces are prohibited.

Fallback fields:

- required Boolean `fallback_triggered`;
- optional `fallback_from_operation_id`, `fallback_to_operation_id`, and `fallback_reason`;
- primary and fallback attempts remain separate operation documents.

Optional `safe_provider_metadata`:

- must be validated by an adapter-specific allowlist;
- must have a strict size limit;
- cannot contain secrets, user content, headers, signed URLs, or unrestricted raw JSON;
- exact adapter-specific metadata schemas require separate approval before implementation.

Rules:

- Every retry gets a new operation ID.
- Attempts for one logical request share `logical_request_id` and retain the same turn ID when present.
- Credentials, authorization headers, and unrestricted raw provider payloads are never stored.
- Missing usage stays unavailable; it is not converted to zero.
- Complete prompts, transcripts, generated responses, raw audio, and detailed billing calculations remain outside this collection.

## 9. `session_events`

Purpose: append-only normalized session timeline.

Status: **Field structure approved. R&D retention is 30 days and durable event payload is limited to 16 KiB.**

Identity and ordering fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `event_id` | UUID string | Required, unique | Event identity and deduplication key. |
| `session_id` | UUID string | Required | Parent session. |
| `turn_id` | UUID string | Optional | Related turn. |
| `operation_id` | UUID string | Optional | Related provider operation. |
| `correlation_id` | String | Required | Connects the event to logs and request chains. |
| `sequence_number` | Integer | Required | Authoritative one-based order within the session. |
| `causation_event_id` | UUID string | Optional | Event that directly caused this event. |
| `supersedes_event_id` | UUID string | Optional | Earlier event corrected by this append-only event. |
| `schema_version` | Integer | Required | Common event-envelope schema version. |
| `payload_schema_version` | Integer | Required | Event-type payload schema version. |

Classification fields:

- required normalized `event_type`, such as `tts.first_audio`;
- required `category`: `session`, `transport`, `speech`, `stt`, `conversation`, `tts`, `playback`, `turn`, `worker`, `usage`, `cost`, `error`, or `consent`;
- required `severity`: `debug`, `info`, `warning`, `error`, or `critical`;
- required `visibility`: `internal` or `browser_safe`;
- an internal event is never exposed to the browser merely because it exists in this collection.

Approved durable event catalogue:

- session and transport: `session.created`, `session.connecting`, `session.active`, `session.end_requested`, `session.ending`, `session.ended`, `session.failed`, `transport.connected`, `transport.disconnected`, `transport.reconnecting`, `transport.reconnected`, `transport.quality_updated`;
- speech and STT: `user.speech_started`, `user.speech_ended`, `stt.stream_started`, `stt.final`, `stt.turn_finalized`, `stt.usage`, `stt.warning`, `stt.failed`, `stt.stream_closed`;
- conversation engine: `conversation.started`, `conversation.first_token`, `conversation.segment_ready`, `conversation.completed`, `conversation.usage`, `conversation.cancelled`, `conversation.failed`;
- TTS and playback: `tts.session_started`, `tts.segment_started`, `tts.first_audio`, `tts.segment_completed`, `tts.usage`, `tts.cancelled`, `tts.failed`, `tts.session_closed`, `playback.started`, `playback.completed`, `playback.cancelled`;
- turn control: `turn.opened`, `turn.completed`, `turn.interruption_detected`, `turn.interrupted`, `turn.failed`, `turn.abandoned`, `turn.discarded`, `turn.false_interruption_suppressed`;
- worker and recovery (category `worker`): `worker.lease_expired`, `worker.recovery_started`, `worker.recovery_claimed`, `worker.recovery_failed`, `worker.self_fenced` (best effort, written only if the database is reachable);
- consent and recording control: `consent.requested`, `consent.granted`, `consent.denied`, `consent.revoked`, `consent.expired`, `recording.started`, `recording.stopped`, `consent.fulfilment_started`, `consent.fulfilment_completed`, `consent.fulfilment_failed`;
- usage, cost, and error: `usage.recorded`, `cost.calculated`, `cost.recalculated`, `error.retry_scheduled`, `error.recovered`, `error.unrecoverable`.

Realtime-only events such as `stt.partial`, `conversation.text_delta`, and `tts.audio_frame` are not persisted individually in this collection. Browser client events `client.ready`, `playback.progress`, `playback.failed`, `client.mic_muted`, and `client.mic_unmuted` are not durable unless 01 §9 lists them as stored. `session.end_requested` is emitted by the API when it records a `termination_request`. 01 §9 is the single event vocabulary.

Timing and ordering fields:

- required `occurred_at` and `recorded_at` UTC datetimes;
- optional `producer_sequence_number`, Boolean `is_late`, and `late_by_ms`;
- `sequence_number`, not timestamp ordering alone, determines session-local order;
- API, worker, and maintenance writers obtain `sequence_number` from one shared repository allocator using `findOneAndUpdate`, `$inc: {event_sequence_counter: 1}`, and the updated counter value;
- Phase 0 allocates once per durable event; unused numbers after failed writes are allowed, but writer-local authoritative counters are prohibited;
- a duplicate delivery reuses the same `event_id` and is deduplicated;
- a late event does not mutate earlier history.

Embedded `producer`:

- required `service`, `service_version`, and `component`;
- optional safe `instance_id` and `adapter_version`.

Optional embedded `provider_context`:

- `provider`, optional `model`, optional `voice_id`, and optional `provider_request_id`;
- exact provider-operation details remain authoritative in `provider_operations`.

Optional embedded `state_transition`:

- `entity_type`: `session`, `turn`, `agent_activity`, or `operation`;
- `from_state`, `to_state`, and `state_revision`;
- used to identify and audit invalid or unexpected transitions.

Optional bounded `payload`:

- must follow an event-type-specific schema and size limit;
- may include a disconnect reason, interruption phase, retry attempt, fallback reason, safe error type, detected language, audio format, or aggregate segment count;
- cannot contain raw audio, complete prompts, complete transcripts, complete LLM responses, credentials, tokens, headers, unrestricted provider payloads, or stack traces;
- serialized event payload is limited to 16 KiB before persistence.

Optional bounded `measurements` array:

- each item contains `name`, `value` as `Decimal128`, `unit`, and optional `measurement_method`;
- suitable for individual latency or quality measurements such as first-token latency, first-audio latency, packet-loss percentage, or interruption latency;
- large time-series monitoring samples are not stored here.

Optional bounded `references`:

- `error_id`, `cost_entry_id`, and `consent_record_id` when applicable; Phase 0 has no benchmark assets (Decision 063), so no `benchmark_asset_id` reference exists;
- complete records remain in their dedicated collections.

Privacy and retention fields:

- required `environment`;
- required `retention_class`: `operational`, `diagnostic`, `audit`, `billing`, or `consent`;
- `expires_at` is required after the parent session terminal retention anchor is known; for `operational`, `diagnostic`, and `audit` events it equals `ended_at + 30 days`; for `consent` and `billing` events it is the later of `ended_at + 30 days` and the top-level `expires_at` of every consent record for the same session;
- required `redaction_status`: `not_required`, `pending`, `redacted`, or `failed`;
- R&D event retention is 30 days; production retention requires separate approval;
- `consent`- and `billing`-class events are excluded from session cleanup; they are retained with the session's `consent_records` and deleted by the protected consent workflow when their own `expires_at` passes, even though the parent session may already have been deleted.

Rules:

- Do not persist individual audio chunks, TTS frames, LLM tokens/text segments, STT partial transcripts, microphone levels, or high-frequency network samples as durable events.
- Consumers deduplicate by event ID.
- Sequence numbers determine session-local order; timestamps alone do not.
- Events are immutable; corrections append a new event using `supersedes_event_id`.
- Unknown future event types are ignored safely and logged.
- Optional diagnostic persistence must not unnecessarily block voice playback, but terminal session/turn events receive reliable persistence retries.
- R&D retention is 30 days and durable payload is limited to 16 KiB; production settings remain pending.

## 10. `cost_entries`

Purpose: reproducible component-level cost evidence.

Status: **Field structure approved. USD is the normalized reporting currency; INR may be used as a display conversion.**

Identity and calculation-lineage fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `cost_entry_id` | UUID string | Required, unique | Exact billing-line identity. |
| `calculation_run_id` | UUID string | Required | Groups every line produced by one calculation/recalculation. |
| `calculation_version` | Integer | Required | Monotonic version within the calculation scope. |
| `supersedes_calculation_run_id` | UUID string | Optional | Previous calculation run replaced by this run. |
| `session_id` | UUID string | Required | Parent session. |
| `turn_id` | UUID string | Optional | Related turn. |
| `operation_id` | UUID string | Optional | Exact provider attempt. |
| `logical_request_id` | UUID string | Optional | Retry-group reference. |
| `correlation_id` | String | Required | Connects usage, calculations, events, and logs. |
| `agent_config_id` | UUID string | Required | Exact runtime configuration. |
| `schema_version` | Integer | Required | Persistence-schema version. |

The calculation scope is the `scope` value (`session`, `turn`, or `operation`) together with its target ID (`session_id`, `turn_id`, or `operation_id`); `calculation_version` is monotonic within one session and scope target. Calculation runs are immutable. The latest successful run is the run with the highest `calculation_version` whose `calculation_status = final` within the same `session_id` and calculation scope; it supplies current totals, and lines from different versions are never added together. Session, turn, and provider-operation cost summaries may reference the current `calculation_run_id`.

Classification fields:

- required `component`: `transport`, `stt`, `conversation_engine`, `tts`, `retrieval`, `tool`, `recording`, or future `telephony`;
- required `cost_category`: `usage_charge`, `platform_fee`, `recording_charge`, `telephony_charge`, `minimum_charge`, `discount`, `credit`, `tax`, or `adjustment`;
- required `scope`: `session`, `turn`, or `operation`.

Embedded `provider_identity`:

- required exact `provider`;
- optional `model`, `voice_id`, `region_label`, `provider_sku`, `pricing_tier`, and `provider_request_id`;
- billing evidence uses exact IDs rather than marketing/display labels alone.

Embedded `quantity`:

- required `native_quantity` as `Decimal128` and `native_unit`;
- required `billable_quantity` as `Decimal128` and `billing_unit`;
- optional `conversion_factor` as `Decimal128`;
- required `conversion_method`;
- supported units include audio seconds/minutes/hours, token classes, characters, generated audio seconds, connected minutes, requests, storage GB-hours, and phone minutes;
- original provider usage is always preserved.

Embedded `rate`:

- required `unit_rate` and `rate_unit_quantity` as `Decimal128`;
- required original rate `currency`;
- required `pricing_basis`: `per_unit`, `tiered`, `flat`, `included_allowance`, or `negotiated`;
- optional `tier_from`, `tier_to`, `minimum_billable_quantity`, and `billing_increment`, using `Decimal128` where numeric;
- required `rate_effective_from`, optional `rate_effective_to`, and required `rate_card_version`;
- gross cost is `billable_quantity / rate_unit_quantity * unit_rate` before approved adjustments;
- usage spanning pricing tiers produces separate cost entries per tier.

Embedded `rate_source`:

- required `source_type`: `public_price`, `provider_documentation`, `contract`, `provider_invoice`, or `manual_override`;
- required `source_reference`, `retrieved_at`, and `effective_date`;
- optional `verified_by` and `evidence_note`;
- contract and negotiated rate evidence is restricted from browser clients.

Embedded `amounts`:

- required `gross_cost`, `discount_amount`, and `net_cost_original_currency` as `Decimal128`;
- optional `credit_amount` and `tax_amount` as `Decimal128`;
- required `tax_treatment`: `excluded`, `included`, `not_applicable`, or `unknown`;
- public-price R&D estimates are pre-tax;
- unknown tax is labelled `unknown` and is not silently converted to zero.

Embedded `currency_conversion`:

- required `original_currency` and normalized `reporting_currency`;
- normalized cross-provider reporting currency is USD;
- required `fx_rate` as `Decimal128`, `fx_source`, and `fx_effective_at`;
- required `converted_net_cost` as `Decimal128`;
- when currencies match, the FX rate is `1`;
- preserve historical FX evidence and original amounts; INR may be calculated for display/reporting without replacing them.

Embedded `rounding`:

- required `rounding_mode`, `decimal_places`, and `rounding_applied_at`;
- `rounding_applied_at` is `quantity`, `line_total`, or `invoice_only`;
- optional `billing_increment`;
- required Boolean `minimum_charge_applied`;
- provider rules are preserved when known; an unknown rule is recorded as an explicit assumption.

Evidence and calculation state:

- required `evidence_status`: `estimated`, `provider_usage_based`, `provider_cost_reported`, `invoice_reconciled`, or `manual`;
- required `calculation_status`: `pending`, `partial`, `final`, `failed`, or `unavailable`;
- estimates are never presented as provider-reported actual costs.

Embedded `reconciliation`:

- required `status`: `not_checked`, `matched`, `mismatch`, or `unavailable`;
- optional `provider_reported_cost`, `difference_amount`, and `difference_percent` as `Decimal128`;
- optional `checked_at` and safe `note`.

Allocation and aggregation fields:

- required `allocation_type`: `direct`, `session_prorated`, `shared`, or `unallocated`;
- required `aggregation_behavior`: `charge` or `allocation_only`;
- optional `source_cost_entry_id` for a derived allocation;
- optional `allocation_basis`: `duration`, `audio_seconds`, `turn_count`, or `token_count`;
- optional `allocation_ratio` as `Decimal128`;
- `charge` contributes to the session total;
- `allocation_only` supports turn analysis but is excluded from the session total to prevent double counting.

Calculation metadata:

- required `calculation_method`, `calculation_engine_version`, `calculated_at`, and `environment`;
- optional `calculated_by` and safe `notes`;
- `expires_at` is required after the parent session terminal retention anchor is known;
- methods may include `provider_reported_quantity_x_public_rate`, `measured_audio_x_public_rate`, `estimated_tokens_x_public_rate`, `provider_reported_cost`, or `manual_adjustment`.

Visibility:

- required `visibility`: `internal`, `browser_summary`, or `restricted_pricing`;
- browser summaries may expose component cost, session/turn total, currency, and estimated/actual status;
- negotiated rates, discounts, contract details, and invoice evidence remain restricted.

Rules:

- Preserve the provider's original currency and billing unit.
- Estimates and actual usage must remain distinguishable.
- Session and turn summaries reconcile with detailed cost entries.
- Use Python `Decimal` and MongoDB `Decimal128`; never use binary floating point for money or billing quantities.
- Failed, timed-out, cancelled, retry, and fallback operations receive cost entries when billable usage is known.
- Unknown usage or cost remains unavailable and is never converted to zero.
- Recalculation creates a new immutable calculation run and preserves prior history.
- Do not store API credentials, payment details, full invoice files, unrestricted contract content, or raw provider payloads.

## 11. `user_feedback`

Purpose: internal tester feedback tied to a session or turn.

Status: **Field structure, initial reason-code taxonomy, and R&D text/array limits approved. R&D retention is 30 days.**

Identity and relationship fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `feedback_id` | UUID string | Required, unique | Feedback identity. |
| `client_submission_id` | UUID string | Required, unique | Idempotency key for browser/API retries. |
| `session_id` | UUID string | Required | Related session. |
| `turn_id` | UUID string | Optional | Related turn. |
| `operation_id` | UUID string | Optional | Related provider operation. |
| `agent_config_id` | UUID string | Required | Exact configuration used. |
| `correlation_id` | String | Required | Connects feedback with operational evidence. |
| `supersedes_feedback_id` | UUID string | Optional | Earlier feedback corrected by this submission. |
| `schema_version` | Integer | Required | Persistence-schema version. |

Target fields:

- required `target_type`: `session`, `turn`, or `operation`;
- required bounded `aspects` containing one or more of `overall`, `transcription`, `response_quality`, `voice_quality`, `latency`, `interruption`, `transport`, or `ui`;
- `turn_id` is required for a turn target and `operation_id` is required for an operation target;
- every referenced turn or operation must belong to the stated session.

Embedded `submitter`:

- required `type`: `internal_tester`, `authenticated_user`, `anonymous_user`, or `system_evaluation`;
- optional `submitter_id`;
- optional safe `role`: `developer`, `qa`, `product`, `support`, or `domain_expert`;
- required `source`: `rd_browser_ui`, `review_console`, `evaluation_import`, or `api`;
- names, email addresses, and phone numbers are not copied into this document.

Optional quick sentiment:

- `thumb`: `up` or `down`;
- integer `overall_rating` from 1 to 5, where 1 is very poor and 5 is excellent;
- an unavailable rating remains absent rather than zero.

Optional embedded `scores`, each an integer from 1 to 5:

- `transcription_accuracy`;
- `response_correctness`;
- `response_relevance`;
- `response_helpfulness`;
- `voice_naturalness`;
- `pronunciation_quality`;
- `response_speed`;
- `interruption_handling`;
- `overall_experience`.

Structured reason fields:

- required `reason_taxonomy_version`;
- optional bounded `reason_codes` containing approved taxonomy values;
- transcription codes: `missed_words`, `wrong_words`, `wrong_language`, `mixed_language_failed`, `numbers_misheard`, `product_name_misheard`, `noise_handling_failed`, `speech_end_detected_early`, `speech_end_detected_late`;
- response codes: `incorrect_answer`, `hallucinated_information`, `irrelevant_answer`, `incomplete_answer`, `did_not_follow_instruction`, `too_verbose`, `too_short`, `unsafe_answer`, `wrong_language_response`, `context_lost`;
- voice codes: `unnatural_voice`, `robotic_voice`, `mispronunciation`, `wrong_voice_language`, `speaking_too_fast`, `speaking_too_slow`, `bad_pauses`, `volume_issue`, `audio_distortion`;
- latency/interruption codes: `slow_first_response`, `slow_transcription`, `slow_generation`, `slow_speech_start`, `long_mid_response_pause`, `barge_in_not_detected`, `false_interruption`, `playback_did_not_stop`, `agent_spoke_over_user`;
- transport/UI codes: `connection_failed`, `reconnection_failed`, `audio_breakup`, `microphone_issue`, `browser_permission_issue`, `ui_state_incorrect`, `other`.

Optional embedded `correction`:

- `corrected_user_transcript`;
- `suggested_agent_response`;
- `expected_action`;
- optional `correction_language`;
- corrections require review before they can become reusable benchmark/evaluation cases.

Optional `comment`:

- plain text only, limited to 4,000 Unicode characters;
- HTML and scripts are rejected;
- content redaction and access rules apply.

At least one of thumb, score, reason code, correction, or comment is required; empty feedback is rejected.

Optional backend-derived `provider_context`:

- `component`, exact `provider`, optional `model`, optional `voice_id`, and `adapter_version`;
- browser clients cannot submit arbitrary provider/configuration claims;
- the backend derives and validates this snapshot from the referenced records.

Embedded `review`:

- required `status`: `unreviewed`, `triaged`, `accepted`, `dismissed`, or `resolved`;
- optional `reviewed_by` and `reviewed_at`;
- optional `resolution_code`: `provider_issue`, `configuration_issue`, `prompt_issue`, `knowledge_issue`, `ui_issue`, `expected_behavior`, `duplicate`, `cannot_reproduce`, or `fixed`;
- optional safe `review_note`;
- required integer `review_revision` for controlled concurrent updates;
- original submitter content remains immutable while this review section may be updated by authorized reviewers.

Lifecycle and context fields:

- required `created_at`, `updated_at`, and `environment`;
- optional `experiment_id`;
- future optional `evaluation_run_id` and `evaluation_case_id`.

Privacy and retention fields:

- required `content_policy_version`;
- required `redaction_status`: `not_required`, `pending`, `redacted`, or `failed`;
- required `retention_class`: `feedback` or `evaluation_candidate`;
- `expires_at` is required after the parent session terminal retention anchor is known;
- comments and corrections are access-controlled because they may contain sensitive content;
- R&D feedback retention is 30 days; production retention remains pending.

Rules:

- Duplicate `client_submission_id` values do not create duplicate feedback.
- A tester correction creates a new record with `supersedes_feedback_id`; original submission content is never silently overwritten.
- Multiple testers may submit independent feedback for the same target.
- Do not embed raw audio, screenshots, files, HTML, scripts, or arbitrary provider payloads.
- Promotion to an evaluation case requires separate review and applicable consent.

## 12. `error_events`

Purpose: normalized searchable failures.

Status: **Field structure and initial error taxonomy approved. R&D retention is 30 days; production retention remains pending.**

Identity and relationship fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `error_id` | UUID string | Required, unique | Exact error occurrence and delivery-deduplication key. |
| `session_id` | UUID string | Required | Related session. |
| `turn_id` | UUID string | Optional | Related turn. |
| `operation_id` | UUID string | Optional | Related provider attempt. |
| `logical_request_id` | UUID string | Optional | Retry-group reference. |
| `event_id` | UUID string | Optional | Related timeline event. |
| `correlation_id` | String | Required | Connects logs and request chains. |
| `root_error_id` | UUID string | Optional | Original error in a cascading failure chain. |
| `caused_by_error_id` | UUID string | Optional | Direct parent error. |
| `error_fingerprint` | String | Required | Sanitized grouping key for comparable failures. |
| `schema_version` | Integer | Required | Persistence-schema version. |

Duplicate deliveries reuse `error_id` and are deduplicated. Separate real occurrences retain separate IDs even when fingerprints match so failure-rate calculations remain accurate.

Classification fields:

- required `error_taxonomy_version`;
- required `error_type`: `authentication_failed`, `permission_denied`, `configuration_invalid`, `rate_limited`, `quota_exhausted`, `connection_failed`, `connection_lost`, `provider_timeout`, `provider_unavailable`, `invalid_audio`, `empty_transcript`, `content_rejected`, `cancellation`, `realtime_overload`, `persistence_failed`, `unknown_provider_error`, or `internal_error`;
- required `category`: `authentication`, `authorization`, `configuration`, `rate_limit`, `quota`, `capacity`, `network`, `timeout`, `provider`, `input_validation`, `content_safety`, `cancellation`, `persistence`, or `internal`;
- `realtime_overload` maps to category `capacity`;
- required `severity`: `info`, `warning`, `error`, or `critical`.

Origin fields:

- required `component`: `browser`, `control_api`, `orchestrator`, `transport`, `stt`, `conversation_engine`, `tts`, `retrieval`, `tool`, `database`, `cost_engine`, or future `telephony`;
- required `origin`: `client`, `application`, `adapter`, `provider`, or `infrastructure`;
- optional `failure_phase`, such as `connect`, `stream`, `finalize`, `generate`, `synthesize`, `playback`, `persist`, `calculate_cost`, or `shutdown`.

Optional embedded `provider_context`:

- `provider`, optional `model`, optional `voice_id`, and `adapter_version`;
- optional `provider_request_id`, safe `provider_error_code`, HTTP status, WebSocket close code, and region;
- raw provider responses, headers, request bodies, and credentials are prohibited.

Expected-control-flow fields:

- required Boolean `is_expected`;
- required Boolean `counts_toward_failure_rate`;
- expected interruption-driven cancellation is normally excluded from provider failure-rate calculations;
- genuine provider timeout or failure normally contributes to the failure rate.

Embedded `retry`:

- required `retryable` and `retry_scheduled`;
- optional `retry_attempt_number`, `retry_after_ms`, and `retry_operation_id`;
- optional `retry_blocked_reason`: `not_retryable`, `attempt_limit_reached`, `session_cancelled`, `turn_cancelled`, `non_idempotent_action`, or `deadline_exceeded`;
- each failed retry creates a new `error_id` linked to its new operation.

Embedded `fallback`:

- required `attempted` and `succeeded`;
- optional `fallback_provider`, `fallback_model`, `fallback_operation_id`, and `failure_reason`;
- successful fallback does not remove or hide the original failure record.

Embedded `impact`:

- required `user_affected`;
- required `session_impact`: `none`, `degraded`, or `terminated`;
- required `turn_impact`: `none`, `delayed`, `failed`, `interrupted`, or `abandoned`;
- required `audio_impact`: `none`, `delayed`, `distorted`, `stopped`, or `unavailable`;
- required `cost_may_be_incurred`;
- recovery does not automatically imply that the user was unaffected.

Embedded `resolution`:

- required `status`: `open`, `retrying`, `recovered`, `unrecoverable`, or `ignored_expected`;
- required integer `revision` for controlled concurrent updates;
- optional `resolved_at`;
- optional `resolution_action`: `retry_succeeded`, `fallback_succeeded`, `reconnected`, `user_retried`, `configuration_changed`, `provider_recovered`, `session_ended`, or `none`;
- optional `resolved_by_operation_id` and safe `resolution_note`;
- original error classification remains immutable; authorized workflows may update only this resolution section.

Safe diagnostics:

- required stable `diagnostic_code` and sanitized `message_safe`;
- optional `exception_class_safe` and bounded allowlisted `safe_details`;
- optional opaque `restricted_log_reference`;
- prohibit API keys, headers, full requests/responses, prompts, transcripts, customer data, database connection strings, sensitive filesystem paths, and stack traces;
- full stack traces remain in a separate restricted operational logging system.

Optional embedded `state_snapshot`:

- session state, agent activity state, turn state, operation state, worker generation, and retry attempt number (the in-memory cancellation generation is not persisted);
- this is bounded failure-time context, not a complete application-state dump.

Optional embedded browser-safe `user_message`:

- stable `code`, sanitized `message_safe`, and `retry_allowed`;
- optional `suggested_action`: `wait`, `retry`, `check_microphone`, `check_connection`, `restart_session`, or `contact_support`;
- internal diagnostics and provider details are never exposed through this object.

Lifecycle fields:

- required `occurred_at`, `detected_at`, `recorded_at`, and `updated_at` UTC datetimes;
- optional `resolved_at` UTC datetime;
- runtime duration calculations use a monotonic clock where applicable.

Privacy and retention fields:

- required `environment`;
- required `redaction_status`: `not_required`, `pending`, `redacted`, or `failed`;
- required `retention_class`: `operational_error`, `security_error`, or `billing_error`;
- `expires_at` is required after the parent session terminal retention anchor is known;
- `security_error` and `billing_error` records are not deleted in the session deletion order; they expire through `ix_error_events_expiry` in a separate reviewed cleanup job, and a dangling `session_id` after the parent session is deleted is allowed;
- R&D error retention is 30 days; production retention remains pending.

Fingerprint rules:

- build `error_fingerprint` from normalized component, error type, provider, model, diagnostic code, and safe provider error code;
- never include transcript, prompt, user ID, session ID, raw message, or secret material in the fingerprint.

Rules:

- General Atlas documents must not contain credentials or unnecessary sensitive provider payloads.
- Expected cancellations must not distort provider failure-rate reporting.
- Retry, fallback, recovery, user impact, and potential cost remain independently queryable.
- Original diagnostic evidence is immutable; resolution updates are revision controlled.

## 13. `consent_records`

Purpose: explicit proof of consent for benchmark audio retention.

Status: **Field structure approved. R&D evidence retention is 30 days subject to asset-deletion completion; production/external retention, deletion deadlines, and legal/privacy requirements remain pending.**

Identity and consent-chain fields:

| Field | Type | Requirement | Purpose |
| --- | --- | --- | --- |
| `_id` | ObjectId | Required, database-generated | Internal MongoDB identity. |
| `consent_record_id` | UUID string | Required, unique | Exact consent decision. |
| `consent_chain_id` | UUID string | Required | Groups grant, denial, revocation, and expiry for one scope. |
| `client_submission_id` | UUID string | Required, unique | Idempotency key for duplicate browser/API submissions. |
| `session_id` | UUID string | Required | Session in which consent was requested. |
| `consent_receipt_id` | UUID string | Required, unique | Safe consent/revocation receipt reference. |
| `supersedes_consent_record_id` | UUID string | Optional | Previous decision replaced by this immutable record. |
| `correlation_id` | String | Required | Connects consent, authorization, events, and fulfilment. |
| `schema_version` | Integer | Required | Persistence-schema version. |

Each record represents exactly one scope and one decision. Decisions are immutable; a later decision appends a new record in the same chain.

Embedded `subject`:

- required `type`: `internal_tester`, `authenticated_user`, or `anonymous_user`;
- optional `subject_id`, `organization_id`, and pseudonymous `anonymous_subject_reference`;
- IP address, raw user-agent, and device fingerprint are prohibited as subject identity.

Required `scope`, exactly one of:

- `record_user_audio`;
- `record_agent_audio`;
- `retain_audio`;
- `internal_human_review`;
- `benchmark_evaluation`;
- `dataset_reuse`;
- `model_training`.

Phase 0 defaults ordinary recording to off. Benchmark recording, retention, and evaluation require explicit compatible grants. Dataset reuse is separate, and model-training consent is not requested or granted by default.

Bounded `data_categories`:

- `user_audio`, `agent_audio`, `transcript`, `agent_response`, `session_metadata`, or `evaluation_labels`;
- categories must be compatible with the selected scope.

Decision fields:

- required `decision`: `granted`, `denied`, `revoked`, or `expired`;
- required `effective_from` and `decision_at` UTC datetimes;
- optional `effective_until` and safe `decision_reason`;
- denial of optional recording does not prevent an ordinary non-recorded voice session.

Embedded `purpose`:

- required `purpose_code`: `rd_voice_quality`, `provider_benchmarking`, `pronunciation_testing`, `latency_testing`, `interruption_testing`, or `evaluation_dataset`;
- required bounded `purpose_description` and `purpose_version`;
- a new unrelated purpose requires a new consent decision.

Embedded `notice`:

- required `notice_id`, `notice_version`, `language`, `title`, bounded `text_snapshot`, `text_hash`, `privacy_policy_version`, `retention_policy_version`, and `presented_at`;
- optional `locale`;
- the snapshot and hash preserve exactly what was presented.

Embedded `affirmation`:

- required `method`: `checkbox_and_button`, `explicit_button`, or future `signed_form`;
- required `affirmed_at`, `ui_version`, and `presentation_surface`;
- `presentation_surface` is `rd_browser_ui` or `review_console`;
- optional `event_id` and safe `evidence_hash`;
- checkboxes cannot be preselected, silence is not consent, and starting a session is not recording consent.

Optional recording-authorization fields:

- `recording_authorization_id`, `authorized_at`, and `authorization_expires_at`;
- before recording, the backend verifies the latest decision is an active, matching, unexpired, unrevoked grant with compatible scope and policy versions;
- every retained benchmark asset references the exact grant.

Embedded `retention`:

- required `retention_allowed`, `retention_policy_version`, and `automatic_deletion_required`;
- optional `retention_duration_days`, `retention_expires_at`, and `storage_region`;
- indefinite retention is not the default; exact duration requires separate approval.

Revocation fields, present on a revocation record:

- `revoked_at`;
- `revocation_source`: `user`, `tester`, `administrator`, or `policy_expiry`;
- optional safe `revocation_reason`;
- revocation uses the same chain, references the previous grant, stops active recording, blocks new assets, emits an event, and begins applicable deletion fulfilment.

Embedded operational `fulfilment`:

- required `status`: `not_required`, `pending`, `queued`, `in_progress`, `completed`, `failed`, or `partially_completed`;
- required integer `revision` for controlled updates;
- optional `deletion_job_id`, `requested_at`, `started_at`, and `completed_at`;
- optional `affected_asset_count`, `deleted_asset_count`, and `failed_asset_count`;
- optional safe `failure_reason` and `verification_reference`;
- the consent decision remains immutable while authorized workers update only this fulfilment section;
- exact deletion deadlines require separate policy/legal approval.

Lifecycle and audit fields:

- required `created_at`, `recorded_at`, `environment`, `captured_by_service`, `capture_service_version`, and `record_checksum`;
- optional safe `recorded_by`.

Privacy and access fields:

- required `visibility` with value `restricted`;
- required `redaction_status`: `not_required`, `pending`, `redacted`, or `failed`;
- required `retention_class` with value `consent_evidence`;
- optional top-level `expires_at`: the earliest time this consent-evidence document itself may be deleted by the protected consent workflow; it is set only after any covered asset deletion is verified and is no earlier than 30 days after the applicable decision;
- `retention.retention_expires_at` is different: it is the time by which data or assets covered by the grant must be deleted, and it never authorizes deletion of the consent record itself;
- browser session-list APIs do not expose consent documents; users receive only a safe receipt/status response;
- raw signatures, identity documents, IP addresses, and browser fingerprints are prohibited;
- administrative access is restricted and audited;
- consent evidence is not deleted with ordinary diagnostic TTLs.

Rules:

- Recording cannot start without active consent.
- Recording status must be visible in the R&D UI.
- If consent validation or storage is unavailable, recording remains off; a non-recorded voice session may continue.
- Retained assets must reference the active grant, chain, permitted purpose, and retention expiry.
- Revocation and deletion deadlines require separate approval before external testing.
- Production/external use requires an applicable legal and privacy review; this schema alone is not legal compliance.

## 14. Deferred benchmark-audio storage boundary

Phase 0 has no `benchmark_assets` collection and no object-storage reference schema. Ordinary audio recording remains off. If reusable benchmark audio is later approved, its storage product, metadata collection, consent linkage, access controls, retention, deletion, and cost require a fresh database/architecture decision before any field or index is implemented.

## 15. Approved evaluation collections

`evaluation_datasets` stores immutable frozen dataset identity, version, composition, checksums, and lifecycle.

`evaluation_cases` stores immutable logical test input, expected behaviour, deterministic assertions, and human rubric.

`evaluation_runs` stores the exact dataset, configuration, prompt, build, dependency lock, execution policy, gates, and rate-card evidence for one run.

`evaluation_results` stores execution attempts for one run/case/repetition slot, with exactly one current attempt per slot and invalid attempts retained (doc 16), each holding normalized output, assertion results, metrics, latency, reliability, usage, cost, validity, and review summary.

`evaluation_human_ratings` stores individual versioned reviewer scorecards without overwriting prior submissions.

Detailed approved schema: `docs/16-evaluation-database-schema.md`. Per Decision 054, these collections, validators, indexes, and repositories are created in WP5, and the evaluation runner is built in WP12.

## 16. Approved reference map

```text
agent_configs
    |
    +---- voice_sessions
              |
              +---- conversation_turns
              |          |
              |          +---- provider_operations
              |          +---- cost_entries
              |          +---- user_feedback
              |          +---- error_events
              |
              +---- session_events
              +---- consent_records
                         |

evaluation_datasets
    |
    +---- evaluation_cases
    +---- evaluation_runs ---- agent_configs
              |
              +---- evaluation_results ---- optional session/turn/operation/cost evidence
                            |
                            +---- evaluation_human_ratings
```

## 17. Approved integrity rules

- application IDs are globally unique;
- child documents reference an existing session when written; retained `consent`/`billing`-class session events and consent records may keep a dangling `session_id` after the parent session expires;
- a turn belongs to its stated session;
- turn sequence numbers are unique within a session;
- event sequence numbers are unique within a session;
- retries use new operation IDs;
- cost entries do not overwrite original quantities or rates silently;
- terminal session states do not return to active states;
- Phase 0 has no benchmark assets (Decision 063); any future benchmark-asset metadata rule is deferred to the storage decision in Section 14.
- frozen evaluation datasets/cases are immutable;
- a run references one exact frozen dataset/configuration/rate-card snapshot;
- exactly one current evaluation result attempt exists per run/case/repetition slot; invalid attempts are retained (doc 16);
- failed and invalid samples remain visible;
- only current submitted human ratings contribute to derived aggregates.

## 18. Approved query patterns

The initial repository/API query catalogue supports:

- exact configuration lookup, agent version history, and active-configuration lists;
- exact session lookup, recent/status-filtered session lists, and configuration-specific session history;
- ordered session turns and event timelines;
- turn/session provider-operation history and logical-request retry chains;
- current cost-run breakdown, session cost history, operation cost, and double-count-safe component totals;
- session/turn feedback and feedback triage queues;
- session/operation errors, recurring fingerprints, and resolution queues;
- consent receipt/chain lookup, latest decision per session/scope, recording authorization, fulfilment queues, and retention-expiry review;
- evaluation dataset/case loading, run lifecycle/progress, ordered result/review queues, individual ratings, reconciliation, and expiry cleanup;
- later provider/model latency, reliability, quality, reason-code, and cost reports after measured R&D demand justifies analytical indexes.

Query rules:

- use exact normalized IDs and enums;
- use cursor pagination rather than deep `skip` pagination;
- use `created_at` plus `_id` for recency cursors and `sequence_number` for session timelines;
- project only fields needed by each endpoint;
- load detail collections with separate bounded/paginated queries rather than one unbounded session document;
- expose only approved repository queries, never arbitrary browser database filters;
- validate references in repositories because MongoDB does not enforce foreign keys.

## 19. Approved launch index plan

Index names are explicit and stable. `_id` indexes are MongoDB-managed and omitted below.

### `agent_configs`

- `uq_agent_config_id`: `{agent_config_id: 1}`, unique;
- `uq_agent_version`: `{agent_id: 1, version: 1}`, unique;
- `uq_agent_active_environment`: `{agent_id: 1, environment: 1, status: 1}`, unique partial where `status = active`;
- `ix_agent_config_list`: `{environment: 1, status: 1, name: 1}`.

### `voice_sessions`

- `uq_voice_session_id`: `{session_id: 1}`, unique;
- `uq_session_client_request_id`: `{client_request_id: 1}`, unique;
- `ix_sessions_recent`: `{environment: 1, created_at: -1, _id: -1}`;
- `ix_sessions_status_recent`: `{environment: 1, status: 1, created_at: -1, _id: -1}`;
- `ix_sessions_agent_config`: `{agent_config_id: 1, created_at: -1}`;
- `ix_sessions_reconcile_due`: `{environment: 1, status: 1, next_reconcile_at: 1}`, partial where status is nonterminal;
- `ix_sessions_lease_due`: `{environment: 1, status: 1, worker_assignment.lease_expires_at: 1}`, partial where an unreleased assignment exists;
- `ix_sessions_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

An initiator-history partial index is deferred until authenticated-user queries are frequent.

### `conversation_turns`

- `uq_turn_id`: `{turn_id: 1}`, unique;
- `uq_session_turn_sequence`: `{session_id: 1, sequence_number: 1}`, unique;
- `ix_turn_status_time`: `{environment: 1, status: 1, created_at: -1}`;
- `ix_turns_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

### `provider_operations`

- `uq_operation_id`: `{operation_id: 1}`, unique;
- `uq_logical_attempt`: `{logical_request_id: 1, attempt_number: 1}`, unique;
- `ix_turn_operations`: `{turn_id: 1, component: 1, created_at: 1}`, partial where `turn_id` exists;
- `ix_session_operations`: `{session_id: 1, created_at: 1}`;
- `ix_operation_status_recent`: `{environment: 1, status: 1, created_at: -1}`;
- `ix_provider_request_lookup`: `{provider_identity.provider: 1, provider_identity.provider_request_id: 1}`, partial where provider request ID exists;
- `ix_provider_operations_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

### `session_events`

- `uq_event_id`: `{event_id: 1}`, unique;
- `uq_session_event_sequence`: `{session_id: 1, sequence_number: 1}`, unique;
- `ix_turn_event_sequence`: `{turn_id: 1, sequence_number: 1}`, partial where `turn_id` exists;
- `ix_session_events_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

A separate event-type index is not required for the initial bounded per-session timeline.

### `cost_entries`

- `uq_cost_entry_id`: `{cost_entry_id: 1}`, unique;
- `ix_cost_calculation_run`: `{calculation_run_id: 1, aggregation_behavior: 1, component: 1}`;
- `ix_session_cost_versions`: `{session_id: 1, scope: 1, calculation_status: 1, calculation_version: -1}`;
- `ix_operation_cost`: `{operation_id: 1, calculated_at: -1}`, partial where `operation_id` exists;
- `ix_cost_entries_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

Session totals select the latest successful (`calculation_status = final`) session-scope calculation run and only `aggregation_behavior = charge`; `allocation_only` rows are excluded from the session total.

### `user_feedback`

- `uq_feedback_id`: `{feedback_id: 1}`, unique;
- `uq_feedback_submission`: `{client_submission_id: 1}`, unique;
- `ix_session_feedback`: `{session_id: 1, created_at: -1}`;
- `ix_feedback_review_queue`: `{environment: 1, review.status: 1, created_at: 1}`;
- `ix_turn_feedback`: `{turn_id: 1, created_at: -1}`, partial where `turn_id` exists;
- `ix_user_feedback_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

### `error_events`

- `uq_error_id`: `{error_id: 1}`, unique;
- `ix_session_errors`: `{session_id: 1, occurred_at: 1}`;
- `ix_operation_errors`: `{operation_id: 1, occurred_at: 1}`, partial where `operation_id` exists;
- `ix_error_fingerprint`: `{error_fingerprint: 1, occurred_at: -1}`;
- `ix_error_resolution_queue`: `{environment: 1, resolution.status: 1, severity: 1, occurred_at: 1}`;
- `ix_error_events_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

Reliability reports filter `counts_toward_failure_rate = true` so expected cancellations are excluded.

### `consent_records`

- `uq_consent_record_id`: `{consent_record_id: 1}`, unique;
- `uq_consent_submission`: `{client_submission_id: 1}`, unique;
- `uq_consent_receipt`: `{consent_receipt_id: 1}`, unique;
- `uq_recording_authorization`: `{recording_authorization_id: 1}`, unique partial where recording authorization exists;
- `ix_consent_chain`: `{consent_chain_id: 1, decision_at: -1}`;
- `ix_session_consent_scope`: `{session_id: 1, scope: 1, decision_at: -1}`;
- `ix_consent_fulfilment`: `{environment: 1, fulfilment.status: 1, created_at: 1}`;
- `ix_consent_retention_expiry`: `{environment: 1, retention.retention_expires_at: 1}`, partial where retention expiry exists;
- `ix_consent_records_expiry`: `{environment: 1, expires_at: 1}`, partial where the top-level consent-evidence expiry exists.

Environment prefixes apply to operational/list/queue scans and to every expiry index, because scheduled cleanup is environment scoped. Globally unique identity indexes and parent-scoped session/turn/chain lookups deliberately do not add an environment prefix because the scoped ID is already the selective authority and must remain globally collision-safe.

### Evaluation collections

The exact approved unique, runner, run-list, result-slot, review-queue, rating-revision, and expiry indexes for `evaluation_datasets`, `evaluation_cases`, `evaluation_runs`, `evaluation_results`, and `evaluation_human_ratings` are defined in `docs/16-evaluation-database-schema.md` Section 12.

### Deferred analytical indexes

Add only after query-volume evidence:

- provider operations by component/provider/model/start time;
- costs by provider/component/calculation time;
- feedback by reason code/date and provider/model/date;
- errors by environment/component/type/date and provider/model/date;
- authenticated initiator session history;
- cross-run provider/model/category/metric/reason-code evaluation analytics after measured demand.

### Intentionally avoided initially

- wildcard indexes;
- an individual index for every field;
- transcript/comment full-text search;
- broad provider-metadata indexes;
- high-cardinality debug-detail indexes;
- speculative benchmark indexes.

Reason-code arrays would create multikey indexes; these remain deferred until reporting volume justifies their write and storage cost. Sensitive-text search requires a separate privacy review.

### TTL indexes

No TTL index is approved for immediate creation. Session/evaluation execution evidence uses explicit `expires_at` fields, ordinary expiry-query indexes, and bounded scheduled child-first cleanup. Cost entries follow their session cleanup order rather than a generic diagnostic rule; consent/benchmark deletion remains workflow controlled. Database expiry is not treated as an authorization check.

### Index monitoring

After implementation, review query latency, documents examined versus returned, index size/usage, write latency, slow queries, and duplicate-key failures. Atlas recommendations are reviewed rather than applied automatically.

## 20. Retention model

Status: **Approved for the R&D environment only. Production/external-use retention requires a fresh decision.**

R&D policy:

| Data | Retention/action |
| --- | --- |
| Ordinary raw session audio | Not stored; realtime processing only. |
| `voice_sessions` | 30 days. |
| `conversation_turns` transcripts and responses | 30 days. |
| `session_events` | 30 days; `consent`- and `billing`-class events are retained with the session's consent records until the consent evidence expires. |
| `provider_operations` | 30 days. |
| `error_events` | 30 days. |
| `user_feedback` | 30 days. |
| `cost_entries` | 30 days for R&D. |
| Consented benchmark audio/assets | Not stored in Phase 0; a future approved storage workflow may use at most 30 days or earlier revocation deletion. |
| `evaluation_runs`, `evaluation_results`, and `evaluation_human_ratings` | 30 days from the terminal run retention anchor. |
| Active/frozen evaluation dataset/case definitions | Exempt while reusable; after retirement, delete only when unreferenced plus a 30-day safety period. |
| Active `agent_configs` | Retention cleanup exempt while active/required by the system. |
| Retired `agent_configs` | Eligible at the later of `retired_at + 30 days` or the latest referencing session/run expiry, and only when no retained record references the version. |
| `consent_records` | Retain while related assets exist and until deletion is verified; otherwise 30 days from the applicable decision. |

Rules:

- eligible R&D records receive a calculated `expires_at` value under a versioned retention policy;
- terminal session finalization uses `voice_sessions.ended_at` as the retention anchor and propagates `expires_at = ended_at + 30 days` to `voice_sessions`, `conversation_turns`, `session_events` (except `consent`/`billing` classes, whose later expiry is defined in Section 9), `provider_operations`, `error_events`, `user_feedback`, and `cost_entries`;
- evaluation execution evidence uses bounded scheduled child-first cleanup; dataset definitions are protected while active/frozen or referenced;
- revocation starts benchmark-asset deletion immediately rather than waiting for the 30-day maximum;
- delete the asset first, verify deletion, close the fulfilment record, and only then make the consent evidence eligible for deletion;
- active configuration records are never removed by operational TTL cleanup;
- cost and consent records are not placed under a generic diagnostic deletion rule; their cleanup follows their explicit policy/workflow;
- unknown/unverified asset deletion prevents deletion of the related consent evidence;
- production data never inherits this 30-day policy automatically;
- the scheduled R&D cleanup runs at least once per 24 hours in batches of at most 100 sessions and is idempotent;
- session deletion order is `user_feedback`, `error_events` (only `operational_error` class; `security_error` and `billing_error` records are removed by their own expiry index job after review, and their `session_id` references may dangle after the parent session is deleted), `cost_entries`, `provider_operations`, `session_events` (only `operational`, `diagnostic`, and `audit` classes), `conversation_turns`, then `voice_sessions`;
- `session_events` with `retention_class = consent` or `billing` are excluded from session cleanup and are deleted by the protected consent workflow after their own `expires_at`; their `session_id`, `turn_id`, `cost_entry_id`, and other references are allowed to dangle after the parent session and child records expire, and readers resolve such references as expired;
- each child collection is counted/verified after deletion; any failure stops parent deletion, records a safe cleanup error, and retries later;
- consent records/assets and retired configurations keep their separate protected workflows;
- dry-run selection and uniquely labelled disposable-fixture tests are required before the first real cleanup run.

## 21. Atlas security decisions

Status: **Partially approved for R&D: one dedicated database user. Network allowlisting and remaining deployment controls require separate confirmation/provisioning.**

Approved R&D database-user policy:

- create one dedicated MongoDB database user for the current development/R&D backend and agent services;
- grant `readWrite` only on the selected R&D database rather than cluster-wide administrative roles;
- do not create separate application, migration, read-only, analytics, or per-developer database users during this phase;
- never expose the MongoDB connection string or credentials to the React/browser client;
- load credentials in Python through environment-based secret configuration and keep local secret files out of source control;
- do not store the password in MongoDB documents, source code, planning documents, logs, screenshots, or generated reports;
- use TLS through the Atlas connection configuration;
- rotate the credential if it is exposed, a team member with access leaves, or the shared R&D access boundary materially changes;
- do not grant this user access to any future production database;
- select the username and generate the password only during separately authorized provisioning.

Trade-off accepted for R&D:

One shared application credential is simpler but does not provide per-developer database attribution or strong separation between runtime and migration actions. Backend application logs and application-level actor IDs remain the primary R&D attribution evidence.

Pending deployment-level decisions:

- Atlas network/IP access list;
- local developer connection process;
- credential distribution/rotation procedure;
- Atlas control-plane human access;
- tier-appropriate audit capability;
- field-level protection if future sensitive fields require it.

Production must use separately approved service identities, least-privilege roles, secret management, network controls, rotation, audit, and incident procedures.

## 22. Backup and recovery decisions

Status: **Approved for R&D: no database backup or restore guarantee. Production remains undecided.**

R&D policy:

- do not configure or operate application-managed MongoDB backups, scheduled exports, snapshot retention, point-in-time recovery, or cross-region recovery for the R&D database;
- treat R&D session, transcript, event, provider-operation, error, feedback, cost, and consent data as disposable within the approved 30-day policy;
- accept that accidental deletion, corruption, cluster loss, or operator error may cause unrecoverable R&D data loss;
- do not promise an R&D recovery-point objective, recovery-time objective, or restore service;
- source code, schemas, migration scripts, documentation, and non-secret seed configuration remain version controlled, but this is not a backup of Atlas records;
- secrets are never included in exports or source control;
- a manual export or backup may be introduced only through a new explicit approval;
- any provider/platform internal replication or resilience is not treated as a project-controlled backup or restore guarantee.

Before production or external-user deployment, separately approve recovery-point/recovery-time objectives, backup technology, encryption/access, snapshot and point-in-time retention, geographic requirements, deletion propagation, and restore testing.

## 23. Environment separation

Status: **Approved for the current R&D phase: one Atlas Flex cluster and one MongoDB database. Production remains separate and undecided.**

Current R&D policy:

- use one AWS Mumbai Atlas Flex cluster;
- use one MongoDB database for local development and shared R&D activity;
- do not create separate local-development and shared-R&D Atlas databases at this phase;
- distinguish records using the approved `environment` field, primarily `development` and `rd`;
- require repository queries and cleanup jobs to apply the intended environment filter where environment-scoped behaviour is expected;
- use unique session/run IDs so developers do not collide even inside the shared database;
- label seed/demo records and automated test runs so they can be identified and cleaned safely;
- do not allow local destructive cleanup commands to target the complete shared database;
- do not introduce production data, credentials, or customer exports into this database;
- use the default database name `voice_agent_rnd`, configurable through `MONGODB_DATABASE` (doc 12, Decision 067); source code reads it from configuration;
- `APP_ENV` is `development` or `rd`; `production` is reserved in schema enums but rejected at Phase 0 startup.

Before production or an external pilot:

- choose a separate production database/project/cluster topology;
- create production-specific credentials, network controls, backup/recovery, retention, monitoring, and access policies;
- do not reuse the R&D database as production by simply changing its label;
- do not copy R&D or production data across environments without an approved migration/sanitization plan.

Trade-off accepted for R&D:

One database reduces setup and cost but provides weaker isolation. An incorrect development query could affect shared R&D data, and the no-backup policy means that loss may be unrecoverable. Environment filters, least-privilege repositories, unique IDs, and narrowly scoped cleanup are therefore mandatory.

## 24. Schema validation and evolution

Status: **Validation architecture and baseline R&D numeric, text, array, payload, timeout, and retry limits approved.**

Validation layers:

1. browser/API request validation;
2. strict Pydantic contract validation;
3. domain/repository invariant validation;
4. strict MongoDB collection `$jsonSchema` validation;
5. unique and partial indexes for approved database-enforceable constraints.

Approved Python model boundaries:

- separate API request and response models;
- separate domain models;
- separate MongoDB persistence-document models;
- separate normalized event and provider-adapter input/output models;
- separate application-settings models;
- do not expose persistence models, MongoDB `_id`, restricted pricing, or internal diagnostics directly to the browser;
- do not pass unrestricted dictionaries through conversation-core or repository boundaries.

Pydantic rules:

- reject unknown fields using strict/forbid-extra configuration;
- enforce approved enums, required fields, canonical UUID strings, timezone-aware UTC datetimes, nested structures, and explicit optionality;
- reject empty or coerced values when the field contract requires a real string, Boolean, integer, decimal, or identifier;
- bound arrays/strings/payloads after their numeric limits are separately approved;
- prevent the browser from supplying arbitrary provider IDs, model IDs, server endpoints, prompts, credentials, or persistence-only fields.

MongoDB validators:

- create validators for all nine core collections and, per Decision 054 (WP5), the five approved evaluation collections;
- use `validationLevel: strict` and `validationAction: error` for new writes;
- validate required root fields, BSON types, approved enums, required embedded objects, schema version, and approved basic bounds;
- reject unknown fields in stable root and security-sensitive embedded structures;
- allow provider-specific bounded containers only after adapter allowlist validation;
- coordinate validator changes with reader, writer, and migration deployment order.

Repository/service invariants include:

- referenced sessions/turns/operations belong together;
- valid state transitions and terminal-state rules;
- current active consent before recording;
- retry limits and idempotency restrictions;
- cost-run and allocation consistency;
- immutable-field protection;
- environment-scoped operations and safe cleanup.

Repository updates are named operations such as session transition, turn finalization, event append, error resolution, feedback review, and consent fulfilment. A generic unrestricted document-update method is prohibited.

Optimistic concurrency:

- use `agent_configs.revision`, `voice_sessions.state_revision`, `worker_assignment.lease_revision` (heartbeat compare-and-set only), `termination_request.revision`, `status_revision`, `review_revision`, `resolution.revision`, `fulfilment.revision`, and, for evaluation records (doc 16), `evaluation_runs.status_revision`, `evaluation_datasets.revision`, `evaluation_cases.revision`, `evaluation_results.result_revision`, and `evaluation_human_ratings.rating_revision` where approved;
- update only when the stored revision matches the caller's expected revision;
- reject stale updates and reload current state;
- retry only when the operation is safe and idempotent.

Encoding rules:

- Python `Decimal` maps to MongoDB `Decimal128`;
- aware datetimes map to BSON UTC datetimes;
- enums persist as approved strings;
- UUID application IDs persist as canonical strings;
- MongoDB `_id` remains an internal ObjectId;
- unavailable optional measurements are omitted, carry their explicit availability/status evidence, and never default silently to zero;
- persistence codecs remain inside the repository/mapping layer rather than domain logic.

Secret rules:

- secret settings use protected application configuration types;
- persistence models do not contain API-key, access-token, authorization-header, or connection-string fields;
- documents may contain approved `credential_ref` values only;
- logging and serialization redact secrets.

Failure behaviour:

- invalid browser/API input returns a safe field-level `422` without a database write;
- invalid provider output fails the normalized operation and creates a safe error record without persisting unrestricted raw payloads;
- MongoDB validation rejection becomes a safe `persistence_failed` error and does not dump the rejected document to general logs;
- revision conflict rejects the stale mutation and follows safe retry/reload rules.

Schema evolution:

- every document carries `schema_version`;
- new writes use the current version;
- readers support the current and controlled previous version during migration;
- additive optional fields may remain compatible;
- changing/removing field meaning or type requires a new schema version;
- deploy readers compatible with old/new documents before changing the validator/writer;
- dry-run migrations and reconcile counts before material writes;
- destructive migration requires separate approval because R&D has no backup;
- retain migration evidence and rollback instructions where rollback is technically possible.

Validation test coverage includes minimum/complete valid fixtures, missing fields, wrong types, unknown fields, invalid enums, excessive text/arrays, secret-field rejection, broken relationships, invalid state changes, stale revisions, MongoDB validator integration, and serialization round trips.

### Approved baseline R&D limits

All text limits count Unicode characters. Serialized object/payload limits count UTF-8 bytes after canonical JSON serialization. A lower adapter/provider limit overrides these maxima and must be validated before making the provider request.

#### Common identifiers and labels

| Value | Maximum |
| --- | ---: |
| Human-readable name | 100 characters |
| Short label, enum-adjacent label, or tag | 50 characters |
| Description | 500 characters |
| Safe reason/note | 2,000 characters |
| Provider/model/voice/SKU/region identifier | 256 characters |
| Correlation or opaque external request ID | 256 characters |
| Tags | 20 items, unique |
| Detected languages | 10 items, unique |

Canonical UUID strings must be exactly 36 characters. IDs generated internally use the one approved canonical representation; arbitrary user-supplied identifier formats are rejected.

#### Configuration and provider-safe containers

| Value | Maximum |
| --- | ---: |
| System instruction | 50,000 characters |
| `safe_options` | 16 KiB and 50 top-level keys |
| `safe_provider_metadata` | 16 KiB and 50 top-level keys |
| `safe_details` | 8 KiB and 30 top-level keys |
| Retryable error types | 20 unique items |

Every key must be on the selected adapter's allowlist; size allowance never authorizes arbitrary fields or secrets.

#### Session and turn content

| Value | Maximum |
| --- | ---: |
| Session duration | 30 minutes |
| One user speech turn | 120 seconds |
| Final user transcript | 10,000 characters |
| Generated agent text | 20,000 characters |
| Text submitted to TTS | 20,000 characters |
| Stored spoken-text estimate | 20,000 characters |
| Detected-language summary | 10 languages |

The 30-minute maximum is an R&D safety limit, not a production call-duration decision.

#### Event, operation, and error limits

| Value | Maximum |
| --- | ---: |
| Durable event payload | 16 KiB |
| Event measurements | 20 items |
| Event related-record references | 8 items |
| Operation request summary | 8 KiB |
| Operation result summary | 8 KiB |
| Normalized operation usage items | 20 items |
| Safe internal diagnostic message | 1,000 characters |
| Browser-safe error message | 500 characters |
| Error `safe_details` | 8 KiB |
| Resolution/review note | 2,000 characters |

Audio chunks, partial transcripts, LLM token segments, and TTS frames remain realtime-only regardless of payload size.

#### Feedback limits

| Value | Maximum |
| --- | ---: |
| Feedback aspects | 8 unique items |
| Reason codes | 10 unique items |
| Comment | 4,000 characters |
| Corrected transcript | 10,000 characters |
| Suggested agent response | 20,000 characters |
| Expected action | 2,000 characters |

#### Consent limits

| Value | Maximum |
| --- | ---: |
| Notice title | 200 characters |
| Notice text snapshot | 20,000 characters |
| Purpose description | 1,000 characters |
| Decision/revocation/failure reason | 2,000 characters |

#### Document-size safety caps

Application validation rejects documents above these serialized BSON-target safety caps, well below MongoDB's platform maximum:

| Collection | Maximum document size |
| --- | ---: |
| `agent_configs` | 256 KiB |
| `voice_sessions` | 128 KiB |
| `conversation_turns` | 256 KiB |
| `provider_operations` | 128 KiB |
| `session_events` | 32 KiB |
| `cost_entries` | 64 KiB |
| `user_feedback` | 64 KiB |
| `error_events` | 64 KiB |
| `consent_records` | 128 KiB |
| `evaluation_datasets` | 128 KiB |
| `evaluation_cases` | 256 KiB |
| `evaluation_runs` | 256 KiB |
| `evaluation_results` | 256 KiB |
| `evaluation_human_ratings` | 64 KiB |

#### Timeout defaults and hard limits

Approved initial R&D defaults:

| Policy | Default |
| --- | ---: |
| Browser join timeout | 15,000 ms |
| Agent join timeout | 20,000 ms |
| Maximum silence before idle activity state | 60,000 ms |
| STT finalization timeout | 3,000 ms |
| LLM first-token timeout | 8,000 ms |
| LLM total timeout | 45,000 ms |
| TTS first-audio timeout | 5,000 ms |
| Maximum user-turn duration | 120,000 ms |
| Session idle timeout | 300,000 ms |
| Maximum session duration | 1,800,000 ms |
| Reconnect window | 20,000 ms |
| Graceful shutdown timeout | 10,000 ms |

Configuration may tune these within validation bounds after measured tests, but a value above the approved maximum session/turn limit requires a new decision.

#### Retry defaults

- maximum three total attempts per logical provider request: initial attempt plus up to two retries;
- initial backoff 250 ms;
- maximum backoff 2,000 ms;
- exponential backoff with jitter;
- retry only allowlisted transient errors;
- stop immediately after session/turn/operation cancellation;
- never automatically retry a non-idempotent tool action.

#### Voice turn-handling defaults

- speech-activity engine: local Silero VAD behind `SpeechActivityPort`;
- VAD input: 16 kHz mono PCM;
- initial activation threshold: 0.5;
- `vad.playback_activation_threshold`: 0.7 while agent audio is playing (the 250 ms confirmation still applies; Decision 067);
- minimum speech duration: 50 ms;
- prefix padding: 500 ms;
- initial local silence detection: approximately 550 ms;
- minimum accepted interruption duration: 250 ms;
- minimum endpointing delay: 700 ms;
- maximum endpointing delay: 1,000 ms (values above 1,000 ms require latency-budget re-approval; Decision 067);
- interruptions enabled;
- sub-250 ms false-interruption suppression enabled;
- endpoint delay measured from the last detected speech frame, not added after the VAD silence window;
- accepted interruptions never resume stale audio automatically;
- preemptive generation disabled initially so R&D measurements are easier to interpret.

#### Numeric precision

- use finite Python `Decimal` values only;
- persist money, quantities, unit rates, FX rates, allocation ratios, and measurement decimals as MongoDB `Decimal128`;
- allow up to 12 fractional decimal places for quantities, rates, costs, and FX evidence;
- allow up to 6 fractional decimal places for percentages;
- preserve provider rounding rules; otherwise round only at the final reporting boundary using the versioned calculation method;
- negative values are rejected except where an explicitly approved credit/adjustment representation requires them.

## 25. Acceptance criteria

The database design is ready for implementation only after explicit approval of:

- core collection list;
- document fields and required/optional status;
- embedding and reference boundaries;
- integrity rules;
- query patterns and indexes;
- retention periods;
- Atlas tier, cloud provider, and region;
- Python driver and repository approach;
- Pydantic, repository, MongoDB, and index validation approach;
- backup and restore requirements;
- authentication and secret management;
- consent and benchmark-asset deletion workflow.

## 26. Confirmation sequence

Take one decision at a time:

1. Core collection list and reference strategy — **Approved**.
2. R&D Atlas tier — **Approved: Flex**.
3. R&D cloud provider and region — **Approved: AWS Mumbai (`ap-south-1` / `AP_SOUTH_1`)**. Production review remains required.
4. Python driver and repository approach — **Approved: PyMongo Async + Pydantic + explicit repositories; no ODM or Motor**.
5. Field definitions — **Approved for all nine core collections and five evaluation collections**. Benchmark-audio asset detail remains deferred until storage/consent approval.
6. Query patterns and indexes — **Approved: focused launch indexes plus evidence-based deferred analytical indexes**.
7. Retention periods — **Approved for R&D: 30 days, with ordinary audio not stored, revocation-triggered asset deletion, consent-evidence ordering, and active-config exemption**.
8. Backup and recovery policy — **Approved for R&D: no database backup, scheduled export, or restore guarantee; production requires a fresh decision**.
9. Environment separation — **Approved for current R&D: one Atlas Flex cluster and one database for development/R&D, with environment-labelled records; production requires a separate decision**.
10. Schema validation — **Approved: strict Pydantic contracts, repository invariants, strict MongoDB JSON Schema validators, optimistic revisions, explicit codecs, and baseline R&D limits/defaults**.
11. Numeric/text/payload/runtime limits — **Approved for R&D as listed in Section 24; provider-specific limits may only be stricter**.

No database implementation starts until the relevant decisions are confirmed.

## 27. Official references

- MongoDB data modelling: https://www.mongodb.com/docs/manual/data-modeling/
- Embedded data: https://www.mongodb.com/docs/manual/data-modeling/embedding/
- Mapping relationships: https://www.mongodb.com/docs/manual/data-modeling/schema-design-process/map-relationships/
- Atlas security: https://www.mongodb.com/docs/atlas/reference/faq/security/
- Atlas cluster security: https://www.mongodb.com/docs/atlas/setup-cluster-security/
- Atlas free cluster limits: https://www.mongodb.com/docs/atlas/reference/free-shared-limitations/
- Atlas pricing: https://www.mongodb.com/pricing
- PyMongo driver: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/
- PyMongo Async migration guidance: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/
- FastAPI integration: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/integrations/fastapi-integration/
