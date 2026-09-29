import type { ReactElement } from "react";
import type { ConfigsState } from "../session/useVoiceSession";
import type { SessionViewState } from "../session/sessionState";
import { isSessionActive } from "./labels";

export interface ControlBarProps {
  readonly configs: ConfigsState;
  readonly selectedConfigId: string | null;
  readonly onSelectConfig: (id: string) => void;
  readonly onReloadConfigs: () => void;
  readonly state: SessionViewState;
  readonly onStart: () => void;
  readonly onStop: () => void;
  readonly onMute: (muted: boolean) => void;
}

export function ControlBar(props: ControlBarProps): ReactElement {
  const { configs, state } = props;
  const active = isSessionActive(state.phase);
  const busy = state.phase === "starting" || state.phase === "connecting" || state.phase === "ending";
  const canStart = configs.status === "ready" && props.selectedConfigId !== null && !active && state.phase !== "ending";
  const canMute = state.phase === "live" && state.mic.status === "active";

  return (
    <section aria-labelledby="controls-heading" className="panel panel-controls">
      <h2 id="controls-heading">Session</h2>

      {configs.status === "loading" && <p className="muted">Loading agent configurations…</p>}
      {configs.status === "error" && (
        <div role="alert" className="inline-error">
          <p>Could not load agent configurations: {configs.message}</p>
          <button type="button" className="btn btn-quiet" onClick={props.onReloadConfigs}>
            Retry
          </button>
        </div>
      )}
      {configs.status === "ready" && configs.items.length === 0 && (
        <p role="alert" className="inline-error">
          No active agent configuration is available.
        </p>
      )}
      {configs.status === "ready" && configs.items.length > 0 && (
        <div className="field">
          <label htmlFor="agent-config">Agent configuration</label>
          <select
            id="agent-config"
            value={props.selectedConfigId ?? ""}
            disabled={active}
            onChange={(event) => {
              props.onSelectConfig(event.target.value);
            }}
          >
            {configs.items.map((item) => (
              <option key={item.agentConfigId} value={item.agentConfigId}>
                {item.name} (v{item.version})
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="actions">
        <button type="button" className="btn btn-primary" disabled={!canStart} onClick={props.onStart}>
          {busy && !active ? "Working…" : "Start session"}
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={!active || state.phase === "starting"}
          onClick={props.onStop}
        >
          End session
        </button>
        <button
          type="button"
          className="btn"
          aria-pressed={state.mic.muted}
          disabled={!canMute}
          onClick={() => {
            props.onMute(!state.mic.muted);
          }}
        >
          {state.mic.muted ? "Unmute microphone" : "Mute microphone"}
        </button>
      </div>
    </section>
  );
}
