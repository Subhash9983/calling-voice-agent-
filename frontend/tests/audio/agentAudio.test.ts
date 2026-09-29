import { describe, expect, it, vi } from "vitest";
import {
  AgentAudioPlayer,
  readInboundAudio,
  type AgentAudioStatus,
  type AttachableAudioTrack,
} from "../../src/audio/agentAudio";

function reportOf(entries: readonly Record<string, unknown>[]): RTCStatsReport {
  return {
    forEach: (callback: (value: unknown) => void) => {
      entries.forEach((entry) => {
        callback(entry);
      });
    },
  } as unknown as RTCStatsReport;
}

function setup() {
  const element = document.createElement("audio");
  const reports: RTCStatsReport[] = [];
  const track: AttachableAudioTrack = {
    attach: vi.fn(() => element),
    detach: vi.fn(() => [element]),
    getRTCStatsReport: vi.fn(() => Promise.resolve(reports.shift())),
  };
  const statuses: AgentAudioStatus[] = [];
  const host = document.createElement("div");
  document.body.appendChild(host);
  const player = new AgentAudioPlayer({
    host,
    onStatus: (status) => statuses.push(status),
    setIntervalFn: () => 1,
    clearIntervalFn: vi.fn(),
  });
  return { element, reports, track, statuses, host, player };
}

describe("readInboundAudio", () => {
  it("reads bytes and audio energy from the inbound audio stat", () => {
    const sample = readInboundAudio(
      reportOf([
        { type: "outbound-rtp", kind: "audio", bytesSent: 9 },
        { type: "inbound-rtp", kind: "audio", bytesReceived: 400, totalAudioEnergy: 0.5 },
      ]),
    );

    expect(sample).toEqual({ bytes: 400, energy: 0.5 });
  });

  it("returns null when no inbound audio stat exists", () => {
    expect(readInboundAudio(reportOf([{ type: "inbound-rtp", kind: "video" }]))).toBeNull();
  });
});

describe("AgentAudioPlayer", () => {
  it("plays only through the LiveKit-attached media element", () => {
    const { player, track, element, host } = setup();

    player.attach(track);

    expect(track.attach).toHaveBeenCalledTimes(1);
    expect(host.contains(element)).toBe(true);
    expect(element.autoplay).toBe(true);
  });

  it("reports subscribed, then playing when the element starts playing", () => {
    const { player, track, element, statuses } = setup();

    player.attach(track);
    element.dispatchEvent(new Event("playing"));

    expect(statuses.map((s) => s.phase)).toEqual(["subscribed", "playing"]);
  });

  it("reports blocked while autoplay is blocked and recovers when unblocked", () => {
    const { player, track, statuses } = setup();
    player.attach(track);

    player.setPlaybackBlocked(true);
    player.setPlaybackBlocked(false);

    expect(statuses.map((s) => s.phase)).toEqual(["subscribed", "blocked", "subscribed"]);
  });

  it("reports failure when the element errors", () => {
    const { player, track, element, statuses } = setup();
    player.attach(track);

    element.dispatchEvent(new Event("error"));

    expect(statuses.at(-1)?.phase).toBe("failed");
  });

  it("detects receiving and audible signal from receiver stat deltas", async () => {
    const { player, track, reports, statuses } = setup();
    player.attach(track);
    reports.push(reportOf([{ type: "inbound-rtp", kind: "audio", bytesReceived: 100, totalAudioEnergy: 0 }]));
    await player.sample();
    reports.push(reportOf([{ type: "inbound-rtp", kind: "audio", bytesReceived: 900, totalAudioEnergy: 0.2 }]));
    await player.sample();

    expect(statuses.at(-1)).toMatchObject({ receiving: true, audible: true });
  });

  it("treats silent packets as receiving but not audible", async () => {
    const { player, track, reports, statuses } = setup();
    player.attach(track);
    reports.push(reportOf([{ type: "inbound-rtp", kind: "audio", bytesReceived: 100, totalAudioEnergy: 0 }]));
    await player.sample();
    reports.push(reportOf([{ type: "inbound-rtp", kind: "audio", bytesReceived: 300, totalAudioEnergy: 0 }]));
    await player.sample();

    expect(statuses.at(-1)).toMatchObject({ receiving: true, audible: false });
  });

  it("detaches, removes the element and resets status", () => {
    const { player, track, element, host, statuses } = setup();
    player.attach(track);

    player.detach();

    expect(track.detach).toHaveBeenCalled();
    expect(host.contains(element)).toBe(false);
    expect(statuses.at(-1)?.phase).toBe("none");
  });

  it("ignores stats errors without throwing", async () => {
    const { player, track } = setup();
    vi.mocked(track.getRTCStatsReport).mockRejectedValue(new Error("gone"));
    player.attach(track);

    await expect(player.sample()).resolves.toBeUndefined();
  });
});
