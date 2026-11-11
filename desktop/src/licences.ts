/**
 * About → Licences (#63): the AGPL line, the exact source of this version,
 * and the third-party notices.
 *
 * The notices are written at build time by `scripts/third_party_notices.py`
 * from what the build actually bundled, and shipped beside the frozen backend
 * as an extra resource; the build refuses to package without them. The
 * window is plain HTML from a `data:` URL, like the splash: no script, no
 * preload, nothing to reach. Its one link goes to the system browser through
 * the shell's `will-navigate` guard, as every outside link does.
 */

import fs from "node:fs";
import path from "node:path";

/** The repository every release is tagged in. */
export const REPOSITORY = "https://github.com/MarioLonghi-com/household-spend-tracker";

/** The notices file's name, in the app's resources folder and in `build/` from source. */
export const NOTICES_FILE = "THIRD-PARTY-NOTICES.txt";

/** The tree exactly as released: the tag a version is published under. */
export function sourceLink(version: string): string {
  const clean = version.trim();
  if (!/^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$/.test(clean)) throw new Error(`not a release version: "${version}"`);
  return `${REPOSITORY}/tree/v${clean}`;
}

/** Where the notices are: the resources folder when packaged, `build/` beside the repository's root from source. */
export function noticesPath(resourcesPath: string | null, here: string = __dirname): string {
  if (resourcesPath) return path.join(resourcesPath, NOTICES_FILE);
  return path.join(here, "..", "..", "build", NOTICES_FILE);
}

/** The notices' text. Null when the file is not there or holds nothing: shown as such, never as a blank page. */
export function loadNotices(file: string): string | null {
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
  return text.trim() ? text : null;
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export type Words = {
  lang: string;
  dark: boolean;
  title: string;
  free: string;
  sourceLabel: string;
  link: string;
  noticesHeading: string;
  /** The notices, or null for the "did not ship" line. */
  notices: string | null;
  missing: string;
  where: string;
};

/** The window's page. Every word is escaped; the notices are a <pre>. */
export function page(words: Words): string {
  const dark = words.dark;
  return `<!doctype html><html lang="${escape(words.lang)}"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>${escape(words.title)}</title>
<style>
  html, body { margin: 0; font: 14px -apple-system, "Segoe UI", system-ui, sans-serif;
    background: ${dark ? "#1e1f22" : "#fafaf8"}; color: ${dark ? "#e8e8e6" : "#1d1d1b"}; }
  main { padding: 20px 24px 32px; max-width: 900px; }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
  h2 { font-size: 15px; font-weight: 600; margin: 24px 0 8px; }
  p { margin: 0 0 8px; line-height: 1.45; }
  a { color: ${dark ? "#8fb3ff" : "#3a62c8"}; word-break: break-all; }
  .where { opacity: .7; font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-word; font: 12px ui-monospace, Menlo, Consolas, monospace;
    line-height: 1.4; margin: 0; padding: 12px 14px; border-radius: 6px;
    background: ${dark ? "#26272b" : "#f1f1ed"}; color: inherit; -webkit-user-select: text; }
</style></head><body><main>
<h1>${escape(words.title)}</h1>
<p>${escape(words.free)}</p>
<p>${escape(words.sourceLabel)}<br><a href="${escape(words.link)}">${escape(words.link)}</a></p>
<h2>${escape(words.noticesHeading)}</h2>
<p class="where">${escape(words.where)}</p>
${words.notices === null ? `<p>${escape(words.missing)}</p>` : `<pre>${escape(words.notices)}</pre>`}
</main></body></html>`;
}
