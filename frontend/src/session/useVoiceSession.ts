import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { createControlApiClient, type ControlApiClient } from "../api";
import { acquireMicrophone } from "../audio/microphone";
import type { AgentConfigView } from "../contracts/sessionApi";
import { LiveKitTransport } from "../livekit/transport";
import { VoiceSessionController, type ControllerDeps } from "./controller";
import type { SessionViewState } from "./sessionState";

export interface VoiceSessionApi {
  readonly state: SessionViewState;
  readonly configs: ConfigsState;
  readonly start: (agentConfigId: string) => void;
  readonly stop: () => void;
  readonly setMuted: (muted: boolean) => void;
  readonly enableAudio: () => void;
  readonly reloadConfigs: () => void;
}

export type ConfigsState =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly items: readonly AgentConfigView[] }
  | { readonly status: "error"; readonly message: string };

export function createDefaultDeps(
  api: ControlApiClient,
  audioHost: () => HTMLElement,
): ControllerDeps {
  return {
    api,
    createTransport: (handlers) => new LiveKitTransport({ handlers, audioHost: audioHost() }),
    acquireMicrophone: () => acquireMicrophone(),
    newId: () => crypto.randomUUID(),
    nowIso: () => new Date().toISOString(),
    sleep: (ms) =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, ms);
      }),
  };
}

/**
 * React binding. `deps` may be injected (tests); the default wires the real
 * API client, LiveKit transport and microphone.
 */
export function useVoiceSession(
  apiBaseUrl: string,
  audioHost: () => HTMLElement,
  injected?: ControllerDeps,
): VoiceSessionApi {
  const [api] = useState(() => injected?.api ?? createControlApiClient({ baseUrl: apiBaseUrl }));
  const [controller] = useState(
    () => new VoiceSessionController(injected ?? createDefaultDeps(api, audioHost)),
  );
  const state = useSyncExternalStore(controller.subscribe, controller.getState);
  const [configs, setConfigs] = useState<ConfigsState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .listAgentConfigs()
      .then((items) => {
        if (!cancelled) {
          setConfigs({ status: "ready", items });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setConfigs({
            status: "error",
            message: error instanceof Error ? error.message : "Agent configurations could not be loaded.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [api, reloadKey]);

  const reloadConfigs = useCallback(() => {
    setConfigs({ status: "loading" });
    setReloadKey((key) => key + 1);
  }, []);

  useEffect(() => {
    const onPageHide = (): void => {
      void controller.stop("browser_closed");
    };
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [controller]);

  return {
    state,
    configs,
    start: (agentConfigId) => {
      void controller.start(agentConfigId);
    },
    stop: () => {
      void controller.stop();
    },
    setMuted: (muted) => {
      void controller.setMuted(muted);
    },
    enableAudio: () => {
      void controller.enableAudio();
    },
    reloadConfigs,
  };
}
