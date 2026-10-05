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
      ? "The agent audio track ended. Audio was heard earlier in this session."
      : "Waiting for the agent audio track.";
  }
  if (agentAudio.phase === "blocked") {
    return "The browser blocked audio playback. Use the button to enable it.";
  }
  if (agentAudio.phase === "failed") {
    return "The audio element reported a playback error.";
  }
  if (audioVerified) {
    return "Audio received and played through the WebRTC audio element.";
  }
  return agentAudio.receiving
    ? "Receiving agent audio packets; waiting for a non-silent signal."
    : "Agent audio track subscribed; no packets received yet.";
}

/**
 * Verifies agent audio without any custom audio path: whether it is a test
 * tone or real synthesized speech, it plays only through the
 * LiveKit-attached WebRTC element, and this panel reads receiver statistics
 * to confirm that audible audio actually arrived for every segment.
 */
export function AudioCheckPanel({ state, onEnableAudio }: AudioCheckPanelProps): ReactElement {
  const blocked = state.agentAudio.phase === "blocked";
  return (
    <section aria-labelledby="audio-heading" className="panel">
      <h2 id="audio-heading">Audio check</h2>
      <p>
        <span className={state.audioVerified ? "chip chip-ok" : "chip"}>
          {state.audioVerified ? "Audio verified" : "Not yet verified"}
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
        With a session active, you should hear the agent&apos;s audio (a test tone or a spoken
        reply, depending on the configuration) and this panel should report a verified signal.
      </p>
    </section>
  );
}
