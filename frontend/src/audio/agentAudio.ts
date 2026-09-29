/**
 * Agent audio playback (docs/06 §10).
 *
 * Playback happens ONLY through the media element that LiveKit attaches to
 * the subscribed `agent-audio` WebRTC track, so browser echo cancellation
 * has the WebRTC playout as its reference signal. No custom Web
 * Audio decoding or scheduling exists anywhere in this code base.
 *
 * Receiver statistics (bytes / audio energy) are polled read-only to
 * confirm that real audio is arriving, which powers the backend test-tone
 * check without any custom audio path.
 */
export const AGENT_AUDIO_TRACK_NAME = "agent-audio";
export const STATS_POLL_INTERVAL_MS = 1000;

export type AgentAudioPhase = "none" | "subscribed" | "playing" | "blocked" | "failed";

export interface AgentAudioStatus {
  readonly phase: AgentAudioPhase;
  /** Inbound RTP bytes increased since the last sample. */
  readonly receiving: boolean;
  /** Received audio energy increased, i.e. non-silent audio arrived. */
  readonly audible: boolean;
}

export const NO_AGENT_AUDIO: AgentAudioStatus = { phase: "none", receiving: false, audible: false };

/** The slice of a LiveKit remote audio track this module needs. */
export interface AttachableAudioTrack {
  attach(): HTMLMediaElement;
  detach(): HTMLMediaElement[];
  getRTCStatsReport(): Promise<RTCStatsReport | undefined>;
}

export interface InboundAudioSample {
  readonly bytes: number;
  readonly energy: number;
}

export function readInboundAudio(report: RTCStatsReport): InboundAudioSample | null {
  let sample: InboundAudioSample | null = null;
  report.forEach((entry: unknown) => {
    const stat = entry as Record<string, unknown>;
    if (stat["type"] === "inbound-rtp" && stat["kind"] === "audio") {
      const bytes = typeof stat["bytesReceived"] === "number" ? stat["bytesReceived"] : 0;
      const energy = typeof stat["totalAudioEnergy"] === "number" ? stat["totalAudioEnergy"] : 0;
      sample = { bytes, energy };
    }
  });
  return sample;
}

export interface AgentAudioPlayerOptions {
  readonly host: HTMLElement;
  readonly onStatus: (status: AgentAudioStatus) => void;
  readonly setIntervalFn?: (handler: () => void, ms: number) => number;
  readonly clearIntervalFn?: (id: number) => void;
}

export class AgentAudioPlayer {
  private track: AttachableAudioTrack | null = null;
  private element: HTMLMediaElement | null = null;
  private timer: number | null = null;
  private previous: InboundAudioSample | null = null;
  private status: AgentAudioStatus = NO_AGENT_AUDIO;
  private blocked = false;

  public constructor(private readonly options: AgentAudioPlayerOptions) {}

  public attach(track: AttachableAudioTrack): void {
    this.detach();
    this.track = track;
    const element = track.attach();
    element.autoplay = true;
    element.dataset["agentAudio"] = "true";
    element.addEventListener("playing", this.onPlaying);
    element.addEventListener("error", this.onError);
    if (element.parentElement !== this.options.host) {
      this.options.host.appendChild(element);
    }
    this.element = element;
    this.previous = null;
    this.publish({ ...NO_AGENT_AUDIO, phase: this.blocked ? "blocked" : "subscribed" });
    const setTimer = this.options.setIntervalFn ?? ((h, ms) => window.setInterval(h, ms));
    this.timer = setTimer(() => {
      void this.sample();
    }, STATS_POLL_INTERVAL_MS);
  }

  /** Reflects the room's autoplay-policy state (LiveKit `canPlaybackAudio`). */
  public setPlaybackBlocked(blocked: boolean): void {
    this.blocked = blocked;
    if (this.element === null) {
      return;
    }
    if (blocked) {
      this.publish({ ...this.status, phase: "blocked" });
    } else if (this.status.phase === "blocked") {
      this.publish({ ...this.status, phase: "subscribed" });
    }
  }

  public detach(): void {
    if (this.timer !== null) {
      const clear = this.options.clearIntervalFn ?? ((id: number) => {
        window.clearInterval(id);
      });
      clear(this.timer);
      this.timer = null;
    }
    if (this.element !== null) {
      this.element.removeEventListener("playing", this.onPlaying);
      this.element.removeEventListener("error", this.onError);
    }
    if (this.track !== null) {
      for (const element of this.track.detach()) {
        element.remove();
      }
    }
    this.element?.remove();
    this.element = null;
    this.track = null;
    this.previous = null;
    if (this.status.phase !== "none") {
      this.publish(NO_AGENT_AUDIO);
    }
  }

  private readonly onPlaying = (): void => {
    this.publish({ ...this.status, phase: "playing" });
  };

  private readonly onError = (): void => {
    this.publish({ ...this.status, phase: "failed" });
  };

  /** Exposed for tests and the interval; safe to call at any time. */
  public async sample(): Promise<void> {
    const track = this.track;
    if (track === null) {
      return;
    }
    let report: RTCStatsReport | undefined;
    try {
      report = await track.getRTCStatsReport();
    } catch {
      return;
    }
    if (report === undefined || this.track !== track) {
      return;
    }
    const current = readInboundAudio(report);
    if (current === null) {
      return;
    }
    const before = this.previous;
    this.previous = current;
    const receiving = before !== null && current.bytes > before.bytes;
    const audible = before !== null && current.energy > before.energy;
    if (receiving !== this.status.receiving || audible !== this.status.audible) {
      this.publish({ ...this.status, receiving, audible });
    }
  }

  private publish(next: AgentAudioStatus): void {
    this.status = next;
    this.options.onStatus(next);
  }
}
