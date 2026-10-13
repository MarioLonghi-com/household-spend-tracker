// @vitest-environment jsdom

/**
 * A slow step says it is still going (the YNAB import of a large budget took
 * minutes and looked stuck, found testing the desktop app, #62).
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Working } from "./bits";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Working", () => {
  it("says what is happening, then after a few seconds that it is still going, and for how long", () => {
    vi.useFakeTimers();
    render(<Working>Importing…</Working>);
    const status = screen.getByRole("status");
    expect(status.textContent).toBe("Importing…");

    act(() => vi.advanceTimersByTime(3_000));
    expect(status.textContent).toContain("Still working (0:03)");
    expect(status.textContent).toContain("keep this window open");

    act(() => vi.advanceTimersByTime(72_000));
    expect(status.textContent).toContain("Still working (1:15)");
  });
});
