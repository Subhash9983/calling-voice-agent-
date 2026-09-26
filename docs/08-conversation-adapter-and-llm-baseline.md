# Conversation Adapter Contract and LLM Baseline

Status: Approved for Phase 0 R&D  
Authority: Decision 032 with Decisions 047, 049, 065, and 067 amendments  
Scope: General Hindi, Hinglish, and English conversation after accepted STT finalization  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `03-backend-module-design.md`, `05-agent-worker-orchestration.md`, `07-stt-adapter-and-baseline.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Baseline provider/model: OpenAI GPT-6 Luna (`gpt-6-luna`)  
Baseline reasoning: `none`  
First benchmark challenger: Grok 4.7 with low reasoning (model name and pricing unverified as of 2026-09-26)

Pricing research date: 2026-09-26

## 1. Purpose

This document defines the replaceable conversation-engine boundary used between accepted user text and speakable agent text. It covers normalized inputs/events, conversation history, streaming, segmentation, cancellation, retry, limits, usage, cost, security, and the initial general LLM.

The core rules are:

> The conversation engine receives accepted durable text, not microphone audio or provisional STT partials.

> Only currently authorized text segments may reach TTS; hidden, cancelled, or late model output cannot become spoken history.

## 2. Approved provider strategy

Phase 0 uses:

1. OpenAI GPT-6 Luna as the first end-to-end general conversation baseline.
2. Responses request `reasoning = {"effort": "none"}` for the latency-sensitive voice path; persisted normalized configuration uses `reasoning.effort`.
3. Grok 4.7 with low reasoning as the first benchmark challenger after the baseline is stable.
4. The existing knowledge-based engine later implements the same normalized conversation port.

GPT-6 Luna was selected for streaming, multilingual text capability, low published token cost, and suitability for focused high-volume work. Grok 4.7 provides the explicitly desired comparison against another current general model but is not the initial baseline because its published token cost is materially higher.

This remains a cascaded text pipeline:

```text
Deepgram STT final
    -> conversation engine text stream
    -> response segmenter
    -> TTS
```

The baseline does not replace independent STT/TTS with a provider-owned speech-to-speech session.

## 3. Normalized conversation port behaviours

The provider-independent port supports:

- create a generation request from strict application models;
- start a text stream;
- receive normalized text/usage/error events;
- cancel the active generation;
- expose safe provider evidence;
- close idempotently.

Conceptually:

```text
create_request(conversation_input)
start_stream()
receive_text_events()
cancel()
get_usage()
close()
```

These are approved behaviours, not final Python method names. Exact SDK, endpoint, and method signatures remain implementation decisions that cannot change the contract silently.

## 4. Request input contract

Every request contains:

- `session_id`;
- `turn_id`;
- `operation_id` and logical request ID;
- correlation ID;
- worker generation;
- cancellation generation;
- exact immutable agent-configuration reference/checksum;
- system-instruction text and version;
- one accepted final user transcript;
- bounded normalized conversation history;
- current language context;
- maximum output tokens;
- absolute request deadline;
- approved tool-set version and definitions, empty in Phase 0;
- provider/model/adapter selection from server configuration.

Prohibited inputs:

- provisional STT partials;
- LiveKit/provider SDK objects;
- browser-supplied system prompt/model/options;
- audio frames or recordings;
- credentials or connection strings;
- raw provider payload/history objects;
- hidden generated text from interrupted/failed turns.

## 5. Approved GPT-6 Luna configuration

Normalized configuration:

```text
provider: openai
model: gpt-6-luna
reasoning:
  effort: none
