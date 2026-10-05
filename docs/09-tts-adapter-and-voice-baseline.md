# TTS Adapter Contract and Voice Baseline

Status: Approved for Phase 0 R&D  
Authority: Decision 033 with Decisions 047, 062, 065, and 067 amendments  
Scope: Streaming Hindi, Hinglish, and Indian-English browser speech  
Depends on: `00-voice-agent-master-plan.md`, `01-system-contracts.md`, `03-backend-module-design.md`, `05-agent-worker-orchestration.md`, `06-livekit-transport-adapter.md`, `08-conversation-adapter-and-llm-baseline.md`  
Implementation status: Implemented (WP9, 2026-10-05)  
Last reviewed: 2026-09-26

Baseline: Sarvam Bulbul v3 (`bulbul:v3`), voice `priya`  
First challenger: ElevenLabs Flash v2.5 with an approved Indian multilingual voice  
Pricing research date: 2026-09-26

## 1. Purpose

This document defines the replaceable text-to-speech boundary between speakable response segments and normalized agent audio. It covers input/output, language routing, normalization, pronunciation, streaming, cancellation, retry, voice consistency, evidence, cost, security, and evaluation.

Core rules:

> Only a currently authorized bounded segment may produce browser audio.

> LLM text, TTS-normalized text, synthesized audio, and delivered/spoken content remain separate evidence.

## 2. Approved provider strategy

Phase 0 uses:

1. Sarvam Bulbul v3 as the end-to-end TTS baseline.
2. Sarvam voice `priya` as the initial voice.
3. ElevenLabs Flash v2.5 as the first challenger after baseline stability.
4. A later blind test of `priya`, `ishita`, `shubh`, and `ratan` before a production voice preference.

Sarvam was selected because Bulbul v3 is the current non-legacy `bulbul:v3` model (Sarvam lists v2 as legacy) and supports Hindi, Indian English, code-mixed Hinglish, streaming, pronunciation dictionaries, 30+ voices, and browser/telephony formats. Official guidance lists `priya` and `ishita` as top female voices across Hindi and English/Indian languages.

Deepgram is not an initial TTS candidate because its published TTS catalogue does not currently include Hindi. OpenAI and ElevenLabs remain benchmark candidates; the baseline prioritizes Indian/code-mixed pronunciation.

## 3. Normalized TTS port

Approved behaviours:

```text
open_session(configuration)
synthesize(segment)
receive_audio_events()
cancel_segment(segment_id)
cancel_turn(turn_id)
flush()
close()
```

These are behaviours, not final Python method names. An adapter may internally use persistent WebSocket or HTTP streaming while preserving segment boundaries and cancellation semantics.

## 4. Baseline configuration

```text
provider: sarvam
model: bulbul:v3
voice: priya
speaking_rate: 1.0
output_encoding: linear16 PCM
sample_rate_hz: 24000
channels: 1
streaming: true
voice_cloning: disabled
audio_storage: disabled
```

Rules:

- credentials remain server-side;
- the browser cannot override provider/model/voice/speaking-rate/language/codec/dictionary;
- use the current non-legacy `bulbul:v3` model identifier, not legacy (`bulbul:v2`) or beta aliases;
- voice selection is immutable per configuration version;
- no silent model/voice/provider fallback;
- provider/voice/adapter/configuration identity is preserved in evidence.

## 5. Input segment

Each segment includes:

- session, turn, segment, operation, logical-request, and correlation IDs;
- sequence number;
- worker and cancellation generations;
- bounded original speakable text;
- normalized language hint;
- voice/configuration version;
- absolute deadline.

Raw LLM/provider events, cancelled/hidden text, browser options, credentials, arbitrary provider metadata, and cross-session audio are prohibited.

## 6. Segment and queue limits

Maximum provider-facing segment: **500 Unicode characters**.

The existing response queue remains capped at five segments.

- prefer a complete natural phrase/sentence below the cap;
- never split a grapheme, word, number, abbreviation, URL, or product name incorrectly;
- split oversized text at the best earlier safe boundary;
- attach stable sequence/cancellation generation;
- apply backpressure rather than unbounded buffering;
- retain the independent 250-token conversation-output cap.

## 7. Language routing

- Hindi/Hinglish -> `hi-IN`;
- English-only -> `en-IN`;
- uncertain mixed text -> current accepted user-turn language context, with `hi-IN` fallback.

