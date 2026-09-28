import type { ReactElement } from "react";
import { getPublicConfig } from "./config";

/**
 * Placeholder session shell for WP1 scaffolding. Later work packages
 * (WP6 onward) add real session state, LiveKit connection, transcript,
 * and telemetry surfaces per docs/06 and docs/14.
 *
 * WP3 adds a hard startup gate on validated public configuration
 * (docs/12-configuration-and-secrets.md §11-12): the session shell never
 * renders session controls on top of missing or malformed configuration,
 * and the failure surface never echoes a raw configuration value.
 */
export function App(): ReactElement {
  const configResult = getPublicConfig();

  if (!configResult.ok) {
    return (
      <main>
        <h1>Voice Agent Session</h1>
        <div role="alert">
          <p>Configuration error: {configResult.error.message}</p>
        </div>
      </main>
    );
  }

  return (
    <main>
      <h1>Voice Agent Session</h1>
      <p>Session controls are not yet implemented.</p>
    </main>
  );
}
