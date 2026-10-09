/**
 * Which catalog the words come from (#53, part of #48).
 *
 * **English is the only language served.** The pt-BR, es-ES and sv-SE
 * catalogs exist and hold drafts, and nobody is offered them -- with one
 * exception: an owner who turns on **review mode** on their own device
 * (#272, Application management) gets them in Profile's language picker,
 * labelled as previews, so the household can review the drafts in the app.
 * Their loaders are in the bundle for that, as chunks loaded only when chosen.
 * #58 ships a language, once it is reviewed, by adding it to
 * `SERVED_LOCALES` -- and `src/locales/catalogs.test.ts` refuses a served
 * catalog that still holds a fuzzy or missing entry.
 *
 * `en-XA` is the pseudo-locale, for CI and development only. It is never
 * offered in the picker; it is reached by storing it under `LOCALE_KEY`
 * (Playwright's en-XA pass does exactly that), and it is what makes an
 * unextracted string visible as plain ASCII among accented text.
 *
 * **The drafts in a QA build (#271).** In `vite build --mode qa`, which is
 * what `npm run e2e` builds, a draft stored on the device is reached the way
 * en-XA is, without review mode. That is how the end-to-end pass walks every
 * screen in each language. The mode is fixed when the bundle is built, so in
 * any other build only review mode reaches a draft.
 *
 * English is loaded with the bundle and activated before the first render, so
 * there is never a frame without words. Another catalog loads lazily.
 */

import { i18n, type Messages } from "@lingui/core";
import { messages as english } from "../locales/en/messages.po";

export { i18n };

/** The language the source is written in, and the one served when nothing else is. */
export const SOURCE_LOCALE = "en";

/** What the language picker offers. One entry means the picker is not shown. */
export const SERVED_LOCALES: readonly string[] = [SOURCE_LOCALE];

/** Reachable by storing it, never offered: the pseudo-locale. */
export const PSEUDO_LOCALE = "en-XA";

/** The languages whose catalogs hold drafts, not yet served (#58). */
export const DRAFT_LOCALES: readonly string[] = ["pt-BR", "es-ES", "sv-SE"];

/** A build made for the end-to-end QA pass, and nothing else (#271). */
const QA_BUILD = import.meta.env.MODE === "qa";

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


/** Whether this device is reviewing the drafts: an owner, review mode on (#272). */
let reviewer = false;
const listeners = new Set<() => void>();

/** Whether the drafts are offered here, as previews. */
export function reviewing(): boolean {
  return reviewer;
}

/** For `useSyncExternalStore`: called when `reviewing()` changes. */
export function onReviewingChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Turn the previews on or off for this device. The shell calls it with
 * "owner, and review mode on" once it knows who is signed in, and with
 * `false` when they leave. Turning on brings back a draft this device chose
 * before; turning off puts a draft that is showing back to English.
 */
export async function setReviewer(allowed: boolean): Promise<void> {
  if (allowed === reviewer) return;
  reviewer = allowed;
  for (const listener of listeners) listener();
  if (allowed) {
    const chosen = rawStoredLocale();
    if (chosen && DRAFT_LOCALES.includes(chosen)) await activate(chosen);
  } else if (DRAFT_LOCALES.includes(i18n.locale)) {
    // Even in a QA build: turning review off is asking for English back.
    await activate(SOURCE_LOCALE);
  }
}

i18n.load(SOURCE_LOCALE, english);
i18n.activate(SOURCE_LOCALE);

const loaded = new Set<string>([SOURCE_LOCALE]);

/** Whether a locale can be activated at all: served, the pseudo-locale, or a draft -- under review, or in a QA build. */
export function isReachable(locale: string): boolean {
  return (
    SERVED_LOCALES.includes(locale) ||
    locale === PSEUDO_LOCALE ||
    ((reviewer || QA_BUILD) && DRAFT_LOCALES.includes(locale))
  );
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
    // Review mode may have been turned off while the catalog arrived.
    if (!isReachable(target)) return i18n.locale;
  }
  i18n.activate(target);
  document.documentElement.lang = target;
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
