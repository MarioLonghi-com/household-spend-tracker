/**
 * What a screen must not show in a language other than English (#271), read
 * out of the page as it is drawn. Shared by the per-language walk and the
 * en-XA walk; see `locales.spec.ts`.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";

const HERE = dirname(fileURLToPath(import.meta.url));

/** msgid and msgstr of every entry, enough of a PO reader for this. */
export function catalog(locale: string): Map<string, string> {
  const text = readFileSync(join(HERE, "..", "src", "locales", locale, "messages.po"), "utf8");
  const found = new Map<string, string>();
  for (const block of text.split(/\n\s*\n/)) {
    let id = "";
    let str = "";
    let field: "id" | "str" | null = null;
    for (const line of block.split("\n")) {
      const unquote = () => JSON.parse(line.slice(line.indexOf('"')));
      if (line.startsWith("msgid ")) [field, id] = ["id", unquote()];
      else if (line.startsWith("msgstr ")) [field, str] = ["str", unquote()];
      else if (line.startsWith('"') && field === "id") id += unquote();
      else if (line.startsWith('"') && field === "str") str += unquote();
    }
    if (id) found.set(id, str);
  }
  return found;
}

/**
 * The English messages a translated screen should never show: whole messages
 * with no placeholder, tag or plural, that the locale's catalog translates to
 * something else. A screen showing one of these word for word is a message
 * that missed the catalog -- or a sentence the server sent in English.
 */
export function englishOnly(locale: string): string[] {
  const translated = catalog(locale);
  return [...translated.entries()]
    .filter(([id, str]) => str && str !== id && !/[{}<>]/.test(id) && /[A-Za-z]{3}/.test(id))
    .map(([id]) => id);
}

export interface Findings {
  /** A message shown in English in a translated screen. */
  english: string[];
  /** Text with English function words in it: a sentence nobody extracted. */
  sentences: string[];
  /** `{0}`, `{name}`, `<0>`: a placeholder or tag that was not filled. */
  placeholders: string[];
  /** The page scrolls sideways. */
  pageScroll: boolean;
  /** A label, button, heading or cell whose text is wider than its box. */
  overflowing: string[];
}

/** Read one screen. `english` is `englishOnly(locale)`, or [] for English itself. */
export async function inspect(page: Page, english: string[]): Promise<Findings> {
  return page.evaluate((englishOnly) => {
    const skip = (node: Element | null) =>
      !!node?.closest("[lang]:not(html), .mono, code, pre, svg, script, style, [hidden], [aria-hidden='true']");
    const visible = (element: Element) => {
      const box = element.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== "hidden";
    };
    const texts: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || skip(parent) || !visible(parent)) continue;
      const text = (node.textContent ?? "").replace(/\s+/g, " ").trim();
      if (text) texts.push(text);
    }
    for (const element of Array.from(document.querySelectorAll("[aria-label], [title], [placeholder]"))) {
      if (skip(element)) continue;
      for (const name of ["aria-label", "title", "placeholder"]) {
        const value = element.getAttribute(name)?.trim();
        if (value) texts.push(value);
      }
    }
    const wanted = new Set(englishOnly);
    const english = [...new Set(texts.filter((text) => wanted.has(text)))];

    // Words English has and these three languages do not.
    const FUNCTION = new Set([
      "the", "you", "your", "is", "are", "this", "that", "with", "will", "not", "and", "of",
      "to", "it", "has", "have", "was", "were", "from", "there", "what", "which", "when",
      "here", "its", "can", "cannot", "does", "would", "into",
    ]);
    const sentences = [
      ...new Set(
        texts.filter((text) => text.toLowerCase().split(/[^a-z']+/).filter((word) => FUNCTION.has(word)).length >= 2),
      ),
    ];
    const placeholders = [...new Set(texts.filter((text) => /\{\w*\}|<\/?\d+\/?>/.test(text)))];

    // Whether the page, or the shell's scrolling main, can be moved sideways.
    // Not `scrollWidth`: a wide table inside its own scroller counts there
    // without the page moving at all.
    const sideways = (element: Element | null) => {
      if (!element) return false;
      const before = element.scrollLeft;
      element.scrollLeft = 10_000;
      const moved = element.scrollLeft > 0;
      element.scrollLeft = before;
      return moved;
    };
    const pageScroll = sideways(document.scrollingElement) || sideways(document.querySelector("main"));
    const overflowing: string[] = [];
    const boxes = "button, th, h1, h2, h3, h4, label, legend, summary, .chip, .tag, .pill, .nav-head, .nav-child, .banner, .stat, dt, dd";
    for (const element of Array.from(document.querySelectorAll<HTMLElement>(boxes))) {
      if (skip(element) || !visible(element)) continue;
      const style = getComputedStyle(element);
      if (style.overflowX === "auto" || style.overflowX === "scroll") continue;
      const clippedDown =
        (style.overflowY === "hidden" || style.overflowY === "clip") && element.scrollHeight > element.clientHeight + 1;
      if (element.scrollWidth > element.clientWidth + 1 || clippedDown) {
        overflowing.push(`${element.tagName.toLowerCase()}${element.className ? "." + String(element.className).split(" ")[0] : ""}: ${(element.textContent ?? "").trim().slice(0, 60)}`);
      }
    }
    return { english, sentences, placeholders, pageScroll, overflowing };
  }, english);
}

/** The accounts `l10n.setup.ts` makes: names are data, and stay as typed. */
export const QA_ACCOUNTS = [
  { name: "QA krona", currency: "SEK", minor: 123456 },
  { name: "QA real", currency: "BRL", minor: 123456 },
  { name: "QA euro", currency: "EUR", minor: 123456 },
  { name: "QA euro large", currency: "EUR", minor: 1234567 },
];

/**
 * Names the demo seed writes in English. They are the household's own data --
 * a category group, a category, the opening-balance payee -- and stay as they
 * were typed in every language; a household made after #268 gets them in its
 * own language.
 */
export const SEEDED_DATA = new Set(["Opening balance", "Income", "Household"]);