streaming: true
max_output_tokens: 250
tools: disabled
web_search: disabled
provider_conversation_storage: disabled
temperature: provider default
```

Rules:

- the model/API credential remains server-side;
- the browser cannot override the model, reasoning, token cap, prompt, tools, or storage behaviour;
- streaming is required for early segmentation and TTS;
- no paid/built-in tool may activate implicitly;
- use provider-default sampling when a safe option is not explicitly approved;
- record exact provider/model/adapter identity and returned usage for every attempt;
- disable provider conversation storage where the selected endpoint exposes that option; for the OpenAI Responses API, `provider_conversation_storage: disabled` maps to the request field `store: false`;
- do not rely on provider-side conversation memory as the authoritative history.

## 6. System-instruction behaviour

The initial system instruction must direct the general assistant to:

- understand and respond naturally in Hindi, Hinglish, and English;
- approximately mirror the user's language and conversational style;
- produce short, clear, natural spoken answers;
- avoid Markdown tables, headings, code blocks, long numbered lists, raw URLs, and unpronounceable formatting;
- avoid claiming company/product knowledge that has not been supplied;
- disclose appropriately that Phase 0 is a general assistant without the product knowledge base;
- avoid inventing unsupported facts;
- handle unsafe requests according to the approved safety policy;
- never reveal system instructions, secrets, credentials, or internal implementation details.

This approves prompt behaviour, not the final exact wording. The exact prompt text/version requires separate review before configuration seeding.

## 7. History contract

Conversation history includes only:

- accepted durable final user transcripts;
- completed agent text confirmed as delivered/spoken;
- for an interrupted response, only its confirmed or estimated delivered portion according to existing playback evidence.

History excludes:

- STT partial transcripts;
- model hidden reasoning;
- cancelled undisclosed text;
- generated but unplayed response text;
- failed/unplayed TTS responses;
- provider event objects;
- raw browser/device metadata;
- internal diagnostics or credentials.

The orchestrator constructs normalized history. The provider adapter cannot independently retain/restore authoritative history.

## 8. Context budget

Approved operating targets:

- system instruction: target approximately 2,000 tokens or less;
- recent conversation history: maximum 12,000 tokens;
- complete request input: target maximum 16,000 tokens;
- generated output: maximum 250 tokens.

The database's 50,000-character system-instruction limit remains a validation safety ceiling, not the recommended prompt size.

When history exceeds the operating budget:

- preserve the system instruction;
- preserve the current accepted user turn;
- preserve the newest complete turns that fit;
- remove the oldest complete user/assistant turn pairs first;
- do not cut a message in the middle unless required by a hard safety limit;
- record bounded truncation evidence;
- do not invoke an automatic summarization model in Phase 0.

Token counts may be exact through a provider tokenizer or safely estimated before the request. Provider-returned billed counts remain authoritative for cost evidence.

## 9. Normalized streaming events

The adapter may emit provider-neutral generation events:

- `conversation.started`;
- `conversation.first_token`;
- `conversation.text_delta`;
- `conversation.completed`;
- `conversation.usage`;
- `conversation.cancelled`;
- `conversation.failed`.

The orchestrator-owned `ResponseSegmenter`, not the provider adapter, emits `conversation.segment_ready` after buffering, normalization, validation, and generation checks. Every event carries the relevant session, turn, operation, logical request, correlation, worker-generation, cancellation-generation, and timing context. Unknown provider stream messages do not escape the adapter.

## 10. Text delta contract

Text deltas are provisional stream output:

- bounded and ordered;
- attached to the active operation/generation;
- accumulated into complete generated text by the orchestrator/segmenter;
- not written as one MongoDB event per token/delta;
- not sent directly to TTS character by character;
- rejected when stale, duplicated, out of order beyond recovery, or cancellation-mismatched.

Browser live-text publication may use approved bounded lossy/reliable response events, but durable final/terminal state remains separate.

## 11. Response segmentation

The application/orchestrator `ResponseSegmenter` converts adapter text deltas into speakable units:

```text
LLM text deltas
    -> bounded phrase/sentence buffer
    -> normalized speakable segment
    -> deterministic segment validation
    -> authorized TTS request
