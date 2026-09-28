# Dependency and Version Matrix

Status: Approved for Phase 0 R&D compatibility validation  
Authority: Decision 037 with Decisions 042, 043, 055, 067, and 068 amendments  
Scope: Direct dependency pins, compatibility gates, and package ownership  
Depends on: `00-voice-agent-master-plan.md`, `03-backend-module-design.md`, `07-stt-adapter-and-baseline.md`, `08-conversation-adapter-and-llm-baseline.md`, `09-tts-adapter-and-voice-baseline.md`, `12-configuration-and-secrets.md`  
Implementation status: Not started  
Last reviewed: 2026-09-28

Research date: 2026-09-26  
Python package manager: uv  
Frontend package manager: npm  
Incremental dependency licence/subscription cost: INR 0

## 1. Purpose

This document defines the candidate runtime versions, direct application dependencies, provider integration method, exact-lock policy, compatibility gate, upgrade controls, exclusions, and cost boundary for the Phase 0 browser voice agent.

These are approved direct pins/candidate pins for the first compatibility resolution. Transitive dependencies become authoritative only through committed lockfiles after the compatibility gate passes. An incompatible direct pin cannot be changed silently; the changed version and evidence require approval.

Core rules:

> Direct dependencies are intentional, minimal, and exact; transitive dependencies are reproducible through committed lockfiles.

> Provider SDKs remain inside replaceable adapters and never become domain contracts.

> Pre-release packages and unreviewed automatic upgrades are prohibited.

## 2. Runtime and package managers

| Component | Approved version | Role |
|---|---:|---|
| CPython | `3.12.14` | Backend, worker, and evaluation runtime |
| uv | `0.12.18` | Python environment, dependency resolution, lock, and execution |
| Node.js | `24.21.0` LTS | Frontend tooling runtime |
| npm | `11.19.0` | Frontend dependency and script manager |

Rationale:

- Python 3.12 is a conservative compatibility line for realtime/provider SDKs while retaining modern asyncio/type features;
- uv 0.12.18 includes a Windows wheel-installation path-traversal security fix relevant to this Windows R&D environment;
- Node 24.21.0 is an LTS release and bundles npm 11.19.0;
- Vite 8 supports the selected Node line.

CPython 3.12.14 is installed as a uv-managed Python build (`uv python install 3.12.14`) rather than a system-wide installer, and `.python-version` pins it for the project.

The runtime/tool versions are recorded in `.python-version`, Node version metadata, application build evidence, and development instructions after implementation is authorized.

## 3. Backend direct runtime dependencies

| Package | Approved candidate pin | Use |
|---|---:|---|
| `fastapi` | `0.141.1` | Control API and OpenAPI contract |
| `uvicorn[standard]` | `0.53.0` | Local ASGI server |
| `pydantic` | `2.13.5` | API/domain validation and serialization |
| `pydantic-settings` | `2.15.0` | Strict bootstrap configuration |
| `pymongo` | `4.18.1` | Async MongoDB Atlas driver and SRV resolution (`dnspython` is a base dependency; the `srv` extra no longer exists — Decision 068) |
| `livekit-agents[silero]` | `1.8.3` | Worker lifecycle, realtime participant/audio/data integration, plus local Silero VAD extra |
| `livekit-api` | `1.2.1` | Server token, dispatch, room, and cleanup control plane |
| `openai` | `2.54.0` | GPT-6 Luna Responses API streaming adapter (Decision 068: `livekit-agents 1.8.3` requires `openai>=2.50,<3`) |
| `deepgram-sdk` | `7.10.0` | Nova-3 streaming STT adapter; PyPI release verified 2026-09-21 |
| `sarvamai` | `0.1.34` | Bulbul v3 streaming TTS adapter |

`uvicorn 0.54.0` and `vite 8.3.1` were very newly released at the research cutoff. The proposed initial pins use the preceding stable versions to avoid adopting same-day releases before compatibility evidence.

No direct dependency is added merely because another framework recommends it. A package must correspond to an approved module or development gate.

