import { useState, type ReactElement } from "react";
import type { PublicConfig } from "../config";
import type { ControllerDeps } from "../session/controller";
import { useVoiceSession } from "../session/useVoiceSession";
import { AudioCheckPanel } from "./AudioCheckPanel";
import { ControlBar } from "./ControlBar";
import { EvidencePanel } from "./EvidencePanel";
import { EventsPanel } from "./EventsPanel";
import { MicrophonePanel } from "./MicrophonePanel";
import { TranscriptPanel } from "./TranscriptPanel";
import { AGENT_STATE_LABELS, QUALITY_LABELS, statusSentence } from "./labels";

export const AUDIO_HOST_ID = "agent-audio-host";

function audioHost(): HTMLElement {
  return document.getElementById(AUDIO_HOST_ID) ?? document.body;
}

export interface SessionScreenProps {
  readonly config: PublicConfig;
  /** Test seam: replaces the real API client, LiveKit transport and microphone. */
  readonly controllerDeps?: ControllerDeps;
}

export function SessionScreen({ config, controllerDeps }: SessionScreenProps): ReactElement {
  const session = useVoiceSession(config.apiBaseUrl, audioHost, controllerDeps);
  const { state } = session;
  const [selectedConfigId, setSelectedConfigId] = useState<string | null>(null);

  const items = session.configs.status === "ready" ? session.configs.items : [];
  const effectiveConfigId = selectedConfigId ?? items[0]?.agentConfigId ?? null;

  return (
    <div className="shell">
      <header className="masthead">
        <p className="eyebrow">Phase 0 R&amp;D / {config.appEnv}</p>
        <h1>Voice Agent Session</h1>
        <p role="status" aria-live="polite" className="status-line" data-phase={state.phase}>
          {statusSentence(state)}
        </p>
      </header>

      {state.error !== null && (
        <div role="alert" className="banner banner-error">
          <strong>Something went wrong.</strong> {state.error.message}
          {state.error.retryable ? " You can start a new session." : ""}
        </div>
      )}
      {state.phase === "reconnecting" && (
        <div role="status" className="banner banner-warn">
          Connection interrupted. Reconnecting within 20 seconds; the agent will not replay old audio.
        </div>
      )}
      {state.mic.status === "lost" && state.mic.message !== null && (
        <div role="alert" className="banner banner-error">
          {state.mic.message}
        </div>
      )}

      <main className="grid">
        <ControlBar
          configs={session.configs}
          selectedConfigId={effectiveConfigId}
          onSelectConfig={setSelectedConfigId}
          onReloadConfigs={session.reloadConfigs}
          state={state}
          onStart={() => {
            if (effectiveConfigId !== null) {
              session.start(effectiveConfigId);
            }
          }}
          onStop={session.stop}
          onMute={session.setMuted}
        />

        <section aria-labelledby="live-heading" className="panel">
          <h2 id="live-heading">Live state</h2>
          <dl className="facts">
            <dt>Agent</dt>
            <dd>{state.agentState === null ? "No activity yet" : AGENT_STATE_LABELS[state.agentState]}</dd>
            <dt>Connection quality</dt>
            <dd>{QUALITY_LABELS[state.quality]}</dd>
            <dt>Transport</dt>
            <dd>{state.transport}</dd>
            {state.configuration !== null && (
              <>
                <dt>Configuration</dt>
                <dd>
                  {state.configuration.name ?? "Unnamed"} v{state.configuration.version}: STT{" "}
                  {state.configuration.stt}, LLM {state.configuration.conversationEngine}, TTS{" "}
                  {state.configuration.tts}
                </dd>
              </>
            )}
          </dl>
          {state.rejectedMessages > 0 && (
            <p className="muted">Ignored {state.rejectedMessages} invalid realtime message(s).</p>
          )}
          {state.lastAgentError !== null && (
            <p role="alert" className="inline-error">
              Agent reported: {state.lastAgentError}
            </p>
          )}
        </section>

        <TranscriptPanel
          user={state.userTranscript}
          agent={state.agentResponse}
          agentState={state.agentState}
          live={state.phase === "live"}
        />
        <AudioCheckPanel state={state} onEnableAudio={session.enableAudio} />
        <MicrophonePanel mic={state.mic} />
        <EvidencePanel evidence={state.evidence} />
        <EventsPanel events={state.events} />
      </main>

      <div id={AUDIO_HOST_ID} hidden aria-hidden="true" />
      <footer className="footer">Build {config.buildVersion}</footer>
    </div>
  );
}
