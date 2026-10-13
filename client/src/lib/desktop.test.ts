// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { activate } from "./i18n";
import { formatLocale, setFormatLocale } from "./locale";

type Holder = { spendTrackerDesktop?: unknown };

afterEach(async () => {
  delete (globalThis as Holder).spendTrackerDesktop;
  setFormatLocale("en-US");
  await activate("en");
});

describe("inside the desktop shell (#62)", () => {
  it("tells the shell each language the window activates, for its menus", async () => {
    const setLanguage = vi.fn();
    (globalThis as Holder).spendTrackerDesktop = { setLanguage };
    await activate("en-XA");
    await activate("en");
    expect(setLanguage.mock.calls).toEqual([["en-XA"], ["en"]]);
  });

  it("formats with the operating system's regional format, not the UI language", () => {
    (globalThis as Holder).spendTrackerDesktop = { systemLocale: "sv-SE", setLanguage: () => undefined };
    setFormatLocale(undefined);
    expect(formatLocale()).toBe("sv-SE");
  });

  it("keeps the browser's when the shell's is not a locale at all", () => {
    (globalThis as Holder).spendTrackerDesktop = { systemLocale: "not a locale!", setLanguage: () => undefined };
    setFormatLocale(undefined);
    expect(formatLocale()).toBe(new Intl.NumberFormat().resolvedOptions().locale);
  });

  it("a format chosen on the device still wins over the system's", () => {
    (globalThis as Holder).spendTrackerDesktop = { systemLocale: "sv-SE", setLanguage: () => undefined };
    setFormatLocale("pt-BR");
    expect(formatLocale()).toBe("pt-BR");
  });

  it("in a browser there is no shell, and nothing is called", async () => {
    expect((globalThis as Holder).spendTrackerDesktop).toBeUndefined();
    await expect(activate("en-XA")).resolves.toBe("en-XA");
  });
});
