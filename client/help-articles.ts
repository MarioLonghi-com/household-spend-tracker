/**
 * The help articles, compiled at build time (#460).
 *
 * `src/help/<locale>/<id>.md` are the source: Markdown with front matter,
 * one file per task per language (`src/help/compile.ts` has the format).
 * This plugin turns them into what the client and the server read, so neither
 * parses Markdown at run time:
 *
 * - `virtual:help-articles/<locale>` -- one language's articles as a block
 *   tree, imported lazily by `src/help/load.ts`, so each language is a chunk
 *   of its own and the main bundle carries none of the text;
 * - `virtual:help-index` -- which screens and which capability each article
 *   is about, a few hundred bytes in the main bundle, so the **?** in a
 *   screen's heading knows whether there is anything to open before any
 *   language's chunk has loaded;
 * - `help/<locale>.json` in the build output -- the same articles with the
 *   body as written, for the agent API (`app/services/help.py`), which reads
 *   them from `app/static/dist/help/`. The image and the desktop bundle both
 *   carry the build; a checkout that never built the client has the server
 *   read the Markdown itself instead.
 *
 * A file that does not compile fails the build, with its path.
 */

import fs from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";
import { compile, sourceOf } from "./src/help/compile.ts";

const ARTICLES = "virtual:help-articles/";
const INDEX = "virtual:help-index";
const RESOLVED = "\0";

/** The languages that have a folder of articles. */
export function helpLocales(root: string): string[] {
  if (!fs.existsSync(root)) return [];
  return fs
    .readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.readdirSync(path.join(root, entry.name)).some((f) => f.endsWith(".md")))
    .map((entry) => entry.name)
    .sort();
}

/** One language's files, by name. */
export function helpFiles(root: string, locale: string): string[] {
  const folder = path.join(root, locale);
  return fs
    .readdirSync(folder)
    .filter((name) => name.endsWith(".md"))
    .sort()
    .map((name) => path.join(folder, name));
}

export function helpArticles(root: string): Plugin {
  return {
    name: "help-articles",
    resolveId(id) {
      if (id === INDEX || id.startsWith(ARTICLES)) return RESOLVED + id;
      return null;
    },
    load(id) {
      if (!id.startsWith(RESOLVED)) return null;
      const name = id.slice(RESOLVED.length);
      if (name === INDEX) {
        // English decides: articles.test.ts holds every language to the
        // same screens and requirement for one id.
        const index: Record<string, { screens: string[]; requires?: string }> = {};
        for (const file of helpFiles(root, "en")) {
          this.addWatchFile(file);
          const { id: key, screens, requires } = compile(fs.readFileSync(file, "utf8"), file);
          index[key] = requires ? { screens, requires } : { screens };
        }
        return `export default ${JSON.stringify(index)};`;
      }
      if (name.startsWith(ARTICLES)) {
        const locale = name.slice(ARTICLES.length);
        if (!helpLocales(root).includes(locale)) this.error(`no help articles in ${locale}`);
        const articles = helpFiles(root, locale).map((file) => {
          this.addWatchFile(file);
          return compile(fs.readFileSync(file, "utf8"), file);
        });
        return `export default ${JSON.stringify(articles)};`;
      }
      return null;
    },
    generateBundle() {
      for (const locale of helpLocales(root)) {
        const articles = helpFiles(root, locale).map((file) => sourceOf(fs.readFileSync(file, "utf8"), file));
        this.emitFile({
          type: "asset",
          fileName: `help/${locale}.json`,
          source: JSON.stringify({ locale, articles }),
        });
      }
    },
  };
}
