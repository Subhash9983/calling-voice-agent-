import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";

describe("App", () => {
  it("renders the session placeholder heading", () => {
    render(<App />);

    const heading = screen.getByRole("heading", {
      name: /voice agent session/i,
    });

    expect(heading.textContent).toBe("Voice Agent Session");
  });
});
