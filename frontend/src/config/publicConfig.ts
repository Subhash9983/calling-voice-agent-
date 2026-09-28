/**
 * Validated browser-public configuration (docs/12-configuration-and-secrets.md §11).
 *
 * This module is the only place application code may read build-time
 * environment configuration. It reads exclusively from the approved
 * allowlist (see `publicEnvAllowlist.ts`), validates every field, and fails
 * safely with a normalized, non-secret-bearing error code when a value is
 * missing or malformed. Nothing here ever handles a provider key, LiveKit
 * secret, or MongoDB URI: those never reach the browser (docs/12 §11).
 */
import { pickAllowedPublicEnv, type RawPublicEnv } from "./publicEnvAllowlist";

export const PUBLIC_APP_ENV_VALUES = ["development", "rd"] as const;

export type PublicAppEnv = (typeof PUBLIC_APP_ENV_VALUES)[number];

export interface PublicConfig {
  readonly apiBaseUrl: string;
  readonly appEnv: PublicAppEnv;
  readonly buildVersion: string;
}

/**
 * Normalized, safe failure codes. The associated `message` never echoes a
 * raw configuration value back to the caller: only the field name and the
 * category of problem, matching the redaction rules in docs/12 §12–13.
 */
export type PublicConfigErrorCode =
  | "missing_api_base_url"
  | "invalid_api_base_url"
  | "unsafe_api_base_url"
  | "missing_app_env"
  | "invalid_app_env"
  | "missing_build_version";

export interface PublicConfigError {
  readonly code: PublicConfigErrorCode;
  readonly field: keyof PublicConfig;
  readonly message: string;
}

export type PublicConfigResult =
  | { readonly ok: true; readonly config: PublicConfig }
  | { readonly ok: false; readonly error: PublicConfigError };

const MAX_BUILD_VERSION_LENGTH = 100;

function fail(
  code: PublicConfigErrorCode,
  field: keyof PublicConfig,
  message: string,
): PublicConfigResult {
  return { ok: false, error: { code, field, message } };
}

function validateApiBaseUrl(
  rawValue: string | undefined,
): { ok: true; value: string } | { ok: false; result: PublicConfigResult } {
  if (rawValue === undefined || rawValue.trim().length === 0) {
    return {
      ok: false,
      result: fail(
        "missing_api_base_url",
        "apiBaseUrl",
        "VITE_API_BASE_URL is required and must not be blank.",
      ),
    };
  }

  let parsed: URL;
  try {
    parsed = new URL(rawValue);
  } catch {
    return {
      ok: false,
      result: fail(
        "invalid_api_base_url",
        "apiBaseUrl",
        "VITE_API_BASE_URL must be a well-formed absolute URL.",
      ),
    };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return {
      ok: false,
      result: fail(
        "invalid_api_base_url",
        "apiBaseUrl",
        "VITE_API_BASE_URL must use http or https.",
      ),
    };
  }

  if (parsed.username.length > 0 || parsed.password.length > 0) {
    return {
      ok: false,
      result: fail(
        "unsafe_api_base_url",
        "apiBaseUrl",
        "VITE_API_BASE_URL must not embed userinfo credentials.",
      ),
    };
  }

  return { ok: true, value: rawValue };
}

function validateAppEnv(
  rawValue: string | undefined,
): { ok: true; value: PublicAppEnv } | { ok: false; result: PublicConfigResult } {
  if (rawValue === undefined || rawValue.trim().length === 0) {
    return {
      ok: false,
      result: fail(
        "missing_app_env",
        "appEnv",
        "VITE_APP_ENV is required and must not be blank.",
      ),
    };
  }

  const candidates: readonly string[] = PUBLIC_APP_ENV_VALUES;
  if (!candidates.includes(rawValue)) {
    return {
      ok: false,
      result: fail(
        "invalid_app_env",
        "appEnv",
        "VITE_APP_ENV must be one of the approved Phase 0 environment labels.",
      ),
    };
  }

  return { ok: true, value: rawValue as PublicAppEnv };
}

function validateBuildVersion(
  rawValue: string | undefined,
): { ok: true; value: string } | { ok: false; result: PublicConfigResult } {
  if (rawValue === undefined || rawValue.trim().length === 0) {
    return {
      ok: false,
      result: fail(
        "missing_build_version",
        "buildVersion",
        "VITE_BUILD_VERSION is required and must not be blank.",
      ),
    };
  }

  const trimmed = rawValue.trim().slice(0, MAX_BUILD_VERSION_LENGTH);
  return { ok: true, value: trimmed };
}

/**
 * Pure validation entry point. Accepts any environment-like object so it can
 * be exercised in tests without depending on Vite's `import.meta.env`.
 */
export function validatePublicConfig(source: RawPublicEnv): PublicConfigResult {
  const allowed = pickAllowedPublicEnv(source);

  const apiBaseUrl = validateApiBaseUrl(allowed.VITE_API_BASE_URL);
  if (!apiBaseUrl.ok) {
    return apiBaseUrl.result;
  }

  const appEnv = validateAppEnv(allowed.VITE_APP_ENV);
  if (!appEnv.ok) {
    return appEnv.result;
  }

  const buildVersion = validateBuildVersion(allowed.VITE_BUILD_VERSION);
  if (!buildVersion.ok) {
    return buildVersion.result;
  }

  return {
    ok: true,
    config: {
      apiBaseUrl: apiBaseUrl.value,
      appEnv: appEnv.value,
      buildVersion: buildVersion.value,
    },
  };
}

/**
 * Reads and validates the browser-public configuration from Vite's
 * `import.meta.env`. This is the function application code should call.
 */
export function getPublicConfig(): PublicConfigResult {
  return validatePublicConfig(import.meta.env);
}
