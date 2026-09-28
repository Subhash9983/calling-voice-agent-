import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getPublicConfig,
  validatePublicConfig,
  type PublicConfigErrorCode,
  type RawPublicEnv,
} from "../../src/config";

const VALID_ENV: RawPublicEnv = {
  VITE_API_BASE_URL: "http://127.0.0.1:8000/api/v1",
  VITE_APP_ENV: "development",
  VITE_BUILD_VERSION: "0.0.0-test",
};

function withoutKey(source: RawPublicEnv, key: keyof RawPublicEnv): RawPublicEnv {
  const next: RawPublicEnv = {};
  for (const [entryKey, entryValue] of Object.entries(source)) {
    if (entryKey !== key) {
      next[entryKey] = entryValue;
    }
  }
  return next;
}

function expectFailureCode(
  result: ReturnType<typeof validatePublicConfig>,
  code: PublicConfigErrorCode,
): void {
  expect(result.ok).toBe(false);
  if (!result.ok) {
    expect(result.error.code).toBe(code);
  }
}

describe("validatePublicConfig", () => {
  it("accepts the approved safe bootstrap values", () => {
    const result = validatePublicConfig(VALID_ENV);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config).toEqual({
        apiBaseUrl: "http://127.0.0.1:8000/api/v1",
        appEnv: "development",
        buildVersion: "0.0.0-test",
      });
    }
  });

  it("accepts the rd environment label", () => {
    const result = validatePublicConfig({ ...VALID_ENV, VITE_APP_ENV: "rd" });

    expect(result.ok).toBe(true);
  });

  it("fails safely when VITE_API_BASE_URL is missing", () => {
    const result = validatePublicConfig(withoutKey(VALID_ENV, "VITE_API_BASE_URL"));

    expectFailureCode(result, "missing_api_base_url");
  });

  it("fails safely when VITE_API_BASE_URL is blank", () => {
    const result = validatePublicConfig({ ...VALID_ENV, VITE_API_BASE_URL: "   " });

    expectFailureCode(result, "missing_api_base_url");
  });

  it("fails safely when VITE_API_BASE_URL is malformed", () => {
    const result = validatePublicConfig({
      ...VALID_ENV,
      VITE_API_BASE_URL: "not-a-valid-url",
    });

    expectFailureCode(result, "invalid_api_base_url");
  });

  it("rejects a non-http(s) VITE_API_BASE_URL scheme", () => {
    const result = validatePublicConfig({
      ...VALID_ENV,
      VITE_API_BASE_URL: "ftp://127.0.0.1:8000/api/v1",
    });

    expectFailureCode(result, "invalid_api_base_url");
  });

  it("rejects a VITE_API_BASE_URL embedding userinfo credentials", () => {
    const result = validatePublicConfig({
      ...VALID_ENV,
      VITE_API_BASE_URL: "http://user:pass@127.0.0.1:8000/api/v1",
    });

    expectFailureCode(result, "unsafe_api_base_url");
  });

  it("fails safely when VITE_APP_ENV is missing", () => {
    const result = validatePublicConfig(withoutKey(VALID_ENV, "VITE_APP_ENV"));

    expectFailureCode(result, "missing_app_env");
  });

  it("rejects an unapproved VITE_APP_ENV value such as production", () => {
    const result = validatePublicConfig({ ...VALID_ENV, VITE_APP_ENV: "production" });

    expectFailureCode(result, "invalid_app_env");
  });

  it("rejects an arbitrary VITE_APP_ENV value", () => {
    const result = validatePublicConfig({ ...VALID_ENV, VITE_APP_ENV: "staging" });

    expectFailureCode(result, "invalid_app_env");
  });

  it("fails safely when VITE_BUILD_VERSION is missing", () => {
    const result = validatePublicConfig(withoutKey(VALID_ENV, "VITE_BUILD_VERSION"));

    expectFailureCode(result, "missing_build_version");
  });

  it("fails safely when VITE_BUILD_VERSION is blank", () => {
    const result = validatePublicConfig({ ...VALID_ENV, VITE_BUILD_VERSION: "   " });

    expectFailureCode(result, "missing_build_version");
  });

  it("trims and bounds an overlong VITE_BUILD_VERSION", () => {
    const overlong = "v".repeat(500);
    const result = validatePublicConfig({ ...VALID_ENV, VITE_BUILD_VERSION: overlong });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config.buildVersion.length).toBe(100);
    }
  });

  it("ignores fields outside the approved public allowlist", () => {
    const result = validatePublicConfig({
      ...VALID_ENV,
      LIVEKIT_API_SECRET: "should-never-be-read",
      MODE: "test",
      DEV: true,
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(JSON.stringify(result.config)).not.toContain("should-never-be-read");
    }
  });

  it("never echoes the raw invalid value in the error message", () => {
    const result = validatePublicConfig({
      ...VALID_ENV,
      VITE_API_BASE_URL: "javascript-injection-marker://unsafe",
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.message).not.toContain("javascript-injection-marker");
    }
  });
});

describe("getPublicConfig", () => {
  beforeEach(() => {
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:8000/api/v1");
    vi.stubEnv("VITE_APP_ENV", "development");
    vi.stubEnv("VITE_BUILD_VERSION", "0.0.0-test");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("reads validated configuration from import.meta.env", () => {
    const result = getPublicConfig();

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config).toEqual({
        apiBaseUrl: "http://127.0.0.1:8000/api/v1",
        appEnv: "development",
        buildVersion: "0.0.0-test",
      });
    }
  });

  it("fails safely when import.meta.env is missing an approved field", () => {
    vi.stubEnv("VITE_APP_ENV", "");

    const result = getPublicConfig();

    expectFailureCode(result, "missing_app_env");
  });
});