## 4. Python project and lock contract

The future implementation uses:

```text
pyproject.toml
uv.lock
.python-version
```

Rules:

- `pyproject.toml` lists intentional direct dependencies and exact runtime constraints;
- `.python-version` selects the approved exact Python runtime;
- `uv.lock` captures exact direct/transitive artifacts and markers across supported environments;
- commit `uv.lock`; do not edit it manually;
- use `uv sync --locked` for reproducible setup after the lock exists;
- use locked/frozen execution in automated checks;
- do not use ad-hoc `pip install` inside the managed project environment;
- no Git branch, mutable URL, local wheel, or unverified private index dependency;
- stable PyPI releases only; alpha, beta, RC, dev, and yanked versions are rejected;
- preserve artifact provenance/hash information available through the lock/resolver;
- `.venv` is local and excluded from version control.

Prerelease exception (Decision 068): the transitive package `opentelemetry-semantic-conventions` (pulled in by `livekit-agents` through `opentelemetry-sdk`) publishes only `bN`-format versions upstream. The version frozen in `uv.lock` is accepted; it is not a direct dependency. The same exception covers the frontend transitive `gensync@1.0.0-beta.2` (Babel toolchain of `@vitejs/plugin-react`). No other prerelease is permitted.

Build backend (Decision 068): `uv_build==0.12.18`, used only to install the project's own `src` layout.

The uv tool itself is pinned/documented separately from application dependencies. A uv upgrade must not modify the dependency lock implicitly.

## 5. LiveKit integration choice

Use:

```text
livekit-agents[silero]==1.8.3
livekit-api==1.2.1
```

Responsibilities:

- `livekit-api` remains in the backend control-plane adapter for token creation, explicit dispatch, room inspection, and cleanup;
- `livekit-agents` remains in the worker/transport adapter for job lifecycle, participant connection, audio tracks, data messages, and transport events;
- the Silero extra is the sole approved LiveKit plugin exception and runs local VAD behind `SpeechActivityPort`; its resolved plugin/model dependencies are frozen by `uv.lock` only after the compatibility gate passes;
- LiveKit `AgentSession` and the LiveKit semantic/audio Turn Detector are not used in Phase 0; the application orchestrator and Turn Manager retain ownership;
- LiveKit objects do not enter domain/conversation/provider ports;
- the browser uses its separately pinned `livekit-client` package.

Do not install `livekit-agents` provider extras/plugins for OpenAI, Deepgram, or TTS in the first baseline. Provider SDKs are integrated through this project's normalized adapters. The approved Silero extra is local speech-activity infrastructure, not a provider integration, and its types/configuration cannot become the provider-independent contract.

If a future LiveKit plugin is evaluated, it must pass the same adapter behaviours, cancellation evidence, usage/cost extraction, and replacement tests before approval.

## 6. OpenAI integration choice

Use the official OpenAI Python SDK inside the conversation adapter:

- asynchronous client;
- Responses API;
- HTTP server-sent-event streaming;
- explicit system instruction and bounded application-owned history;
- provider-side conversation storage disabled where supported;
- no built-in tools/search;
- local cancellation generation remains authoritative.

Do not use the OpenAI Agents SDK or OpenAI Realtime speech-to-speech API in Phase 0. The approved architecture keeps independent STT, text conversation, and TTS adapters.

The SDK object/event types remain inside the adapter and are normalized before orchestration. The application does not depend on provider-authoritative conversation state.

## 7. Deepgram integration choice

Use the official `deepgram-sdk` v7 line, initially pinned to `7.10.0`, inside the STT adapter.

- use its supported async realtime/live transcription connection;
- pass normalized PCM and approved Nova-3 multilingual options;
- map SDK events/errors/usage to internal contracts;
- keep provider endpoint/SDK types inside the adapter;
- do not hand-roll a Deepgram WebSocket while the official SDK satisfies the approved contract;
- do not use a LiveKit Deepgram plugin in the initial baseline.

A direct WebSocket implementation would require evidence that the SDK blocks a required approved behaviour and separate approval.

