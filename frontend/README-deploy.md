# Frontend deployment (Render Static Site) — limited-sharing R&D exception

> Authority: Decision 070 (limited-sharing remote deployment exception), as
> summarized by the orchestrator for this work package. This deployment adds
> **no authentication, account system, or login screen**. It is a direct URL
> shared with 1-2 named testers only — do not post or link it anywhere public.
> The backend enforces a server-side daily spend cap; if a session is refused
> for budget reasons, the browser shows the backend's generic error message
> through the existing error UI (no frontend change needed for that case).

## Build-time configuration

This app reads exactly three build-time environment variables, validated at
startup by `src/config/publicConfig.ts` against the allowlist in
`src/config/publicEnvAllowlist.ts` (docs/12 §11). There is no runtime config
and no hardcoded fallback — if any of these are missing or invalid, the app
fails fast with a safe, non-secret-bearing error instead of guessing a value.

| Variable | Value for this deployment | Notes |
|---|---|---|
| `VITE_API_BASE_URL` | The backend's real Render URL, including the `/api/v1` prefix, e.g. `https://<your-backend>.onrender.com/api/v1` | Must be an absolute `http(s)` URL with no embedded userinfo credentials. |
| `VITE_APP_ENV` | `rd` | Phase 0 only approves `development` and `rd` (see `PUBLIC_APP_ENV_VALUES` in `src/config/publicConfig.ts`) — there is no `production` value yet, so use `rd` for this limited-sharing deployment. |
| `VITE_BUILD_VERSION` | Any non-blank build identifier, e.g. a short git SHA or release tag | Truncated to 100 characters; purely informational/diagnostic. |

Set these in the Render Static Site's environment variables (Settings →
Environment) before building — Vite inlines them into the bundle at build
time, so changing them requires a rebuild, not just a redeploy.

## Render Static Site settings

- **Build command:** `npm ci && npm run build`
- **Publish directory:** `dist`
- **Rewrites/redirects:** none required. This is a single-page app with no
  client-side router (no `react-router` or similar dependency in
  `package.json`, no history-API navigation in `src/`) — there is only one
  route, served by `index.html` at the root. A SPA history-mode rewrite rule
  is unnecessary; do not add one unless a router is introduced later.

## Cross-origin behavior

The frontend (Render Static Site domain) and backend (Render Web Service
domain) are different origins. This already works under the existing
security model (docs/04, WP4):

- `src/api/controlApiClient.ts` sends every request with `credentials: "omit"`
  and `cache: "no-store"` — no cookies are ever sent, so there is nothing for
  CORS credentials rules to conflict with.
- The backend's CORS policy allows exactly one exact origin (the frontend's
  real Render URL) with credentials off. As long as `VITE_API_BASE_URL` points
  at the correct backend origin, and the backend's allowed-origin
  configuration is updated to the frontend's actual Render URL, no frontend
  change is needed.

## LiveKit connection

The browser never hardcodes a LiveKit URL. `src/session/controller.ts` calls
`transport.connect(join.url, join.joinToken)`, where `join` is the
`TransportJoin` parsed from the control API's session-create/join-token
response body (`src/contracts/sessionApi.ts`). The LiveKit URL and the
short-lived scoped token are entirely backend-determined per request.

## Scope reminder (Decision 070)

- No login, access code, password gate, or account system has been added, and
  none should be added for this deployment — only share the URL directly with
  the 1-2 intended testers.
- The server-side daily spend cap and any "not available" / service-unavailable
  responses are a backend concern; the existing generic error UI
  (`src/components/SessionScreen.tsx`) already renders the backend's safe
  error message and retry hint without modification.
