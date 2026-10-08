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
import { compileMessageOrThrow } from "@lingui/message-utils/compileMessage";
import config from "../../lingui.config";
import { PSEUDO_LOCALE, SERVED_LOCALES, SOURCE_LOCALE } from "../lib/i18n";

interface Entry {
  id: string;
  translation: string;
  fuzzy: boolean;
  /** The translator notes, `#.` lines: a source `comment` (#228). */
  notes: string[];
  context: string;
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
    let context = "";
    const notes: string[] = [];
    for (const line of lines) {
      if (line.startsWith("#,")) fuzzy ||= line.includes("fuzzy");
      // Lingui's own "placeholder {0}: expr" lines are about the code, not a note.
      else if (line.startsWith("#. ") && !line.startsWith("#. placeholder ")) notes.push(line.slice(3));
      else if (line.startsWith("msgctxt ")) context = unquote(line);
      else if (line.startsWith("msgid ")) [field, id] = ["id", unquote(line)];
      else if (line.startsWith("msgstr ")) [field, translation] = ["str", unquote(line)];
      else if (line.startsWith('"') && field === "id") id += unquote(line);
      else if (line.startsWith('"') && field === "str") translation += unquote(line);
    }
    if (field && id) entries.push({ id, translation, fuzzy, notes, context });
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

/**
 * Short messages that need no note: the same word, the same job, on every
 * screen of every app. Reviewed; add to it only with a reason a translator
 * would accept (#228).
 */
const SELF_EXPLANATORY = new Set(["OK", "Cancel"]);

/** Words a reader sees, leaving out placeholders, tags and plural syntax. */
export function wordsIn(message: string): number {
  if (/\{\w+, (plural|select)/.test(message)) return 0;
  const plain = message.replace(/<\/?\d+\/?>/g, " ").replace(/\{[^{}]*\}/g, " ");
  return (plain.match(/[A-Za-z][A-Za-z'’-]*/g) ?? []).length;
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

  it.each(config.locales)("%s: every message it holds is valid ICU and keeps the source's placeholders", (locale) => {
    const source = new Map(catalog(SOURCE_LOCALE).map((one) => [one.id, one.translation]));
    const names = (text: string) => [...new Set(text.match(/\{(\w+)[,}]/g) ?? [])].sort();
    const tags = (text: string) => (text.match(/<\/?\d+>/g) ?? []).sort();
    for (const one of catalog(locale)) {
      if (!one.translation) continue;
      expect(() => compileMessageOrThrow(one.translation), `${locale}: ${one.id}`).not.toThrow();
      const english = source.get(one.id) ?? one.id;
      expect([names(one.translation), tags(one.translation)], `${locale}: ${one.id}`).toEqual([
        names(english),
        tags(english),
      ]);
    }
  });

  it("give every message of one or two words a translator note, or a reviewed reason not to", () => {
    // A translator sees only the English. "Balance", "Split" or "Open" alone
    // could be a button, a heading, a column or a state, and in finance most
    // have more than one meaning (#228).
    const bare = catalog(SOURCE_LOCALE)
      .filter((one) => wordsIn(one.id) >= 1 && wordsIn(one.id) <= 2)
      .filter((one) => one.notes.length === 0 && !SELF_EXPLANATORY.has(one.id))
      .map((one) => one.id);
    expect(bare).toEqual([]);
  });

  it.each(config.locales)("%s carries the same translator notes as the English catalog", (locale) => {
    const key = (one: Entry) => `${one.context}\u0004${one.id}`;
    const english = new Map(catalog(SOURCE_LOCALE).map((one) => [key(one), one.notes]));
    for (const one of catalog(locale)) expect(one.notes, `${locale}: ${one.id}`).toEqual(english.get(key(one)));
  });

  it("keeps each translator note to one short line", () => {
    const long = catalog(SOURCE_LOCALE)
      .flatMap((one) => one.notes)
      .filter((note) => note.length > 120);
    expect(long).toEqual([]);
  });

  it("serves no draft language yet", () => {
    expect(SERVED_LOCALES.filter((one) => translated.includes(one))).toEqual([]);
  });
});

describe("counting a message's words", () => {
  it("leaves out placeholders, tags and plurals", () => {
    expect(wordsIn("Balance")).toBe(1);
    expect(wordsIn("Rename {0}")).toBe(1);
    expect(wordsIn("From <0>{where}</0>.")).toBe(1);
    expect(wordsIn("{0}")).toBe(0);
    expect(wordsIn("{count, plural, one {# day} other {# days}}")).toBe(0);
    expect(wordsIn("Save receipt notes")).toBe(3);
  });
});

describe("the PO reader", () => {
  it("sees a translator note and a context", () => {
    const [entry] = readPo(['#. Column heading', 'msgctxt "noun"', 'msgid "Transfer"', 'msgstr ""'].join("\n"));
    expect([entry.notes, entry.context]).toEqual([["Column heading"], "noun"]);
  });

  it("sees a fuzzy flag and a continued msgstr", () => {
    const entries = readPo(
      ['#, fuzzy', 'msgid "Accounts"', 'msgstr ""', '"Con"', '"tas"', "", 'msgid "History"', 'msgstr ""'].join("\n"),
    );
    expect(entries).toEqual([
      { id: "Accounts", translation: "Contas", fuzzy: true, notes: [], context: "" },
      { id: "History", translation: "", fuzzy: false, notes: [], context: "" },
    ]);
  });
});
