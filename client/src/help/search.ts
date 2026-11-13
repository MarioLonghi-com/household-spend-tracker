/**
 * "How do I…?" answered from the articles (#460 §5).
 *
 * MiniSearch: pure JavaScript, no dependencies, no `eval`, so nothing the CSP
 * has to allow. Prefix and fuzzy matching, with the title and the `also:`
 * phrasings weighted above the body. No stemming yet: revisit it with real
 * questions.
 *
 * Accents are folded per language, so "configuracion" finds "configuración"
 * and "categoria" finds "categoría" -- except the
 * letters a language counts as letters of their own. Swedish å, ä and ö are
 * not an a or an o with a mark on (`år` and `ar` are different words), so in
 * Swedish they are kept and only the marks Swedish borrows (é, ü…) are
 * folded. Words that say nothing about the task ("how do I") are left out of
 * both the index and the question.
 */

import MiniSearch from "minisearch";
import type { Article } from "./compile";

/** Small words, per language, that would otherwise match every article. */
const STOP_WORDS: Record<string, readonly string[]> = {
  en: ["a", "an", "and", "the", "to", "of", "in", "on", "for", "is", "it", "i", "do", "how", "can", "my", "what", "with", "or", "be", "you", "your", "this", "that", "at", "as", "by", "from"],
  "pt-BR": ["a", "o", "as", "os", "um", "uma", "e", "de", "do", "da", "dos", "das", "em", "no", "na", "para", "por", "com", "que", "como", "eu", "meu", "minha", "se", "é", "ao", "ou", "você", "seu", "sua"],
  "es-ES": ["a", "el", "la", "los", "las", "un", "una", "y", "de", "del", "en", "para", "por", "con", "que", "como", "cómo", "yo", "mi", "mis", "se", "es", "al", "o", "tu", "tus", "lo"],
  "sv-SE": ["en", "ett", "och", "att", "i", "på", "för", "av", "med", "som", "hur", "jag", "min", "mitt", "mina", "det", "den", "är", "till", "om", "eller", "du", "din", "kan", "gör"],
};

/** Letters a language keeps, accent and all. */
const OWN_LETTERS: Record<string, RegExp> = {
  "sv-SE": /[åäö]/g,
};

/** A word as the index compares it: lower case, accents folded the language's way. */
export function fold(word: string, locale: string): string {
  const lower = word.toLocaleLowerCase(locale);
  const own = OWN_LETTERS[locale];
  const kept: string[] = [];
  // Set the language's own letters aside, fold the rest, put them back.
  const marked = own ? lower.replace(own, (letter) => `\u0000${kept.push(letter) - 1}\u0000`) : lower;
  const folded = marked.normalize("NFD").replace(/\p{M}/gu, "");
  return own ? folded.replace(/\u0000(\d+)\u0000/g, (_, at: string) => kept[Number(at)]) : folded;
}

export type HelpIndex = {
  search: (question: string) => Article[];
};

/** An index over one language's articles. */
export function indexArticles(articles: Article[], locale: string): HelpIndex {
  const stop = new Set((STOP_WORDS[locale] ?? STOP_WORDS.en).map((word) => fold(word, locale)));
  const processTerm = (term: string) => {
    const folded = fold(term, locale);
    return folded.length === 0 || stop.has(folded) ? null : folded;
  };
  const engine = new MiniSearch<Article>({
    fields: ["title", "also", "text"],
    storeFields: [],
    extractField: (article, field) => {
      const value = article[field as keyof Article];
      return Array.isArray(value) ? value.join(" \n ") : String(value ?? "");
    },
    processTerm,
    searchOptions: {
      boost: { title: 3, also: 2 },
      prefix: true,
      fuzzy: 0.2,
      combineWith: "OR",
      processTerm,
    },
  });
  engine.addAll(articles);
  const byId = new Map(articles.map((article) => [article.id, article]));
  return {
    search: (question) =>
      question.trim()
        ? engine.search(question).flatMap((hit) => (byId.has(hit.id) ? [byId.get(hit.id)!] : []))
        : [],
  };
}
