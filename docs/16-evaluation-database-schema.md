# Evaluation Database Schema

Status: Approved for Phase 0 R&D benchmarking  
Authority: Decisions 040, 045, 046, 052, 054, 057, 063, 064, and 067  
Scope: Evaluation dataset, case, run, result-attempt, and human-rating persistence  
Depends on: `00-voice-agent-master-plan.md`, `02-database-design.md`, `11-phase0-evaluation-plan.md`, `15-phase0-pricing-and-cost-model.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Database: Existing single MongoDB Atlas Flex R&D database

## 1. Purpose

This document defines the MongoDB contracts for versioned evaluation datasets, logical cases, benchmark runs, per-execution results, and human ratings. It supports the approved 100-case Phase 0 evaluation plan without storing ordinary audio, provisional transcripts, provider raw payloads, secrets, or production/customer data.

The five collections are:

1. `evaluation_datasets`
2. `evaluation_cases`
3. `evaluation_runs`
4. `evaluation_results`
5. `evaluation_human_ratings`

Core rules:

> Dataset meaning is immutable after freezing; a material correction creates a new dataset version.

> One logical case can produce multiple execution results without changing the case count.

> Run configuration, output evidence, deterministic assertions, human judgement, latency, reliability, and cost remain separately traceable.

> Invalid harness samples stay visible and are never silently removed to improve a score.

## 2. Scope and exclusions

Included:

- the approved 60 transcript-to-LLM, 30 live-browser-voice, and 10 reliability/failure case layers;
- 80 development and 20 holdout cases;
- exact per-layer repetitions per configuration: transcript-to-LLM `3`, live-browser voice `1`, reliability/failure `3`, giving 240 logical result slots;
- exact dataset, case, agent configuration, prompt, provider/model, build, dependency-lock, and rate-card identity;
- deterministic assertion results;
- normalized latency, reliability, usage, cost, and failure evidence;
- individual 1–5 human ratings and bounded comments;
- 30-day R&D execution-evidence retention.

Excluded until separately approved:

- runner fixture implementation derived from the exact case content approved in Decision 041;
- reusable recorded/synthetic benchmark audio;
- object storage;
- LLM-as-judge outputs;
- production traffic samples;
- customer/production PII;
- knowledge retrieval/citation evaluation;
- adjudication/consensus workflow beyond preserving individual ratings;
- public browser access to evaluation records;
- production retention, backup, analytics warehouse, or cross-region replication.

## 3. Reference model

```text
evaluation_datasets
        |
        +---- evaluation_cases
        |
        +---- evaluation_runs ---- agent_configs
                    |              prompt/config checksum
                    |              application/lock checksum
                    |              rate-card ID
                    |
                    +---- evaluation_results ---- voice_sessions (optional)
                                  |                conversation_turns (optional)
                                  |                provider_operations (optional)
                                  |                cost_entries (optional)
                                  |
                                  +---- evaluation_human_ratings
```

MongoDB does not enforce foreign keys. Repositories validate every relationship before writing and reject mismatched dataset/run/case/result identity.

## 4. Common representation rules

- MongoDB `_id` is an internal ObjectId and is never the public identity.
- Application IDs are server-generated canonical UUID strings.
- All timestamps are timezone-aware UTC BSON datetimes.
- Money always uses finite `Decimal` values mapped to BSON `Decimal128`, matching doc 02; rates, quantities, scores used in calculations, and percentages also use `Decimal128` where fractional precision matters.
- Enums persist as lowercase `snake_case` approved strings (Decision 057).
- `environment` is `development` or `rd` only; `production` is intentionally excluded in Phase 0 (Decision 067), although doc 02 reserves it in core schema enums.
- Unknown fields are rejected by strict Pydantic models and MongoDB validators.
- Every document has `schema_version`, `environment`, and `created_at`.
- Mutable lifecycle documents also have `updated_at` and a revision field.
- Empty/unavailable optional measurements are omitted and retain an explicit availability reason/status; they never default to zero.
- Checksums use one implementation-approved canonical serialization/hash algorithm recorded beside the checksum.
- Provider SDK objects and unrestricted provider payloads are prohibited.
- Secrets, credentials, signed URLs, connection strings, prompt secrets, and raw exception locals are prohibited.

## 5. `evaluation_datasets`

Purpose: identify one immutable versioned collection of logical evaluation cases.

### Root fields

| Field | Type | Requirement | Purpose |
|---|---|---|---|
| `_id` | ObjectId | Required | Internal MongoDB identity. |
| `evaluation_dataset_id` | UUID string | Required, unique | Identity of this exact dataset version. |
| `dataset_key` | String | Required | Stable logical family key, such as the Phase 0 baseline family. |
| `name` | String | Required | Human-readable name. |
| `description` | String | Required, bounded | Safe scope description. |
| `version` | Integer | Required | Monotonically increasing within `dataset_key`. |
| `revision` | Integer | Required | Optimistic-concurrency revision for draft edits, freeze, and retirement; incremented by every permitted update. |
| `status` | Enum | Required | `draft`, `frozen`, or `retired`. |
| `phase` | Enum | Required | Initially `phase0`. |
| `purpose` | Enum | Required | `development`, `regression`, `release`, or `benchmark`. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development` or `rd` for Phase 0. |
| `tags` | String array | Optional, bounded | Safe dataset labels. |
| `composition` | Object | Required | Approved count summaries. |
| `case_set_checksum` | String | Required when frozen | Integrity of the ordered case set. |
| `checksum_algorithm` | String | Required when checksum exists | Canonical checksum method/version. |
| `source_revision` | String | Required | Repository fixture/source revision without a secret URL/token. |
| `change_note` | String | Required when `version > 1` | Meaningful difference from prior version. |
| `previous_dataset_id` | UUID string | Optional | Prior version lineage. |
| `retention` | Object | Required | Active-definition/retirement retention state. |
| `created_at`, `updated_at`, `created_by` | UTC datetime, safe actor ref | `created_at`, `updated_at`, and creator required | Creation and mutable-draft evidence. |
| `frozen_at`, `frozen_by` | UTC datetime, safe actor ref | Required when frozen | Freeze evidence. |
| `retired_at`, `retired_by` | UTC datetime, safe actor ref | Required when retired | Retirement evidence. |

