// @vitest-environment jsdom

/**
 * The (?) bubble stays inside the window (#412).
 *
 * It opens rightwards from its button. Beside the currency toggle on Income vs
 * Expense the button sits near the right edge, and the bubble ran off it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Hint, bubbleNudge } from "./bits";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("bubbleNudge", () => {
  it("leaves a bubble that fits where it is", () => {
    expect(bubbleNudge(100, 440, 1280)).toBe(0);
  });

  it("moves one that runs off the right edge back inside, 16px from it", () => {
    // 340 wide, opening at 1100 in a 1280 window: its right edge would be 1440.
    expect(bubbleNudge(1100, 1440, 1280)).toBe(1280 - 16 - 1440);
  });

  it("keeps the left edge on screen when the bubble is wider than the room", () => {
    // A 390 window, a 500px bubble opening at 300: moved left, but its left
    // edge stops 16px in -- the text starts there.
    expect(bubbleNudge(300, 800, 390)).toBe(16 - 300);
    // A 340px one fits between the margins, so it is right-aligned to the edge.
    expect(bubbleNudge(300, 640, 390)).toBe(390 - 16 - 640);
  });
});

describe("the bubble on screen", () => {
  it("is moved left, and its arrow right, when it would cross the right edge", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1280 });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("bubble")
        ? ({ left: 1100, right: 1440, top: 0, bottom: 100, width: 340, height: 100, x: 1100, y: 0, toJSON: () => ({}) } as DOMRect)
        : ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
    });
    render(<Hint label="why one currency">An explanation</Hint>);
    fireEvent.click(screen.getByRole("button"));

    const bubble = screen.getByRole("note");
    expect(bubble.style.transform).toBe("translateX(-176px)");
    expect(bubble.style.getPropertyValue("--bubble-arrow")).toBe("186px");
  });

  it("is left alone when it fits", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1280 });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      { left: 100, right: 440, top: 0, bottom: 100, width: 340, height: 100, x: 100, y: 0, toJSON: () => ({}) } as DOMRect,
    );
    render(<Hint label="why one currency">An explanation</Hint>);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("note").style.transform).toBe("");
  });
});
