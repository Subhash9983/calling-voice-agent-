/**
 * Tiny strict-validation helpers for untrusted JSON (API responses and
 * LiveKit data packets). Every reader either returns a typed value or
 * throws `ContractError` with a path-only message: the offending value is
 * never echoed, because it may be secret material (docs/04 §19).
 */
export class ContractError extends Error {
  public readonly path: string;

  public constructor(path: string, problem: string) {
    super(`${path}: ${problem}`);
    this.name = "ContractError";
    this.path = path;
  }
}

export type JsonRecord = Readonly<Record<string, unknown>>;

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function readRecord(value: unknown, path: string): JsonRecord {
  if (!isRecord(value)) {
    throw new ContractError(path, "expected an object");
  }
  return value;
}

export function readArray(value: unknown, path: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new ContractError(path, "expected an array");
  }
  return value as readonly unknown[];
}

export function readString(
  source: JsonRecord,
  key: string,
  path: string,
  maxLength = 2048,
): string {
  const value = source[key];
  if (typeof value !== "string" || value.length > maxLength) {
    throw new ContractError(`${path}.${key}`, "expected a bounded string");
  }
  return value;
}

export function readOptionalString(
  source: JsonRecord,
  key: string,
  path: string,
  maxLength = 2048,
): string | null {
  const value = source[key];
  if (value === undefined || value === null) {
    return null;
  }
  return readString(source, key, path, maxLength);
}

export function readNumber(source: JsonRecord, key: string, path: string): number {
  const value = source[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ContractError(`${path}.${key}`, "expected a finite number");
  }
  return value;
}

export function readBoolean(source: JsonRecord, key: string, path: string): boolean {
  const value = source[key];
  if (typeof value !== "boolean") {
    throw new ContractError(`${path}.${key}`, "expected a boolean");
  }
  return value;
}

export function readOptionalBoolean(
  source: JsonRecord,
  key: string,
  path: string,
): boolean | null {
  const value = source[key];
  if (value === undefined || value === null) {
    return null;
  }
  return readBoolean(source, key, path);
}

export function readOptionalNumber(source: JsonRecord, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function readOneOf<T extends string>(
  source: JsonRecord,
  key: string,
  path: string,
  allowed: readonly T[],
): T {
  const value = source[key];
  const match = allowed.find((candidate) => candidate === value);
  if (match === undefined) {
    throw new ContractError(`${path}.${key}`, "value is not in the approved set");
  }
  return match;
}

export function readOptionalOneOf<T extends string>(
  source: JsonRecord,
  key: string,
  path: string,
  allowed: readonly T[],
): T | null {
  const value = source[key];
  if (value === undefined || value === null) {
    return null;
  }
  return readOneOf(source, key, path, allowed);
}
