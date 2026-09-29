import type { ReactElement } from "react";
import type { TranscriptLine } from "../session/sessionState";

function Lines({ lines, empty }: { readonly lines: readonly TranscriptLine[]; readonly empty: string }): ReactElement {
  if (lines.length === 0) {
    return <p className="muted">{empty}</p>;
  }
  return (
    <ol className="lines">
      {lines.map((line) => (
        <li key={line.id} className={line.isFinal ? "line" : "line line-partial"}>
          {line.text}
          {!line.isFinal && <span className="muted"> (partial)</span>}
        </li>
      ))}
    </ol>
  );
}

export interface TranscriptPanelProps {
  readonly user: readonly TranscriptLine[];
  readonly agent: readonly TranscriptLine[];
}

/** Text is rendered as plain React text: never as HTML. */
export function TranscriptPanel({ user, agent }: TranscriptPanelProps): ReactElement {
  return (
    <section aria-labelledby="transcript-heading" className="panel panel-wide">
      <h2 id="transcript-heading">Conversation</h2>
      <div className="columns">
        <div role="log" aria-label="Your transcript" aria-live="off" tabIndex={0}>
          <h3>You</h3>
          <Lines lines={user} empty="Transcript appears here once speech recognition is enabled." />
        </div>
        <div role="log" aria-label="Agent response" aria-live="off" tabIndex={0}>
          <h3>Agent</h3>
          <Lines lines={agent} empty="Agent replies appear here once the conversation engine is enabled." />
        </div>
      </div>
    </section>
  );
}
