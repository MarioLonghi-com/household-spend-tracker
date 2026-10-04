// @vitest-environment jsdom

/**
 * A screen that throws while rendering leaves the menu standing, and going
 * to another screen clears the error (#193).
 */

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ScreenBoundary } from "./ScreenBoundary";
import { format } from "../lib/money";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function Broken(): never {
  throw new RangeError("Invalid currency code : €€€");
}

function Shell() {
  const [page, setPage] = useState<"broken" | "fine">("broken");
  return (
    <div>
      <nav>
        <button onClick={() => setPage("broken")}>Broken page</button>
        <button onClick={() => setPage("fine")}>Fine page</button>
      </nav>
      <main>
        <ScreenBoundary resetKey={page}>
          {page === "broken" ? <Broken /> : <p>{format(1234, "EUR")} in the fine page</p>}
        </ScreenBoundary>
      </main>
    </div>
  );
}

describe("ScreenBoundary", () => {
  it("keeps the navigation when a screen throws, and recovers on a screen change", () => {
    // React logs the caught error; keep the run readable.
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Shell />);

    expect(screen.getByRole("alert").textContent).toContain("Invalid currency code");
    expect(screen.getByRole("button", { name: "Fine page" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Fine page" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(/in the fine page/)).toBeTruthy();

    // And back again: the broken screen is caught a second time, not remembered.
    fireEvent.click(screen.getByRole("button", { name: "Broken page" }));
    expect(screen.getByRole("alert").textContent).toContain("Invalid currency code");
  });
});
