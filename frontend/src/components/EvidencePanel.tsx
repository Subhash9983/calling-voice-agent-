import type { ReactElement } from "react";
import type { EvidenceState } from "../session/evidence";

const NOT_AVAILABLE = "Not available yet";

function formatSeconds(seconds: number | null): string {
  return seconds === null ? NOT_AVAILABLE : `${seconds.toFixed(1)} s`;
}

/** Per-session STT summary (docs/04 §12, §14). Missing data is neutral, never an error. */
export function EvidencePanel({ evidence }: { readonly evidence: EvidenceState }): ReactElement {
  const cost = evidence.status === "ready" ? evidence.cost : null;
  const sttUsd = cost?.sttUsd ?? null;
  return (
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
  );
}