## 8. Sarvam integration choice

Use the official stable `sarvamai` Python SDK, initially pinned to `0.1.34`, inside the TTS adapter.

- use its async streaming/WebSocket TTS support for Bulbul v3;
- map raw provider audio into the approved normalized PCM contract;
- keep authentication, typed provider errors, retries, and event objects inside the adapter;
- disable provider retry behaviour when it would conflict with the application retry/cancellation contract;
- do not use alpha/pre-release Sarvam packages;
- do not use a LiveKit Sarvam plugin in the initial baseline.

Sarvam's documented Python streaming-STT helper has raw-PCM constraints. This does not block the Phase 0 baseline because Deepgram is the baseline STT. The Sarvam STT challenger transport/SDK choice is reviewed when that benchmark adapter is approved.

## 9. MongoDB integration choice

Use `pymongo==4.18.1` and `pymongo.AsyncMongoClient` directly.

- no Motor;
- no Beanie or another ODM;
- explicit repositories and BSON codecs;
- one client per process/event loop, not shared across loops;
- MongoDB Stable API configuration where compatible with Atlas;
- explicit bounded timeouts and readiness checks;
- Decimal128/ObjectId/timestamp handling stays in persistence codecs;
- driver objects do not leave the infrastructure layer.

PyMongo 4.18.1 includes a published security fix and the Async API is GA in the current driver line. Motor was deprecated on 2025-05-14; ordinary bug-fix support ended on 2026-05-14 and critical-fix support ends on 2027-05-14. Motor is therefore not introduced into this new codebase.

## 10. Frontend direct runtime dependencies

| Package | Approved candidate pin | Use |
|---|---:|---|
| `react` | `19.3.0` | UI component runtime |
| `react-dom` | `19.3.0` | Browser rendering |
| `vite` | `8.3.0` | Development server and production build |
| `@vitejs/plugin-react` | `6.1.1` | React transform and development refresh |
| `typescript` | `6.0.3` | Static type checking on the latest line supported by the selected TypeScript ESLint range |
| `livekit-client` | `2.22.3` | Browser WebRTC audio/data connection |

The initial UI uses React state/hooks, browser `fetch`, Web APIs, and `livekit-client`. Do not add initially:

- a UI component framework;
- Redux, Zustand, or another global-state library;
- React Router;
- Axios;
- a charting package;
- LiveKit's React component library;
- a browser provider SDK or provider API key.

The approved single-page sandbox does not yet require these dependencies. A demonstrated requirement and separate approval precede any addition.

## 11. Frontend project and lock contract

The future implementation uses:

```text
package.json
package-lock.json
.nvmrc
```

Rules:

- npm package-management version is documented through the Node/npm runtime contract;
- `package.json` uses exact direct versions rather than caret/tilde ranges;
- configure save-exact behaviour;
- commit `package-lock.json` and do not edit it manually;
- use `npm ci` for reproducible locked installs;
- record the Node engine/runtime expectation;
- stable registry releases only; no alpha/beta/RC/nightly packages;
- no Git branch, CDN script, mutable URL, or globally required application package;
- build output and `node_modules` are not version-controlled;
- browser bundle inspection is part of secret/dependency validation.

## 12. Test and development dependencies

Approved backend candidates:

| Package | Approved candidate |
|---|---:|
| `pytest` | `9.1.1` |
| `pytest-asyncio` | Latest stable version compatible with Python/pytest, then exact lock |
| `pytest-cov` | Latest stable compatible version, then exact lock |
| `ruff` | Latest stable compatible version, then exact lock |
| `mypy` | Latest stable compatible version, then exact lock |
| `httpx` | `0.28.1` (WP4: direct dev dependency for control-API tests via `httpx.ASGITransport`; same version already resolved transitively, so the lock resolution is unchanged) |

Approved frontend candidates:

| Package | Approved candidate |
|---|---:|
| `vitest` | Stable `5.x`, exact compatible lock |
| `@testing-library/react` | `16.3.3` |
| `@playwright/test` | `1.63.0` |
| `eslint` | `10.11.0` |
| TypeScript ESLint packages | Latest stable mutually compatible set, then exact lock |

