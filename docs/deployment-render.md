# Render deployment (limited sharing, Decision 070)

## Services

- `voice-agent-api` (web): the control API, `python -m voice_agent.control_api --persistence mongodb`,
  bound to 0.0.0.0:10000 only because `APP_DEPLOYMENT_MODE=remote_limited_sharing`.
- `voice-agent-worker` (background worker): the LiveKit agent worker,
  `python -m voice_agent.agent_worker --media-mode conversation`. It opens no public port.
- The frontend static site is deployed separately; its API base URL must be the web service URL.

## Environment variables (names only; enter secret values in the Render dashboard)

| Name | Service | Purpose |
|---|---|---|
| APP_DEPLOYMENT_MODE | api | `remote_limited_sharing`; enables the Decision 070 exception |
| APP_API_HOST | api | `0.0.0.0` (accepted only in remote mode) |
| APP_API_PORT / PORT | api | `10000`, the port Render routes to |
| APP_PUBLIC_ORIGIN | api | the frontend's exact `https://<site>.onrender.com` origin |
| APP_API_PUBLIC_HOST | api | this API's own hostname, e.g. `<api>.onrender.com` (no scheme/port) |
| APP_DAILY_SPEND_CAP_INR | api | hard daily cap, default and maximum 200.00 |
| APP_DEFAULT_AGENT_CONFIG_ID | both | `00000000-0000-4000-8000-00000000c9a1` (full pipeline; must be seeded in Atlas) |
| APP_AGENT_NAME | both | LiveKit dispatch name; identical in both services |
| MONGODB_URI | both | Atlas connection string (database `voice_agent_rnd`) |
| LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET | both | LiveKit Cloud project |
| DEEPGRAM_API_KEY, OPENAI_API_KEY, SARVAM_API_KEY | worker | provider credentials |
| PYTHON_VERSION, UV_PYTHON_PREFERENCE | both | 3.12.14 runtime used by uv |

`VOICE_AGENT_SECRETS_FILE` is not used on Render; settings load from environment variables only.

## MongoDB Atlas network access (user action, decision point)

Atlas must accept connections from Render. Render's static outbound IPs are a paid-tier feature:
on such a plan, add those IPs to Atlas Network Access. On a plan without static outbound IPs, the
only option is Atlas "Allow Access from Anywhere" (0.0.0.0/0), which leaves the R&D database
protected by its credentials alone. That is a real security tradeoff the user must decide
explicitly; it is not recommended by default.

## Decision 070 conditions

The URL is shared directly with one or two named testers and works without any login, so treat it
as a credential: anyone who has it can use the app, list sessions and read their transcripts. New
sessions are refused once the UTC day's recorded provider cost reaches INR 200.00 (Render's own
hosting fees are not included). The loopback-only bind is relaxed only by the explicit deployment
mode flag; local use is unchanged. No accounts or session ownership exist, and the existing
Atlas/secrets/retention boundaries are unchanged. Extending or replacing this arrangement needs a
new explicit decision from the user.
