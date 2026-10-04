/**
 * Light, dark, or whatever the system is doing.
 *
 * Until now the app followed `prefers-color-scheme` and offered no way out,
 * which is the right default -- a household ledger is opened at all hours --
 * and the wrong only option. Someone reading a statement on a bright train
 * wants light at 9pm; someone doing the books in bed wants dark at noon.
 *
 * ## Why an attribute and a media query rather than one or the other
 *
 * The obvious implementation resolves the choice in JavaScript and writes the
 * answer onto `:root` -- one code path, no duplicated CSS. It is not available
 * here: `main.py` sends `script-src 'self'` with no `'unsafe-inline'`, so the
 * pre-paint inline script that stops the page flashing the wrong scheme cannot
 * exist. `app/static/snap/index.html` carries a comment about the same CSP
 * costing it a single-file page.
 *
 * So the stylesheet keeps its `@media (prefers-color-scheme: dark)` block and
 * that block is what runs on a first paint. This module only writes
 * `data-theme` when the person has actually chosen a side, and the CSS is
 * written so the attribute wins:
 *
 *     @media (prefers-color-scheme: dark) {
 *       :root:not([data-theme="light"]) { ...dark... }
 *     }
 *     :root[data-theme="dark"] { ...dark... }
 *
 * Equal specificity, so the later rule wins the tie; the `:not()` is what lets
 * a person force light while their laptop is dark. `tests/styles-agree.test.ts`
 * asserts those two blocks declare the same thing, because they are the same
 * thing written twice and drift between them would show up as one wrong colour
 * on one screen in one mode.
 *
 * ## Per device, not per account
 *
 * It lives in `localStorage`, like `/snap`'s household pin: it is a fact about
 * the screen you are looking at, not about who you are, and the phone in a dark
 * kitchen and the desktop at the window deserve different answers. That also
 * means every read is in a `try` -- a private window or blocked site data
 * throws rather than returning null, and the app has to render anyway.
 */

/** What the person chose. `system` means "keep following the OS". */
export type Appearance = "light" | "dark" | "system";

export const APPEARANCE_KEY = "spendtracker.appearance";

const CHOICES: readonly Appearance[] = ["light", "dark", "system"];

export function isAppearance(value: unknown): value is Appearance {
  return typeof value === "string" && (CHOICES as readonly string[]).includes(value);
}

/**
 * What is stored, or `system`.
 *
 * Anything unrecognised is treated as no choice at all rather than repaired or
 * reported: the only ways to get a bad value here are a hand-edited store or a
 * future version that wrote something this one does not know, and in both cases
 * following the system is the answer that cannot be wrong.
 */
export function storedAppearance(): Appearance {
  try {
    const found = window.localStorage.getItem(APPEARANCE_KEY);
    return isAppearance(found) ? found : "system";
  } catch {
    return "system";
  }
}

function remember(choice: Appearance): void {
  try {
    // `system` removes the key rather than storing the word. A device that has
    // never chosen and one that chose "follow along" want identical behaviour
    // for ever, so they should not be two different states.
    if (choice === "system") window.localStorage.removeItem(APPEARANCE_KEY);
    else window.localStorage.setItem(APPEARANCE_KEY, choice);
  } catch {
    /* a private window; the choice still applies for this page's life */
  }
}

/**
 * Put the choice on the document.
 *
 * `system` removes the attribute entirely rather than setting it to "system",
 * so the CSS has one condition to test instead of two and an older stylesheet
 * seeing an attribute it does not know cannot get it wrong.
 */
export function applyAppearance(choice: Appearance, root: HTMLElement = document.documentElement): void {
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

/** Choose, remember, and paint. The one call a control needs. */
export function chooseAppearance(choice: Appearance, root?: HTMLElement): void {
  remember(choice);
  applyAppearance(choice, root);
}

/** Whether the *system* is dark, whatever the person has chosen. */
export function systemIsDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

/** Which scheme is actually on screen, once the choice is taken into account. */
export function effectiveScheme(choice: Appearance = storedAppearance()): "light" | "dark" {
  if (choice !== "system") return choice;
  return systemIsDark() ? "dark" : "light";
}

/**
 * Call the handler when the *system* scheme changes.
 *
 * Only useful to something that reads a colour in JavaScript rather than
 * leaving it to CSS -- `/snap` picks the household accent that way, and until
 * this existed it kept the scheme it was born with until the page was reloaded.
 * Returns its own unsubscribe.
 */
export function watchSystemScheme(handler: (dark: boolean) => void): () => void {
  let query: MediaQueryList;
  try {
    query = window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    return () => {};
  }
  const onChange = (event: MediaQueryListEvent) => handler(event.matches);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}
