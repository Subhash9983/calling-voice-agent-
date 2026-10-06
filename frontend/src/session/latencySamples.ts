/**
 * Browser-side composed latency samples (docs/06 §15, docs/01 §21, §9).
 *
 * On a turn's first `va.playback.v1` "started" message, this tracker takes a
 * baseline WebRTC receiver-stats snapshot for the `agent-audio` track, polls
 * until audio is actually flowing (`inbound-rtp.totalSamplesReceived`
 * increasing), waits ~1s for playout to settle, then takes a final snapshot
 * and sends exactly one `client.latency_sample` for that turn. A turn with no
 * `turn_id` (a worker-opened greeting has one; a turn this worker did not
 * open does not) is never sampled. Raw stats are never stored, only the two
 * bounded derived integers.
 */

export const LATENCY_POLL_INTERVAL_MS = 250;
export const LATENCY_SETTLE_DELAY_MS = 1000;

export interface LatencySample {
  readonly browserPlayoutMs: number;
  readonly networkOneWayMs: number | null;
}

export type StatsProvider = () => Promise<RTCStatsReport | undefined>;
export type LatencySampleSender = (turnId: string, sample: LatencySample) => void;

interface MediaPlayoutPoint {
  readonly totalPlayoutDelay: number;
  readonly totalSamplesCount: number;
}

interface InboundRtpPoint {
  readonly totalSamplesReceived: number | null;
  readonly jitterBufferDelay: number | null;
  readonly jitterBufferEmittedCount: number | null;
}

interface StatsPoint {
  readonly mediaPlayout: MediaPlayoutPoint | null;
  readonly inboundRtp: InboundRtpPoint | null;
  readonly networkOneWayMs: number | null;
}

interface CandidatePair {
  readonly currentRoundTripTime: number | undefined;
  readonly state: string | undefined;
  readonly nominated: boolean | undefined;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function readSelectedCandidatePair(report: RTCStatsReport): CandidatePair | null {
  let selectedId: string | null = null;
  const pairs = new Map<string, CandidatePair>();
  report.forEach((entry: unknown) => {
    const stat = entry as Record<string, unknown>;
    if (stat["type"] === "transport" && typeof stat["selectedCandidatePairId"] === "string") {
      selectedId = stat["selectedCandidatePairId"];
    } else if (stat["type"] === "candidate-pair" && typeof stat["id"] === "string") {
      pairs.set(stat["id"], {
        currentRoundTripTime: typeof stat["currentRoundTripTime"] === "number" ? stat["currentRoundTripTime"] : undefined,
        state: typeof stat["state"] === "string" ? stat["state"] : undefined,
        nominated: typeof stat["nominated"] === "boolean" ? stat["nominated"] : undefined,
      });
    }
  });
  if (selectedId !== null) {
    const pair = pairs.get(selectedId);
    if (pair !== undefined) {
      return pair;
    }
  }
  for (const pair of pairs.values()) {
    if (pair.state === "succeeded" && pair.nominated === true) {
      return pair;
    }
  }
  return null;
}

function computeNetworkOneWayMs(report: RTCStatsReport): number | null {
  const pair = readSelectedCandidatePair(report);
  if (pair === null || pair.currentRoundTripTime === undefined) {
    return null;
  }
  return Math.round((pair.currentRoundTripTime * 1000) / 2);
}

function readStatsPoint(report: RTCStatsReport): StatsPoint {
  let mediaPlayout: MediaPlayoutPoint | null = null;
  let inboundRtp: InboundRtpPoint | null = null;
  report.forEach((entry: unknown) => {
    const stat = entry as Record<string, unknown>;
    if (stat["type"] === "media-playout") {
      const totalPlayoutDelay = stat["totalPlayoutDelay"];
      const totalSamplesCount = stat["totalSamplesCount"];
      if (typeof totalPlayoutDelay === "number" && typeof totalSamplesCount === "number") {
        mediaPlayout = { totalPlayoutDelay, totalSamplesCount };
      }
    } else if (stat["type"] === "inbound-rtp" && stat["kind"] === "audio") {
      inboundRtp = {
        totalSamplesReceived: numberOrNull(stat["totalSamplesReceived"]),
        jitterBufferDelay: numberOrNull(stat["jitterBufferDelay"]),
        jitterBufferEmittedCount: numberOrNull(stat["jitterBufferEmittedCount"]),
      };
    }
  });
  return { mediaPlayout, inboundRtp, networkOneWayMs: computeNetworkOneWayMs(report) };
}

function clampMs(value: number): number {
  return Math.min(60000, Math.max(0, value));
}

function computeFromMediaPlayout(baseline: StatsPoint, final: StatsPoint): number | null {
  const before = baseline.mediaPlayout;
  const after = final.mediaPlayout;
  if (before === null || after === null) {
    return null;
  }
  const deltaCount = after.totalSamplesCount - before.totalSamplesCount;
  if (deltaCount <= 0) {
    return null;
  }
  const deltaDelay = after.totalPlayoutDelay - before.totalPlayoutDelay;
  return clampMs(Math.round((deltaDelay / deltaCount) * 1000));
}

function computeFromInboundRtp(baseline: StatsPoint, final: StatsPoint): number | null {
  const before = baseline.inboundRtp;
  const after = final.inboundRtp;
  if (
    before === null ||
    after === null ||
    before.jitterBufferDelay === null ||
    after.jitterBufferDelay === null ||
    before.jitterBufferEmittedCount === null ||
    after.jitterBufferEmittedCount === null
  ) {
    return null;
  }
  const deltaCount = after.jitterBufferEmittedCount - before.jitterBufferEmittedCount;
  if (deltaCount <= 0) {
    return null;
  }
  const deltaDelay = after.jitterBufferDelay - before.jitterBufferDelay;
  return clampMs(Math.round((deltaDelay / deltaCount) * 1000));
}

/** Media-playout delay-per-sample first; inbound-rtp jitter-buffer delay as fallback. */
function computeBrowserPlayoutMs(baseline: StatsPoint, final: StatsPoint): number | null {
  const fromMediaPlayout = computeFromMediaPlayout(baseline, final);
  return fromMediaPlayout ?? computeFromInboundRtp(baseline, final);
}

export class LatencySampleTracker {
  private readonly sentTurnIds = new Set<string>();
  private activeTurnId: string | null = null;
  private baseline: StatsPoint | null = null;
  private lastPoll: StatsPoint | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private settleTimer: ReturnType<typeof setTimeout> | null = null;

