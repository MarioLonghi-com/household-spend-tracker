// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { menu, pageLabel, tabTitle } from "../App";
import { Problem } from "../components/bits";
import { LanguagePicker } from "../components/LanguagePicker";
import { ApiError } from "./api";
import {
  LOCALE_KEY,
  PSEUDO_LOCALE,
  SERVED_LOCALES,
  activate,
  i18n,
  negotiate,
  storedLocale,
} from "./i18n";
import { uiLanguage } from "./locale";

afterEach(async () => {
  cleanup();
  await activate("en");
  window.localStorage.clear();
});

/** Plain ASCII letters: what an unextracted string looks like under en-XA. */
const ascii = /^[\x20-\x7e]*$/;

describe("English is the only language served", () => {
  it("offers English alone, so the picker is not drawn", () => {
    expect(SERVED_LOCALES).toEqual(["en"]);
    const { container } = render(<LanguagePicker />);
    expect(container.innerHTML).toBe("");
  });

  it("draws the picker once a second language is served", () => {
    render(<LanguagePicker served={["en", "sv-SE"]} />);
    expect(screen.getByRole("option", { name: "svenska (Sverige)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "English" })).toBeTruthy();
  });

  it.each(["pt-BR", "es-ES", "sv-SE"])("will not activate the draft %s, stored or asked for", async (locale) => {
    window.localStorage.setItem(LOCALE_KEY, locale);
    expect(storedLocale()).toBeNull();
    expect(await activate(locale)).toBe("en");
    expect([i18n.locale, uiLanguage()]).toEqual(["en", "en"]);
    expect(pageLabel(menu("Casa Doe"), "register")).toBe("Transactions");
  });

  it("answers English to a browser that asks for any of the drafts", () => {
    expect(negotiate(["pt-BR", "sv-SE", "es-ES"])).toBe("en");
  });

  it("keeps every English word the shell had", () => {
    const sections = menu("Casa Doe");
    expect(sections.map((one) => one.label)).toEqual(["Register", "Reports", "Casa Doe", "Admin"]);
    expect(tabTitle(pageLabel(sections, "rules"), "Casa Doe")).toBe(
      "Spend Tracker - Payee Naming Rules - Casa Doe",
    );
  });
});

describe("choosing a language for a first visit", () => {
  const later = ["en", "pt-BR", "es-ES", "sv-SE"];

  it("takes the first exact match from the browser's list", () => {
    expect(negotiate(["sv-SE", "pt-BR"], later)).toBe("sv-SE");
  });

  it("falls back from pt-PT to pt-BR, then to English", () => {
    expect(negotiate(["pt-PT"], later)).toBe("pt-BR");
    expect(negotiate(["pt-PT"], ["en"])).toBe("en");
    expect(negotiate(["fr-FR", "de"], later)).toBe("en");
  });

  it("matches a bare language and ignores case", () => {
    expect(negotiate(["SV"], later)).toBe("sv-SE");
    expect(negotiate(["es-mx"], later)).toBe("es-ES");
  });

  it("survives storage that throws", () => {
    const original = window.localStorage.getItem;
    window.localStorage.getItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(storedLocale()).toBeNull();
    } finally {
      window.localStorage.getItem = original;
    }
  });
});

describe("the en-XA pseudo-locale", () => {
  it("is reachable by storing it, and changes the shell's words", async () => {
    window.localStorage.setItem(LOCALE_KEY, PSEUDO_LOCALE);
    expect(storedLocale()).toBe(PSEUDO_LOCALE);
    expect(await activate(PSEUDO_LOCALE)).toBe(PSEUDO_LOCALE);
    const label = pageLabel(menu("Casa Doe"), "register")!;
    expect(label).not.toBe("Transactions");
    expect(label).not.toMatch(ascii);
    expect(document.documentElement.lang).toBe("en-XA");
  });

  it("puts the page in the tab title in the second language, and the app's name in English", async () => {
    await activate(PSEUDO_LOCALE);
    const title = tabTitle(pageLabel(menu("Casa Doe"), "accounts"), "Casa Doe");
    expect(title.startsWith("Spend Tracker - ")).toBe(true);
    expect(title.endsWith(" - Casa Doe")).toBe(true);
    expect(title).not.toBe("Spend Tracker - Accounts - Casa Doe");
  });
});

describe("a refusal with a code", () => {
  const refusal = new ApiError("that does not balance: -SEK 234.56 out. Tick or untick rows…", 409, undefined, {
    detail: "that does not balance: -SEK 234.56 out. Tick or untick rows…",
    code: "reconcile.does_not_balance",
    params: { difference: -23456, currency: "SEK" },
  });

  it("shows the server's sentence in English, byte for byte", () => {
    render(<Problem error={refusal} />);
    expect(screen.getByRole("alert").textContent).toBe(refusal.message);
  });

  it("shows the catalog's message under another language, with the amount formatted for it", async () => {
    await activate(PSEUDO_LOCALE);
    render(<Problem error={refusal} />);
    const shown = screen.getByRole("alert").textContent!;
    expect(shown).not.toBe(refusal.message);
    expect(shown).toContain("SEK");
    expect(shown).toContain("234.56");
  });

  it("falls back to the sentence for a code this build does not know", async () => {
    await activate(PSEUDO_LOCALE);
    const unknown = new ApiError("something new", 422, undefined, { code: "not.yet", params: {} });
    render(<Problem error={unknown} />);
    expect(screen.getByRole("alert").textContent).toBe("something new");
  });

  it.each([
    [1, "too many attempts Try again in 1 second."],
    [2, "too many attempts Try again in 2 seconds."],
  ])("says how long to wait, %s, with the singular for one (#269)", (wait, said) => {
    render(<Problem error={new ApiError("too many attempts", 429, wait)} />);
    expect(screen.getByRole("alert").textContent).toBe(said);
  });

  it("says nothing about waiting when there is no wait", () => {
    render(<Problem error={new ApiError("too many attempts", 429, 0)} />);
    expect(screen.getByRole("alert").textContent).toBe("too many attempts");
  });
});
