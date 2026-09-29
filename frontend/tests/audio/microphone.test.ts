import { describe, expect, it, vi } from "vitest";
import {
  REQUESTED_MIC_CONSTRAINTS,
  acquireMicrophone,
  buildConstraintReport,
  classifyMicrophoneError,
  watchDeviceLoss,
  type MediaDevicesLike,
} from "../../src/audio/microphone";

const ALL_SUPPORTED: MediaTrackSupportedConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: true,
};

function fakeTrack(settings: MediaTrackSettings): MediaStreamTrack {
  const target = new EventTarget();
  return Object.assign(target, {
    getSettings: () => settings,
    stop: vi.fn(),
  }) as unknown as MediaStreamTrack;
}

function devicesReturning(track: MediaStreamTrack, supported = ALL_SUPPORTED): MediaDevicesLike {
  return {
    getUserMedia: vi.fn().mockResolvedValue({ getAudioTracks: () => [track] }),
    getSupportedConstraints: () => supported,
  };
}

const ACTIVE_SETTINGS: MediaTrackSettings = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
};

describe("acquireMicrophone", () => {
  it("requests echo cancellation, noise suppression, AGC and mono capture", async () => {
    const devices = devicesReturning(fakeTrack(ACTIVE_SETTINGS));

    await acquireMicrophone(devices);

    expect(devices.getUserMedia).toHaveBeenCalledWith({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
      video: false,
    });
    expect(REQUESTED_MIC_CONSTRAINTS.channelCount).toBe(1);
  });

  it("reports every constraint active when the browser applied them", async () => {
    const result = await acquireMicrophone(devicesReturning(fakeTrack(ACTIVE_SETTINGS)));

    expect(result.ok && result.report.degraded).toEqual([]);
  });

  it("surfaces constraints the browser does not support instead of assuming them", async () => {
    const supported = { ...ALL_SUPPORTED, noiseSuppression: false };
    const settings: MediaTrackSettings = { ...ACTIVE_SETTINGS };
    delete settings.noiseSuppression;

    const result = await acquireMicrophone(devicesReturning(fakeTrack(settings), supported));

    expect(result.ok && result.report.statuses.noiseSuppression).toBe("unsupported");
    expect(result.ok && result.report.degraded).toEqual(["noiseSuppression"]);
  });

  it("flags constraints applied with a different value or not reported", () => {
    const report = buildConstraintReport(
      { echoCancellation: false, channelCount: 2 },
      ALL_SUPPORTED,
    );

    expect(report.statuses).toEqual({
      echoCancellation: "not_applied",
      noiseSuppression: "unreported",
      autoGainControl: "unreported",
      channelCount: "not_applied",
    });
    expect(report.degraded).toHaveLength(4);
  });

  it.each([
    ["NotAllowedError", "permission_denied"],
    ["SecurityError", "permission_denied"],
    ["NotFoundError", "no_device"],
    ["NotReadableError", "device_busy"],
    ["WeirdError", "unknown"],
  ])("classifies %s as %s", (name, kind) => {
    const error = Object.assign(new Error("x"), { name });

    expect(classifyMicrophoneError(error).kind).toBe(kind);
  });

  it("returns a user-readable permission-denied result", async () => {
    const devices: MediaDevicesLike = {
      getUserMedia: vi.fn().mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" })),
      getSupportedConstraints: () => ALL_SUPPORTED,
    };

    const result = await acquireMicrophone(devices);

    expect(result.ok).toBe(false);
    expect(!result.ok && result.error.kind).toBe("permission_denied");
    expect(!result.ok && result.error.message).toMatch(/blocked/i);
  });

  it("reports no device when the stream has no audio track", async () => {
    const devices: MediaDevicesLike = {
      getUserMedia: vi.fn().mockResolvedValue({ getAudioTracks: () => [] }),
      getSupportedConstraints: () => ALL_SUPPORTED,
    };

    const result = await acquireMicrophone(devices);

    expect(!result.ok && result.error.kind).toBe("no_device");
  });

  it("reports unsupported when mediaDevices is unavailable", async () => {
    const result = await acquireMicrophone(null);

    expect(result.ok).toBe(false);
  });
});

describe("watchDeviceLoss", () => {
  it("notifies once the capture track ends and can be unsubscribed", () => {
    const track = fakeTrack(ACTIVE_SETTINGS);
    const onLost = vi.fn();
    const stop = watchDeviceLoss(track, onLost);

    track.dispatchEvent(new Event("ended"));
    stop();
    track.dispatchEvent(new Event("ended"));

    expect(onLost).toHaveBeenCalledTimes(1);
  });
});
