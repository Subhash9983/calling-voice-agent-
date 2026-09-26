# Configuration and Secrets Contract

Status: Approved for Phase 0 R&D  
Authority: Decision 036 with Decisions 058, 059, and 067 amendments  
Scope: Bootstrap settings, browser-public configuration, and local R&D secret boundaries  
Depends on: `00-voice-agent-master-plan.md`, `02-database-design.md`, `03-backend-module-design.md`, `04-control-api-contract.md`, `05-agent-worker-orchestration.md`, `06-livekit-transport-adapter.md`  
Implementation status: Not started  
Last reviewed: 2026-09-26

Python settings library: Pydantic Settings v2  
Secret storage: Local file outside the repository and OneDrive  
Incremental R&D configuration-management cost: INR 0

## 1. Purpose

This document separates safe bootstrap settings, versioned agent behaviour, browser-public configuration, and secrets. It defines configuration sources, precedence, credential references, startup validation, logging redaction, local R&D rotation, and the production boundary.

The workspace is inside OneDrive. Real API keys, database connection strings, and provider credentials must therefore not be stored in a project `.env` file or any other repository/OneDrive path.

Core rules:

> Secrets remain outside the repository, OneDrive, MongoDB documents, browser bundles, API responses, and ordinary logs.

> Material voice-agent behaviour is controlled by an immutable versioned `agent_config`, not by undocumented environment overrides.

> A process fails safely at startup when an enabled baseline dependency is missing or invalid.

## 2. Configuration classes

| Class | Examples | Authoritative storage |
|---|---|---|
| Safe application defaults | default host, bounded internal defaults | Version-controlled Python/default configuration |
| R&D environment settings | environment name, port, public origin, database name | Version-controlled safe development configuration or explicit runtime override |
| Agent behaviour | provider/model, voice, prompt, language, timeouts, retry, rate-card version | Immutable versioned MongoDB `agent_configs` |
| Secrets | API keys, LiveKit secret, MongoDB URI | External local R&D secret file or explicit process environment |
| Browser-public configuration | API base URL, environment label, build version | Vite public build configuration |

No configuration class may be moved to a less protected location merely for convenience.

## 3. Python settings approach

The control API and agent worker use Pydantic Settings v2 for bootstrap configuration.

Requirements:

- strict typed fields;
- explicit aliases for accepted environment-variable names;
- unknown or misspelled settings rejected where the selected source supports detection;
- required baseline values validated before the process reports readiness;
- URLs, ports, environment names, file paths, origins, and database names validated;
- placeholder/example secret values rejected;
- safe custom validation errors that never echo secret values;
- immutable settings after successful construction;
- no arbitrary runtime mutation or browser override.

The exact package versions are pinned in `13-dependency-and-version-matrix.md`: `pydantic==2.13.5` and `pydantic-settings==2.15.0`, subject to that document's compatibility gate.

## 4. External R&D secret file

Recommended Windows location:

```text
C:\Users\<current-user>\AppData\Local\VoiceAgentRND\secrets.env
```

The process receives or resolves this path through:

```text
VOICE_AGENT_SECRETS_FILE=<absolute external path>
```

Rules:

- the file remains outside this Git workspace and OneDrive;
- the path must resolve to a regular local file, not a repository-relative path;
- filesystem access is restricted to the current Windows user as far as the local environment permits;
- the file is plain text for the approved single-user R&D phase, not a production vault;
- the application never creates, overwrites, prints, copies, uploads, or backs up the file automatically;
- its values are loaded in memory only by server-side processes that require them;
- no secret value is written back to disk by the application;
- no file watcher or hot reload is enabled;
- startup fails safely if the configured file cannot be validated or required enabled secrets are absent.

The repository may contain `.env.example` with variable names and unmistakable placeholders only. It cannot contain working credentials.

## 5. Baseline secret names

The enabled Phase 0 baseline requires:

```text
MONGODB_URI
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
DEEPGRAM_API_KEY
OPENAI_API_KEY
SARVAM_API_KEY
```

