import { expect, test } from "@playwright/test";

const API = "http://127.0.0.1:8000/api/v1";
const CORS = {
  "access-control-allow-origin": "http://127.0.0.1:5173",
  "access-control-allow-headers": "content-type",
  "access-control-allow-methods": "GET,POST,OPTIONS",
};

test.skip(process.env["E2E"] !== "1", "Set E2E=1 to run browser E2E tests.");

test("start against a mocked API surfaces a transport failure and ends the session", async ({ page }) => {
  let endCalls = 0;

  await page.route(`${API}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS });
      return;
    }
    if (url.pathname.endsWith("/agent-configs")) {
      await route.fulfill({
        headers: CORS,
        json: {
          items: [{ agent_config_id: "cfg-1", name: "Mock agent", version: 1, description: null }],
          next_cursor: null,
          request_id: "r",
        },
      });
    } else if (url.pathname.endsWith("/sessions")) {
      await route.fulfill({
        status: 201,
        headers: CORS,
        json: {
          idempotent_replay: false,
          session: { session_id: "sess-1", status: "connecting", agent_activity_state: null, created_at: "2026-09-29T10:00:00Z", maximum_session_ms: 1800000 },
          // Unroutable loopback port: the LiveKit connect fails without touching a real server.
          transport: { provider: "livekit", url: "ws://127.0.0.1:1", room_name: "va-rd-x", participant_identity: "br", join_token: "e2e.mock.token", token_expires_at: "2026-09-29T10:10:00Z" },
          configuration: { agent_config_id: "cfg-1", name: "Mock agent", version: 1, stt: "stt", conversation_engine: "llm", tts: "tts" },
          request_id: "r",
        },
      });
    } else if (url.pathname.endsWith("/end")) {
      endCalls += 1;
      await route.fulfill({
        status: 202,
        headers: CORS,
        json: { data: { session_id: "sess-1", status: "ending", revision: 2, termination_request_revision: 1, disconnect_reason: "transport_error" }, idempotent_replay: false, request_id: "r" },
      });
    } else {
      await route.fulfill({ headers: CORS, json: { items: [], next_cursor: null, request_id: "r" } });
    }
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Voice Agent Session" })).toBeVisible();
  const start = page.getByRole("button", { name: "Start session" });
  await expect(start).toBeEnabled();

  await start.click();

  await expect(page.getByRole("alert").first()).toContainText(/could not be established|went wrong/i);
  await expect(page.getByRole("button", { name: "Start session" })).toBeEnabled();
  expect(endCalls).toBeGreaterThan(0);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("e2e.mock.token");
});
