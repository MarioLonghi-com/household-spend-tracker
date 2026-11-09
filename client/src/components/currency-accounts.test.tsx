// @vitest-environment jsdom

/**
 * The (i) beside a currency on the Exchange rates page (#411): the accounts
 * in that currency, readable, sorted, and all of it inside the window.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { CurrencyAccounts, placeBubble } from "./CurrencyAccounts";
import type { FxAccountRef } from "../lib/fx";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const SEK: FxAccountRef[] = [
  { id: "a2", name: "Savings pot", institution: null, country: null, country_name: null, flag: "🇺🇳", closed: false },
  { id: "a1", name: "Kronor", institution: "Nordbank", country: "SE", country_name: "Sweden", flag: "🇸🇪", closed: true },
  { id: "a3", name: "buffer", institution: "Andelsbank", country: "DK", country_name: "Denmark", flag: "🇩🇰", closed: false },
];

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return { left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) } as DOMRect;
}

/** The button at `button`, the bubble `width` x `height`, in a window of `w` x `h`. */
function layout(button: DOMRect, width: number, height: number, w: number, h: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: w });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: h });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("bubble") ? rect(0, 0, width, height) : button;
  });
}

describe("placeBubble", () => {
  it("opens below and to the right of a button with room", () => {
    const at = placeBubble(rect(100, 100, 16, 16), { width: 300, height: 200 }, { width: 1280, height: 800 });
    expect(at).toEqual({ left: 92, top: 124, maxHeight: 800 - 16 - 124, above: false, arrow: 10 });
  });

  it("keeps a currency in the right-hand column inside the window", () => {
    const at = placeBubble(rect(1250, 100, 16, 16), { width: 300, height: 200 }, { width: 1280, height: 800 });
    expect(at.left + 300).toBe(1280 - 16);
    // The arrow moves the other way, to keep pointing at the button.
    expect(at.left + at.arrow).toBeGreaterThan(1250 - 8);
  });

  it("keeps it inside a phone's width", () => {
    const at = placeBubble(rect(300, 100, 16, 16), { width: 343, height: 200 }, { width: 375, height: 812 });
    expect(at.left).toBe(16);
    expect(at.left + 343).toBeLessThanOrEqual(375 - 16);
  });

  it("opens above when there is no room below and more above, and caps its height", () => {
    const at = placeBubble(rect(100, 700, 16, 16), { width: 300, height: 400 }, { width: 1280, height: 800 });
    expect(at.above).toBe(true);
    expect(at.maxHeight).toBe(700 - 8 - 16);
    expect(at.top).toBe(700 - 8 - 400);
    expect(at.top).toBeGreaterThanOrEqual(16);
  });

  it("caps a list taller than either side, so it scrolls inside", () => {
    const at = placeBubble(rect(100, 380, 16, 16), { width: 300, height: 2000 }, { width: 1280, height: 800 });
    expect(at.top).toBeGreaterThanOrEqual(16);
    expect(at.top + Math.min(2000, at.maxHeight)).toBeLessThanOrEqual(800 - 16);
  });
});

describe("the (i) on screen", () => {
  it("is a button named for its currency, and opens the accounts sorted by name", () => {
    layout(rect(100, 100, 16, 16), 300, 200, 1280, 800);
    render(<CurrencyAccounts currency="SEK" accounts={SEK} />);
    const button = screen.getByRole("button", { name: "Accounts in SEK" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");

    const list = screen.getByRole("region", { name: "Accounts in SEK" });
    const names = within(list).getAllByRole("listitem").map((item) => item.querySelector(".currency-account-name")?.textContent);
    expect(names).toEqual(["buffer", "Kronor", "Savings pot"]);
  });

  it("shows each account's bank and country, and marks the closed one closed", () => {
    layout(rect(100, 100, 16, 16), 300, 200, 1280, 800);
    render(<CurrencyAccounts currency="SEK" accounts={SEK} />);
    fireEvent.click(screen.getByRole("button", { name: "Accounts in SEK" }));
    const [buffer, kronor, pot] = screen.getAllByRole("listitem");
    expect(kronor.textContent).toContain("Nordbank");
    expect(kronor.textContent).toContain("Sweden");
    expect(kronor.querySelector(".tag.closed")?.textContent).toBe("Closed");
    expect(buffer.textContent).toContain("Andelsbank");
    expect(buffer.textContent).toContain("Denmark");
    expect(buffer.querySelector(".tag.closed")).toBeNull();
    expect(pot.textContent).toContain("No bank set");
    expect(pot.textContent).toContain("no country set");
  });

  it("is reachable from the keyboard and closes with Escape, back on its button", () => {
    layout(rect(100, 100, 16, 16), 300, 200, 1280, 800);
    render(<CurrencyAccounts currency="SEK" accounts={SEK} />);
    const button = screen.getByRole("button", { name: "Accounts in SEK" });
    fireEvent.click(button);
    const list = screen.getByRole("region", { name: "Accounts in SEK" });
    // Focusable, so the arrows scroll a long list; next to the button, so Tab reaches it.
    expect(list.tabIndex).toBe(0);
    expect(button.nextElementSibling).toBe(list);
    list.focus();
    fireEvent.keyDown(list, { key: "Escape" });
    expect(screen.queryByRole("region")).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button);
  });

  it("closes on a click anywhere else", () => {
    layout(rect(100, 100, 16, 16), 300, 200, 1280, 800);
    render(
      <div>
        <CurrencyAccounts currency="SEK" accounts={SEK} />
        <p>elsewhere</p>
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Accounts in SEK" }));
    fireEvent.mouseDown(screen.getByRole("region"));
    expect(screen.queryByRole("region")).not.toBeNull();
    fireEvent.mouseDown(screen.getByText("elsewhere"));
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("is moved inside the window for a currency near the right edge", () => {
    layout(rect(1250, 100, 16, 16), 300, 200, 1280, 800);
    render(<CurrencyAccounts currency="SEK" accounts={SEK} />);
    fireEvent.click(screen.getByRole("button", { name: "Accounts in SEK" }));
    const list = screen.getByRole("region");
    expect(list.style.left).toBe(`${1280 - 16 - 300}px`);
    expect(list.style.top).toBe("124px");
    expect(list.style.visibility).toBe("");
  });

  it("is kept inside a phone's width", () => {
    layout(rect(320, 100, 16, 16), 343, 200, 375, 812);
    render(<CurrencyAccounts currency="SEK" accounts={SEK} />);
    fireEvent.click(screen.getByRole("button", { name: "Accounts in SEK" }));
    expect(screen.getByRole("region").style.left).toBe("16px");
  });
});