`LIVEKIT_URL` is required operational connection configuration. It may reveal infrastructure topology and is handled server-side even though it is not equivalent to a credential. The control API may return only the safe client connection URL required for an authorized join.

The first challenger adapters may later use:

```text
XAI_API_KEY
ELEVENLABS_API_KEY
```

Challenger credentials are optional while those adapters are disabled. Enabling a challenger without its required credential fails validation for that benchmark/configuration rather than breaking the unrelated baseline.

No provider key name implies permission to expose its value through configuration inspection.

## 6. Safe bootstrap settings

The initial safe bootstrap contract is:

```text
# APP_ENV: development | rd (production is rejected in Phase 0)
APP_ENV=development
APP_LOG_LEVEL=INFO
APP_API_HOST=127.0.0.1
APP_API_PORT=8000
APP_PUBLIC_ORIGIN=http://127.0.0.1:5173
APP_AGENT_NAME=phase0-voice-agent
APP_DEFAULT_AGENT_CONFIG_ID=<approved-config-id>

# MONGODB_DATABASE: default database name
MONGODB_DATABASE=voice_agent_rnd
LIVEKIT_URL=<livekit-server-url>
VOICE_AGENT_SECRETS_FILE=<absolute-external-secret-file-path>
```

Rules:

- `APP_ENV` accepts `development` or `rd` only; `production` is reserved in the `02-database-design.md` schema enums for the future, but Phase 0 startup rejects it;
- Phase 0 control API binds to loopback by default;
- `APP_PUBLIC_ORIGIN` is an exact trusted origin, never wildcard CORS;
- the Vite development server binds to and is opened at `http://127.0.0.1:5173`; `localhost` is not treated as an interchangeable origin;
- ports must be valid and cannot silently choose a random public binding;
- `MONGODB_DATABASE` defaults to `voice_agent_rnd`, the approved single R&D database; it is configurable but must name that one approved database;
- the default agent configuration ID must resolve to an active exact version/checksum before readiness;
- safe configuration files cannot contain passwords, connection strings, tokens, or provider keys.

## 7. Behaviour configuration boundary

These material settings belong in immutable versioned `agent_configs`, not ordinary environment variables:

- transport/STT/conversation/TTS provider and model IDs;
- prompt ID, version, and checksum;
- voice, language routing, audio encoding, and sample rate;
- speech-activity engine/version, VAD thresholds/timings, turn/endpoint, interruption, timeout, retry, and queue policy;
- output/context/segment limits;
- tool-set and knowledge-engine mode;
- pronunciation/keyterm references;
- cost rate-card and currency references;
- safe provider options and adapter versions.

Named speech-activity keys include `vad.playback_activation_threshold` (default `0.7`), the Silero activation threshold used for interruption candidates while agent audio is playing (the normal threshold is `0.5`; the 250 ms confirmation still applies). The endpoint deadline is capped at 1,000 ms; a value above 1,000 ms requires re-approval of the latency budget.

An environment variable cannot silently replace a model, voice, prompt, endpointing value, or safety policy. A material change creates a new agent-configuration version and new comparison evidence.

## 8. Source precedence

For bootstrap fields, highest priority is:

1. explicit process environment variable;
2. external R&D secret file;
3. safe environment-specific configuration;
4. safe application default.

This precedence permits a deliberate temporary local override while keeping values typed and inspectable. It does not allow a bootstrap value to override immutable agent behaviour.

The external secret file must not override safe behaviour fields that do not belong to the secret/bootstrap contract. Conflicting duplicate secret definitions create safe startup evidence without logging either value.

## 9. Credential references

MongoDB stores a reference, never a credential:

```text
credential_ref: env:OPENAI_API_KEY
credential_ref: env:DEEPGRAM_API_KEY
credential_ref: env:SARVAM_API_KEY
```

The prefix describes the configured resolver contract; it does not expose the value. Only a server-side credential resolver may turn an allowed reference into a secret for its adapter.

