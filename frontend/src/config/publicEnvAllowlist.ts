/**
 * Browser-public configuration allowlist (docs/12-configuration-and-secrets.md §11).
 *
 * Every name in this list is, by Vite convention, embedded verbatim into the
 * built browser bundle. Nothing outside this list may ever be read by
 * application code, and no other name may be added without an approved
 * change to docs/12.
 *
 * Do NOT add provider keys, LiveKit secrets, MongoDB URIs, prompt text, or
 * any other server-side setting to this list.
 */
export const PUBLIC_ENV_ALLOWLIST = [
  "VITE_API_BASE_URL",
  "VITE_APP_ENV",
  "VITE_BUILD_VERSION",
] as const;

export type PublicEnvKey = (typeof PUBLIC_ENV_ALLOWLIST)[number];

export type RawPublicEnv = Record<string, string | boolean | undefined>;

/**
 * Restricts an arbitrary environment-like object down to the approved
 * browser-public allowlist. Any key outside the allowlist (a browser
 * override attempt, an accidental secret-bearing variable, or Vite's own
 * built-in fields such as `MODE`/`DEV`/`PROD`) is dropped rather than read.
 */
export function pickAllowedPublicEnv(
  source: RawPublicEnv,
): Partial<Record<PublicEnvKey, string>> {
  const picked: Partial<Record<PublicEnvKey, string>> = {};

  for (const key of PUBLIC_ENV_ALLOWLIST) {
    const value = source[key];
    if (typeof value === "string") {
      picked[key] = value;
    }
  }

  return picked;
}
