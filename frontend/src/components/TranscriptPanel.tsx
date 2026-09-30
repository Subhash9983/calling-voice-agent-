import type { ReactElement } from "react";
import type { AgentActivityState } from "../contracts/sessionApi";
import type { TranscriptLine } from "../session/sessionState";

interface LinesProps {
  readonly lines: readonly TranscriptLine[];
  readonly label: string;
  readonly empty: string;
  readonly provisionalLabel: string;
}

/**
 * Finals live in a polite log (announced once, on addition). The single
 * provisional line sits outside the live region so screen readers are not
 * flooded by every partial.
 */
function Lines({ lines, label, empty, provisionalLabel }: LinesProps): ReactElement {
  const finals = lines.filter((line) => line.isFinal);
  const provisional = lines.find((line) => !line.isFinal);
  return (
    <div className="transcript-column">
      <div role="log" aria-label={label} aria-live="polite" aria-relevant="additions" tabIndex={0}>
        {finals.length === 0 && provisional === undefined ? (
          <p className="muted">{empty}</p>
        ) : (
          <ol className="lines">
            {finals.map((line) => (
              <li key={line.id} className="line transcript-text" dir="auto">
                {line.text}
              </li>
            ))}
          </ol>
        )}
      </div>
      {provisional !== undefined && (
        <p className="line line-partial transcript-text" dir="auto" data-testid="provisional-line">
          <span className="visually-hidden">{provisionalLabel}: </span>
          {provisional.text}
          <span className="muted" aria-hidden="true">
            {" "}
            …
          </span>
        </p>
      )}
    </div>
  );
}

const LISTENING_TEXT: Readonly<Partial<Record<AgentActivityState, string>>> = {
  listening: "Listening",
  transcribing: "Hearing you",
};

export interface TranscriptPanelProps {
  readonly user: readonly TranscriptLine[];
  readonly agent: readonly TranscriptLine[];
  readonly agentState: AgentActivityState | null;
  readonly live: boolean;
}

/** Text is rendered as plain React text: never as HTML. */
export function TranscriptPanel({ user, agent, agentState, live }: TranscriptPanelProps): ReactElement {
  const indicator = live && agentState !== null ? (LISTENING_TEXT[agentState] ?? null) : null;
  return (
    <section aria-labelledby="transcript-heading" className="panel panel-wide">
      <h2 id="transcript-heading">Conversation</h2>
      {/* The masthead already announces state changes politely; keep this one silent. */}
      <p
        role="status"
        aria-live="off"
        className="listening"
        data-state={indicator === null ? "idle" : (agentState ?? "idle")}
      >
        <span className="listening-dot" aria-hidden="true" />
        {indicator ?? "Not listening"}
      </p>
      <div className="columns">
        <div>
          <h3>You</h3>
          <Lines
            lines={user}
            label="Your transcript"
            empty="Transcript appears here once speech recognition is enabled."
            provisionalLabel="Hearing, not final"
          />
        </div>
        <div>
          <h3>Agent</h3>
          <Lines
            lines={agent}
            label="Agent response"
            empty="Agent replies appear here once the conversation engine is enabled."
            provisionalLabel="Reply in progress"
          />
        </div>
      </div>
    </section>
  );
}
