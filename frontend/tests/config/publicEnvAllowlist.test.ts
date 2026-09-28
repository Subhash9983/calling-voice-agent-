import { describe, expect, it } from "vitest";
import { PUBLIC_ENV_ALLOWLIST, pickAllowedPublicEnv } from "../../src/config";

describe("PUBLIC_ENV_ALLOWLIST", () => {
  it("contains exactly the three approved docs/12 §11 names", () => {
    expect(PUBLIC_ENV_ALLOWLIST).toEqual([
      "VITE_API_BASE_URL",
      "VITE_APP_ENV",
      "VITE_BUILD_VERSION",
    ]);
  });
});

describe("pickAllowedPublicEnv", () => {
  it("keeps only allowlisted string values", () => {
    const picked = pickAllowedPublicEnv({
      VITE_API_BASE_URL: "http://127.0.0.1:8000/api/v1",
      VITE_APP_ENV: "development",
      VITE_BUILD_VERSION: "0.0.0-test",
    });

    expect(picked).toEqual({
      VITE_API_BASE_URL: "http://127.0.0.1:8000/api/v1",
      VITE_APP_ENV: "development",
      VITE_BUILD_VERSION: "0.0.0-test",
    });
  });

  it("drops any key outside the allowlist, including secret-shaped names", () => {
    const picked = pickAllowedPublicEnv({
      VITE_API_BASE_URL: "http://127.0.0.1:8000/api/v1",
      VITE_APP_ENV: "development",
      VITE_BUILD_VERSION: "0.0.0-test",
      OPENAI_API_KEY: "sk-canary-should-not-appear",
      DEEPGRAM_API_KEY: "canary-should-not-appear",
      SARVAM_API_KEY: "canary-should-not-appear",
      LIVEKIT_API_SECRET: "canary-should-not-appear",
      MONGODB_URI: "mongodb://canary-should-not-appear",
      MODE: "test",
      DEV: true,
    });

    expect(Object.keys(picked)).toEqual([
      "VITE_API_BASE_URL",
      "VITE_APP_ENV",
      "VITE_BUILD_VERSION",
    ]);
  });

  it("drops non-string values for allowlisted keys rather than coercing them", () => {
    const picked = pickAllowedPublicEnv({
      VITE_API_BASE_URL: true,
      VITE_APP_ENV: undefined,
    });

    expect(picked).toEqual({});
  });
});
