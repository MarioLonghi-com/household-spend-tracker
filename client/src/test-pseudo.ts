/**
 * For the en-XA tests (#54-#56): every word a screen shows that nobody
 * extracted. A word made only of plain ASCII letters is one the catalog did
 * not reach; data and the product's name are exempt.
 */

/** Shown as typed or as sent, in every language. */
export const DATA = new Set([
  "Spend",
  "Tracker",
  "Robin",
  "Sam",
  "robin@example.com",
  "Casa",
  "Doe",
  "Laptop",
  "SPENDTRACKER_SECRET_KEY",
  "secret.key",
  "ABCDEFGH",
  "Claude",
  "Desktop",
  "HTTPS",
  // Stays as it is in every language (glossary).
  "IBAN",
  // A bank's own words, as its files write them.
  "Current",
  "Savings",
  // Lists are joined by `listText` (Intl's own conjunction outside English),
  // not by the catalog, so the pseudo-locale cannot accent it. `orText` is the
  // same for alternatives.
  "and",
  "or",
  // Month names come from Intl in the format locale, which the tests pin to
  // en-US; the pseudo-locale has no calendar of its own to give them.
  ..."January February March April May June July August September October November December".split(" "),
  ..."Jan Feb Mar Apr Jun Jul Aug Sep Oct Nov Dec".split(" "),
]);

/** Words of plain ASCII letters, from the text and the labels people are given. */
export function untranslated(root: HTMLElement): string[] {
  const shown: string[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    // Code to type, not words to read -- and a block that says which language
    // it is in, like a guide not yet written in this one, is in that language.
    if ((node.parentElement?.closest(".mono, .codes, svg, [lang]:not(html)") ?? null) !== null) continue;
    shown.push(node.textContent ?? "");
  }
  for (const element of Array.from(root.querySelectorAll("[aria-label], [title], [placeholder], [data-label]"))) {
    if (element.closest("[lang]:not(html)")) continue;
    // data-label is what a phone shows beside each cell of a table row.
    for (const name of ["aria-label", "title", "placeholder", "data-label"]) {
      const value = element.getAttribute(name);
      if (value) shown.push(value);
    }
  }
  return shown
    .map((text) => text.replace(/\S+@\S+/g, " "))
    .flatMap((text) => text.split(/[\s.,;:!?()"'…·—–/-]+/))
    // A currency code is data, in every language.
    // Two letters and up: "of", "to" and "in" are English too. A code in
    // capitals (EUR, ES) is data.
    .filter((word) => /^[A-Za-z]{2,}$/.test(word) && !/^[A-Z]{2,3}$/.test(word) && !DATA.has(word));
}
