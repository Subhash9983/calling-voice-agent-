/**
 * Normalized control-API error (docs/04 §19). Messages come from the
 * approved envelope or from fixed client strings; they never contain
 * request bodies, tokens, or raw network exception text.
 */
import { isRecord } from "../contracts/validate";

export const CLIENT_ERROR_CODES = ["NETWORK_ERROR", "TIMEOUT", "INVALID_RESPONSE"] as const;

export interface FieldError {
  readonly field: string;
  readonly code: string;
}

export class ApiError extends Error {
  public readonly code: string;
  public readonly status: number | null;
  public readonly retryable: boolean;
  public readonly suggestedAction: string | null;
  public readonly requestId: string | null;
  public readonly fieldErrors: readonly FieldError[];

  public constructor(init: {
    readonly code: string;
    readonly message: string;
    readonly status: number | null;
    readonly retryable: boolean;
    readonly suggestedAction?: string | null;
    readonly requestId?: string | null;
    readonly fieldErrors?: readonly FieldError[];
  }) {
    super(init.message);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.retryable = init.retryable;
    this.suggestedAction = init.suggestedAction ?? null;
    this.requestId = init.requestId ?? null;
    this.fieldErrors = init.fieldErrors ?? [];
  }
}

export function clientError(
  code: (typeof CLIENT_ERROR_CODES)[number],
  message: string,
  retryable: boolean,
): ApiError {
  return new ApiError({ code, message, status: null, retryable });
}

function optionalText(value: unknown, max: number): string | null {
  return typeof value === "string" ? value.slice(0, max) : null;
}

function parseFieldErrors(value: unknown): readonly FieldError[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return (value as readonly unknown[]).slice(0, 20).flatMap((entry) => {
    if (!isRecord(entry)) {
      return [];
    }
    const field = optionalText(entry["field"], 200);
    const code = optionalText(entry["code"], 64);
    return field !== null && code !== null ? [{ field, code }] : [];
  });
}

/** Builds an ApiError from a non-2xx response body, tolerating a malformed one. */
export function apiErrorFromBody(status: number, body: unknown): ApiError {
  const envelope = isRecord(body) ? body : {};
  const error = isRecord(envelope["error"]) ? envelope["error"] : null;
  if (error === null) {
    return new ApiError({
      code: "INVALID_RESPONSE",
      message: "The server returned an unexpected error response.",
      status,
      retryable: status >= 500,
    });
  }
  return new ApiError({
    code: optionalText(error["code"], 64) ?? "INTERNAL_ERROR",
    message: optionalText(error["message"], 300) ?? "The request failed.",
    status,
    retryable: error["retryable"] === true,
    suggestedAction: optionalText(error["suggested_action"], 200),
    requestId: optionalText(envelope["request_id"], 64),
    fieldErrors: parseFieldErrors(error["field_errors"]),
  });
}