### `composition`

Required fields:

- `total_case_count`;
- `layer_counts`: transcript, live voice, and reliability/failure;
- `split_counts`: development and holdout;
- `language_counts`: Hindi, Hinglish, English, and `mixed`;
- `severity_counts`;
- `layer_repetition_counts`: transcript-to-LLM `3`, live voice `1`, and reliability/failure `3`;
- `expected_result_slot_count` for the configured repetition policy (`240` for the initial release dataset).

For the initial release dataset, the frozen validator/repository invariant requires:

- total 100 logical cases;
- layers exactly 60 transcript, 30 live voice, and 10 reliability/failure;
- splits exactly 80 development and 20 holdout;
- per-layer repetition policy `3/1/3` (transcript/live voice/reliability) and exactly 240 logical result slots per configuration.

### `retention`

Required fields are `policy_version` and `state`, where state is `active_definition` or `retired_pending_expiry`. Active/draft/frozen definitions have no `expires_at`. A retired dataset records `retired_at` and receives `expires_at` no earlier than 30 days after retirement and no earlier than the latest expiry of every dependent run/result/rating. Cleanup verifies that no live run or case reference remains before deletion.

### Lifecycle

- draft metadata/composition may be edited through named repository operations;
- freezing recomputes counts/checksum and validates every referenced case;
- a frozen dataset and its cases are immutable in meaning;
- a correction creates a new dataset version and case records;
- a run may reference only a frozen dataset;
- retirement prevents new runs but never mutates historical runs/results.

## 6. `evaluation_cases`

Purpose: store one immutable logical scenario and its scoring contract within one dataset version.

### Root fields