The exact compatible versions for the explicitly marked test/lint companion packages are accepted only after the lock resolution proves peer compatibility; the resolved versions are documented before implementation proceeds beyond the skeleton. TypeScript 7 is excluded because the current TypeScript ESLint support range is `<6.1.0`; `typescript==6.0.3` must pass `npm run lint`, typecheck, test, and build before the frontend lock is accepted.

Initially install only Playwright Chromium. Firefox and WebKit binaries are added when cross-browser voice testing is explicitly scheduled. Playwright is a local pinned development dependency, not a global package.

## 13. Optional challenger dependencies

Do not install challenger SDKs in the baseline environment merely because challenger providers are planned.

Later candidates may include:

- xAI official or approved OpenAI-compatible client for Grok;
- official ElevenLabs SDK for Flash v2.5;
- Sarvam STT raw WebSocket/SDK path;
- alternative transport SDKs.

For each challenger, first approve the endpoint/SDK strategy, stable direct version, credential, adapter mapping, cancellation/retry behaviour, usage/cost extraction, and evaluation scope. Prefer optional dependency groups so baseline installs remain minimal.

## 14. Compatibility gate

Before the candidate matrix becomes the locked implementation baseline:

1. resolve all Python dependencies for CPython 3.12.14 on the R&D Windows environment;
2. reject source builds or missing wheels that introduce an unapproved toolchain/risk, and confirm that the Silero VAD dependencies of `livekit-agents[silero]`, including `onnxruntime`, resolve to prebuilt Windows wheels for CPython 3.12 (win_amd64) and that the Silero model loads locally;
3. import and start the FastAPI control process and LiveKit worker independently;
4. validate safe Pydantic Settings construction/redaction;
5. connect and ping MongoDB Atlas with AsyncMongoClient;
6. create/validate a LiveKit token, explicit dispatch, worker connection, and cleanup path;
7. open/send/finalize/close a Deepgram streaming STT session;
8. stream an OpenAI Responses request, usage, and cancellation through the adapter;
9. receive first playable Bulbul v3 audio and cancel/close safely through Sarvam;
10. install the frontend with `npm ci`, type-check, test, and create a production build;
11. join LiveKit from the browser and pass two-way audio/data smoke tests;
12. run adapter contract, cancellation, redaction, and cost/usage smoke fixtures;
13. inspect lockfile changes, direct/transitive versions, licences, and known security advisories.

If a direct candidate is incompatible, yanked, insecure, unavailable, or cannot satisfy the approved contract, stop and propose an evidence-backed replacement. Do not silently upgrade/downgrade or bypass the provider adapter.

## 15. Dependency update policy

- no automatic merge or unattended dependency upgrades;
- update one logical package/provider family at a time;
- read authoritative release/migration/security notes;
- change declared version and regenerate the lockfile intentionally;
- inspect dependency-tree and licence changes;
- run unit, adapter-contract, integration, browser, security, and affected evaluation tests;
- compare quality, latency, reliability, and cost when a provider SDK/runtime can affect them;
- create a new application/configuration evidence version where runtime behaviour changes;
- retain the previous lockfile in version history for rollback;
- prioritize a confirmed security fix but still review/test the exact change;
- never run broad `update latest` as part of ordinary application startup.

New releases do not make the current lock stale automatically. Upgrade decisions remain explicit.

## 16. Explicit exclusions

The Phase 0 baseline does not use:

- Motor or MongoDB ODMs;
- LangChain, LlamaIndex, or a general agent framework;
- OpenAI Agents SDK or Realtime speech-to-speech;
- LiveKit provider plugins/extras other than the explicitly approved local Silero VAD extra;
- LiveKit `AgentSession` and semantic/audio Turn Detector;
- Pipecat, Vapi, Daily, or alternative transport SDKs;
- Redis, Celery, or a background-job framework;
- Docker/Kubernetes/cloud-deployment packages;
- frontend state/router/UI/chart frameworks;
- challenger xAI/ElevenLabs SDKs before their adapter milestone;
- prerelease/yanked/mutable Git dependencies;
- globally installed application dependencies;
- multiple Python or JavaScript package managers for the same workspace.

