import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      return sourceFiles(path);
    }
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

function offenders(pattern: RegExp): string[] {
  return sourceFiles(SRC_DIR).filter((file) => pattern.test(readFileSync(file, "utf8")));
}

describe("browser source policy (docs/06 §5, §10; docs/12 §11)", () => {
  it("has no custom Web Audio playback path", () => {
    expect(offenders(/\b(AudioContext|webkitAudioContext|createBufferSource|AudioWorklet)\b/)).toEqual([]);
  });

  it("never touches web storage or cookies", () => {
    expect(offenders(/\b(localStorage|sessionStorage|indexedDB|document\.cookie)\b/)).toEqual([]);
  });

  it("does not log through the console", () => {
    expect(offenders(/\bconsole\.(log|info|warn|error|debug)\b/)).toEqual([]);
  });

  it("never reads provider or server secret names", () => {
    expect(
      offenders(/(LIVEKIT_API_(KEY|SECRET)|MONGODB_URI|DEEPGRAM_API_KEY|OPENAI_API_KEY|SARVAM_API_KEY)/),
    ).toEqual([]);
  });

  it("never renders HTML from message content", () => {
    expect(offenders(/dangerouslySetInnerHTML|\.innerHTML\s*=/)).toEqual([]);
  });
});
