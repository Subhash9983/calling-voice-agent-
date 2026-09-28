import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";

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

  it("renders the session placeholder heading when configuration is valid", () => {
    render(<App />);

    const heading = screen.getByRole("heading", {
      name: /voice agent session/i,
    });

    expect(heading.textContent).toBe("Voice Agent Session");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a safe error state instead of session controls when configuration is invalid", () => {
    vi.stubEnv("VITE_APP_ENV", "production");

    render(<App />);

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain(
      "VITE_APP_ENV must be one of the approved Phase 0 environment labels.",
    );
    expect(
      screen.queryByText(/session controls are not yet implemented/i),
    ).toBeNull();
  });
});
