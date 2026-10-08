/**
 * What the catalogs may hold (#53).
 *
 * A draft translation is marked `#, fuzzy` until a native speaker has reviewed
 * it (#58), and a fuzzy entry is never served. Lingui itself ignores the flag
 * -- a fuzzy `msgstr` would be shown like any other -- so the rule is held
 * here, from both sides:
 *
 * - a catalog that is not served may hold translations only as fuzzy drafts;
 * - a catalog that is served may hold no fuzzy entry and no missing one.
 *
 * Which catalogs are served is `SERVED_LOCALES`; the loaders in `lib/i18n.ts`
 * are the only way a catalog reaches the bundle, and its tests show a draft
 * locale cannot be activated.
 */
import { describe, expect, it } from "vitest";
import config from "../../lingui.config";
import { PSEUDO_LOCALE, SERVED_LOCALES, SOURCE_LOCALE } from "../lib/i18n";

interface Entry {
  id: string;
  translation: string;
  fuzzy: boolean;
}

/** Enough of a PO reader for this: entries, their flags, and their msgstr. */
export function readPo(text: string): Entry[] {
  const unquote = (line: string) => JSON.parse(line.slice(line.indexOf('"')));
  const entries: Entry[] = [];
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.split("\n");
    let id = "";
    let translation = "";
    let field: "id" | "str" | null = null;
    let fuzzy = false;
    for (const line of lines) {
      if (line.startsWith("#,")) fuzzy ||= line.includes("fuzzy");
      else if (line.startsWith("msgid ")) [field, id] = ["id", unquote(line)];
      else if (line.startsWith("msgstr ")) [field, translation] = ["str", unquote(line)];
      else if (line.startsWith('"') && field === "id") id += unquote(line);
      else if (line.startsWith('"') && field === "str") translation += unquote(line);
    }
    if (field && id) entries.push({ id, translation, fuzzy });
  }
  return entries;
}

/** Every catalog's text, read as text rather than compiled. */
const FILES = import.meta.glob<string>("./*/messages.po", {
  query: "?raw",
  import: "default",
  eager: true,
});

function catalog(locale: string): Entry[] {
  return readPo(FILES[`./${locale}/messages.po`] ?? "");
}

const translated = config.locales.filter((one) => one !== SOURCE_LOCALE && one !== PSEUDO_LOCALE);

describe("the catalogs", () => {
  it("exist for every locale the config names", () => {
    for (const locale of config.locales) {
      expect(FILES[`./${locale}/messages.po`], locale).toBeTypeOf("string");
    }
  });

  it("all hold the same messages as the English one", () => {
    const english = catalog(SOURCE_LOCALE).map((one) => one.id).sort();
    expect(english.length).toBeGreaterThan(0);
    for (const locale of config.locales) {
      expect(catalog(locale).map((one) => one.id).sort(), locale).toEqual(english);
    }
  });

  it.each(translated)("%s holds its translations only as fuzzy drafts while it is not served", (locale) => {
    if (SERVED_LOCALES.includes(locale)) return;
    const served = catalog(locale).filter((one) => one.translation && !one.fuzzy);
    expect(served.map((one) => one.id)).toEqual([]);
  });

  it.each(translated)("%s, once served, has nothing fuzzy and nothing missing", (locale) => {
    if (!SERVED_LOCALES.includes(locale)) return;
    const unready = catalog(locale).filter((one) => one.fuzzy || !one.translation);
    expect(unready.map((one) => one.id)).toEqual([]);
  });

  it("serves no draft language yet", () => {
    expect(SERVED_LOCALES.filter((one) => translated.includes(one))).toEqual([]);
  });
});

describe("the PO reader", () => {
  it("sees a fuzzy flag and a continued msgstr", () => {
    const entries = readPo(
      ['#, fuzzy', 'msgid "Accounts"', 'msgstr ""', '"Con"', '"tas"', "", 'msgid "History"', 'msgstr ""'].join("\n"),
    );
    expect(entries).toEqual([
      { id: "Accounts", translation: "Contas", fuzzy: true },
      { id: "History", translation: "", fuzzy: false },
    ]);
  });
});
