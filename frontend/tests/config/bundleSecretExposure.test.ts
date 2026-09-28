import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const VITE_BIN = join(dirname(require.resolve("vite/package.json")), "bin", "vite.js");
const TEST_FILE_DIR = dirname(fileURLToPath(import.meta.url));

/**
 * Proves docs/12-configuration-and-secrets.md §11: the browser bundle must
 * never contain a server secret value or a provider/bootstrap secret
 * setting name, even when a synthetic secret-like value is present in the
 * build environment under its real (non-`VITE_`) name.
 *
 * Vite only ever embeds environment variables prefixed with `VITE_`
 * (`envPrefix`, defaulting to `VITE_`) into `import.meta.env`. The baseline
 * secret names in docs/12 §5 (`MONGODB_URI`, `LIVEKIT_API_KEY`,
 * `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`,
 * `SARVAM_API_KEY`) are never `VITE_`-prefixed, so this test exercises that
 * real build boundary end-to-end instead of only asserting against our own
 * source code.
 */

const REPO_ROOT = join(TEST_FILE_DIR, "..", "..");

const SYNTHETIC_SECRET_VALUE = "canary-secret-value-should-never-appear-in-dist";

const FORBIDDEN_SECRET_ENV: Readonly<Record<string, string>> = {
  MONGODB_URI: `mongodb+srv://canary:${SYNTHETIC_SECRET_VALUE}@example.invalid/voice_agent_rnd`,
  LIVEKIT_API_KEY: SYNTHETIC_SECRET_VALUE,
  LIVEKIT_API_SECRET: SYNTHETIC_SECRET_VALUE,
  DEEPGRAM_API_KEY: SYNTHETIC_SECRET_VALUE,
  OPENAI_API_KEY: SYNTHETIC_SECRET_VALUE,
  SARVAM_API_KEY: SYNTHETIC_SECRET_VALUE,
};

const FORBIDDEN_NAME_NEEDLES = [
  "MONGODB_URI",
  "LIVEKIT_API_KEY",
  "LIVEKIT_API_SECRET",
  "DEEPGRAM_API_KEY",
  "OPENAI_API_KEY",
  "SARVAM_API_KEY",
];

function collectBuiltFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];

  for (const entry of entries) {
    const fullPath = join(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...collectBuiltFiles(fullPath));
    } else {
      files.push(fullPath);
    }
  }

  return files;
}

describe("browser bundle secret exposure", () => {
  it(
    "excludes synthetic secret values and baseline secret names from the built output",
    () => {
      const outDir = mkdtempSync(join(tmpdir(), "va-frontend-dist-"));

      try {
        execFileSync(
          process.execPath,
          [VITE_BIN, "build", "--outDir", outDir, "--emptyOutDir"],
          {
            cwd: REPO_ROOT,
            env: {
              ...process.env,
              ...FORBIDDEN_SECRET_ENV,
              VITE_API_BASE_URL: "http://127.0.0.1:8000/api/v1",
              VITE_APP_ENV: "development",
              VITE_BUILD_VERSION: "0.0.0-bundle-test",
            },
            stdio: "pipe",
          },
        );

        const builtFiles = collectBuiltFiles(outDir).filter(
          (file) => !file.endsWith(".map"),
        );
        expect(builtFiles.length).toBeGreaterThan(0);

        for (const file of builtFiles) {
          const contents = readFileSync(file, "utf8");

          expect(contents).not.toContain(SYNTHETIC_SECRET_VALUE);

          for (const needle of FORBIDDEN_NAME_NEEDLES) {
            expect(contents).not.toContain(needle);
          }
        }
      } finally {
        rmSync(outDir, { recursive: true, force: true });
      }
    },
    120_000,
  );
});