Preserve native Devanagari, keep natural code-mixed text coherent, do not split languages across providers, and do not translate/transliterate merely for TTS. The browser cannot select the language. The selected hint is recorded per operation/segment.

## 8. Pre-TTS normalization

Deterministic speakability normalization may:

- remove Markdown/control/list-heading syntax;
- convert formatting boundaries to safe natural pauses;
- remove unsupported emoji/invisible control characters;
- avoid reading long raw URLs verbatim;
- normalize approved dates, currency, decimals, digit sequences, and abbreviations;
- apply approved product-term pronunciation mappings;
- collapse whitespace and enforce the segment cap.

It cannot add facts, translate, change policy meaning, or materially rewrite the answer.

Track separately:

1. LLM-generated text;
2. normalized text sent to TTS;
3. provider synthesis/audio evidence;
4. delivered/spoken text evidence.

## 9. Pronunciation dictionary

A server-owned versioned dictionary may cover NiaLabs, IvyPrints, Schoollog, approved product/module names, KYC, OTP, API, and approved recurring terms.

- no arbitrary browser entries;
- store only a safe dictionary reference/version in configuration;
- behaviour-changing dictionary edits create a new configuration version;
- actual words/pronunciations require separate approval;
- missing/rejected required dictionary creates explicit evidence;
- dictionary data cannot contain secrets or unapproved customer PII.

## 10. Normalized events

- `tts.session_started`;
- `tts.segment_started`;
- `tts.first_audio`;
- `tts.audio_frame`;
- `tts.segment_completed`;
- `tts.usage`;
- `tts.cancelled`;
- `tts.failed`;
- `tts.session_closed`.

Events contain applicable session/turn/segment/operation/correlation/generation/timing context. Provider SDK objects do not escape. Audio chunks are transient, not individual MongoDB events.

## 11. Output audio

Normalized output is:

- signed 16-bit little-endian PCM;
- mono;
- 24,000 Hz;
- recommended 20 ms frames;
- explicit sample/frame duration;
- monotonic timestamp;
- segment sequence and generations;
- free of provider SDK objects.

Compressed/container audio is decoded and validated inside the adapter. LiveKit owns final publication conversion.

## 12. Streaming/playback flow

```text
Stable LLM phrase
    -> normalize/split
    -> TTS stream
    -> first provider audio
    -> normalized PCM
    -> bounded playback queue
    -> LiveKit agent-audio
    -> browser acknowledgement
```

The approved first-audio timeout is 5 seconds. Record synthesis request, stream readiness, first provider byte, first playable frame, publication, browser playback start, synthesis completion, and playback completion/cancellation times.

## 13. Cancellation and barge-in

Accepted interruption follows the canonical order (Decision 067):

1. Confirm the interruption candidate (≥250 ms of VAD speech at the playback activation threshold).
2. Increment the in-memory cancellation generation, which fences old-generation output authorization.
3. Cancel LLM streaming and TTS: queued synthesis segments first, then the active segment.
4. Clear the application playback queue and call `AudioSource.clear_queue()`.
5. Notify the browser with the interruption state message.
6. Record events and finalize delivered evidence.

Every old-generation frame/event is rejected from step 2 onward.

Provider acknowledgement is evidence only. If a clean post-cancel stream boundary cannot be proven, close/discard the connection and open a clean one. Old audio can never leak into the next turn.

Measure milliseconds of old audio audible after interruption acceptance.

## 14. Retry policy

- transient failure before first audio may retry;
- generated but unplayed audio may allow a guarded retry;
- once audio is delivered, do not automatically replay the full segment;
- auth, unsupported configuration, invalid text, deadline, and cancellation do not retry automatically;
- every attempt has separate timing, usage, failure, and cost evidence;
- generation checks reject late audio;
- fail/degrade rather than duplicate speech.

## 15. Voice consistency

- one voice/configuration per session;
- language switching does not change voice;
- browser cannot change voice/speaking rate;
- failure cannot silently select another voice/provider;
- any fallback requires a separate approved policy;
- benchmark cross-turn timbre, loudness, pace, accent, and prosody consistency.

## 16. AI-voice disclosure

