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
  // Lists are joined by `listText` (Intl's own conjunction outside English),
  // not by the catalog, so the pseudo-locale cannot accent it.
  "and",
]);

/** Words of plain ASCII letters, from the text and the labels people are given. */
export function untranslated(root: HTMLElement): string[] {
  const shown: string[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    // Code to type, not words to read.
    if ((node.parentElement?.closest(".mono, .codes, svg") ?? null) !== null) continue;
    shown.push(node.textContent ?? "");
  }
  for (const element of Array.from(root.querySelectorAll("[aria-label], [title], [placeholder]"))) {
    for (const name of ["aria-label", "title", "placeholder"]) {
      const value = element.getAttribute(name);
      if (value) shown.push(value);
    }
  }
  return shown
    .map((text) => text.replace(/\S+@\S+/g, " "))
    .flatMap((text) => text.split(/[\s.,;:!?()"'…·—–/-]+/))
    // A currency code is data, in every language.
    .filter((word) => /^[A-Za-z]{3,}$/.test(word) && !/^[A-Z]{3}$/.test(word) && !DATA.has(word));
}
