import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

/**
 * Browser E2E (opt-in): run with `E2E=1 npx playwright test`. All API
 * traffic is mocked in the spec via `page.route`; no real backend, LiveKit
 * server, or provider is contacted. Chromium uses a fake microphone fed by
 * a small generated WAV.
 *
 * QA policy: traces are kept only on failure, screenshots only on failure,
 * video is off, and artifacts go under the git-ignored repo `outputs/` tree.
 */
const FAKE_AUDIO = fileURLToPath(new URL("./tests/e2e/fixtures/tone-440hz.wav", import.meta.url).href);

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: "**/*.spec.ts",
  outputDir: "../outputs/playwright",
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    launchOptions: {
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        `--use-file-for-fake-audio-capture=${FAKE_AUDIO}`,
      ],
    },
    permissions: ["microphone"],
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
  },
});