The browser must clearly disclose that the voice is AI-generated. The agent cannot claim to be human. Exact UI wording is separately reviewed, but disclosure cannot be omitted.

## 17. Voice cloning

Phase 0 disables instant/professional/custom cloning, employee/founder/celebrity/customer/tester replicas, uploaded samples, dynamic community voices, and voice-creation consent recordings.

Future custom voices require separate identity, consent, authorization, purpose, retention/deletion, security, misuse, and provider-policy design.

## 18. Usage and cost evidence

Capture:

- original/normalized/provider-billed character count;
- credits where applicable;
- generated audio duration;
- request/segment count;
- provider/model/voice/language/codec/rate;
- dictionary feature usage;
- safe provider usage/request ID;
- quantity source: provider, measured, derived, or estimated.

Dated rate cards calculate cost. Failed/cancelled/retried work may remain billable. Missing usage is unavailable, never zero. Subscription minimums, credits, rollover, overage, tax, and FX stay explicit.

## 19. Pricing snapshot

Assumption: 200 characters per response; 10 responses (2,000 characters) per test session. This is a provider comparison only; the Phase 0 budget basis is 6,000 characters per session (INR 18.00 on Bulbul v3) per `15-phase0-pricing-and-cost-model.md` §6, which governs.

| Provider/model | 200 characters | 10-turn session | 1,000 responses | Caveat |
|---|---:|---:|---:|---|
| Sarvam Bulbul v3 | INR 0.60 | INR 6 | INR 600 | INR 3/1,000 chars; Indian baseline. |
| OpenAI TTS-1 | USD 0.003 | USD 0.03 | USD 3 | USD 15/1M chars; voices English-optimized. |
| OpenAI TTS-1 HD | USD 0.006 | USD 0.06 | USD 6 | USD 30/1M chars. |
| ElevenLabs Flash v2.5 | ~USD 0.0165-0.020 | ~USD 0.165-0.20 | ~USD 16.50-20 | Subscription-credit allocation estimate. |
| Cartesia Sonic 3.6 | ~USD 0.028-0.038/generated minute | not normalized | not normalized | Model name and rate unverified as of 2026-09-26; Hindi suitability unverified. |

Sarvam equals INR 3,000 per million characters. ElevenLabs estimate assumes about 0.5 credit per Flash character and allocates published Starter-Pro subscription prices across included credits; it is not a guaranteed invoice rate.

Do not mix INR/USD through an undocumented fixed exchange rate. `cost_entries` preserves native currency and dated FX evidence.

## 20. Required measurements

- request-to-first provider byte/playable frame/browser playback;
- total synthesis latency and real-time factor;
- interruption-to-silence/audio leakage;
- Hindi pronunciation and Hinglish naturalness;
- Indian-English accent quality;
- product/module names;
- numbers, dates, currency, abbreviations, URLs, and OTPs;
- pace, pauses, prosody, and emotional appropriateness;
- timbre/loudness/accent consistency;
- clicks, noise, glitches, repetition, truncation, and silence defects;
- intelligibility and Mean Opinion Score;
- retry/error/rate-limit rate;
- billed characters/credits/audio seconds and cost.

Use end-to-end provider + network + adapter + LiveKit + browser measurements, not vendor latency claims alone.

## 21. Voice evaluation set

Blind fixtures cover pure Hindi, Indian English, natural Hinglish, greetings/questions/empathy/errors, product/module names, digit sequences/OTP/currency/decimals/percentages/dates/times, abbreviations, short/long sentences, pauses, and interruption mid-sentence.

Initial Sarvam voices: `priya`, `ishita`, `shubh`, `ratan`. Provider labels may be hidden. Any stored benchmark audio requires a separately approved asset design; ordinary session audio remains unstored.

## 22. Persistence mapping

`agent_configs.tts` stores:

- `provider = sarvam`;
- `model = bulbul:v3`;
- adapter version;
- `voice_id = priya`;
- `hi-IN`/`en-IN` language routing;
- `audio_encoding = linear16`;
- `sample_rate_hz = 24000`;
- normalized `speaking_rate = 1.0`, mapped by the Sarvam adapter to provider field `pace`;
- safe streaming/mono/500-character/no-cloning/no-storage/no-fallback options;
- optional approved dictionary reference;
- external `credential_ref`, never the key.

