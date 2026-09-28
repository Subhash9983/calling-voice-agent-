# Phase 0 Pricing and Cost Model

Status: Approved for Phase 0 R&D  
Authority: Decision 039 with Decisions 048, 049, 066, 067, and 069 amendments  
Scope: Dated Phase 0 provider, platform, database, and per-session cost model  
Depends on: `00-voice-agent-master-plan.md`, `07-stt-adapter-and-baseline.md`, `08-conversation-adapter-and-llm-baseline.md`, `09-tts-adapter-and-voice-baseline.md`, `11-phase0-evaluation-plan.md`  
Implementation status: Not started  
Last reviewed: 2026-09-28

Rate-card ID: `phase0_rate_card_2026_09_26_v1`  
Research date: 2026-09-26  
Reporting currency: USD normalized (Decision 016); INR display/report conversion (Decision 069)

## 1. Purpose

This document defines the dated provider rates, currency conversion, tax/payment-fee treatment, cost formulas, example session assumptions, free-credit accounting, and spending gates for the Phase 0 browser voice agent.

It distinguishes:

- marginal provider cost caused by one additional session;
- fixed/shared monthly platform cost allocated across sessions;
- gross list-price cost before credits;
- net payable cost after eligible credits;
- pre-tax technical cost;
- invoice/tax/payment-adjusted cash cost;
- estimated, provisional, and final cost evidence.

Core rules:

> A missing rate or usage quantity is never converted to zero cost.

> Promotional/free-credit savings are recorded separately from gross list-price usage.

> The application stores the exact dated rate-card version used for each calculation.

> Budget estimates use conservative assumptions; final cost uses provider usage/invoice evidence.

This document governs the Phase 0 cost basis (Decision 067). The R&D workbook `outputs/voice-agent-rnd-2026-09-23/voice_agent_api_rnd_matrix.xlsx` is a dated research snapshot; where its figures or assumptions differ from this document, this document wins.

## 2. Approved baseline rate card

### 2.1 LiveKit Cloud

Approved R&D plan: Build

| Item | Approved planning treatment |
|---|---:|
| Monthly plan price | USD 0 |
| Included WebRTC participant minutes | 5,000/month |
| Included cloud-deployed agent session minutes | 1,000/month |
| Included downstream transfer | 50 GB/month |
| Overage on Build | Not budgeted; allowance is a hard cap |

The baseline Python worker runs locally and connects as a participant; it is not initially deployed as a managed LiveKit Cloud agent. Therefore cloud-deployed agent-session minutes are not treated as a baseline billable component. Browser and worker WebRTC participant minutes plus downstream transfer remain measurable.

For a session where one browser and one locally hosted worker remain connected for the full duration:

```text
WebRTC participant minutes = session minutes × 2
```

Build-plan capacity under that assumption is at most 2,500 usable room-minutes per month, or 250 ten-minute sessions, before considering other projects/participants and transfer limits. The application stops rather than assuming paid overage when a free hard cap is reached.

Sources:

