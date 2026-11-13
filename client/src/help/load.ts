/**
 * Which language's articles to show, and fetching them (#460).
 *
 * Each served language is a chunk of its own, fetched the first time the
 * panel opens in it -- the main bundle carries no article text. A language
 * with no articles (the pseudo-locale, a draft language) gets the English
 * ones, and says so, the way the import guide always did.
 */

import type { Article } from "./compile";

const LOADERS: Record<string, () => Promise<{ default: Article[] }>> = {
  en: () => import("virtual:help-articles/en"),
  "pt-BR": () => import("virtual:help-articles/pt-BR"),
  "es-ES": () => import("virtual:help-articles/es-ES"),
  "sv-SE": () => import("virtual:help-articles/sv-SE"),
};

/** The languages the articles are written in. */
export const HELP_LOCALES: readonly string[] = Object.keys(LOADERS);

export type Loaded = {
  /** The language the articles are in. */
  locale: string;
  articles: Article[];
  /** True when they are English standing in for the language asked for. */
  fallback: boolean;
};

/** The language whose articles a reader of `locale` gets. */
export function helpLocaleFor(locale: string): string {
  return locale in LOADERS ? locale : "en";
}

const cache = new Map<string, Promise<Article[]>>();

export function loadHelp(locale: string): Promise<Loaded> {
  const chosen = helpLocaleFor(locale);
  let pending = cache.get(chosen);
  if (!pending) {
    pending = LOADERS[chosen]().then((module) => module.default);
    // A failed fetch (a deploy replaced the chunk) is asked for again next time.
    pending.catch(() => cache.delete(chosen));
    cache.set(chosen, pending);
  }
  return pending.then((articles) => ({ locale: chosen, articles, fallback: chosen !== locale }));
}