| Field | Type | Requirement | Purpose |
|---|---|---|---|
| `_id` | ObjectId | Required | Internal identity. |
| `evaluation_case_id` | UUID string | Required, unique | Exact case-version identity. |
| `evaluation_dataset_id` | UUID string | Required | Parent exact dataset version. |
| `case_key` | String | Required | Stable logical scenario key across dataset versions. |
| `sequence_number` | Integer | Required | Deterministic ordering within the dataset. |
| `revision` | Integer | Required | Optimistic-concurrency revision for draft edits; frozen with the dataset. |
| `layer` | Enum | Required | `transcript_llm`, `live_voice`, or `reliability_failure`. |
| `category` | String/enum | Required | Approved evaluation category. |
| `severity` | Enum | Required | `critical`, `high`, `medium`, or `low`. |
| `split` | Enum | Required, restricted | `development` or `holdout`. |
| `primary_language` | Enum | Required | `hi`, `hinglish`, `en`, or `mixed`; `mixed` means a deliberately multilingual case that is not primarily natural Hinglish code-mixing. See the language-code note below. |
| `expected_script` | Enum | Required | `devanagari`, `latin`, `mixed`, or `not_applicable`. |
| `tags` | String array | Optional, bounded | Scenario/noise/behaviour labels. |
| `input` | Discriminated object | Required | Layer-specific safe input. |
| `expected` | Object | Required | Required/prohibited outcomes. |
| `automated_assertions` | Object array | Required, may be empty | Versioned deterministic checks. |
| `human_rubric` | Object | Required | Applicable dimensions and guidance. |
| `repetition_policy` | Object | Required | Required logical repetitions. |
| `case_checksum` | String | Required | Exact case integrity. |
| `checksum_algorithm` | String | Required | Checksum method/version. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development` or `rd`. |
| `created_at`, `updated_at`, `created_by` | UTC datetime, safe actor ref | `created_at`, `updated_at`, and creator required | Creation and mutable-draft evidence. |

Language-code note: `primary_language` values map to the doc 17 language-assertion labels as `hi` → `L-HI`, `en` → `L-EN`, and `hinglish` → `L-HING`; `mixed` has no single label, and its case assertions list the applicable labels (for example `L-EXPLICIT` or `L-NOSWITCH`).

The initial per-layer repetition policy is exact: transcript-to-LLM `3`, live-browser voice `1`, and reliability/failure `3`. Therefore one complete configuration has `180 + 30 + 30 = 240` logical result slots. Each case's `repetition_policy` must match its layer; infrastructure/provider retries do not create additional logical slots.

### Transcript input

Allowed fields:

- bounded `history_turns` containing roles and safe text;
- required `accepted_user_transcript`;
- optional safe `scenario_context` that is part of the test, not hidden provider data;
- optional deterministic `input_variables` from an approved allowlist.

No raw microphone audio, provider transcript object, real customer record, credential, or live external lookup is allowed.

### Live voice input

Allowed fields:

- safe tester instruction;
- optional read-aloud text/reference phrases;
- expected language and speaking-style label;
- approved noise/pause/correction labels;
- required browser actions;
- no audio bytes or object-storage reference in Phase 0.

### Reliability/failure input

Allowed fields:

- approved fault-scenario code;
- target normalized component;
- allowlisted fault parameters;
- event/step at which the fault is introduced;
- recovery/terminal-state expectation.

Arbitrary code, endpoint, shell command, provider payload, destructive database action, or unrestricted delay/retry setting is prohibited.

### `expected`

Bounded fields include:

- expected response language/script;
- required behaviour codes;
- prohibited behaviour/capability-claim codes;
- critical names, numbers, dates, currency, or test terms;
- expected clarification policy;
- expected final transcript meaning when applicable;
- expected lifecycle/terminal state;
- allowed normalized failure/fallback outcomes;
- critical-failure codes with zero tolerance;
- optional safe reference examples that are not treated as the only valid wording.

### Automated assertions

Each assertion definition contains:

- unique `assertion_id` within the case;
- `assertion_type` from the runner allowlist;
- severity and critical Boolean;
- target evidence field;
- bounded safe parameters;
- expected outcome;
- assertion/rule version.

No case document contains executable Python/JavaScript, arbitrary regex from browser input, provider credentials, or an unreviewed external URL.

### Human rubric

Contains:

- rubric version;
- applicable dimensions from the approved scorecard;
- dimension-specific safe guidance/anchors;
- whether a comment is required for low scores;
- optional approved reason-code allowlist.

## 7. `evaluation_runs`

Purpose: represent one immutable benchmark configuration and its mutable execution lifecycle.

### Root fields

| Field | Type | Requirement | Purpose |
|---|---|---|---|
| `_id` | ObjectId | Required | Internal identity. |
| `evaluation_run_id` | UUID string | Required, unique | Public run identity. |
| `client_request_id` | UUID string | Required, unique | Idempotent run creation. |
| `benchmark_group_id` | UUID string | Optional | Groups fair candidate comparisons. |
| `name` | String | Required | Safe run label. |
| `purpose` | Enum | Required | `development`, `regression`, `release`, or `benchmark`. |
| `status` | Enum | Required | `queued`, `validating`, `running`, `awaiting_human_review`, `reconciling`, `completed`, `failed`, `cancelled`, `invalid`, or `abandoned`. |
| `status_revision` | Integer | Required | Optimistic lifecycle concurrency. |
| `dataset_snapshot` | Object | Required | Exact frozen dataset identity/checksum/composition. |
| `configuration_snapshot` | Object | Required | Exact safe application/provider configuration identity. |
| `execution_policy` | Object | Required | Scope/repetition/concurrency/retry/seed policy. |
| `gate_snapshot` | Object | Required | Evaluation gates applied to this run. |
| `progress` | Object | Required | Expected/completed/failed/invalid/cancelled counts. |
| `summary` | Object | Optional until reconciliation | Derived quality/latency/reliability/cost/human summaries. |
| `failure` | Object | Optional | Safe terminal run failure. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development` or `rd`. |
| `initiated_by` | Safe actor ref | Required | R&D initiator label. |
| `created_at`, `started_at`, `ended_at`, `updated_at` | UTC datetime | `created_at` and `updated_at` required; `started_at` required after start; `ended_at` required when terminal | Lifecycle evidence. |
| `status_changed_at` | UTC datetime | Required | Set by every run status transition (and at creation); not changed by progress or summary writes. Anchors the maximum-dwell rule. |
| `expires_at` | UTC datetime | Required after terminal retention anchor is known | 30-day execution-evidence expiry: `ended_at + 30 days`. |

### Run terminal states and retention anchor

- terminal run statuses are `completed`, `failed`, `cancelled`, `invalid`, and `abandoned`; all other statuses are nonterminal;
- the run's `ended_at`, set when it enters a terminal status, is the retention anchor, and `expires_at = ended_at + 30 days`;
- "mark expiry" is the named operation that, when the run becomes terminal, sets the run's `expires_at` and copies the same value to every result and human rating of that run; any result still `pending` or `running` is first finalized as `cancelled`;
- maximum dwell: a run that stays in any nonterminal status, including `awaiting_human_review` or `reconciling`, for 30 days after `status_changed_at` is automatically transitioned to terminal `abandoned` (with a safe `failure` reason) by the scheduled evaluation cleanup, and then marked for expiry; therefore every run eventually expires;
- an `abandoned` run keeps its results and ratings visible until expiry and is never used as a completed comparison.

