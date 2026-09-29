import type { ReactElement } from "react";
import { SessionScreen } from "./components";
import { getPublicConfig } from "./config";
import type { ControllerDeps } from "./session/controller";

export interface AppProps {
  /** Test seam: replaces the real API client, LiveKit transport and microphone. */
  readonly controllerDeps?: ControllerDeps;
}

/**
 * WP3 startup gate (docs/12-configuration-and-secrets.md §11-12): the
 * session UI never renders on top of missing or malformed configuration,
 * and the failure surface never echoes a raw configuration value.
 */
export function App({ controllerDeps }: AppProps = {}): ReactElement {
  const configResult = getPublicConfig();

  if (!configResult.ok) {
    return (
      <main className="shell">
        <h1>Voice Agent Session</h1>
        <div role="alert" className="banner banner-error">
          <p>Configuration error: {configResult.error.message}</p>
        </div>
      </main>
    );
  }

  return <SessionScreen config={configResult.config} controllerDeps={controllerDeps} />;
}
