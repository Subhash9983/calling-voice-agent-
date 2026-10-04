import type { ReactElement } from "react";
import type { EvidenceState } from "../session/evidence";

const NOT_AVAILABLE = "Not available yet";

function formatSeconds(seconds: number | null): string {
  return seconds === null ? NOT_AVAILABLE : `${seconds.toFixed(1)} s`;
}

function formatTokens(tokens: number | null): string {
  return tokens === null ? NOT_AVAILABLE : tokens.toLocaleString();
}

/** Per-session STT summary (docs/04 §12, §14). Missing data is neutral, never an error. */
export function EvidencePanel({ evidence }: { readonly evidence: EvidenceState }): ReactElement {
  const cost = evidence.status === "ready" ? evidence.cost : null;
  const sttUsd = cost?.sttUsd ?? null;
  const conversation = evidence.status === "ready" ? (evidence.conversation ?? null) : null;
  const conversationCost = evidence.status === "ready" ? (evidence.conversationCost ?? null) : null;
  const conversationUsd = conversationCost?.conversationUsd ?? null;
  return (
    <>
      <section aria-labelledby="evidence-heading" className="panel">
        <h2 id="evidence-heading">Speech recognition summary</h2>
        {evidence.status === "idle" ? (
          <p className="muted">Shown after the session ends.</p>
        ) : (
          <dl className="facts">
            <dt>STT operations</dt>
            <dd>{evidence.operations === null ? NOT_AVAILABLE : evidence.operations.count}</dd>
            <dt>Audio transcribed</dt>
            <dd>{formatSeconds(evidence.operations?.audioSeconds ?? null)}</dd>
            <dt>Estimated cost</dt>
            <dd>
              {sttUsd === null || cost === null ? NOT_AVAILABLE : `$${sttUsd} (${cost.calculationStatus})`}
            </dd>
          </dl>
        )}
      </section>
      <section aria-labelledby="conversation-evidence-heading" className="panel">
        <h2 id="conversation-evidence-heading">Conversation engine summary</h2>
        {evidence.status === "idle" ? (
          <p className="muted">Shown after the session ends.</p>
        ) : (
          <dl className="facts">
            <dt>Conversation operations</dt>
            <dd>{conversation === null ? NOT_AVAILABLE : conversation.count}</dd>
            <dt>Input tokens</dt>
            <dd>{formatTokens(conversation?.inputTokens ?? null)}</dd>
            <dt>Output tokens</dt>
            <dd>{formatTokens(conversation?.outputTokens ?? null)}</dd>
            <dt>Estimated cost</dt>
            <dd>
              {conversationUsd === null || conversationCost === null
                ? NOT_AVAILABLE
                : `$${conversationUsd} (${conversationCost.calculationStatus})`}
            </dd>
          </dl>
        )}
      </section>
    </>
  );
}
