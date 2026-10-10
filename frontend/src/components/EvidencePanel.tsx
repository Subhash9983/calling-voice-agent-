import { Fragment, type ReactElement } from "react";
import type {
  ErrorCodeCount,
  EvidenceState,
  LatencyStageSummary,
  OverallCostSummary,
  SessionOutcomeSummary,
} from "../session/evidence";

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

/** INR first (the deployment's home currency), USD alongside for precision; USD alone if INR's own FX conversion is unavailable (never derived client-side). */
function formatCost(usd: string | null, inrDisplay: string | null | undefined, status: string): string {
  if (usd === null) {
    return NOT_AVAILABLE;
  }
  return inrDisplay === null || inrDisplay === undefined
    ? `$${usd} (${status})`
    : `₹${inrDisplay} ($${usd}, ${status})`;
}

function formatCostRate(usdPerMin: string | null, inrPerMinDisplay: string | null | undefined): string {
  if (usdPerMin === null) {
    return NOT_AVAILABLE;
  }
  return inrPerMinDisplay === null || inrPerMinDisplay === undefined
    ? `$${usdPerMin}/min`
    : `₹${inrPerMinDisplay}/min ($${usdPerMin}/min)`;
}

/** Turns a safe snake_case enum value into a short readable label (never echoes free text). */
function humanize(value: string): string {
  const spaced = value.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function errorCodeLabel(code: ErrorCodeCount): string {
  return `${humanize(code.component)} / ${humanize(code.errorType)} (${String(code.count)})`;
}

function SessionOutcomeSection({ outcome }: { readonly outcome: SessionOutcomeSummary | null }): ReactElement {
  return (
    <section aria-labelledby="outcome-evidence-heading">
      <h3 id="outcome-evidence-heading">Session outcome</h3>
      {outcome === null ? (
        <p className="muted">Shown after the session ends.</p>
      ) : (
        <>
          <dl className="facts">
            <dt>Final status</dt>
            <dd>{humanize(outcome.status)}</dd>
            <dt>End reason</dt>
            <dd>{outcome.disconnectReason === null ? NOT_AVAILABLE : humanize(outcome.disconnectReason)}</dd>
            <dt>Errors recorded</dt>
            <dd>{outcome.errorCount === null ? NOT_AVAILABLE : outcome.errorCount}</dd>
          </dl>
          {outcome.errorCodes.length > 0 && (
            <ul className="error-codes">
              {outcome.errorCodes.map((code) => (
                <li key={`${code.component}:${code.errorType}`}>{errorCodeLabel(code)}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function OverallCostSection({ cost }: { readonly cost: OverallCostSummary | null }): ReactElement {
  return (
    <section aria-labelledby="overall-cost-evidence-heading">
      <h3 id="overall-cost-evidence-heading">Overall cost</h3>
      {cost === null ? (
        <p className="muted">Shown after the session ends.</p>
      ) : (
        <dl className="facts">
          <dt>Total cost</dt>
          <dd>{formatCost(cost.totalUsd, cost.totalInrDisplay, cost.calculationStatus)}</dd>
          <dt>Cost per minute</dt>
          <dd>{formatCostRate(cost.costPerMinuteUsd, cost.costPerMinuteInrDisplay)}</dd>
        </dl>
      )}
    </section>
  );
}

function LatencySummarySection({ latency }: { readonly latency: readonly LatencyStageSummary[] | null }): ReactElement {
  return (
    <section aria-labelledby="latency-evidence-heading">
      <h3 id="latency-evidence-heading">Latency summary</h3>
      {latency === null ? (
        <p className="muted">Shown after the session ends.</p>
      ) : (
        <dl className="facts">
          {latency.map((stage) => (
            <Fragment key={stage.key}>
              <dt>{stage.label}</dt>
              <dd>
                {stage.metric === null
                  ? NOT_AVAILABLE
                  : `${formatMs(stage.metric.averageMs)} avg, ${formatMs(stage.metric.p95Ms)} p95 (n=${String(stage.metric.sampleCount)})`}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
    </section>
  );
}

/** Per-session evidence report (docs/04 §8, §12-§14, §17). Missing data is neutral, never an error. */
export function EvidencePanel({ evidence }: { readonly evidence: EvidenceState }): ReactElement {
  const cost = evidence.status === "ready" ? evidence.cost : null;
  const sttUsd = cost?.sttUsd ?? null;
  const conversation = evidence.status === "ready" ? (evidence.conversation ?? null) : null;
  const conversationCost = evidence.status === "ready" ? (evidence.conversationCost ?? null) : null;
  const conversationUsd = conversationCost?.conversationUsd ?? null;
  const tts = evidence.status === "ready" ? (evidence.tts ?? null) : null;
  const ttsCost = evidence.status === "ready" ? (evidence.ttsCost ?? null) : null;
  const ttsUsd = ttsCost?.ttsUsd ?? null;
  const overallCost = evidence.status === "ready" ? (evidence.overallCost ?? null) : null;
  const outcome = evidence.status === "ready" ? evidence.outcome : null;
  const latency = evidence.status === "ready" ? evidence.latency : null;
  return (
    <section aria-labelledby="evidence-report-heading" className="panel">
      <h2 id="evidence-report-heading">Session evidence</h2>

      <SessionOutcomeSection outcome={outcome} />
      <OverallCostSection cost={overallCost} />

      <section aria-labelledby="evidence-heading">
        <h3 id="evidence-heading">Speech recognition summary</h3>
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
              {sttUsd === null || cost === null ? NOT_AVAILABLE : formatCost(sttUsd, cost.sttInrDisplay, cost.calculationStatus)}
            </dd>
          </dl>
        )}
      </section>

      <section aria-labelledby="conversation-evidence-heading">
        <h3 id="conversation-evidence-heading">Conversation engine summary</h3>
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
                : formatCost(conversationUsd, conversationCost.conversationInrDisplay, conversationCost.calculationStatus)}
            </dd>
          </dl>
        )}
      </section>

      <section aria-labelledby="tts-evidence-heading">
        <h3 id="tts-evidence-heading">Speech synthesis summary</h3>
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
              {ttsUsd === null || ttsCost === null ? NOT_AVAILABLE : formatCost(ttsUsd, ttsCost.ttsInrDisplay, ttsCost.calculationStatus)}
            </dd>
          </dl>
        )}
      </section>

      <LatencySummarySection latency={latency} />
    </section>
  );
}