### `dataset_snapshot`

Required:

- `evaluation_dataset_id`, dataset key/version, case-set checksum;
- total/layer/split counts;
- selected split/safe case subset identity;
- holdout access Boolean without exposing holdout content through general APIs.

### `configuration_snapshot`

Required:

- `agent_config_id`, version, and checksum;
- prompt ID/version/checksum;
- transport/STT/conversation/TTS provider/model/voice/adapter identities;
- safe material runtime options;
- application build/version/commit reference;
- Python/frontend dependency lock checksums;
- evaluation runner/rule/rubric versions;
- cost rate-card ID;
- environment/region labels;
- no credentials or secret-bearing settings.

### `execution_policy`

Contains:

- selected layers/splits/cases;
- exact per-layer repetition counts and expected total logical slots (`240` for the initial complete configuration);
- requested concurrency and actual bounded concurrency;
- runner retry policy distinguished from provider retries;
- random seed/order policy;
- live-case manual execution policy;
- invalid-sample/rerun policy;
- stop-on-critical and spend-cap behaviour.

Exact runner package/CLI and default concurrency remain a separate implementation decision. The persisted values make every executed run reproducible/explainable.

### Run immutability

After status leaves `queued`:

- dataset, configuration, execution-policy meaning, gates, and rate-card identity cannot change;
- lifecycle/progress/summary/failure fields may change only through named revision-checked operations;
- changing a prompt/model/voice/adapter/options/gates/case set creates a new run;
- a run summary never replaces its underlying results/ratings.

## 8. `evaluation_results`

Purpose: store one content-immutable execution attempt for one logical case/repetition slot in one run. Content-immutable means the execution evidence (output, assertions, measurements, usage/cost, evidence references, and failure) is never rewritten after the attempt becomes terminal; only the lifecycle fields listed under "Result mutation" may change.

### Root fields

