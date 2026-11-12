/**
 * Keeping the help articles true (#460 §7, §8).
 *
 * - every article compiles, and its file is named after its `id`;
 * - no two articles in one language share an `id`;
 * - every served language has every English article, and none English lacks;
 * - `screens:` and `screen:` links name screens the shell has, `help:` links
 *   name articles that exist, `requires:` names a capability that exists;
 * - an article's screens and requirement are the same in every language, so
 *   the **?** on a screen offers the same articles whatever the language;
 * - a label quoted in bold is a message of that language's catalog, so
 *   renaming a button fails here until the articles follow.
 */

import { describe, expect, it } from "vitest";
import { compile, labels, links, type Article } from "./compile";
import { isScreen } from "../lib/screens";
import { EVERYTHING } from "../lib/capabilities";
import { SERVED_LOCALES, SOURCE_LOCALE, TRANSLATED_LOCALES } from "../lib/i18n";
import { readPo } from "../lib/po";

const FILES = import.meta.glob<string>("./*/*.md", { query: "?raw", import: "default", eager: true });
const CATALOGS = import.meta.glob<string>("../locales/*/messages.po", {
  query: "?raw",
  import: "default",
  eager: true,
});

type Found = { file: string; locale: string; article: Article };

const found: Found[] = Object.entries(FILES).map(([path, source]) => {
  const [, locale, file] = path.match(/^\.\/([^/]+)\/([^/]+)\.md$/)!;
  return { file, locale, article: compile(source, path) };
});

function inLanguage(locale: string): Found[] {
  return found.filter((one) => one.locale === locale);
}

/** What a language's catalog shows: each message's translation (English's own words for English). */
function shownWords(locale: string): Set<string> {
  const text = CATALOGS[`../locales/${locale}/messages.po`] ?? "";
  return new Set(readPo(text).map((entry) => entry.translation).filter(Boolean));
}

describe("the help articles", () => {
  it("are written in exactly the served languages", () => {
    expect([...new Set(found.map((one) => one.locale))].sort()).toEqual([...SERVED_LOCALES].sort());
  });

  it("each have a file named after their id", () => {
    const misnamed = found.filter((one) => one.file !== one.article.id).map((one) => `${one.locale}/${one.file}.md`);
    expect(misnamed).toEqual([]);
  });

  it("never share an id within one language", () => {
    for (const locale of SERVED_LOCALES) {
      const ids = inLanguage(locale).map((one) => one.article.id);
      expect(ids.filter((id, at) => ids.indexOf(id) !== at)).toEqual([]);
    }
  });

  it.each(TRANSLATED_LOCALES)("are all in %s, and nothing English lacks", (locale) => {
    const english = inLanguage(SOURCE_LOCALE).map((one) => one.article.id).sort();
    const translated = inLanguage(locale).map((one) => one.article.id).sort();
    expect(translated).toEqual(english);
  });

  it("name the same screens and need the same capability in every language", () => {
    const english = new Map(inLanguage(SOURCE_LOCALE).map((one) => [one.article.id, one.article]));
    const differ = found
      .filter((one) => english.has(one.article.id))
      .filter((one) => {
        const source = english.get(one.article.id)!;
        return (
          JSON.stringify([...one.article.screens].sort()) !== JSON.stringify([...source.screens].sort()) ||
          one.article.requires !== source.requires
        );
      })
      .map((one) => `${one.locale}/${one.article.id}`);
    expect(differ).toEqual([]);
  });

  it("name only screens the shell has", () => {
    const wrong = found.flatMap((one) =>
      one.article.screens.filter((screen) => !isScreen(screen)).map((screen) => `${one.locale}/${one.article.id}: ${screen}`),
    );
    expect(wrong).toEqual([]);
    // Every article is on at least one screen's ?, or only search finds it.
    expect(found.filter((one) => one.article.screens.length === 0).map((one) => one.article.id)).toEqual([]);
  });

  it("need only capabilities that exist", () => {
    const wrong = found
      .filter((one) => one.article.requires && !(one.article.requires in EVERYTHING))
      .map((one) => `${one.locale}/${one.article.id}: ${one.article.requires}`);
    expect(wrong).toEqual([]);
  });

  it("link only to articles and screens that exist", () => {
    const wrong: string[] = [];
    for (const one of found) {
      const ids = new Set(inLanguage(one.locale).map((other) => other.article.id));
      for (const href of links(one.article.blocks)) {
        if (href.startsWith("help:") && !ids.has(href.slice(5))) wrong.push(`${one.locale}/${one.article.id}: ${href}`);
        if (href.startsWith("screen:") && !isScreen(href.slice(7))) wrong.push(`${one.locale}/${one.article.id}: ${href}`);
      }
    }
    expect(wrong).toEqual([]);
  });

  it.each(SERVED_LOCALES)("quote in bold, in %s, only labels that language's catalog shows", (locale) => {
    const words = shownWords(locale);
    expect(words.size).toBeGreaterThan(1000);
    const wrong = inLanguage(locale).flatMap((one) =>
      labels(one.article.blocks)
        .filter((label) => !words.has(label))
        .map((label) => `${one.article.id}: **${label}**`),
    );
    expect(wrong).toEqual([]);
  });

  it("have something to say", () => {
    const thin = found.filter((one) => one.article.text.length < 200).map((one) => `${one.locale}/${one.article.id}`);
    expect(thin).toEqual([]);
  });
});