The credential-reference allowlist covers provider adapter credentials only: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` and `SARVAM_API_KEY`, plus `XAI_API_KEY` and `ELEVENLABS_API_KEY` once their challenger adapters are approved. `MONGODB_URI`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` are bootstrap-only: they are loaded from the bootstrap sources in §8 at process start and cannot be selected through a `credential_ref`.

Rules:

- accept only approved reference schemes and allowlisted variable names;
- reject browser-provided or arbitrary credential references;
- do not support command execution, URL fetching, templating, or path traversal in a reference;
- resolve as late as practical and never attach the value to domain events;
- provider SDK objects/headers containing credentials remain inside their adapter;
- configuration checks may report available/missing/invalid, never the value, prefix, suffix, or length.

## 10. Process access

For the single-machine R&D phase, the control API and worker may load the same external secret source. Each process exposes only the credentials its enabled adapters require to its internal modules.

The API uses MongoDB and LiveKit control-plane credentials. The worker uses MongoDB, LiveKit, and enabled STT/LLM/TTS credentials. Provider-independent domain code receives adapter instances, not credentials.

Separate service identities and least-privilege production secret injection are deferred to production architecture. The shared local file is not approved for a multi-user, shared-host, server, container-cluster, or public deployment.

## 11. Browser-public configuration

The Vite application may receive only:

```text
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
VITE_APP_ENV=development
VITE_BUILD_VERSION=<build-version>
```

All `VITE_*` values are treated as public because they can be embedded in the browser bundle.

Never expose through the bundle, HTML, source maps, local storage, browser logs, or public configuration responses:

- `MONGODB_URI`;
- LiveKit API key or secret;
- STT/LLM/TTS provider keys;
- system prompt text or restricted internal configuration;
- administrative/service tokens;
- secret-file paths or host filesystem details.

The control API returns a short-lived least-privilege LiveKit participant token and only the safe connection/session fields defined in the approved API contract. The token is transient and is not treated as static frontend configuration.

## 12. Startup validation and readiness

The control API validates before readiness:

- bootstrap field syntax and environment boundary;
- exact frontend origin;
- MongoDB URI presence and successful approved database access;
- database name;
- LiveKit URL/key/secret presence and required control-plane capability;
- active default agent-configuration identity/version/checksum;
- required indexes/schema compatibility when implemented.

The worker additionally validates:

- Deepgram, OpenAI, and Sarvam credentials for the enabled baseline;
- enabled adapter registration;
- provider/model/voice/language/audio-option compatibility;
- prompt/configuration/rate-card references;
- MongoDB and LiveKit worker access.

Missing, blank, example, malformed, or unsupported enabled values prevent readiness. Authentication checks use bounded safe calls when supported and do not generate user content merely to test a key.

Liveness may remain healthy while readiness is false. Safe readiness detail uses normalized component/status/reason codes without secrets or raw provider errors.

## 13. Logging and redaction

Redact or exclude:

- all configured secret fields;
- MongoDB URI credentials and query secrets;
- authorization/cookie headers;
- LiveKit participant tokens;
- signed URLs;
- provider request payload fields that may contain credentials;
- secret-file contents and absolute path details from browser/general errors;
- credential-like high-entropy values caught by bounded defensive filters.

Safe evidence may contain:

```text
credential_source: external_secret_file
credential_available: true
```

Do not log secret values, prefixes, suffixes, lengths, hashes, or reversible encodings. Redaction is defence in depth; code must avoid adding secrets to logs/events/errors in the first place.

Debug mode does not waive these rules. Raw settings dumps, environment dumps, request headers, provider objects, and exception locals are prohibited in ordinary logs.

## 14. Git and workspace protection

Implementation must provide defence-in-depth ignore patterns for:

- `.env` and `.env.*`, while explicitly permitting a safe `.env.example`;
- local secrets and credential exports;
- service-account JSON;
- PEM/private-key files;
- provider CLI credential caches if they can appear in the workspace.

Before commit/review, automated secret scanning is recommended. Ignore rules do not make it acceptable to temporarily place a real secret in the repository because Git history and OneDrive may retain it.

