# STT Adapter Contract and Phase 0 Baseline

Status: Approved for Phase 0 R&D  
Authority: Decision 031 with Decisions 042, 043, 048, 061, 062, and 067 amendments  
Scope: Browser streaming STT for Hindi, Hinglish, and English  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `03-backend-module-design.md`, `05-agent-worker-orchestration.md`, `06-livekit-transport-adapter.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Baseline provider/model: Deepgram Nova-3 Multilingual (`model=nova-3`, `language=multi`)  
First benchmark challenger: Sarvam Saaras realtime (`saaras:v3-realtime`, beta) with code-mixed transcription  
Pricing research date: 2026-09-26

## 1. Purpose

This document defines the provider-independent streaming speech-to-text boundary, its lifecycle, normalized events, language behaviour, reliability rules, evidence, cost calculation, evaluation metrics, and approved initial provider.

The core rules are:

> Partial transcripts are UI hints; only a durable accepted final transcript may authorize conversation generation.

> Provider SDK objects and provider-specific event shapes stay inside the STT adapter.

## 2. Approved provider strategy

Phase 0 uses:

1. Deepgram Nova-3 Multilingual as the first end-to-end baseline.
2. Sarvam Saaras realtime/codemix as the first STT benchmark challenger after the baseline is stable.
3. Other providers only through later approved benchmark runs.

Deepgram Nova-3 was chosen because it supports streaming partial/final transcripts, Hindi and English multilingual/code-switching, keyterm prompting, and low published streaming cost while leaving turn authority with the application orchestrator.

Deepgram Flux Multilingual remains a later benchmark. Its model-native turn handling overlaps with the already-approved orchestrator turn manager, so it is not the initial baseline.

Sarvam is the first challenger because its speech models and `codemix` mode target Indian-language and Hindi-English speech. The newer true-partial realtime path (`saaras:v3-realtime`) is labelled beta by Sarvam; it must be benchmarked, and its beta status re-checked, before it may replace the baseline.

## 3. Normalized STT port behaviours

The provider-independent port supports these behaviours:

- create a session-oriented stream from immutable configuration;
- start/connect the stream;
- accept normalized audio frames;
- receive normalized STT events;
- request finalization of a specific orchestrator turn;
- cancel work for a specific turn/generation;
- expose usage and safe provider evidence;
- close idempotently.

Conceptually:

```text
create_stream(configuration)
start()
write_audio(normalized_audio_frame)
finalize_turn(turn_id)
receive_events()
cancel_turn(turn_id)
close()
```

These are approved behaviours, not final Python method names. Exact Python names/signatures are decided in implementation planning without changing the contract.

## 4. Input audio contract

The adapter accepts only the approved normalized audio frame:

- signed 16-bit little-endian PCM;
- mono;
- explicit `sample_rate_hz`;
- recommended 20 ms duration;
- monotonic capture timestamp;
- safe session/track identity;
- worker generation;
- cancellation generation;
- no LiveKit or provider SDK object.

The baseline configuration records the source sample rate and encoding. Any Deepgram-required conversion or resampling occurs inside the adapter. Audio frames are not written to MongoDB or ordinary logs.

## 5. Approved initial language behaviour

Normalized configuration:

```text
language_mode: auto
expected_languages: [hi, en]
code_switching: true
translation: false
transliteration: false
partial_transcripts: true
punctuation: true
smart_formatting: true
diarization: false
```

Baseline provider mapping:

```text
provider: deepgram
model: nova-3
language: multi
```

Rules:

- preserve the recognized language/script rather than silently translating to English;
- do not automatically convert Devanagari to Roman script or Roman text to Devanagari;
- permit natural Hindi-English code-switching;
- treat provider language labels/confidence as evidence, not guaranteed truth;
- never fabricate a detected language when the provider does not return one;
- evaluate script consistency and Hinglish behaviour using real test audio.

## 6. Product vocabulary and keyterms

The immutable agent configuration may contain a bounded product-vocabulary list for:

- company and product names;
- module/feature names;
- approved abbreviations;
- recurring technical terms;
- person names only when privacy-approved and necessary.

Phase 0 limits:

- maximum 50 unique terms;
- bounded term length;
- trimmed Unicode text;
- no control characters, markup, credentials, prompt instructions, or arbitrary browser input;
- terms are loaded only from the server-approved immutable configuration.

In Phase 0 the live baseline keyterm list is empty and the paid keyterm add-on is excluded from the baseline rate (Decision 043; `15-phase0-pricing-and-cost-model.md`). The adapter implements and mock-tests the bounded keyterm option path only. If a non-empty list is later approved, Deepgram maps it to keyterm prompting where the selected model/API supports it, and the extra keyterm charge is recorded separately in cost evidence; enabling it requires separate cost/configuration approval. A rejected/unsupported keyterm option cannot silently disable the complete STT stream; it creates safe configuration/startup evidence and follows the approved failure policy.

## 7. Normalized events

The adapter may emit:

- `stt.stream_started`;
- `stt.partial`;
- `stt.final`;
- `stt.turn_finalized`;
- `stt.usage`;
- `stt.warning`;
- `stt.failed`;
- `stt.stream_closed`.

Unknown provider messages are not forwarded directly. They are either safely ignored, mapped to an approved event, or mapped to a bounded warning/error when operationally important.

Routine provider speech-start/endpoint messages (for example Deepgram `SpeechStarted` and `UtteranceEnd`) are recorded only as aggregate counters on the provider-operation evidence, not as durable `stt.warning` events. They do not reuse authoritative `user.speech_started` or `user.speech_ended`, which originate from local VAD.

`stt.warning` is reserved for operationally important anomalies (for example a rejected option, protocol irregularity, or finalization timeout) and is rate-capped per stream; warnings beyond the cap are counted on the operation evidence instead of being persisted individually.

## 8. Partial transcript contract

A normalized partial event contains:

- session, current turn, operation, and correlation identifiers;
- worker and cancellation generations;
- monotonically increasing provider/adapter revision where available;
- bounded provisional text;
- optional provisional language and confidence;
- occurrence and receipt timestamps.

Rules:

- partial text is provisional and replaceable;
- partial text is used only for live UI and approved ephemeral metrics;
- partial text does not enter conversation history;
- partial text cannot start the LLM;
- ordinary partial text is not persisted in `conversation_turns` or as one MongoDB event per update;
- stale generation/turn partials are discarded;
- browser publication follows the lossy LiveKit transcript topic contract.

## 9. Final transcript contract

A normalized final event contains:

- `session_id`;
- `turn_id`;
- `operation_id` and logical request reference;
- `transcript_id`;
- correlation ID;
- worker and cancellation generations;
- bounded final text;
- optional detected language(s);
- optional provider confidence;
- optional word/segment timing when safely supported;
- input audio start/end/duration evidence;
- provider, exact model identifier, and adapter version;
- provider request/result identifier when safe;
- occurrence/receipt/finalization timing;
- usage evidence where available.

Rules:

- unavailable confidence/language/timing is `null`/unavailable, never zero or invented;
- whitespace-only/empty final text cannot start conversation generation;
- the final transcript is sanitized and durably stored before the orchestrator authorizes the LLM;
- `is_final=true` finalizes one provider transcript segment, not necessarily the complete application utterance;
- multiple non-overlapping finalized segments for one turn are appended in mapped audio-time order;
- only the same provider result ID, or the same normalized audio interval plus text fingerprint, is treated as a duplicate;
- a late result from an old worker, operation, turn, or cancellation generation is `discarded_late`;
- the adapter cannot edit a durable accepted final transcript in place;
- an actual human correction is stored through the separately approved correction/evaluation path, not as a hidden provider rewrite.

## 10. Speech, turn, and endpointing ownership

The local worker `SpeechActivityDetector`, backed initially by Silero VAD, is authoritative for speech-activity start and stop. The Turn Manager owns the user turn, endpoint commitment, and interruption acceptance. Endpointing is measured from the last locally detected speech frame; the initial total endpoint deadline is 700 ms, and tuning is capped at 1,000 ms unless the latency budget is re-approved (Decision 067).

Deepgram signals such as `SpeechStarted`, `speech_final`, `UtteranceEnd`, and provider finalization are advisory inputs. Deepgram remains authoritative for transcript segments/text, but these activity/endpoint signals cannot independently:

- create/close an application turn;
- authorize an LLM request;
- interrupt agent playback;
- change durable session state.

Turn finalization flow:

```text
Local Silero VAD detects/accepts speech stop
    -> identifies active turn
    -> Turn Manager reaches its total endpoint deadline
    -> asks STT adapter to finalize that turn
    -> adapter sends Deepgram `Finalize`
    -> adapter accumulates `is_final` segments in audio-time order
       until a result with `from_finalize=true` arrives (or 3 s timeout)
    -> adapter emits `stt.turn_finalized`
    -> validates generations/identity
    -> persists final transcript
    -> authorizes conversation generation
