import type { ReactElement } from "react";

/**
 * Placeholder session shell for WP1 scaffolding. Later work packages
 * (WP6 onward) add real session state, LiveKit connection, transcript,
 * and telemetry surfaces per docs/06 and docs/14.
 */
export function App(): ReactElement {
  return (
    <main>
      <h1>Voice Agent Session</h1>
      <p>Session controls are not yet implemented.</p>
    </main>
  );
}