  public constructor(
    private readonly getStats: StatsProvider,
    private readonly send: LatencySampleSender,
  ) {}

  /** Call once for every `va.playback.v1` "started" message, with its envelope turn id. */
  public onPlaybackStarted(turnId: string | null): void {
    if (turnId === null || this.sentTurnIds.has(turnId) || this.activeTurnId === turnId) {
      return;
    }
    this.cancelActive();
    this.activeTurnId = turnId;
    void this.captureBaseline(turnId);
  }

  /** Stops any in-flight capture; already-sent turns are unaffected. */
  public dispose(): void {
    this.cancelActive();
  }

  private async captureBaseline(turnId: string): Promise<void> {
    const report = await this.getStats();
    if (this.activeTurnId !== turnId) {
      return;
    }
    if (report === undefined) {
      this.activeTurnId = null;
      return;
    }
    const point = readStatsPoint(report);
    this.baseline = point;
    this.lastPoll = point;
    this.pollTimer = setInterval(() => {
      void this.poll(turnId);
    }, LATENCY_POLL_INTERVAL_MS);
  }

  private async poll(turnId: string): Promise<void> {
    if (this.activeTurnId !== turnId) {
      return;
    }
    const report = await this.getStats();
    if (this.activeTurnId !== turnId || report === undefined) {
      return;
    }
    const point = readStatsPoint(report);
    const before = this.lastPoll?.inboundRtp?.totalSamplesReceived ?? null;
    const after = point.inboundRtp?.totalSamplesReceived ?? null;
    this.lastPoll = point;
    if (before !== null && after !== null && after > before) {
      this.clearPollTimer();
      this.settleTimer = setTimeout(() => {
        void this.finish(turnId);
      }, LATENCY_SETTLE_DELAY_MS);
    }
  }

  private async finish(turnId: string): Promise<void> {
    this.settleTimer = null;
    if (this.activeTurnId !== turnId) {
      return;
    }
    const report = await this.getStats();
    if (this.activeTurnId !== turnId) {
      return;
    }
    const baseline = this.baseline;
    this.activeTurnId = null;
    this.baseline = null;
    this.lastPoll = null;
    if (baseline === null || report === undefined) {
      return;
    }
    const final = readStatsPoint(report);
    const browserPlayoutMs = computeBrowserPlayoutMs(baseline, final);
    if (browserPlayoutMs === null) {
      return;
    }
    const networkOneWayMs = final.networkOneWayMs ?? baseline.networkOneWayMs;
    this.sentTurnIds.add(turnId);
    this.send(turnId, { browserPlayoutMs, networkOneWayMs });
  }

  private cancelActive(): void {
    this.activeTurnId = null;
    this.baseline = null;
    this.lastPoll = null;
    this.clearPollTimer();
    if (this.settleTimer !== null) {
      clearTimeout(this.settleTimer);
      this.settleTimer = null;
    }
  }

  private clearPollTimer(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }
}