If a secret is accidentally exposed, remove it from use and rotate/revoke it; deleting the visible file is not sufficient.

## 15. R&D secret rotation

Approved manual sequence:

1. create or obtain a new project-specific R&D key;
2. replace the value in the external secret file without printing it;
3. restart affected API/worker processes;
4. verify readiness and safe provider authentication status;
5. complete a bounded smoke test where required;
6. revoke the old key in the provider console;
7. record only safe rotation metadata outside ordinary session logs.

Phase 0 has no hot reload, automatic rotation, overlapping-key orchestration, or in-application key-management UI.

## 16. Provider-account safeguards

- use project-specific R&D credentials rather than personal/master/root keys where providers support it;
- grant only the required API/product scope where granular permissions exist;
- configure provider spend/rate alerts or limits where available;
- never paste keys into source, Markdown, tickets, chat, screenshots, test fixtures, or shell history;
- revoke unused challenger/provider keys;
- treat a suspected leak as compromise and rotate promptly;
- do not reuse R&D credentials for production.

Provider-specific permission, spend-limit, and rotation mechanics are verified at implementation/onboarding time because they may change.

## 17. Cost

Approved incremental Phase 0 configuration-management cost:

| Component | Cost |
|---|---:|
| Pydantic Settings | INR 0 |
| External local secret file | INR 0 |
| Local configuration validation/redaction code | No third-party service fee |

MongoDB Atlas, LiveKit, STT, LLM, TTS, tax, FX, and network usage remain separate dated cost entries. A future managed production secret store may introduce storage, API-call, rotation, audit, and egress charges and requires separate approval.

## 18. Testing gates

- settings load from the approved sources with the documented precedence;
- missing/blank/example baseline secrets prevent readiness;
- a disabled challenger does not require its credential;
- an enabled challenger without its credential fails safely;
- behaviour cannot be changed through an unapproved environment variable;
- the selected immutable agent configuration and checksum are validated;
- browser bundle/config/API projections contain no server secret or restricted field;
- logs/errors/events redact synthetic secret fixtures and connection-string credentials;
- credential references accept only allowlisted schemes/names;
- no settings/environment dump occurs in debug/error paths;
- configuration objects are immutable after startup;
- rotation takes effect only after controlled restart;
- repository scans find no real credential fixture.

Synthetic canary values, never working keys, are used to verify redaction.

## 19. Phase 0 exclusions

- real secrets in the repository, project `.env`, OneDrive, MongoDB, browser, or ordinary logs;
- production managed secret vault;
- container/Kubernetes/cloud secret injection;
- automatic or hot secret rotation;
- public deployment or shared-host multi-user use;
- browser-selectable provider credentials/configuration;
- runtime prompt/model/voice override through environment variables;
- administrative credential-management API/UI;
- secret backup by this project;
- production service identities, DPA, residency, and compliance claims.

## 20. Deferred decisions

- committed safe configuration file format and exact filenames;
- external secret-file setup script/process;
- provider-specific project/key permissions and spend controls;
- production secret manager/vendor;
- separate API/worker production service identities;
- automated rotation and overlapping-key procedure;
- CI/CD secret injection and secret-scanning product;
- deployment-specific network identity and workload federation.

Each requires separate approval.

## 21. Acceptance criteria

- Pydantic Settings v2 is the approved Python bootstrap settings mechanism;
- real R&D secrets live outside the repository and OneDrive;
- the project stores only safe example names/placeholders;
- the approved source precedence is explicit and tested;
- behaviour remains governed by versioned immutable `agent_configs`;
- MongoDB stores only allowlisted credential references, never values;
- the browser receives only approved public configuration and transient join data;
- API/worker readiness validates their enabled dependencies safely;
- no log/error/event leaks secret values or credential-bearing payloads;
- local manual rotation requires restart and old-key revocation;
- configuration management adds no third-party R&D service charge;
- production secret architecture remains unapproved and out of scope;
- no secret/configuration/code files are created merely because this contract is approved.
