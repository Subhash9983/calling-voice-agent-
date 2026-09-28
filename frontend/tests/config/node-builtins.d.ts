/**
 * Minimal ambient declarations for the Node.js built-in modules used by the
 * WP3 bundle-inspection test (`bundleSecretExposure.test.ts`).
 *
 * `@types/node` is not an approved dependency in
 * docs/13-dependency-and-version-matrix.md, so this file declares only the
 * exact shapes this test suite needs rather than adding a new package.
 */

declare module "node:child_process" {
  export interface ExecFileSyncOptions {
    cwd?: string;
    env?: Record<string, string | undefined>;
    stdio?: "pipe" | "ignore" | "inherit";
  }

  export function execFileSync(
    file: string,
    args: readonly string[],
    options?: ExecFileSyncOptions,
  ): unknown;
}

declare module "node:fs" {
  export interface Stats {
    isDirectory(): boolean;
  }

  export interface RmOptions {
    recursive?: boolean;
    force?: boolean;
  }

  export function mkdtempSync(prefix: string): string;
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function readdirSync(path: string): string[];
  export function rmSync(path: string, options?: RmOptions): void;
  export function statSync(path: string): Stats;
}

declare module "node:module" {
  export interface NodeRequire {
    resolve(id: string): string;
  }

  export function createRequire(url: string): NodeRequire;
}

declare module "node:os" {
  export function tmpdir(): string;
}

declare module "node:path" {
  export function join(...parts: string[]): string;
  export function dirname(path: string): string;
}

declare module "node:url" {
  export function fileURLToPath(url: string): string;
}

declare const process: {
  execPath: string;
  platform: string;
  env: Record<string, string | undefined>;
};