| Field | Type | Requirement | Purpose |
|---|---|---|---|
| `_id` | ObjectId | Required | Internal identity. |
| `evaluation_result_id` | UUID string | Required, unique | Result identity. |
| `evaluation_run_id` | UUID string | Required | Parent run. |
| `evaluation_dataset_id` | UUID string | Required | Parent frozen dataset. |
| `evaluation_case_id` | UUID string | Required | Exact logical case. |
| `case_key` | String | Required | Stable case label for comparison. |
| `case_sequence_number` | Integer | Required | Dataset order. |
| `layer`, `category`, `severity`, `split` | Enums | Required | Denormalized safe reporting dimensions. |
| `repetition_index` | Integer | Required | One-based logical repetition. |
| `attempt_index` | Integer | Required | One-based execution attempt inside the logical run/case/repetition slot. |
| `is_current_attempt` | Boolean | Required | Marks the attempt currently used for slot-level reporting. |
| `supersedes_evaluation_result_id` | UUID string | Optional | Earlier harness-invalid attempt replaced by this rerun. |
| `status` | Enum | Required | `pending`, `running`, `completed`, `failed`, `cancelled`, or `invalid`. |
| `result_revision` | Integer | Required | Controls derived-review summary updates. |
| `validity` | Object | Required | Sample inclusion/invalidation evidence. |
| `evidence_references` | Object | Required | Safe session/turn/operation/cost references. |
| `output_evidence` | Object | Required | Bounded normalized text/lifecycle evidence. |
| `assertion_results` | Object array | Required on evaluated terminal results | Per-assertion outcome. |
| `critical_failures` | String array | Required, may be empty | Zero-tolerance failures. |
| `measurements` | Object | Required | Normalized quality/latency/reliability metrics. |
| `usage_and_cost` | Object | Required | Evidence references and bounded summary. |
| `human_review_summary` | Object | Required | Derived rating count/status/aggregates. |
| `failure` | Object | Optional | Safe execution/application/provider failure. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development` or `rd`. |
| `created_at`, `started_at`, `ended_at`, `updated_at` | UTC datetime | `created_at` and `updated_at` required; `started_at` required after start; `ended_at` required when terminal | Lifecycle evidence. |
| `expires_at` | UTC datetime | Required after terminal run anchor | Approved 30-day expiry. |

### Validity

Fields:

- `is_valid_sample` Boolean;
- optional reason code and safe note;
- invalidation source: `harness`, `test_setup`, `application`, or `provider`;
- whether the sample counts in each quality/reliability/latency/cost denominator;
- invalidated timestamp and safe actor/runner reference.

Only genuine harness/test-setup failures may be excluded from applicable quality denominators. Application/provider failures remain visible and count according to the approved evaluation plan.

### Execution-attempt lineage

- the logical slot is `(evaluation_run_id, evaluation_case_id, repetition_index)`;
- every execution writes a new result with the next `attempt_index`; results are never overwritten;
- only a genuine harness/test-setup invalidation may authorize another attempt for the same slot;
- the previous attempt remains `invalid`, becomes `is_current_attempt = false`, and is linked by `supersedes_evaluation_result_id`;
- application/provider failures are valid measured outcomes and cannot be rerun merely to improve a score;
- exactly one attempt per logical slot is current, and run summaries use that current attempt while still reporting invalid-attempt counts.

### Result mutation

- while an attempt is `pending` or `running`, the runner fills its execution fields through the named reserve/start/finalize operations;
- after the attempt is terminal, the only mutable fields are `status` (only to `invalid` through mark-invalid), `validity`, `is_current_attempt`, `human_review_summary`, `expires_at`, `updated_at`, and `result_revision`; every such change increments `result_revision` and is revision-checked;
- replacement lineage is stored only on the new attempt (`supersedes_evaluation_result_id`); the superseded attempt is not rewritten beyond the fields above;
- a rerun is one MongoDB multi-document transaction that first sets the old current attempt to `is_current_attempt = false` (with its expected `result_revision`) and then inserts the new attempt with `is_current_attempt = true`, so `uq_evaluation_current_slot` is never violated; if the transaction aborts, neither write persists and the operation is retried or reported.

### Evidence references

May contain bounded arrays/references for:

- `session_id`;
- relevant `turn_id` values;
- `operation_id` values;
- `cost_entry_id`/calculation-run identity;
- durable event-range/IDs;
- sanitized local evidence/report reference and checksum.

References must belong to the same execution. Ordinary audio and signed URLs are prohibited.

Evidence references may outlive the records they point to: core session records expire at `voice_sessions.ended_at + 30 days` (doc 02), while run evidence expires at the run's `ended_at + 30 days`. References are therefore allowed to dangle, and readers resolve a missing referenced record as `expired` rather than as an integrity error.

### Output evidence

Uses one of two modes:

- `embedded_safe_text` for transcript harness results;
- `session_references` for live/reliability results, with only bounded normalized excerpts/summaries where required.

Allowed evidence includes accepted final transcript, generated response, TTS-submitted response, delivered/spoken-text status, response language/script, terminal state, greeting count, and fallback code. Partial transcripts, audio frames, raw model streams, hidden reasoning, and unrestricted provider payloads are prohibited.

### Assertion results

Each result contains:

- assertion ID/type/rule version;
- `passed`, `failed`, `not_applicable`, or `unavailable` outcome;
- severity and critical Boolean;
- bounded normalized actual/expected summary;
- safe reason code;
- evaluation timestamp.

Unavailable is not a pass. Critical failed assertions set the result/run critical-failure state.

### Measurements

Allowlisted fields cover:

- language/script/instruction/format/transcript/term correctness;
- end-to-end success and lifecycle correctness;
- accepted speech-end to playback-start;
- STT final, LLM first token/completion, TTS first audio/completion;
- publish-to-playback, interruption-to-silence, reconnect, and completion durations;
- retry/failure counts and terminal outcome;
- no arbitrary metric names without a schema/rule version.

### Usage and cost

Contains:

- rate-card ID and calculation-run reference;
- provider operation/cost-entry references;
- gross/net, marginal/allocated, source-currency/INR summaries;
- usage/calculation/reconciliation status;
- no hidden retry cost or silent zero for missing usage.

### Human review summary

Derived only from current submitted `evaluation_human_ratings`:

- `review_status`: `not_required`, `pending`, `in_progress`, or `complete`;
- expected/submitted reviewer counts;
- per-dimension count/mean when available;
- overall mean;
- low-score/reason-code counts;
- last aggregation timestamp/version.

Individual ratings remain in their own collection and are never overwritten by the summary.

## 9. `evaluation_human_ratings`

Purpose: preserve one reviewer's versioned scorecard for one evaluation result.

### Root fields

| Field | Type | Requirement | Purpose |
|---|---|---|---|
| `_id` | ObjectId | Required | Internal identity. |
| `evaluation_human_rating_id` | UUID string | Required, unique | Rating identity. |
| `evaluation_result_id` | UUID string | Required | Rated result. |
| `evaluation_run_id` | UUID string | Required | Parent run. |
| `evaluation_dataset_id` | UUID string | Required | Parent dataset. |
| `evaluation_case_id` | UUID string | Required | Parent case. |
| `reviewer_ref` | Safe pseudonymous string | Required | Reviewer identity without name/email. |
| `rubric_version` | String | Required | Exact scorecard. |
| `rating_revision` | Integer | Required | Reviewer/result rubric revision. |
| `status` | Enum | Required | `draft`, `submitted`, or `superseded`. |
| `is_current` | Boolean | Required | Current rating revision marker. |
| `supersedes_rating_id` | UUID string | Optional | Correction lineage. |
| `scores` | Object | Required on submission | Applicable 1–5 integer ratings. |
| `reason_codes` | String array | Optional, bounded | Approved review reasons. |
| `comment` | String | Optional, bounded | Safe review note. |
| `rating_checksum` | String | Required on submission | Submitted scorecard integrity. |
| `schema_version` | Integer | Required | Persistence-schema version. |
| `environment` | Enum | Required | `development` or `rd`. |
| `created_at`, `updated_at`, `submitted_at` | UTC datetime | `created_at` and `updated_at` required; `submitted_at` required when submitted or superseded | Lifecycle evidence. |
| `expires_at` | UTC datetime | Required when parent run retention anchor exists | Same approved run-evidence expiry. |

### Scores

Only applicable approved dimensions may appear:

- correctness;
- relevance;
- conversational naturalness;
- language quality;
- voice intelligibility;
- pronunciation;
- perceived response speed;
- safety appropriateness;
- overall conversation quality.

Every stored score is an integer from 1 through 5. Missing/non-applicable dimensions remain absent, not zero. A submitted rating requires overall score when the rubric marks it applicable.

### Rating corrections

- submitted ratings are content-immutable: `scores`, `reason_codes`, `comment`, `rubric_version`, `rating_checksum`, and `submitted_at` never change after submission;
- a `draft` rating's content may be edited before submission;
- after creation the only mutable fields are `status` (`draft -> submitted -> superseded`), `is_current`, `expires_at`, and `updated_at`; `rating_revision` identifies the scorecard revision and is fixed per document, so each correction is a new document with the next `rating_revision`;
- a correction inserts a new revision with `supersedes_rating_id`;
- a correction is one MongoDB multi-document transaction that first sets the prior current rating to `status = superseded` and `is_current = false` (conditioned on it still being current and submitted) and then inserts the new current rating, so `uq_evaluation_current_rating` is never violated;
- only one current submitted rating per result/reviewer/rubric is allowed;
- run/result aggregates recompute from current submitted ratings only;
- reviewer names, emails, signatures, IP addresses, device fingerprints, and HR/customer IDs are prohibited.

Phase 0 permits one pseudonymous R&D reviewer. The schema preserves multiple independent reviewers later without adding adjudication behaviour.

## 10. Integrity rules

- all application IDs are globally unique;
- dataset key/version is unique;
- case key and sequence number are unique inside a dataset version;
- a frozen dataset composition equals the actual validated case set;
- a run references one frozen non-retired-at-start dataset snapshot;
- run dataset/configuration/gates become immutable once execution starts;
- exactly one current result attempt exists for each run/case/repetition slot while superseded invalid attempts remain content-immutable (only the lifecycle fields in Section 8 may change);
- result dataset/case identity must match the run snapshot;
- referenced session/turn/operation/cost records must belong to that execution/configuration;
- a valid result cannot be terminal without required assertion/measurement/cost-status evidence;
- an invalid result must retain its reason/source and cannot silently disappear;
- critical assertion failure cannot be overridden by a human average;
- rating references must match result/run/dataset/case identity;
- only current submitted ratings contribute to aggregates;
- a result summary cannot claim complete human review without the required current ratings;
- ordinary audio, partial transcripts, raw provider payloads, and secrets are rejected.

## 11. Approved repository queries

### Dataset/case queries

- exact dataset ID or key/version;
- list datasets by environment/status/purpose and recency;
- ordered cases for a dataset filtered by approved layer/split/category;
- exact case ID/key within dataset;
- runner-only holdout case loading;
- checksum/composition verification before freeze/run.

### Run queries

- idempotent create lookup by `client_request_id`;
- exact run ID;
- recent runs by environment/status/purpose;
- runs for dataset/configuration/benchmark group;
- lifecycle/progress/reconciliation queues;
- run expiry/cleanup queue.

### Result/rating queries

- ordered/paginated results for one run;
- exact run/case/repetition slot and ordered attempt lineage;
- failed/critical/invalid/pending-review results in one run;
- exact result plus current human ratings;
- review queue by run/review status;
- ratings for one result or reviewer within one run;
- expiry/cleanup queues.

General browser/session APIs do not expose holdout content, full case definitions, reviewer comments, or evaluation administration. Evaluation endpoints/tooling require a separately approved local administrative boundary.

## 12. Approved indexes

Index names are stable; MongoDB-managed `_id` indexes are omitted.

### `evaluation_datasets`

- `uq_evaluation_dataset_id`: `{evaluation_dataset_id: 1}`, unique;
- `uq_evaluation_dataset_version`: `{dataset_key: 1, version: 1}`, unique;
- `ix_evaluation_datasets_list`: `{environment: 1, status: 1, purpose: 1, created_at: -1, _id: -1}`;
- `ix_evaluation_dataset_retention`: `{environment: 1, retention.expires_at: 1}`, partial where retirement expiry exists.

### `evaluation_cases`

- `uq_evaluation_case_id`: `{evaluation_case_id: 1}`, unique;
- `uq_evaluation_case_key`: `{evaluation_dataset_id: 1, case_key: 1}`, unique;
- `uq_evaluation_case_sequence`: `{evaluation_dataset_id: 1, sequence_number: 1}`, unique;
- `ix_evaluation_case_runner`: `{evaluation_dataset_id: 1, split: 1, layer: 1, sequence_number: 1}`.

### `evaluation_runs`

- `uq_evaluation_run_id`: `{evaluation_run_id: 1}`, unique;
- `uq_evaluation_run_request`: `{client_request_id: 1}`, unique;
- `ix_evaluation_runs_recent`: `{environment: 1, status: 1, created_at: -1, _id: -1}`;
- `ix_evaluation_runs_dataset`: `{dataset_snapshot.evaluation_dataset_id: 1, created_at: -1}`;
- `ix_evaluation_runs_config`: `{configuration_snapshot.agent_config_id: 1, created_at: -1}`;
- `ix_evaluation_runs_benchmark_group`: `{benchmark_group_id: 1, created_at: 1}`, partial where group ID exists;
- `ix_evaluation_runs_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists;
- `ix_evaluation_runs_dwell`: `{environment: 1, status: 1, status_changed_at: 1}`, supporting the maximum-dwell scan of nonterminal runs.