```

Deepgram finalization: on endpoint commit the adapter sends the Deepgram `Finalize` control message. It keeps assembling `is_final=true` segments for the turn window in mapped audio-time order (each segment may also be surfaced as `stt.final`) until it receives a result with `from_finalize=true`, which marks the provider flush as complete. Only then does it emit the single turn-final `stt.turn_finalized` event carrying the assembled text. If no `from_finalize=true` result arrives within 3 seconds, the adapter emits `stt.turn_finalized` with the segments assembled so far and records a bounded finalization-timeout warning; an empty assembly follows the empty-final rules in section 9.

Initial timing uses approximately 550 ms of local VAD silence detection and a 700 ms total endpoint deadline. The two values are not added: after VAD stop, the Turn Manager waits only the remaining time since the last speech frame. Later tuning may choose a total deadline from 700 ms up to a cap of 1,000 ms; any value above 1,000 ms (the former tunable range reached 2,000 ms) requires re-approval of the latency budget, because the endpoint stage sits inside the response-latency budget (Decision 067). Provider endpoint signals may trigger diagnostics or help request an STT flush, but application ownership prevents a provider swap from changing business behaviour silently.

## 11. Session-stream lifecycle

- open one STT stream near session activation;
- carry multiple sequential turns when the provider supports it;
- map each provider stream epoch and relative word/segment interval to normalized capture timestamps;
- bind a final segment to the accepted turn speech window by audio-time overlap, never callback arrival time;
- keep one authoritative active-turn window and a bounded finalized-segment assembler;
- prevent transcript leakage across turns;
- reject agent-playback-only/old-window segments and evidence ambiguous overlap rather than attaching it to the next turn;
- define each turn window from its first accepted local speech frame through its last accepted speech frame plus endpoint grace; a callback arriving later still belongs to that interval;
- during agent playback, words whose mapped audio ends before the accepted interruption-candidate start are excluded from the new turn, preventing queued echo or a brief cough from becoming leading transcript text;
- send keepalive/control messages only when the provider requires them;
- close the stream when the session becomes terminal;
- treat reconnect/retry as a new provider-operation attempt.

An adapter may internally use turn-scoped calls while preserving this normalized session-oriented behaviour.

## 12. Retry, reconnect, and buffering

Approved global retry policy applies:

- maximum three total attempts per logical request/stream recovery;
- initial backoff 250 ms;
- maximum backoff 2,000 ms;
- exponential backoff with jitter;
- only allowlisted transient connection, rate-limit, or provider-unavailable errors retry;
- cancellation/deadline/terminal session stops retries.

Rules:

- every attempt receives a new `provider_operation` document;
- original logical request/session references remain stable;
- user audio is never silently dropped;
- audio may be replayed only when the approved bounded inbound buffer clearly contains the required uncommitted frames and the provider semantics make replay safe;
- possible duplicate/unknown audio state fails or degrades the affected turn rather than inventing a transcript;
- no final transcript means no conversation request;
- retry evidence includes latency, processed duration, usage, cost, failure, and result disposition for each attempt.

## 13. Cancellation and late-result control

Every frame/control request/result is checked against:

- session ID;
- turn ID where applicable;
- STT operation ID;
- worker generation;
- cancellation generation.

After accepted interruption, session end, turn cancellation, or worker replacement, matching old results cannot affect the UI, durable transcript, LLM input, or conversation history. Provider cancellation success is evidence only; generation checks remain authoritative.

## 14. Usage and cost evidence

The adapter reports provider quantities without hardcoding price:

- connected stream duration where billing uses connection time;
- processed audio duration where billing uses audio time;
- request/stream count;
- model and language mode;
- optional paid features such as keyterm prompting;
- provider-reported usage/charge identifiers when safe;
- quantity source and whether measured, provider-reported, derived, or estimated.

The cost engine applies the dated rate card. Provider cost is never assumed zero when usage evidence is absent. Retry attempts that processed billable audio remain cost evidence even when the turn fails.

## 15. Approved pricing snapshot

The following public rates were observed on 2026-09-26. They are research inputs, not permanently hardcoded runtime prices.

| Provider/model | Native published rate | 30 minutes | 1,000 minutes | Billing/caveat |
|---|---:|---:|---:|---|
| Deepgram Nova-3 Multilingual — current promotional | USD 0.0058/min | USD 0.174 | USD 5.80 | Dated actual estimate only while provider confirms the limited-time promotional rate. |
| Deepgram Nova-3 Multilingual — published regular | USD 0.0092/min | USD 0.276 | USD 9.20 | Approved conservative budget/ceiling rate. |
| Deepgram Nova-3 Multilingual + keyterms — promotional | USD 0.0071/min | USD 0.213 | USD 7.10 | Promotional base USD 0.0058 + USD 0.0013/min keyterm add-on. Excluded from Phase 0 (live keyterm list is empty). |
| Deepgram Nova-3 Multilingual + keyterms — regular | USD 0.0105/min | USD 0.315 | USD 10.50 | Regular base USD 0.0092 + USD 0.0013/min keyterm add-on. Excluded from Phase 0 (live keyterm list is empty). |
| Deepgram Flux Multilingual | USD 0.0078/min | USD 0.234 | USD 7.80 | Later benchmark; model-native turn handling. |
| Sarvam realtime STT (`saaras:v3-realtime`, beta) | INR 30/hour | INR 15 | INR 500 | Beta model; verify model/realtime entitlement and taxes at run time. |
| AssemblyAI U-3.5 Pro Realtime | USD 0.45/hour | USD 0.225 | USD 7.50 | Session-duration billing. Model name and rate unverified as of 2026-09-26. |
| OpenAI GPT-Live-Transcribe | USD 0.017/min | USD 0.51 | USD 17.00 | Live partial transcription option. Model name and rate unverified as of 2026-09-26. |
| OpenAI GPT-Transcribe | USD 0.0045/min | USD 0.135 | USD 4.50 | Turn-committed workflow, not the initial live-partial baseline. |
| Google Cloud Chirp 3/STT V2 | USD 0.016/min | USD 0.48 | USD 16.00 | Region/channel/cloud charges and eligibility may apply. |

Calculations:

```text
per-minute plan: rate * billable minutes
per-hour plan: hourly rate * billable minutes / 60
```

Final invoice may also include tax, currency conversion, region, paid features, minimum/session billing, or discounts. `cost_entries` preserves native quantity/currency/rate evidence and normalized USD reporting separately.

## 16. Required STT measurements

Every provider/model benchmark measures:

- time to stream ready;
- speech-start detection latency;
- local-VAD speech-stop detection latency;
- time to first useful partial transcript;
- speech-end to accepted final transcript latency;
- total STT turn latency;
- partial transcript stability/revision rate;
- Word Error Rate (WER);
- Character Error Rate (CER);
- Hindi-English code-switch accuracy;
- product/company/module-name accuracy;
- number, date, currency, email, and identifier accuracy;
- deletion, insertion, substitution, missed-word, and hallucinated-word counts;
- false speech-start rate;
- sub-250 ms false-interruption suppression rate;
- premature and late endpoint rate;
- empty-final and duplicate-final rates;
- reconnect/recovery success;
- error/rate-limit rate;
- processed/connected billable duration;
- actual or estimated cost per audio minute, turn, and session.

CER is required for Indic-script comparison. Ordinary WER alone is insufficient for code-mixed speech, spelling variants, script variants, and product names.

## 17. Initial evaluation dataset requirements

The later approved evaluation dataset should include consented/synthetic clips across:

- clean English, clean Hindi, and natural Hinglish;
- within-sentence code switching;
- Indian English accents and regional Hindi accents;
- male/female and varied speaking pace where consented;
- quiet office, fan, street, and moderate background speech;
- near/far microphone and browser microphone variation;
- product names such as NiaLabs, IvyPrints, and Schoollog;
- numbers, fees, dates, phone-like digit sequences, IDs, and abbreviations;
- short confirmations, long questions, pauses, self-correction, and interruptions.

Ordinary Phase 0 audio is not stored. Dataset asset capture requires the already-approved consent-controlled benchmark path and a later explicit evaluation implementation decision.

## 18. Persistence mapping

`agent_configs.stt` records:

- `provider = deepgram`;
- `model = nova-3`;
- internal adapter version;
- `language_mode = auto`;
- expected languages `hi` and `en` in validated safe options;
- `code_switching = true`;
- translation/transliteration disabled;
- exact audio encoding/sample rate;
- partial transcripts enabled;
- punctuation/smart formatting enabled;
- diarization disabled;
- bounded approved keyterms when used;
- external `credential_ref`, never the credential.

`provider_operations` stores attempt-level provider/model/adapter identity, timing, usage, result/failure disposition, retry lineage, and cost summary. `conversation_turns.user_input` stores the accepted bounded final transcript and its approved evidence. Partial transcripts/audio/provider payloads are not stored there.

## 19. Security and privacy

- STT API credentials remain server-side and load through `credential_ref`;
- browser cannot select model, endpoint, language options, or keyterms;
- raw provider events, headers, signed URLs, credentials, and stack traces never enter browser messages or ordinary MongoDB documents;
- transcript text receives the approved sanitization/retention policy;
- audio is processed in memory and not recorded in ordinary R&D sessions;
- logs use IDs, timings, lengths, status, and safe error codes instead of transcript/audio content;
- provider data residency/retention terms require separate production review.

## 20. Error normalization

Provider errors map to stable application categories such as:

- STT authentication/authorization failed;
- unsupported configuration/language/audio;
- stream connection/start failed;
- audio rejected;
- stream protocol invalid;
- finalization timeout;
- rate limited;
- provider unavailable;
- cancellation failed;
- usage unavailable;
- STT internal error.

Each error includes safe retryability, failure phase, provider/model context, attempt number, and provider request ID when safe. Raw provider response bodies and transcript/audio content are excluded from ordinary error evidence.

## 21. Testing gates before first provider integration is accepted

- mock adapter passes lifecycle/partial/final/cancel/error contract tests;
- Deepgram adapter passes the same contract suite;
- final transcript persists before any mock/real LLM call;
- partial transcripts never enter LLM history or durable turn text;
- empty, duplicate, stale, and late finals are safely handled;
- endpoint commit sends Deepgram `Finalize`, and `stt.turn_finalized` is emitted only after a `from_finalize=true` result or the 3 s timeout, with segments in audio-time order;
- routine provider `SpeechStarted`/`UtteranceEnd` messages produce only aggregate counters, and `stt.warning` respects its per-stream cap;
- bounded queue overflow creates explicit evidence;
- retry creates distinct attempt records and no silent audio loss;
- language/keyterm options are generated only from approved configuration;
- costs match deterministic duration/rate fixtures;
- credentials/tokens/transcript text do not leak to ordinary logs;
- browser UI receives bounded lossy partials and reliable final state.

## 22. Deferred decisions

- exact Deepgram Python SDK/package version and final Python method names;
- credential provisioning and Atlas/host secret mechanism;
- exact keyterm list for each product configuration;
- benchmark dataset implementation and consented audio assets;
- benchmark pass/fail thresholds after collecting representative ground truth;
- Sarvam realtime endpoint/model version available to the account at test time;
- later OpenAI, Google, AssemblyAI, ElevenLabs, AWS, Azure, or other adapters;
- fallback provider policy in production;
- production data residency, DPA, retention, concurrency, SLA, and committed pricing.

Each deferred item requires discussion/approval before it changes implementation scope.

## 23. Phase 0 exclusions

- STT credentials in the browser;
- browser-selected provider/model/options/keyterms;
- automatic translation or transliteration;
- speaker diarization for a one-user browser session;
- storing raw audio or ordinary partial transcripts;
- letting provider endpointing directly start the LLM;
- using Deepgram speech/endpoint signals as authoritative speech activity;
- using LiveKit `AgentSession` or the LiveKit semantic/audio Turn Detector;
- automatic cross-provider fallback;
- production SLA/data-residency claims;
- hardcoded permanent provider pricing;
- simultaneous calls to multiple paid STT providers outside an approved benchmark run.

## 24. Acceptance criteria

- Deepgram Nova-3 Multilingual is the initial baseline and Sarvam is the first challenger;
- STT remains replaceable through normalized session-stream behaviours/events;
- Hindi, English, and code-switch configuration is explicit and reproducible;
- transcript script is preserved without silent translation/transliteration;
- product vocabulary is server-owned, bounded, and costed when enabled;
- partial text stays transient and cannot start the LLM;
- durable accepted final text is required before conversation generation;
- application turn ownership remains outside the provider;
- local Silero VAD owns speech activity and the Turn Manager owns endpoint/interruption commitment;
- retry/cancellation/late-result behaviour matches worker generations;
- usage, rate date, native currency, and paid features support auditable cost calculation;
- provider accuracy is decided by the approved measurements and real benchmark data, not marketing claims.

## 25. Official provider references

- [Deepgram models and languages](https://developers.deepgram.com/docs/models-languages-overview)
- [Deepgram streaming features](https://developers.deepgram.com/docs/stt-streaming-feature-overview)
- [Deepgram multilingual voice-agent guidance](https://developers.deepgram.com/docs/multilingual-voice-agent)
- [Deepgram pricing](https://deepgram.com/pricing)
- [Sarvam streaming STT](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/streaming-api)
- [Sarvam STT overview](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/overview)
- [Sarvam pricing](https://www.sarvam.ai/api-pricing)
- [OpenAI Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
- [OpenAI transcription pricing](https://developers.openai.com/api/docs/pricing)
- [Google Cloud Speech-to-Text](https://cloud.google.com/speech-to-text)
- [Google Cloud Speech-to-Text pricing](https://cloud.google.com/speech-to-text/pricing)
- [AssemblyAI Realtime Speech-to-Text](https://www.assemblyai.com/products/streaming-speech-to-text)
- [ElevenLabs transcription overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)
- [Amazon Transcribe streaming language identification](https://docs.aws.amazon.com/transcribe/latest/dg/lang-id-stream.html)