## 17. Cost and licensing boundary

The selected runtime, package managers, frameworks, clients, and test tools are open-source packages with no approved per-use library subscription fee:

```text
Incremental dependency licence/subscription cost: INR 0
```

This does not make the overall system free. Provider API usage, LiveKit Cloud, Atlas Flex, network, storage, FX/tax, paid support, CI compute, and future hosting remain separate costs. Playwright browser downloads consume local disk/network but do not add an approved per-test software fee.

Licences and dependency metadata are recorded/checked during lock creation. Open licence risk (Decision 068): `sarvamai==0.1.34` declares no licence; terms must be confirmed with Sarvam before WP9. A package with an incompatible licence requires removal or separate approval.

## 18. Deferred decisions

- exact resolved versions of marked companion lint/test packages;
- transitive dependency graph and artifact hashes until lock resolution;
- challenger SDK/package versions;
- Sarvam STT direct WebSocket versus SDK;
- deployment/container/production runtime images;
- cross-browser Playwright browser set;
- automated dependency update/scanning service;
- SBOM format and release-signing/attestation workflow;
- production support contracts or paid licences.

Each requires separate approval or compatibility evidence as stated.

## 19. Acceptance criteria

- CPython 3.12.14/uv 0.12.18 and Node 24.21.0/npm 11.19.0 are the initial runtimes/tools;
- TypeScript 6.0.3 is used instead of unsupported TypeScript 7 and must pass the complete lint/typecheck/build compatibility gate;
- backend/frontend direct candidate pins match this matrix;
- providers use their official SDKs inside replaceable adapters as approved;
- LiveKit provider plugins other than the approved local Silero VAD extra and general agent frameworks remain absent;
- LiveKit `AgentSession` and semantic/audio Turn Detector remain absent;
- PyMongo Async is used directly without Motor/ODM;
- lockfiles are committed and installed in locked mode;
- pre-release, yanked, mutable, and silent dependency changes are rejected;
- the full compatibility gate passes before the matrix becomes the implementation baseline;
- any failed direct pin returns for evidence-backed approval rather than silent substitution;
- libraries add no approved third-party subscription fee;
- no package/project/lock files are created merely because this design is approved.

## 20. Primary references

- [Python 3.12.14 release status](https://www.python.org/downloads/release/python-31214/)
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)
- [uv 0.12.18 security release](https://github.com/astral-sh/uv/releases)
- [Node.js 24.21.0 archive](https://nodejs.org/en/download/archive/v24.21.0)
- [FastAPI releases](https://fastapi.tiangolo.com/release-notes/)
- [PyMongo releases](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/release-notes/)
- [PyMongo Async migration](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/)
- [Motor lifecycle notice](https://www.mongodb.com/docs/drivers/motor/)
- [LiveKit Agents plugins/version family](https://docs.livekit.io/agents/models/)
- [LiveKit Silero VAD](https://docs.livekit.io/agents/logic/turns/vad/)
- [LiveKit JavaScript client](https://docs.livekit.io/reference/client-sdk-js/)
- [TypeScript ESLint supported dependency versions](https://typescript-eslint.io/users/dependency-versions/)
- [OpenAI official SDKs](https://developers.openai.com/api/docs/libraries)
- [OpenAI Responses streaming](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Deepgram Python SDK](https://github.com/deepgram/deepgram-python-sdk)
- [Deepgram SDK 7.10.0 on PyPI](https://pypi.org/project/deepgram-sdk/7.10.0/)
- [Sarvam official SDKs](https://docs.sarvam.ai/api/getting-started/sdks)
- [React versions](https://react.dev/versions)
- [Vite releases](https://vite.dev/releases)
- [Playwright browsers](https://playwright.dev/docs/browsers)
