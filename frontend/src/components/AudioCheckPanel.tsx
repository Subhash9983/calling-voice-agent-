import type { ReactElement } from "react";
import type { SessionViewState } from "../session/sessionState";

export interface AudioCheckPanelProps {
  readonly state: SessionViewState;
  readonly onEnableAudio: () => void;
}

function describeAudio(state: SessionViewState): string {
  const { agentAudio, audioVerified } = state;
  if (agentAudio.phase === "none") {
    return audioVerified
      ? "The agent audio track ended. A signal was heard earlier in this session."
      : "Waiting for the agent audio track.";
  }
  if (agentAudio.phase === "blocked") {
    return "The browser blocked audio playback. Use the button to enable it.";
  }
  if (agentAudio.phase === "failed") {
    return "The audio element reported a playback error.";
  }
  if (audioVerified) {
    return "Audio signal received and played through the WebRTC audio element.";
  }
  return agentAudio.receiving
    ? "Receiving agent audio packets; waiting for a non-silent signal."
    : "Agent audio track subscribed; no packets received yet.";
}

/**
 * Verifies the backend test tone without any custom audio path: the tone
 * plays only through the LiveKit-attached WebRTC element; this panel reads
 * receiver statistics to confirm that audible audio actually arrived.
 */
export function AudioCheckPanel({ state, onEnableAudio }: AudioCheckPanelProps): ReactElement {
  const blocked = state.agentAudio.phase === "blocked";
  return (
    <section aria-labelledby="audio-heading" className="panel">
      <h2 id="audio-heading">Audio check</h2>
      <p>
        <span className={state.audioVerified ? "chip chip-ok" : "chip"}>
          {state.audioVerified ? "Tone verified" : "Not yet verified"}
        </span>
      </p>
      <p role="status" aria-live="polite">
        {describeAudio(state)}
      </p>
      {blocked && (
        <button type="button" className="btn btn-primary" onClick={onEnableAudio}>
          Enable audio playback
        </button>
      )}
      <p className="muted">
        With a session active and the backend test tone enabled, you should hear the tone and this
        panel should report a verified signal.
      </p>
    </section>
  );
}
