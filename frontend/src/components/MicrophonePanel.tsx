import type { ReactElement } from "react";
import type { ConstraintName } from "../audio/microphone";
import type { SessionViewState } from "../session/sessionState";
import { CONSTRAINT_LABELS, CONSTRAINT_STATUS_LABELS } from "./labels";

const MIC_STATUS_TEXT: Readonly<Record<SessionViewState["mic"]["status"], string>> = {
  idle: "Not started",
  requesting: "Waiting for permission",
  active: "Capturing",
  released: "Released (microphone off)",
  denied: "Permission denied",
  lost: "Device lost",
  error: "Unavailable",
};

const NAMES: readonly ConstraintName[] = [
  "echoCancellation",
  "noiseSuppression",
  "autoGainControl",
  "channelCount",
];

export function MicrophonePanel({ mic }: { readonly mic: SessionViewState["mic"] }): ReactElement {
  return (
    <section aria-labelledby="mic-heading" className="panel">
      <h2 id="mic-heading">Microphone</h2>
      <p>
        <span className="chip">{MIC_STATUS_TEXT[mic.status]}</span>{" "}
        {mic.muted && <span className="chip chip-warn">Muted</span>}
      </p>
      {(mic.status === "denied" || mic.status === "error") && mic.message !== null && (
        <p role="alert" className="inline-error">
          {mic.message}
        </p>
      )}
      {mic.report !== null && (
        <>
          {mic.report.degraded.length > 0 && (
            <p role="alert" className="inline-error">
              Some capture processing is not verifiably active:{" "}
              {mic.report.degraded.map((name) => CONSTRAINT_LABELS[name]).join(", ")}. Echo may reach
              the agent.
            </p>
          )}
          <ul className="constraints">
            {NAMES.map((name) => (
              <li key={name}>
                {CONSTRAINT_LABELS[name]}: {CONSTRAINT_STATUS_LABELS[mic.report?.statuses[name] ?? "unreported"]}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
