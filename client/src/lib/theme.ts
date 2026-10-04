/**
 * Painting a household's colours onto the page.
 *
 * The colours arrive from the server already resolved -- palette, plus this
 * household's accent adjusted to a shade that works in each scheme. Nothing
 * here decides what a colour should be; it only puts what it was given onto
 * `:root`, where `styles.css` is already reading it.
 *
 * Both schemes are written at once, into a stylesheet rather than onto the
 * element's inline style, so the browser keeps choosing between them with the
 * `prefers-color-scheme` query the stylesheet already has. Setting one scheme's
 * values inline would pin the app to whichever scheme was current when the
 * household loaded, and it would stop following the system at sunset.
 *
 * It emits the same three-rule shape `styles.css` uses, for the same reason --
 * a person who has chosen light or dark in Your account must get their
 * household's colours for *that* scheme, not for the one their laptop is in.
 * Generated from one object, so unlike the static sheet these two cannot drift.
 * See `lib/appearance.ts`.
 */

import type { Scheme } from "./types";

const STYLE_ID = "household-colours";

/**
 * What may be written into the sheet, checked here as well as on the server.
 *
 * This `<style>` is the reason the CSP keeps `style-src 'unsafe-inline'`, so
 * it is the one place a value from the server is pasted into CSS as text. The
 * server validates colours as hex before storing them; this is the second
 * lock, so that a server bug, an old row or a tampered response cannot close
 * the rule and write one of its own (`red; } body { ... `). A name must be a
 * plain lower-case identifier and a value exactly `#rrggbb`; anything else is
 * left out and the stylesheet's default for that variable stands.
 */
const NAME = /^[a-z][a-z0-9_]*$/;
const HEX = /^#[0-9a-fA-F]{6}$/;

export function safeEntries(scheme: Scheme): [string, string][] {
  return Object.entries(scheme as unknown as Record<string, unknown>).filter(
    (entry): entry is [string, string] =>
      NAME.test(entry[0]) && typeof entry[1] === "string" && HEX.test(entry[1]),
  );
}

function block(selector: string, scheme: Scheme): string {
  const lines = safeEntries(scheme)
    .map(([name, value]) => `  --${name.replace(/_/g, "-")}: ${value};`)
    .join("\n");
  return `${selector} {\n${lines}\n}`;
}

/** Put a household's colours on the page. Passing null returns to the defaults. */
export function applyColours(colours: { light: Scheme; dark: Scheme } | null): void {
  const existing = document.getElementById(STYLE_ID);
  if (!colours) {
    existing?.remove();
    return;
  }

  const sheet = existing ?? document.createElement("style");
  sheet.id = STYLE_ID;
  sheet.textContent = [
    block(":root", colours.light),
    // Dark when the system says so and nobody overrode it...
    `@media (prefers-color-scheme: dark) {\n${block('  :root:not([data-theme="light"])', colours.dark)}\n}`,
    // ...and dark because somebody asked for it. Last, so it wins the tie.
    block(':root[data-theme="dark"]', colours.dark),
  ].join("\n");
  if (!existing) document.head.append(sheet);
}
