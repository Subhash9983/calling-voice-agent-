import type { ConstraintName, ConstraintStatus } from "../audio/microphone";
import type { AgentActivityState } from "../contracts/sessionApi";
import type { LinkQuality } from "../livekit/transport";
import type { SessionPhase, SessionViewState } from "../session/sessionState";

export const PHASE_LABELS: Readonly<Record<SessionPhase, string>> = {
  idle: "Ready to start",
  starting: "Starting session",
  connecting: "Connecting to the room",
  live: "Connected",
  reconnecting: "Reconnecting",
  ending: "Ending session",
  ended: "Session ended",
  failed: "Session failed",
};

export const AGENT_STATE_LABELS: Readonly<Record<AgentActivityState, string>> = {
  idle: "Agent idle",
  listening: "Agent listening",
  transcribing: "Agent transcribing",
  thinking: "Agent thinking",
  speaking: "Agent speaking",
  interrupted: "Agent interrupted",
  recovering: "Reconnecting agent",
  error: "Agent error",
};

export const QUALITY_LABELS: Readonly<Record<LinkQuality, string>> = {
  excellent: "Excellent",
  good: "Good",
  poor: "Poor",
  lost: "Lost",
  unknown: "Not measured",
};

export const CONSTRAINT_LABELS: Readonly<Record<ConstraintName, string>> = {
  echoCancellation: "Echo cancellation",
  noiseSuppression: "Noise suppression",
  autoGainControl: "Automatic gain control",
  channelCount: "Mono capture",
};

export const CONSTRAINT_STATUS_LABELS: Readonly<Record<ConstraintStatus, string>> = {
  active: "Active",
  not_applied: "Not applied by the browser",
  unreported: "Unverified (browser did not report)",
  unsupported: "Unsupported by this browser",
};

/** Single polite live-region sentence summarising the session. */
export function statusSentence(state: SessionViewState): string {
  const base = PHASE_LABELS[state.phase];
  if (state.phase === "live" && state.agentState !== null) {
    return `${base}. ${AGENT_STATE_LABELS[state.agentState]}.`;
  }
  if (state.phase === "reconnecting") {
    return `${base}. Reconnecting agent.`;
  }
  return `${base}.`;
}

export function isSessionActive(phase: SessionPhase): boolean {
  return phase === "starting" || phase === "connecting" || phase === "live" || phase === "reconnecting";
}
