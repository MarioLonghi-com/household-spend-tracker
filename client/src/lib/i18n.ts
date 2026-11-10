/**
 * Which catalog the words come from (#53, part of #48).
 *
 * **Four languages are served** since 0.15.0 (#58): English, and the
 * Brazilian Portuguese, Spanish and Swedish the household reviewed in the app
 * (#272). Each is in Profile's language picker for everyone, chosen per
 * device (#48), and `src/locales/catalogs.test.ts` refuses a served catalog
 * that holds a fuzzy or missing entry -- so a new message ships with its
 * three translations, or not at all. A further language starts as `#, fuzzy`
 * drafts and joins `SERVED_LOCALES` once reviewed.
 *
 * **Review mode** (#272) is still an owner's, per device: on, "Suggest a
 * better wording" is offered on every screen shown in a translated language,
 * and Application management lists what the household suggested.
 *
 * `en-XA` is the pseudo-locale, for CI and development only. It is never
 * offered in the picker; it is reached by storing it under `LOCALE_KEY`
 * (Playwright's en-XA pass does exactly that), and it is what makes an
 * unextracted string visible as plain ASCII among accented text.
 *
 * English is loaded with the bundle and activated before the first render, so
 * there is never a frame without words. Another catalog loads lazily, as its
 * own chunk, the first time a device chooses it.
 */

import { i18n, type Messages } from "@lingui/core";
import { messages as english } from "../locales/en/messages.po";
import { desktop } from "./desktop";

export { i18n };

/** The language the source is written in, and the one served when nothing else is. */
export const SOURCE_LOCALE = "en";

/** The languages besides English, in the picker's order. Reviewed and shipped in 0.15.0 (#58). */
export const TRANSLATED_LOCALES: readonly string[] = ["pt-BR", "es-ES", "sv-SE"];

/** What the language picker offers. One entry would mean the picker is not shown. */
export const SERVED_LOCALES: readonly string[] = [SOURCE_LOCALE, ...TRANSLATED_LOCALES];

/** Reachable by storing it, never offered: the pseudo-locale. */
export const PSEUDO_LOCALE = "en-XA";

/** Where this device's choice of language is kept. Per device, as #48 decided. */
export const LOCALE_KEY = "spendtracker.locale";

/**
 * How each catalog other than English arrives, each its own chunk, fetched
 * only when that language is chosen. Whether it may be chosen is
 * `isReachable`'s question, not this list's.
 */
const LOADERS: Record<string, () => Promise<{ messages: Messages }>> = {
  [PSEUDO_LOCALE]: () => import("../locales/en-XA/messages.po"),
  "pt-BR": () => import("../locales/pt-BR/messages.po"),
  "es-ES": () => import("../locales/es-ES/messages.po"),
  "sv-SE": () => import("../locales/sv-SE/messages.po"),
};


/** Whether this device is reviewing the translations: an owner, review mode on (#272). */
let reviewer = false;
const listeners = new Set<() => void>();

/** Whether "Suggest a better wording" is offered here. */
export function reviewing(): boolean {
  return reviewer;
}

/** For `useSyncExternalStore`: called when `reviewing()` changes. */
export function onReviewingChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Turn review mode on or off for this device. The shell calls it with
 * "owner, and review mode on" once it knows who is signed in, and with
 * `false` when they leave. The language itself is not touched: every served
 * language is anyone's to keep, review mode or not.
 */
export async function setReviewer(allowed: boolean): Promise<void> {
  if (allowed === reviewer) return;
  reviewer = allowed;
  for (const listener of listeners) listener();
}

i18n.load(SOURCE_LOCALE, english);
i18n.activate(SOURCE_LOCALE);

const loaded = new Set<string>([SOURCE_LOCALE]);

/** Whether a locale can be activated at all: served, or the pseudo-locale. */
export function isReachable(locale: string): boolean {
  return SERVED_LOCALES.includes(locale) || locale === PSEUDO_LOCALE;
}

/**
 * The best served locale for what the browser asks for, or English.
 *
 * Exact first, then the language alone -- `pt-PT` takes `pt-BR` when that is
 * the Portuguese served, since a Portuguese reader reads Brazilian Portuguese
 * far better than English. `available` is a parameter so the rule can be
 * tested before a second language is served.
 */
export function negotiate(
  requested: readonly string[],
  available: readonly string[] = SERVED_LOCALES,
): string {
  for (const wanted of requested) {
    const exact = available.find((one) => one.toLowerCase() === wanted.toLowerCase());
    if (exact) return exact;
  }
  for (const wanted of requested) {
    const language = wanted.split("-")[0].toLowerCase();
    const near = available.find((one) => one.split("-")[0].toLowerCase() === language);
    if (near) return near;
  }
  return SOURCE_LOCALE;
}

/** What this device stored, whatever it is. */
function rawStoredLocale(): string | null {
  try {
    return window.localStorage.getItem(LOCALE_KEY);
  } catch {
    return null;
  }
}

/** What this device stored, if it is something this device may show now. */
export function storedLocale(): string | null {
  const value = rawStoredLocale();
  return value && isReachable(value) ? value : null;
}

/** Remember a choice on this device. A blocked store means it lasts the session. */
export function storeLocale(locale: string | null): void {
  try {
    if (locale) window.localStorage.setItem(LOCALE_KEY, locale);
    else window.localStorage.removeItem(LOCALE_KEY);
  } catch {
    // Private window or blocked site data: the choice still applies now.
  }
}

/** Load a catalog if it is not loaded, and make it the one the words come from. */
export async function activate(locale: string): Promise<string> {
  const target = isReachable(locale) ? locale : SOURCE_LOCALE;
  if (!loaded.has(target)) {
    const loader = LOADERS[target];
    if (!loader) return i18n.locale;
    const { messages } = await loader();
    i18n.load(target, messages);
    loaded.add(target);
  }
  i18n.activate(target);
  document.documentElement.lang = target;
  // The desktop app's menus and dialogs follow the window (#62).
  desktop()?.setLanguage(target);
  return target;
}

/**
 * Before the first render: the stored choice, else the browser's languages
 * negotiated against what is served. English needs no wait; another catalog
 * swaps in when it arrives, and `I18nProvider` re-renders for it.
 */
export function startI18n(): Promise<string> {
  const browser = typeof navigator === "undefined" ? [] : (navigator.languages ?? []);
  return activate(storedLocale() ?? negotiate(browser));
}
