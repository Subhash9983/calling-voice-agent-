import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { fakeDeps } from "./support/fakeDeps";

describe("App", () => {
  beforeEach(() => {
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:8000/api/v1");
    vi.stubEnv("VITE_APP_ENV", "development");
    vi.stubEnv("VITE_BUILD_VERSION", "0.0.0-test");
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("renders the session heading and controls when configuration is valid", async () => {
    render(<App controllerDeps={fakeDeps().deps} />);

    const heading = screen.getByRole("heading", { level: 1, name: /voice agent session/i });
    await waitFor(() => {
      expect(screen.getByRole<HTMLButtonElement>("button", { name: /start session/i }).disabled).toBe(false);
    });

    expect(heading.textContent).toBe("Voice Agent Session");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a safe error state instead of session controls when configuration is invalid", () => {
    vi.stubEnv("VITE_APP_ENV", "production");

    render(<App controllerDeps={fakeDeps().deps} />);

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain(
      "VITE_APP_ENV must be one of the approved Phase 0 environment labels.",
    );
    expect(screen.queryByRole("button", { name: /start session/i })).toBeNull();
  });
});