- [LiveKit pricing](https://livekit.com/pricing)
- [LiveKit Cloud quotas and metered resources](https://docs.livekit.io/deploy/admin/quotas-and-limits/)
- [LiveKit Cloud billing and increments](https://docs.livekit.io/deploy/admin/billing/)

### 2.2 MongoDB Atlas Flex

Approved R&D deployment: Atlas Flex, AWS Mumbai (`ap-south-1`)

| Usage tier | Monthly cost | Hourly cost |
|---|---:|---:|
| 0–100 ops/second | USD 8 | USD 0.0110 |
| 100–200 ops/second | USD 15 | USD 0.0205 |
| 200–300 ops/second | USD 21 | USD 0.0288 |
| 300–400 ops/second | USD 26 | USD 0.0356 |
| 400–500 ops/second | USD 30 | USD 0.0411 |

The Flex range includes 5 GB storage, 100 base operations/second, and unlimited data transfer under the published Flex terms. The approved single-user workload budgets the USD 8 base case but preserves USD 30 as the documented monthly platform maximum.

Source: [MongoDB Atlas Flex costs](https://www.mongodb.com/docs/atlas/billing/atlas-flex-costs/)

### 2.3 Deepgram Nova-3 Multilingual streaming STT

| Rate | USD/minute | Treatment |
|---|---:|---|
| Current displayed promotional PAYG rate | 0.0058 | Actual estimate only while provider confirms this rate |
| Published regular PAYG rate | 0.0092 | Approved budget/ceiling rate |
| Smart formatting | Included | No separate add-on cost |
| Keyterm prompting | 0.0013 | Excluded until separately enabled/approved |

The baseline cost is based on billable streaming audio evidence returned/reconciled by the provider. Until measured evidence proves that silence or paused transport is excluded, the conservative session budget treats the entire active STT stream duration as billable.

The STT adapter implements and mock-tests the bounded keyterm option path, but the Phase 0 live baseline uses an empty keyterm list. Therefore no keyterm add-on is included in the baseline rate. Enabling a non-empty live list requires separate cost/configuration approval and a new comparison configuration.

Retries and reopened streams remain separate usage attempts. A failed transcript does not erase processed audio cost.

Source: [Deepgram pricing](https://deepgram.com/pricing)

### 2.4 OpenAI GPT-6 Luna

Approved API path: Responses API, Standard processing, short-context rates, no regional/Fast-mode uplift

| Usage type | USD per 1M tokens |
|---|---:|
| Input | 0.10 |
| Cached input | 0.01 |
| Cache write | 0.125 |
| Output | 0.50 |

The baseline budget does not assume a cache discount. Reasoning effort is `none`; tool calls, web search, files, hosted containers, audio models, and regional/Fast processing are disabled and excluded.

For each attempt:

```text
LLM USD =
  (uncached input tokens / 1,000,000 × 0.10)
  + (cached input tokens / 1,000,000 × 0.01)
  + (cache-write tokens / 1,000,000 × 0.125)
  + (output tokens / 1,000,000 × 0.50)
```

Cancelled/retried attempts are costed from reported usage. If final usage is unavailable, the entry stays estimated/unavailable rather than zero.

Sources:

- [Official OpenAI GPT-6 Luna model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Official OpenAI API pricing](https://developers.openai.com/api/docs/pricing)

### 2.5 Sarvam Bulbul v3 streaming TTS

| Usage type | INR rate |
|---|---:|
| Real-time/streaming TTS | ₹3.00 per 1,000 characters |

The billable quantity is provider-accepted synthesized input characters, not LLM tokens, delivered audio duration, or browser playback duration.

```text
Sarvam TTS INR = synthesized characters / 1,000 × 3.00
```

Every retry/cancelled synthesis that the provider reports as billable remains a separate cost attempt. Generated LLM characters that never reach TTS are not charged as TTS usage.

Sources:

- [Sarvam API pricing](https://www.sarvam.ai/api-pricing)
- [Sarvam Bulbul model documentation](https://docs.sarvam.ai/api/getting-started/models/bulbul)

## 3. Currency conversion

Reference evidence:

- accessible RBI/FBIL-reference archive value for 2026-09-24: INR 95.9099 per USD;
- approved conservative planning rate: INR 100 per USD.

Source: [RBI reference-rate archive at MSEI](https://www.msei.in/markets/currency/historical-data/rbireferenceratearchives)

Rules:

- budget/projection calculations use `planning_fx_rate = 100 INR/USD`;
- the rate-card records the reference source, observation date, and value separately;
- final invoice cost uses the actual card/bank/provider conversion evidence when available;
- bank/card FX spread and foreign transaction fee are separate cost lines, never hidden by changing the provider rate;
- Sarvam INR charges are not converted through USD;
- every calculation stores source currency, original amount, FX rate, FX date/source/type, normalized USD amount, INR display amount, and rounding method (Decision 069);
- a changed planning FX rate creates a new rate-card version.

## 4. Tax and payment treatment

The technical rate card does not hard-code GST as a provider usage rate.

For planning only, an 18% tax cash-buffer scenario is displayed after the 20% general contingency. Actual tax treatment depends on the supplier invoice, customer/entity/GST registration, place of supply, reverse-charge obligations, and available input tax credit. It must be confirmed by the company's accountant/CA.

The CBIC FAQ states that qualifying software/service imports by an Indian firm can attract IGST under reverse charge even when paid in INR. This document is cost planning, not tax advice.

Source: [CBIC GST sectoral FAQ](https://cbic-gst.gov.in/sectoral-faq.html)

Each final cost run can include:

- provider subtotal;
- provider-applied tax;
- separately payable/reverse-charge tax;
- payment processor/card fee;
- FX spread/fee;
- eligible credit/refund;
- gross cash outflow;
- recoverable input credit when finance confirms it;
- net accounting cost.

Unknown tax/fee values remain `unavailable` or `estimated`, not zero.

## 5. Cost calculation hierarchy

### 5.1 Provider attempt

```text
attempt gross cost = normalized billable quantity × dated unit rate
attempt net cost = gross cost - allocated eligible provider credit
```

### 5.2 Turn

```text
turn variable cost =
  sum(all STT attempts)
  + sum(all LLM attempts)
  + sum(all TTS attempts)
```

All billable retries/failures are included. Superseded calculation runs remain traceable but are not double-counted in the current turn total.

### 5.3 Session

```text
session variable cost = sum(current turn variable costs)
session marginal cost/min = session variable cost / session connected minutes
```

LiveKit usage is reported separately even while its marginal payable cost is zero within the Build allowance.

### 5.4 Fully allocated R&D cost

```text
allocated Atlas cost/session = Atlas monthly cost / valid sessions in billing month

fully allocated cost/session =
  session variable cost
  + allocated Atlas cost/session
  + allocated non-zero platform/payment/tax costs

fully allocated cost/min = fully allocated cost/session / session minutes
```

An allocated fixed cost is an analytical view; it does not change the marginal cost of one additional session.

## 6. Approved conservative example session

Assumptions:

- session connected duration: 10 minutes;
- Deepgram stream billed conservatively for all 10 minutes;
- 10 LLM responses per session at the 250-output-token cap each;
- cumulative LLM usage: 60,000 uncached input tokens and 2,500 output tokens (10 × 250); input counts every billed turn's system instructions, retained history/context, and current user content;
- Sarvam synthesized text: 6,000 characters, about 2.4 characters per output token, which is typical for Devanagari/Hinglish text; the character budget is still recorded independently because tokenizer ratios, normalization, and which generated text reaches TTS vary;
- two LiveKit WebRTC participants for the session;
- no provider tool calls/add-ons;
- planning FX: INR 100/USD;
- Deepgram budgeted at regular rather than promotional rate.

| Component | Formula | Cost/session | Cost/session-minute |
|---|---|---:|---:|
| Deepgram STT | 10 × USD 0.0092 × ₹100 | ₹9.20 | ₹0.920 |
| GPT-6 Luna input | 60,000 / 1M × USD 0.10 × ₹100 | ₹0.60 | ₹0.060 |
| GPT-6 Luna output | 2,500 / 1M × USD 0.50 × ₹100 | ₹0.125 | ₹0.0125 |
| Sarvam TTS | 6,000 / 1,000 × ₹3 | ₹18.00 | ₹1.800 |
| LiveKit Build payable usage | Within allowance | ₹0.00 | ₹0.000 |
| **Variable total** |  | **₹27.925 (≈₹27.93)** | **₹2.7925** |

At Deepgram's currently displayed promotional rate, the same variable estimate is ₹24.525/session or ₹2.4525/session-minute. The budget continues to use ₹27.925 until a new approved rate-card changes it.

LLM input sensitivity for the same session is ₹0.20 at 20,000 input tokens, ₹0.60 at the 60,000-token planning baseline, and ₹1.20 at 120,000 input tokens. Sessions may exceed the stress example; measured provider usage replaces the estimate when available.

## 7. Atlas allocation examples

Assume ten-minute valid sessions and the INR 800 planning value for the Atlas USD 8 base month:

| Valid sessions/month | Total voice minutes | Atlas allocation/min | Variable/min | Fully allocated pre-tax/min |
|---:|---:|---:|---:|---:|
| 20 | 200 | ₹4.00 | ₹2.7925 | ₹6.7925 |
| 100 | 1,000 | ₹0.80 | ₹2.7925 | ₹3.5925 |
| 250 | 2,500 | ₹0.32 | ₹2.7925 | ₹3.1125 |

The 250-session example reaches the 5,000 LiveKit WebRTC participant-minute allowance when two participants remain connected for ten minutes each. It is a capacity boundary, not approval to run that volume.

If Atlas reaches its published USD 30 monthly maximum, the allocation uses INR 3,000 instead of INR 800.

## 8. Initial 20-session R&D budget

The initial paid baseline allowance covers up to 20 valid sessions of up to ten minutes each under the conservative example assumptions.

### Expected Atlas base case

| Item | INR |
|---|---:|
| 20 session variable API budget | 558.50 |
| Atlas Flex base planning cost | 800.00 |
| Pre-tax/pre-fee subtotal | 1,358.50 |
| After 20% contingency | 1,630.20 |
| After separate 18% tax cash-buffer scenario | 1,923.64 |

Approved expected spend alert: **INR 2,000 per billing month**.

### Atlas maximum scenario

| Item | INR |
|---|---:|
| 20 session variable API budget | 558.50 |
| Atlas Flex published maximum | 3,000.00 |
| Pre-tax/pre-fee subtotal | 3,558.50 |
| After 20% contingency | 4,270.20 |
| After separate 18% tax cash-buffer scenario | 5,038.84 |

Approved absolute R&D monthly ceiling: **INR 5,100**.

The ceiling is not a target and does not authorize unnecessary calls. It is a stop boundary covering the approved baseline services only.

## 8A. Full paid evaluation run projection

This is a planning figure only. The 20-session smoke allowance does not authorize it, and a full paid run of the `17-phase0-evaluation-case-catalog.md` suite requires separate approval based on this projection refreshed with measured baseline usage.

| Layer | Basis | Variable INR |
|---|---|---:|
| Transcript-to-LLM | 60 cases × 3 = 180 responses × (~2,500 input tokens × USD 0.10/1M + 250 output tokens × USD 0.50/1M) × ₹100 | 6.75 (≈7) |
| Live browser voice | 30 results × ₹27.925 conservative ten-minute session | 837.75 |
| `INT-LIVE` interruption protocol | 2 live sessions × ₹27.925 (outside the 100-case catalog) | 55.85 |
| Reliability/failure | 30 slots on deterministic mock/fault adapters per catalog §18 | 0.00 |
| **Variable subtotal** |  | **900.35** |

If the reliability layer were ever run on paid providers instead of mocks, its conservative upper bound would be 30 × ₹27.925 = ₹837.75 and would need its own approval.

Loaded view of the full run on its own:

| Item | Atlas base (INR) | Atlas maximum (INR) |
|---|---:|---:|
| Variable subtotal | 900.35 | 900.35 |
| Plus Atlas month | 1,700.35 | 3,900.35 |
| After 20% contingency | 2,040.42 | 4,680.42 |
| After separate 18% tax cash-buffer scenario | 2,407.70 | 5,522.90 |

Combined-month warning: a full run plus the 20 smoke-test sessions in the same billing month has a variable subtotal of ₹1,458.85 (900.35 + 558.50). With the Atlas base month counted once, that becomes ₹2,258.85, then ₹2,710.62 after contingency and ₹3,198.53 after the tax buffer. This exceeds the ₹2,000 expected-spend alert, so it needs explicit approval, but it stays under the ₹5,100 ceiling. If Atlas instead reaches its USD 30 maximum, the same month reaches ₹6,313.73 (and the full run alone ₹5,522.90), which exceeds the ceiling. Section 9's stop rule then applies: split the work across billing months or obtain explicit approval first.

## 9. Alert and stop behaviour

- at projected or recorded INR 1,500: emit an internal warning and recheck usage/rate evidence;
- at INR 2,000: pause expansion/repeated nonessential paid tests and review actual provider/Atlas usage;
- before any action projected to exceed INR 5,100 in the billing month: stop new paid tests and obtain explicit approval;
- a provider's own lower quota/credit/spend limit wins over these application thresholds;
- free hard-cap exhaustion is a stop condition, not permission to upgrade a plan;
- a plan purchase, provider top-up, paid add-on, or higher ceiling requires separate approval;
- billing alerts may lag, so the application also maintains its own usage projection.

## 10. Free credits and discounts

Free trials, startup credits, promotional balances, and coupons are never assumed to exist in the baseline budget.

When verified:

- record provider, credit ID/reference, currency, starting value, expiry, eligibility, and remaining value;
- calculate gross list-price cost first;
- apply credit as a separate adjustment;
- report both gross technical cost and net payable cost;
- do not transfer a discount from one provider/model/plan to another;
- do not treat an expired/unconfirmed credit as available;
- do not hide usage merely because net payable cost is zero.

## 11. Required cost evidence

Each provider operation records, when available:

- provider, service, model, adapter, and endpoint mode;
- rate-card ID and exact unit rate;
- original billable quantity/unit;
- attempt result, retry/cancellation lineage, and usage status;
- gross provider cost in source currency;
- applied credit/discount separately;
- FX source/rate/date, USD normalized value, and INR display value;
- tax/payment/FX-fee status;
- calculation status and timestamp;
- estimation reason when provider-final evidence is unavailable.

Each session summary reports:

- STT, LLM, TTS, transport, database allocation, tax/fee, and credit lines;
- variable/marginal and fully allocated costs separately;
- gross and net payable totals;
- connected minutes and cost per minute;
- actual versus estimated/provisional/final status;
- retry/failure cost that contributed to the total.

## 12. Reconciliation

- attempt entries are the primary usage evidence;
- turn/session totals are derived summaries;
- current line items reconcile with the current total within 1% after documented rounding;
- invoice/provider-dashboard reconciliation creates a new calculation run rather than overwriting the original evidence;
- rate corrections or late usage preserve the superseded calculation lineage;
- credits, refunds, tax, and FX adjustments use separate entries;
- no completed billed attempt is dropped because its output was cancelled, failed, or unused.

## 13. Rate-card refresh policy

Refresh the source rate and create a new version:

- before the first paid call;
- when a promotion starts/expires;
- when a provider/model/plan/region changes;
- when FX planning assumptions change materially;
- at least monthly while paid R&D testing is active;
- before benchmark comparisons or a published cost recommendation.

Historical sessions remain tied to the rate card that produced their estimate. An invoice reconciliation can supersede the calculation run without rewriting the historical configuration.

## 14. Exclusions

This rate card does not authorize or price:

- xAI/Grok, Sarvam STT, ElevenLabs, or alternative transport challengers;
- LiveKit Ship/Scale, managed worker deployment, inference, observability recording, telephony, ingress, or egress;
- MongoDB production/dedicated tiers, backup, support, or additional services;
- OpenAI tools, Fast mode, regional processing, audio/realtime models, or long-context uplift;
- Deepgram paid add-ons;
- object storage, production hosting, CI/CD, monitoring SaaS, domains, certificates, phone numbers, SIP, or human-transfer services;
- employee/developer time and hardware/electricity;
- definitive legal/tax advice.

Each excluded charge requires a dated rate and explicit approval before use.

## 15. Acceptance criteria

- the approved rate card is versioned and dated;
- gross list-price, credit-adjusted, fixed/shared, tax/fee, and fully allocated costs remain distinct;
- planning uses INR 100/USD while final evidence uses actual conversion data when available;
- Deepgram budgets the regular rate and records the promotional rate only as current actual evidence;
- the conservative example variable rate is INR 2.7925 per session-minute (₹27.925 per ten-minute session);
- 20 ten-minute sessions plus Atlas base remain within the INR 2,000 expected alert scenario;
- INR 5,100 is a hard approval boundary, not a spending target;
- unknown usage/rate/tax/fee values are never treated as zero;
- retries and failed/cancelled billable attempts remain in cost evidence;
- rate changes create a new rate-card version;
- no paid call or plan upgrade is authorized merely by this document.
