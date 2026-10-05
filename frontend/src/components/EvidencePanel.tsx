import type { ReactElement } from "react";
import type { EvidenceState } from "../session/evidence";

const NOT_AVAILABLE = "Not available yet";

function formatSeconds(seconds: number | null): string {
  return seconds === null ? NOT_AVAILABLE : `${seconds.toFixed(1)} s`;
}

function formatTokens(tokens: number | null): string {
  return tokens === null ? NOT_AVAILABLE : tokens.toLocaleString();
}

function formatCharacters(characters: number | null): string {
  return characters === null ? NOT_AVAILABLE : characters.toLocaleString();
}

function formatMs(ms: number | null): string {
  return ms === null ? NOT_AVAILABLE : `${ms.toFixed(0)} ms`;
}

/** Per-session STT summary (docs/04 §12, §14). Missing data is neutral, never an error. */
export function EvidencePanel({ evidence }: { readonly evidence: EvidenceState }): ReactElement {
  const cost = evidence.status === "ready" ? evidence.cost : null;
  const sttUsd = cost?.sttUsd ?? null;
  const conversation = evidence.status === "ready" ? (evidence.conversation ?? null) : null;
  const conversationCost = evidence.status === "ready" ? (evidence.conversationCost ?? null) : null;
  const conversationUsd = conversationCost?.conversationUsd ?? null;
  const tts = evidence.status === "ready" ? (evidence.tts ?? null) : null;
  const ttsCost = evidence.status === "ready" ? (evidence.ttsCost ?? null) : null;
  const ttsUsd = ttsCost?.ttsUsd ?? null;
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
      <section aria-labelledby="tts-evidence-heading" className="panel">
        <h2 id="tts-evidence-heading">Speech synthesis summary</h2>
        {evidence.status === "idle" ? (
          <p className="muted">Shown after the session ends.</p>
        ) : (
          <dl className="facts">
            <dt>Synthesis operations</dt>
            <dd>{tts === null ? NOT_AVAILABLE : tts.count}</dd>
            <dt>Characters synthesized</dt>
            <dd>{formatCharacters(tts?.charactersSynthesized ?? null)}</dd>
            <dt>First-audio timing</dt>
            <dd>{formatMs(tts?.firstAudioMs ?? null)}</dd>
            <dt>Estimated cost</dt>
            <dd>
              {ttsUsd === null || ttsCost === null ? NOT_AVAILABLE : `$${ttsUsd} (${ttsCost.calculationStatus})`}
            </dd>
          </dl>
        )}
      </section>
    </>
  );
}
