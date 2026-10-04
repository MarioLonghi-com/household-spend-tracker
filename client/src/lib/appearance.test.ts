// @vitest-environment jsdom

/**
 * The appearance choice: what is stored, what lands on the document, and what
 * happens when the store is not there at all.
 *
 * Every test asserts the value that changed -- the attribute on the element,
 * the string in the store -- rather than that a function was reached.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  APPEARANCE_KEY,
  type Appearance,
  applyAppearance,
  chooseAppearance,
  effectiveScheme,
  isAppearance,
  storedAppearance,
  systemIsDark,
  watchSystemScheme,
} from "./appearance";

/** A `matchMedia` that answers a scheme, and can change its mind. */
function fakeMatchMedia(dark: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => listeners.delete(fn),
  };
  return {
    install: () => vi.stubGlobal("matchMedia", () => query),
    flipTo: (nowDark: boolean) => {
      query.matches = nowDark;
      listeners.forEach((fn) => fn({ matches: nowDark } as MediaQueryListEvent));
    },
    listenerCount: () => listeners.size,
  };
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("what is stored", () => {
  it("is system when nothing has ever been chosen", () => {
    expect(storedAppearance()).toBe("system");
  });

  it("comes back as the thing that was chosen", () => {
    chooseAppearance("dark");
    expect(window.localStorage.getItem(APPEARANCE_KEY)).toBe("dark");
    expect(storedAppearance()).toBe("dark");
  });

  it("stores nothing at all for system, so no-choice and follow-along are one state", () => {
    chooseAppearance("light");
    expect(window.localStorage.getItem(APPEARANCE_KEY)).toBe("light");

    chooseAppearance("system");
    expect(window.localStorage.getItem(APPEARANCE_KEY)).toBeNull();
    expect(storedAppearance()).toBe("system");
  });

  it("treats a value it does not recognise as no choice", () => {
    window.localStorage.setItem(APPEARANCE_KEY, "sepia");
    expect(storedAppearance()).toBe("system");
    expect(isAppearance("sepia")).toBe(false);
  });

  it("survives a store that throws, as a private window's does", () => {
    const boom = () => {
      throw new DOMException("denied");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(boom);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(boom);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(boom);

    expect(storedAppearance()).toBe("system");
    // And the choice still reaches the document, which is the half that matters
    // for the page you are looking at right now.
    expect(() => chooseAppearance("dark")).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    vi.restoreAllMocks();
  });
});

describe("what lands on the document", () => {
  it("sets the attribute for a real choice", () => {
    applyAppearance("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    applyAppearance("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("removes the attribute for system rather than writing the word", () => {
    applyAppearance("dark");
    applyAppearance("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("can paint an element that is not the document, which is how it is tested", () => {
    const root = document.createElement("html");
    applyAppearance("dark", root);
    expect(root.getAttribute("data-theme")).toBe("dark");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});

describe("what is actually on screen", () => {
  it("is the choice itself when one was made, whatever the system says", () => {
    fakeMatchMedia(true).install();
    expect(systemIsDark()).toBe(true);
    // The whole point of the feature: a dark laptop, held in light.
    expect(effectiveScheme("light")).toBe("light");
    expect(effectiveScheme("dark")).toBe("dark");
  });

  it("is the system's answer under system", () => {
    fakeMatchMedia(true).install();
    expect(effectiveScheme("system")).toBe("dark");

    fakeMatchMedia(false).install();
    expect(effectiveScheme("system")).toBe("light");
  });

  it("reads the stored choice when asked nothing", () => {
    fakeMatchMedia(true).install();
    chooseAppearance("light");
    expect(effectiveScheme()).toBe("light");
  });

  it("says light when the browser has no matchMedia to ask", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(systemIsDark()).toBe(false);
    expect(effectiveScheme("system")).toBe("light");
  });
});

describe("watching the system", () => {
  it("reports a change and then stops when unsubscribed", () => {
    const media = fakeMatchMedia(false);
    media.install();

    const seen: boolean[] = [];
    const stop = watchSystemScheme((dark) => seen.push(dark));
    media.flipTo(true);
    media.flipTo(false);
    expect(seen).toEqual([true, false]);

    stop();
    expect(media.listenerCount()).toBe(0);
    media.flipTo(true);
    expect(seen).toEqual([true, false]);
  });

  it("hands back a working unsubscribe even where matchMedia is missing", () => {
    vi.stubGlobal("matchMedia", undefined);
    const stop = watchSystemScheme(() => {
      throw new Error("must not be called");
    });
    expect(() => stop()).not.toThrow();
  });
});

describe("the choices themselves", () => {
  it("accepts exactly three", () => {
    const good: Appearance[] = ["light", "dark", "system"];
    good.forEach((one) => expect(isAppearance(one)).toBe(true));
    [null, undefined, 1, "Dark", "auto", ""].forEach((bad) =>
      expect(isAppearance(bad)).toBe(false),
    );
  });
});
