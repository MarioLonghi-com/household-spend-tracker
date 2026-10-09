/**
 * Which catalog the words come from (#53, part of #48).
 *
 * **English is the only language served.** The pt-BR, es-ES and sv-SE
 * catalogs exist and hold drafts, every entry `#, fuzzy`, and none of them is
 * reachable from here: there is no loader for them, so they are not in the
 * bundle at all. #58 ships them, after a native speaker has reviewed each one,
 * by adding it to `SERVED_LOCALES` and `LOADERS` -- and
 * `src/locales/catalogs.test.ts` refuses a served catalog that still holds a
 * fuzzy or missing entry.
 *
 * `en-XA` is the pseudo-locale, for CI and development only. It is never
 * offered in the picker; it is reached by storing it under `LOCALE_KEY`
 * (Playwright's en-XA pass does exactly that), and it is what makes an
 * unextracted string visible as plain ASCII among accented text.
 *
 * **The drafts in a QA build (#271).** `vite build --mode qa`, which is what
 * `npm run e2e` builds, also has loaders for the drafts, reached the same way
 * as en-XA: stored on the device. That is how the end-to-end pass walks every
 * screen in each language before anyone reviews it. The mode is fixed when
 * the bundle is built, so no other build has those loaders -- or the drafts.
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
 * How each catalog other than English arrives. Only the ones that may be
 * served are listed: a catalog with no loader cannot reach a browser.
 */
const LOADERS: Record<string, () => Promise<{ messages: Messages }>> = {
  [PSEUDO_LOCALE]: () => import("../locales/en-XA/messages.po"),
  ...(QA_BUILD
    ? {
        "pt-BR": () => import("../locales/pt-BR/messages.po"),
        "es-ES": () => import("../locales/es-ES/messages.po"),
        "sv-SE": () => import("../locales/sv-SE/messages.po"),
      }
    : {}),
};

i18n.load(SOURCE_LOCALE, english);
i18n.activate(SOURCE_LOCALE);

const loaded = new Set<string>([SOURCE_LOCALE]);

/** Whether a locale can be activated at all: served, the pseudo-locale, or a draft in a QA build. */
export function isReachable(locale: string): boolean {
  return (
    SERVED_LOCALES.includes(locale) ||
    locale === PSEUDO_LOCALE ||
    (QA_BUILD && DRAFT_LOCALES.includes(locale))
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

/** What this device stored, if it is something this build can show. */
export function storedLocale(): string | null {
  try {
    const value = window.localStorage.getItem(LOCALE_KEY);
    return value && isReachable(value) ? value : null;
  } catch {
    return null;
  }
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
