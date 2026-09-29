import type { ReactElement } from "react";
import type { SessionEventItem } from "../contracts/sessionApi";

export function EventsPanel({ events }: { readonly events: readonly SessionEventItem[] }): ReactElement {
  return (
    <section aria-labelledby="events-heading" className="panel panel-wide">
      <h2 id="events-heading">Durable event timeline</h2>
      {events.length === 0 ? (
        <p className="muted">Loaded from the control API after the session ends.</p>
      ) : (
        <ol className="events">
          {events.map((event) => (
            <li key={event.eventId}>
              <span className="mono">#{event.sequenceNumber}</span> {event.eventType}{" "}
              <span className="muted">({event.severity})</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