```

Rules:

- emit the first stable natural phrase without waiting for the entire response;
- preserve word order and meaning;
- avoid splitting numbers, decimals, currency, dates, abbreviations, product names, URLs, or identifiers incorrectly;
- remove/transform Markdown/control formatting safely for speech;
- preserve separate generated text, text sent to TTS, and delivered/spoken text;
- enforce the approved five-segment queue bound;
- attach segment sequence and cancellation generation;
- validate every candidate segment before TTS for current generation, non-empty user-facing text, unsupported markup/raw URL/stage-direction/prompt-disclosure patterns, and the 500-character TTS boundary;
- maintain a rolling generated-token/output-length budget while streaming;
- retain an unfinished trailing phrase/sentence in the buffer rather than sending it to TTS;
- never emit a segment from stale/cancelled output.

The segmenter may normalize presentation for speech but cannot add unsupported facts or materially rewrite the answer.

## 12. Completion contract

Normalized completion evidence contains:

- finish reason;
- complete bounded generated text;
- text/segment counts;
- provider/model/adapter identity;
- provider response/request ID when safe;
- first-token and total timing;
- input, cached-input, cache-write, reasoning, and output token usage when returned;
- truncation/context evidence;
- tool activity, always none in Phase 0;
- result disposition and cancellation status.

Missing usage is unavailable, never zero. Generated completion does not imply that all text was synthesized or spoken.

Output-cap/truncation rules:

- normal completion may release the final buffered tail only after it passes the same segment validation and forms a complete speakable unit;
- the normalized finish reason `maximum_tokens` maps from the Responses API `status = incomplete` with `incomplete_details.reason = "max_output_tokens"`;
- an incomplete trailing unit is discarded and never synthesized or added to delivered history;
- if no complete meaningful segment was delivered, use the exact versioned response-truncated fallback once and record `response_completion_status = truncated_fallback`;
- if one or more complete segments were delivered, do not append a fabricated completion; record `response_completion_status = truncated_partial` and expose a safe UI notice;
- in both cases the turn's terminal status is `completed`; truncation is represented only by `response_completion_status` (Decision 067), never by a separate turn status;
- final whole-response validation/audit still records violations, but it cannot retroactively authorize or repair already delivered segments.

## 13. Cancellation and interruption

Accepted barge-in/session cancellation follows the canonical interruption order (Decision 067):

1. confirm the interruption candidate (≥250 ms of VAD speech at the playback activation threshold);
2. increment the in-memory cancellation generation, which fences old-generation output authorization;
3. cancel the conversation stream and TTS producers: queued segments first, then the active segment;
4. clear the application playback queue and call `AudioSource.clear_queue()`;
5. notify the browser with the interruption state message;
6. record events and evidence, including available generated/delivered evidence.

Throughout and after these steps, late text/usage/audio events from the old generation are discarded where they cannot update output, and only delivered assistant content is retained in history.

Provider cancellation acknowledgement is evidence, not authorization. Local worker/cancellation generations remain authoritative even if the provider continues sending data after cancel.

Cancellation performance is a required benchmark metric.

## 14. Retry policy for streamed generation

The approved maximum-three-attempt policy applies with streaming restrictions:

- a transient failure before first output may retry;
- received but not browser/TTS-delivered output may permit a guarded full retry;
- once any response portion has been delivered/spoken, do not automatically restart the full response;
- rate limit, temporary connection failure, and provider-unavailable errors may be retryable;
- authentication, invalid configuration/prompt, safety rejection, deadline, cancellation, and non-idempotent tool outcomes are not automatically retryable;
- every attempt receives its own operation, latency, error, token, and cost evidence;
- late output from a failed attempt cannot enter TTS/history.

The affected turn fails/degrades safely rather than speaking duplicate or contradictory responses.

## 15. Phase 0 tool policy

The approved tool list is empty:

- no web/X search;
- no file search;
- no code execution or calculator;
- no database query tool;
- no CRM/email/calendar action;
- no product knowledge retrieval;
- no arbitrary external side effect.

Provider features/tools are disabled even when the model supports them. A model cannot enable tools based on user text.

The future existing knowledge system implements or is composed behind the approved `ConversationEnginePort`; it is not casually exposed as an unrestricted model tool.

## 16. Usage and cost evidence

The adapter reports:

- uncached input tokens;
- cached input tokens;
- cache-write tokens where applicable;
- reasoning tokens where applicable;
- output tokens;
- provider/model/service tier/region evidence when safe;
- paid tool calls, always none in Phase 0;
- provider request/usage identifiers when safe;
- usage source: provider-reported, measured, derived, or estimated.

The cost engine applies a dated rate card. Retry/cancelled attempts remain billable evidence when the provider processed tokens. Cache discounts, long-context multipliers, fast/regional tiers, tool fees, tax, and FX remain explicit rather than assumed.

## 17. Approved pricing snapshot

Example assumption for comparable text-only turns (comparison only; the Phase 0 session cost basis of 250 output tokens per response is governed by `15-phase0-pricing-and-cost-model.md`):

```text
2,000 input tokens
100 output tokens
no tool calls
no cache discount
short-context standard service
```

Public prices observed on 2026-09-26:

| Model | Input per 1M | Output per 1M | Example per turn | 1,000 turns |
|---|---:|---:|---:|---:|
| OpenAI GPT-6 Luna | USD 0.10 | USD 0.50 | USD 0.00025 | USD 0.25 |
| Google Gemini 3.7 Flash (unverified as of 2026-09-26) | USD 0.75 promotional | USD 3.75 promotional | USD 0.001875 | USD 1.875 |
| Anthropic Claude Haiku 4.5 | USD 1.00 | USD 5.00 | USD 0.0025 | USD 2.50 |
| Grok 4.7 (unverified as of 2026-09-26) | USD 2.00 | USD 6.00 | USD 0.0046 | USD 4.60 |
| OpenAI GPT-6 Sol | USD 2.00 | USD 10.00 | USD 0.0050 | USD 5.00 |

Approximate 10-turn cost under the same average:

- GPT-6 Luna: USD 0.0025;
- Gemini 3.7 Flash: USD 0.01875;
- Claude Haiku 4.5: USD 0.025;
- Grok 4.7: USD 0.046;
- GPT-6 Sol: USD 0.05.

Formula:

```text
(input_tokens / 1,000,000 * input_rate)
+ (cached_input_tokens / 1,000,000 * cached_rate)
+ (cache_write_tokens / 1,000,000 * cache_write_rate)
+ (reasoning/output_tokens / 1,000,000 * applicable_output_rate)
+ tool/service/regional charges
```

These examples are LLM-only. STT, TTS, LiveKit, database, hosting, tax, and FX are separate cost entries. Gemini pricing observed is promotional through 2026-12-31. Claude Haiku 4.5 has a listed retirement not sooner than 2026-10-15, so it is not selected as a new baseline.

## 18. Required LLM measurements

Every candidate/model configuration benchmark measures:

- request-to-first-text-token latency;
- request-to-first-speakable-segment latency;
- total generation latency;
- output tokens per second;
- cancellation acknowledgement time;
- late deltas after cancellation;
- Hindi response quality;
- Hinglish naturalness;
- English response quality;
- language/style mirroring;
- spoken brevity and clarity;
- instruction adherence;
- unsupported factual invention/hallucination;
- repeated/looping phrases;
- unsafe/refusal correctness;
- honesty about absent product knowledge;
- response completeness within 250 tokens;
- retry, timeout, rate-limit, and failure rate;
- input/cached/reasoning/output token counts;
- cost per turn and session.

Identical approved prompts/history are used across providers. Human comparison may hide provider/model labels to reduce rating bias.

## 19. Persistence mapping

`agent_configs.conversation_engine` stores:

- `provider = openai`;
- `model = gpt-6-luna`;
- internal adapter version;
- `max_output_tokens = 250`;
- exact approved system instruction and version after its separate review;
- `tool_set_version` referencing an empty Phase 0 set;
- safe options for reasoning `none`, streaming, disabled provider storage/tools/search, and provider-default sampling;
- external `credential_ref`, never the credential.

`provider_operations` stores attempt-level provider/model/adapter identity, timing, finish/result disposition, token usage, retry/cancellation lineage, and cost summary. `conversation_turns.agent_response` separately records generated text, text sent to TTS, and confirmed/estimated/unavailable spoken text according to the existing contract.

Individual token deltas and hidden reasoning are not persisted.

## 20. Security and privacy

- LLM credentials remain server-side through `credential_ref`;
- the browser cannot set prompt/model/reasoning/tools/storage/endpoint;
- provider-side conversation storage is disabled where supported;
- only bounded required conversation text is sent;
- raw provider payloads, headers, encrypted reasoning, credentials, and stack traces never enter ordinary MongoDB documents/browser events;
- hidden reasoning is neither requested for display nor stored;
- general logs use IDs, counts, timings, status, and safe diagnostic codes rather than prompt/transcript/response content;
- production data residency, DPA, retention, abuse monitoring, and compliance require separate review.

## 21. Error normalization

Provider failures map to stable categories such as:

- conversation authentication/authorization failed;
- unsupported model/configuration;
- invalid input/context too large;
- safety rejection;
- request/stream start failed;
- stream protocol invalid;
- first-token timeout;
- total-generation timeout;
- rate limited;
- provider unavailable;
- cancellation failed;
- usage unavailable;
- conversation internal error.

Errors contain safe provider/model/attempt/failure-phase/retryability evidence and provider request ID when safe. Raw prompts, responses, provider bodies, reasoning, and secrets are excluded from ordinary error documents.

## 22. Testing gates

- mock conversation adapter passes start/stream/segment/complete/cancel/fail/usage contract tests;
- GPT-6 Luna adapter passes the same suite;
- no request can start from an STT partial or non-durable final;
- tools/search/storage remain disabled;
- context truncation removes only oldest complete turns and records evidence;
- first segment arrives before full completion in streaming tests;
- segmenter does not split protected terms/numbers incorrectly in approved fixtures;
- cancellation prevents stale segments from reaching TTS;
- retry after delivered output cannot duplicate the response;
- history includes only accepted user and delivered agent content;
- token/cost fixtures reconcile to Decimal calculations;
- browser/log/database projections contain no credentials, hidden reasoning, or raw provider payloads.

## 23. Deferred decisions

- exact OpenAI endpoint and Python SDK/package version;
- API credential provisioning/rotation mechanism;
- final system-instruction wording and initial version;
- exact tokenizer/estimator implementation;
- Grok adapter implementation and exact low-reasoning configuration;
- benchmark dataset, scoring weights, and pass/fail thresholds;
- later Gemini, Claude, Mistral, local/open-weight, or other adapters;
- automatic provider fallback policy;
- product knowledge-engine composition/replacement details;
- future tools and their authorization/idempotency contracts;
- production data residency, retention, DPA, SLA, rate limits, and committed pricing.

Every deferred item requires discussion/approval before implementation scope changes.

## 24. Phase 0 exclusions

- provider speech-to-speech conversation replacing separate STT/TTS;
- STT partials as model input;
- browser-selected prompt/model/options/tools;
- web/X/file search or external actions;
- product knowledge claims without supplied context;
- provider-authoritative conversation memory;
- automatic history summarization;
- storing hidden reasoning or token-by-token deltas;
- automatic provider fallback;
- retrying a full answer after part was delivered;
- production compliance/SLA/residency claims;
- permanent hardcoded model price.

## 25. Acceptance criteria

- GPT-6 Luna with reasoning `none` is the Phase 0 baseline and Grok 4.7 low is the first challenger;
- the conversation engine consumes only accepted durable final user text;
- model output streams through normalized events and the approved segmenter;
- response output is capped at 250 tokens;
- history/context rules are bounded, deterministic, and based on delivered content;
- Phase 0 tools, search, and provider conversation storage are disabled;
- cancellation generations prevent late output from TTS/history;
- streamed retries cannot duplicate already delivered speech;
- token usage and dated rate evidence support auditable per-turn/session costs;
- Hindi/Hinglish/English quality, latency, reliability, safety, and cost determine later model changes through benchmarks;
- future knowledge integration can replace/compose behind the same conversation port.

## 26. Official provider references

- [OpenAI GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI production and streaming guidance](https://developers.openai.com/api/docs/guides/production-best-practices)
- [Grok 4.7](https://docs.x.ai/developers/models/grok-4.7)
- [Grok model catalogue and pricing](https://docs.x.ai/developers/models)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API streaming endpoints](https://ai.google.dev/api)
- [Claude model overview](https://platform.claude.com/docs/en/models/overview)
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)
