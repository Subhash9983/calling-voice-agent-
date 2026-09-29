/**
 * Browser microphone capture (docs/06 §9, docs/14 §12).
 *
 * Requests echoCancellation / noiseSuppression / autoGainControl and mono
 * capture, then verifies what the browser actually applied. A constraint
 * the browser does not support, or reports as not applied, is surfaced in
 * the `ConstraintReport`: it is never silently assumed to be active.
 */
export const REQUESTED_MIC_CONSTRAINTS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
} as const;

export type ConstraintName = keyof typeof REQUESTED_MIC_CONSTRAINTS;

const CONSTRAINT_NAMES: readonly ConstraintName[] = [
  "echoCancellation",
  "noiseSuppression",
  "autoGainControl",
  "channelCount",
];

/**
 * - `active`: browser reports the requested value.
 * - `not_applied`: browser reports a different value.
 * - `unreported`: supported, but the track settings do not say (unverified).
 * - `unsupported`: the browser does not implement the constraint.
 */
export type ConstraintStatus = "active" | "not_applied" | "unreported" | "unsupported";

export interface ConstraintReport {
  readonly statuses: Readonly<Record<ConstraintName, ConstraintStatus>>;
  /** Constraint names that are not verifiably active. */
  readonly degraded: readonly ConstraintName[];
}

export type MicrophoneErrorKind =
  | "permission_denied"
  | "no_device"
  | "device_busy"
  | "insecure_context"
  | "unsupported"
  | "unknown";

export interface MicrophoneError {
  readonly kind: MicrophoneErrorKind;
  readonly message: string;
}

export type MicrophoneResult =
  | { readonly ok: true; readonly track: MediaStreamTrack; readonly report: ConstraintReport }
  | { readonly ok: false; readonly error: MicrophoneError };

export interface MediaDevicesLike {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>;
  getSupportedConstraints(): MediaTrackSupportedConstraints;
}

const ERROR_MESSAGES: Readonly<Record<MicrophoneErrorKind, string>> = {
  permission_denied:
    "Microphone access was blocked. Allow the microphone for this site in the browser, then try again.",
  no_device: "No microphone was found. Connect one and try again.",
  device_busy: "The microphone is in use by another application or could not be started.",
  insecure_context: "Microphone capture requires a secure page (https or 127.0.0.1).",
  unsupported: "This browser does not support microphone capture.",
  unknown: "The microphone could not be started.",
};

export function microphoneError(kind: MicrophoneErrorKind): MicrophoneError {
  return { kind, message: ERROR_MESSAGES[kind] };
}

export function classifyMicrophoneError(error: unknown): MicrophoneError {
  const name = error instanceof Error ? error.name : "";
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
    case "PermissionDeniedError":
      return microphoneError("permission_denied");
    case "NotFoundError":
    case "OverconstrainedError":
    case "DevicesNotFoundError":
      return microphoneError("no_device");
    case "NotReadableError":
    case "AbortError":
    case "TrackStartError":
      return microphoneError("device_busy");
    default:
      return microphoneError("unknown");
  }
}

export function buildConstraintReport(
  settings: MediaTrackSettings,
  supported: MediaTrackSupportedConstraints,
): ConstraintReport {
  const statuses = {} as Record<ConstraintName, ConstraintStatus>;
  for (const name of CONSTRAINT_NAMES) {
    statuses[name] = statusFor(name, settings, supported);
  }
  return {
    statuses,
    degraded: CONSTRAINT_NAMES.filter((name) => statuses[name] !== "active"),
  };
}

function statusFor(
  name: ConstraintName,
  settings: MediaTrackSettings,
  supported: MediaTrackSupportedConstraints,
): ConstraintStatus {
  if (supported[name] !== true) {
    return "unsupported";
  }
  const actual = settings[name];
  if (actual === undefined) {
    return "unreported";
  }
  return actual === REQUESTED_MIC_CONSTRAINTS[name] ? "active" : "not_applied";
}

function defaultMediaDevices(): MediaDevicesLike | null {
  if (typeof navigator === "undefined" || navigator.mediaDevices === undefined) {
    return null;
  }
  return navigator.mediaDevices;
}

export async function acquireMicrophone(
  mediaDevices: MediaDevicesLike | null = defaultMediaDevices(),
): Promise<MicrophoneResult> {
  if (mediaDevices === null) {
    const kind = typeof window !== "undefined" && !window.isSecureContext ? "insecure_context" : "unsupported";
    return { ok: false, error: microphoneError(kind) };
  }
  try {
    const stream = await mediaDevices.getUserMedia({
      audio: { ...REQUESTED_MIC_CONSTRAINTS },
      video: false,
    });
    const [track] = stream.getAudioTracks();
    if (track === undefined) {
      return { ok: false, error: microphoneError("no_device") };
    }
    const report = buildConstraintReport(track.getSettings(), mediaDevices.getSupportedConstraints());
    return { ok: true, track, report };
  } catch (error: unknown) {
    return { ok: false, error: classifyMicrophoneError(error) };
  }
}

/** Calls `onLost` when the capture device is unplugged or revoked. Returns an unsubscribe. */
export function watchDeviceLoss(track: MediaStreamTrack, onLost: () => void): () => void {
  const handler = (): void => {
    onLost();
  };
  track.addEventListener("ended", handler);
  return () => {
    track.removeEventListener("ended", handler);
  };
}