### `evaluation_results`

- `uq_evaluation_result_id`: `{evaluation_result_id: 1}`, unique;
- `uq_evaluation_result_attempt`: `{evaluation_run_id: 1, evaluation_case_id: 1, repetition_index: 1, attempt_index: 1}`, unique;
- `uq_evaluation_current_slot`: `{evaluation_run_id: 1, evaluation_case_id: 1, repetition_index: 1, is_current_attempt: 1}`, unique partial where `is_current_attempt = true`;
- `ix_evaluation_results_run_order`: `{evaluation_run_id: 1, case_sequence_number: 1, repetition_index: 1, attempt_index: 1}`;
- `ix_evaluation_results_run_status`: `{evaluation_run_id: 1, status: 1, case_sequence_number: 1}`;
- `ix_evaluation_results_review_queue`: `{evaluation_run_id: 1, human_review_summary.review_status: 1, case_sequence_number: 1}`;
- `ix_evaluation_results_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

### `evaluation_human_ratings`

- `uq_evaluation_human_rating_id`: `{evaluation_human_rating_id: 1}`, unique;
- `uq_evaluation_rating_revision`: `{evaluation_result_id: 1, reviewer_ref: 1, rubric_version: 1, rating_revision: 1}`, unique;
- `uq_evaluation_current_rating`: `{evaluation_result_id: 1, reviewer_ref: 1, rubric_version: 1, is_current: 1}`, unique partial where `is_current = true`;
- `ix_evaluation_ratings_result`: `{evaluation_result_id: 1, status: 1, submitted_at: 1}`;
- `ix_evaluation_ratings_run_reviewer`: `{evaluation_run_id: 1, reviewer_ref: 1, submitted_at: 1}`;
- `ix_evaluation_ratings_expiry`: `{environment: 1, expires_at: 1}`, partial where expiry exists.

No provider/model/category/metric/reason-code multikey analytical indexes are approved initially. Reports read the bounded result set for one run. Add cross-run analytical indexes only after measured query evidence.

## 13. Retention and deletion

### Definition records

- draft/frozen datasets and their cases are configuration-like reusable definitions and do not receive an ordinary 30-day execution TTL while active;
- retiring a dataset prevents new runs;
- retired datasets and the cases of a retired dataset become eligible for deletion only after no unexpired run/result/rating references remain, followed by the approved 30-day safety period;
- canonical non-secret source fixtures remain version controlled; this is not an Atlas backup.

### Execution evidence

- terminal evaluation runs, results, and human ratings are retained for 30 days from the run terminal retention anchor;
- all children use the same logical run expiry so one comparison remains coherent;
- ordinary audio is never stored;
- referenced session/turn/operation/cost records follow their own approved 30-day policies;
- a result cannot extend the retention of customer/production data because such data is prohibited in Phase 0;
- R&D has no backup or restore guarantee.

### Cleanup mechanism

Phase 0 uses a scheduled, environment-scoped cleanup workflow with environment-prefixed ordinary expiry indexes, not direct TTL indexes for these five collections initially. Before deletion it applies the maximum-dwell rule (nonterminal runs older than 30 days since their last status change become `abandoned`) and marks expiry for newly terminal runs. It then processes:

1. expired human ratings;
2. expired results;
3. expired runs;
4. cases of a retired dataset, then the retired dataset, only after reference checks and their safety period.

Each cleanup batch is bounded, dry-run capable, counts candidates/deletions/failures, and never targets the entire shared database. TTL conversion, retry automation, and orphan repair remain subject to implementation evidence; no broad destructive command is authorized by this schema.

## 14. Validation and size limits

Strict Pydantic and MongoDB validators enforce:

- required fields/types/enums and canonical IDs;
- exact allowed nested keys;
- dataset composition and lifecycle rules;
- discriminated layer input;
- allowlisted assertion/rubric/metric/reason codes;
- score range 1–5;
- result/run relationship and repetition bounds;
- revision-controlled lifecycle updates;
- retention/expiry requirements;
- prohibited secret/audio/raw-payload fields.

Approved bounds:

| Value | Maximum |
|---|---:|
| Dataset name | 100 characters |
| Dataset/case/run description or instruction | 2,000 characters |
| Tags | 20 unique items |
| Case history turns | 20 |
| One case input/final transcript | 10,000 characters |
| One expected/reference response | 20,000 characters |
| Automated assertions per case | 50 |
| Required/prohibited behaviour codes | 30 each |
| Critical terms/numbers | 50 |
| Assertion results per result | 50 |
| Evidence references per result/type | 50 |
| Human-rating reason codes | 10 |
| Human-rating comment | 2,000 characters |
| Safe failure/invalidation note | 2,000 characters |

Document-size safety caps:

| Collection | Maximum serialized target size |
|---|---:|
| `evaluation_datasets` | 128 KiB |
| `evaluation_cases` | 256 KiB |
| `evaluation_runs` | 256 KiB |
| `evaluation_results` | 256 KiB |
| `evaluation_human_ratings` | 64 KiB |

Large outputs, event streams, provider payloads, audio, or reports are not embedded to consume the remaining MongoDB platform limit.

## 15. Mutation operations

Allowed named repository operations include:

- create/update draft dataset;
- add/update draft case;
- validate/freeze dataset;
- retire dataset;
- idempotently create run;
- transition run status and progress with expected revision;
- reserve/start/finalize/mark-invalid one result attempt and revision-check the current-slot pointer;
- rerun a harness-invalid slot in one multi-document transaction (flip old current to false, then insert the new current attempt);
- attach bounded reconciliation/derived summary;
- create/submit human rating, and supersede it in one multi-document transaction (flip old current to superseded/not current, then insert the new current rating);
- recompute review/run aggregates;
- apply the 30-day maximum-dwell transition to `abandoned`;
- mark expiry (copy the terminal run's `expires_at` to its results and ratings) and execute bounded scheduled cleanup.

Prohibited:

- generic unrestricted document patch/update;
- editing a frozen case/dataset in place;
- rewriting terminal execution evidence to improve a score;
- deleting failed/invalid samples outside approved retention cleanup;
- browser-provided assertion code/provider options;
- direct browser MongoDB access;
- unscoped cleanup or destructive migration without separate approval.

## 16. Cost and Atlas impact

These collections use the existing Atlas Flex database; no second database, cluster, cache, or analytics product is added.

Expected Phase 0 volume is small:

- 100 logical cases;
- 240 logical result slots per configuration (180 transcript + 30 live voice + 30 reliability/failure), plus any retained invalid attempts;
- bounded individual human ratings;
- 30-day execution-evidence retention.

Incremental software licence cost is INR 0. MongoDB operations/storage contribute to the existing Flex usage tier and are measured under `15-phase0-pricing-and-cost-model.md`. A workload that projects Atlas above the approved spend boundary stops for review rather than silently scaling or adding analytical infrastructure.

## 17. Migration order

Per Decision 054, collections, validators, indexes, and repositories are created in WP5 and the runner is built in WP12:

1. add strict Pydantic persistence/domain contracts and codecs;
2. create all five collections with strict validators;
3. create unique/query/expiry indexes;
4. run valid/invalid validator fixtures;
5. load a non-secret draft dataset fixture;
6. verify counts/checksums and freeze only after exact case-content approval;
7. create a mock-adapter run and result/rating fixtures;
8. test scheduled cleanup only against uniquely labelled disposable fixtures;
9. reconcile document/index counts and retain sanitized migration evidence;
10. enable real evaluation runs only after runner and exact dataset approval.

Because R&D has no backup, any destructive schema migration requires explicit approval and a dry-run/reconciliation plan.

## 18. Deferred decisions

- source-fixture representation/checksum generation from the exact 100-case catalog approved in Decision 041;
- evaluation runner package, CLI, default concurrency, and scheduling;
- holdout-unsealing operational ceremony beyond repository restriction;
- LLM judge schema/model/prompt/calibration;
- multiple-reviewer assignment, agreement, consensus, and adjudication;
- reusable benchmark audio/object storage/consent linkage;
- cross-run analytical indexes/dashboard/warehouse;
- fixed empirical per-turn/session release gate after 20 valid sessions;
- provider-weighted final selection formula;
- production/continuous evaluation retention, backup, privacy, and access control.

## 19. Acceptance criteria

- five evaluation collections have explicit field, relationship, validator, index, and retention contracts;
- datasets/cases are immutable after freeze;
- the initial frozen dataset must validate exact 100/60/30/10 and 80/20 counts;
- the initial complete configuration must validate exact layer repetitions `3/1/3` and exactly 240 logical result slots;
- each result attempt is unique per run/case/repetition/attempt, exactly one attempt is current per logical slot, and invalid attempts remain traceable;
- run identity preserves exact dataset/config/prompt/build/lock/rate-card evidence;
- invalid and failed samples stay visible;
- deterministic assertions and individual human ratings remain separate;
- one current rating per result/reviewer/rubric is enforced;
- ordinary audio, partial transcripts, provider raw payloads, secrets, and production data are prohibited;
- run/result/rating execution evidence expires after 30 days through bounded scheduled cleanup;
- active dataset definitions remain until retired and safely unreferenced;
- evaluation administration/holdout data is not exposed through general browser APIs;
- existing Atlas Flex and spend limits remain authoritative;
- no collection/code/migration is created merely because this schema is approved.