`provider_operations` stores attempt identity, relationships, timing, usage, disposition, retries, and cost. `conversation_turns.agent_response` keeps generated, normalized, synthesized, and delivered evidence. Raw audio/provider payloads are not persisted.

## 23. Security and privacy

- server-side credentials only;
- browser cannot choose voice/model/dictionary/options;
- no raw provider requests/responses, headers, secrets, or signed URLs in browser/ordinary MongoDB;
- audio stays in memory and is not recorded;
- general logs use IDs/counts/timings/status/safe codes rather than text/audio;
- cloning remains disabled;
- production residency/retention/DPA/training terms require separate review.

## 24. Error normalization

Normalize authentication, unsupported configuration, invalid/oversized text, unavailable dictionary, stream start, corrupt chunk, first-audio/total timeout, rate limit, provider unavailable, cancellation/flush, usage unavailable, and internal failures.

Errors include safe provider/model/voice/attempt/segment/phase/retryability/request-ID evidence. Raw text/audio/provider bodies/secrets are excluded.

## 25. Testing gates

- mock and Sarvam adapters pass identical lifecycle/audio/cancel/error/usage contracts;
- only authorized generations emit frames;
- frames normalize to mono 24 kHz signed-16 PCM/20 ms;
- 500-character/safe-boundary rules hold;
- text fixtures preserve meaning and handle formatting/numbers/names;
- no old audio reaches the next turn after cancellation;
- retries cannot replay delivered segments;
- voice/language stays stable;
- Decimal cost fixtures reconcile;
- no credentials/raw audio/payload/text leak to general logs;
- later UI tests verify AI-voice disclosure.

## 26. Deferred decisions

- exact Sarvam SDK/package and HTTP-stream versus persistent-WebSocket choice;
- credential provisioning/rotation;
- pronunciation dictionary contents/asset creation;
- blind-test result and whether `priya` remains preferred;
- exact ElevenLabs challenger voice/plan;
- later OpenAI/Cartesia/other adapters;
- provider/voice fallback;
- generated benchmark-audio storage;
- custom/clone voice design;
- production residency, retention, DPA, SLA, concurrency, and pricing.

Each requires separate approval.

## 27. Phase 0 exclusions

- browser voice/provider/model controls;
- cloning/custom/community voices;
- ordinary generated-audio storage;
- unbounded segments/queues;
- automatic translation/transliteration;
- silent fallback;
- replay after delivered audio;
- raw audio/provider events in MongoDB/logs;
- absent AI-voice disclosure;
- production compliance claims;
- permanent hardcoded price.

## 28. Acceptance criteria

- Bulbul v3 `priya` is baseline; ElevenLabs Flash v2.5 is first challenger;
- output is streaming mono 24 kHz Linear16 normalized to 20 ms;
- segment cap is 500 Unicode characters and queue cap is five;
- Hindi/Hinglish uses `hi-IN`, English uses `en-IN`, one voice stays stable;
- normalization preserves meaning and evidence boundaries;
- generations prevent stale audio and measure interruption leakage;
- retries cannot duplicate delivered audio;
- cloning/storage/browser controls remain off;
- AI voice disclosure is mandatory;
- dated usage/currency evidence supports auditable cost;
- blind quality/latency/cost tests determine later changes.

## 29. Official references

- [Sarvam Bulbul v3](https://docs.sarvam.ai/api/getting-started/models/bulbul)
- [Sarvam TTS overview](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/overview)
- [Sarvam voice selection](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/how-to/change-the-speaker-voice)
- [Sarvam Indian-language guidance](https://docs.sarvam.ai/api/getting-started/building-for-india)
- [Sarvam pricing](https://www.sarvam.ai/api-pricing)
- [ElevenLabs models](https://elevenlabs.io/docs/overview/models)
- [ElevenLabs TTS](https://elevenlabs.io/docs/overview/capabilities/text-to-speech)
- [ElevenLabs streaming](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)
- [ElevenLabs pricing](https://elevenlabs.io/pricing)
- [OpenAI TTS guide](https://developers.openai.com/api/docs/guides/text-to-speech)
- [OpenAI TTS-1](https://developers.openai.com/api/docs/models/tts-1)
- [Deepgram TTS languages](https://developers.deepgram.com/docs/tts-models-languages-overview)
- [Cartesia pricing](https://www.cartesia.ai/pricing)
