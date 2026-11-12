/**
 * A help article, from Markdown to the block tree the panel draws (#460).
 *
 * Runs at build time, inside the Vite plugin (`client/help-articles.ts`), and
 * in the tests. Nothing here reaches the browser: the client receives the
 * tree, already built, and `HelpArticle.tsx` turns it into elements. So there
 * is no Markdown parser at run time, no `dangerouslySetInnerHTML`, and nothing
 * the CSP would have to allow.
 *
 * ## The format
 *
 * One file per task per language, `src/help/<locale>/<id>.md`:
 *
 *     ---
 *     id: split-transaction
 *     title: Split a transaction
 *     screens: [register]
 *     also:
 *       - two categories for one purchase
 *       - divide a receipt
 *     requires: snap
 *     ---
 *
 * - `id` is the same in every language; `title` is the language's own.
 * - `screens` are the shell's screen names (`App.tsx`): the **?** on each of
 *   those screens lists the article.
 * - `also` is how else a person might ask. Searched, never shown.
 * - `requires`, optional, is a capability (`lib/capabilities.ts`): the
 *   article is left out where the deployment does not offer it.
 *
 * The body is a small, strict subset of Markdown, and anything outside it is
 * an error at build time rather than a page that renders oddly:
 *
 * - `## Heading` and `### Heading` (the title is the article's only `#`);
 * - paragraphs; `- item` and `1. item` lists, one level, an item continued
 *   on lines indented by two spaces or more;
 * - `**Label**` -- **only** for a label the app shows, exactly as its
 *   catalog has it in that language (a test holds every one to the catalog);
 * - `*emphasis*`, `` `code` ``, `<kbd>Esc</kbd>`;
 * - `[words](help:<id>)` to another article, `[words](screen:<name>)` to a
 *   screen of the app (following it closes the panel), and `https://` links.
 */

/** Words, as the renderer draws them. A string is plain text. */
export type Inline =
  | string
  | { t: "strong"; c: Inline[] }
  | { t: "em"; c: Inline[] }
  | { t: "code"; v: string }
  | { t: "kbd"; v: string }
  | { t: "link"; href: string; c: Inline[] };

export type Block =
  | { t: "h"; level: 2 | 3; c: Inline[] }
  | { t: "p"; c: Inline[] }
  | { t: "ul" | "ol"; items: Inline[][] };

/** What the front matter says. */
export type Meta = {
  id: string;
  title: string;
  screens: string[];
  also: string[];
  requires?: string;
};

/** One article, compiled: what the client chunk carries. */
export type Article = Meta & {
  blocks: Block[];
  /** Every word of the body as plain text, for the search index. */
  text: string;
};

/** One article as the agent API serves it: the body as written. */
export type ArticleSource = Meta & { markdown: string };

export class HelpSyntaxError extends Error {}

const LINK = /^(help:[a-z0-9-]+|screen:[a-z0-9-]+|https:\/\/\S+)$/;

/** The front matter and the body, apart. */
export function split(source: string, where = "article"): { meta: Meta; body: string } {
  const text = source.replace(/\r\n/g, "\n");
  const match = text.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!match) throw new HelpSyntaxError(`${where}: no front matter between --- lines`);
  const fields: Record<string, string | string[]> = {};
  let listing: string | null = null;
  for (const raw of match[1].split("\n")) {
    if (!raw.trim()) continue;
    const item = raw.match(/^\s+-\s+(.+)$/);
    if (item && listing) {
      (fields[listing] as string[]).push(item[1].trim());
      continue;
    }
    const pair = raw.match(/^([a-z]+):\s*(.*)$/);
    if (!pair) throw new HelpSyntaxError(`${where}: cannot read front matter line "${raw}"`);
    const [, key, value] = pair;
    if (key in fields) throw new HelpSyntaxError(`${where}: ${key} twice`);
    if (value === "") {
      fields[key] = [];
      listing = key;
    } else if (value.startsWith("[")) {
      if (!value.endsWith("]")) throw new HelpSyntaxError(`${where}: ${key} has no closing ]`);
      fields[key] = value
        .slice(1, -1)
        .split(",")
        .map((one) => one.trim())
        .filter(Boolean);
      listing = null;
    } else {
      fields[key] = value.trim();
      listing = null;
    }
  }
  const known = new Set(["id", "title", "screens", "also", "requires"]);
  for (const key of Object.keys(fields)) {
    if (!known.has(key)) throw new HelpSyntaxError(`${where}: unknown front matter "${key}"`);
  }
  const one = (key: string): string | undefined => {
    const value = fields[key];
    if (value === undefined) return undefined;
    if (typeof value !== "string") throw new HelpSyntaxError(`${where}: ${key} is one value, not a list`);
    return value;
  };
  const many = (key: string): string[] => {
    const value = fields[key];
    if (value === undefined) return [];
    return typeof value === "string" ? [value] : value;
  };
  const id = one("id");
  const title = one("title");
  if (!id || !/^[a-z0-9]+(-[a-z0-9]+)*$/.test(id)) throw new HelpSyntaxError(`${where}: id must be lower-case words joined by -`);
  if (!title) throw new HelpSyntaxError(`${where}: no title`);
  const requires = one("requires");
  return {
    meta: { id, title, screens: many("screens"), also: many("also"), ...(requires ? { requires } : {}) },
    body: match[2].trim(),
  };
}

/** The inline marks of one stretch of text. */
export function inline(text: string, where = "article"): Inline[] {
  const out: Inline[] = [];
  let plain = "";
  const flush = () => {
    if (plain) out.push(plain);
    plain = "";
  };
  let at = 0;
  while (at < text.length) {
    const rest = text.slice(at);
    const ch = text[at];
    if (ch === "\\" && at + 1 < text.length && /[\\`*_[\]()<>]/.test(text[at + 1])) {
      plain += text[at + 1];
      at += 2;
      continue;
    }
    if (rest.startsWith("**")) {
      const end = rest.indexOf("**", 2);
      if (end < 3) throw new HelpSyntaxError(`${where}: ** with no closing ** in "${text}"`);
      flush();
      out.push({ t: "strong", c: inline(rest.slice(2, end), where) });
      at += end + 2;
      continue;
    }
    if (ch === "*") {
      const end = rest.indexOf("*", 1);
      if (end < 2) throw new HelpSyntaxError(`${where}: * with no closing * in "${text}"`);
      flush();
      out.push({ t: "em", c: inline(rest.slice(1, end), where) });
      at += end + 1;
      continue;
    }
    if (ch === "`") {
      const end = rest.indexOf("`", 1);
      if (end < 2) throw new HelpSyntaxError(`${where}: \` with no closing \` in "${text}"`);
      flush();
      out.push({ t: "code", v: rest.slice(1, end) });
      at += end + 1;
      continue;
    }
    if (rest.startsWith("<kbd>")) {
      const end = rest.indexOf("</kbd>");
      if (end < 6) throw new HelpSyntaxError(`${where}: <kbd> with no </kbd> in "${text}"`);
      flush();
      out.push({ t: "kbd", v: rest.slice(5, end) });
      at += end + 6;
      continue;
    }
    if (ch === "<") throw new HelpSyntaxError(`${where}: HTML is not part of the format: "${rest.slice(0, 20)}"`);
    if (ch === "[") {
      const close = rest.indexOf("](");
      const end = close === -1 ? -1 : rest.indexOf(")", close);
      if (close < 1 || end === -1) throw new HelpSyntaxError(`${where}: [ with no ](…) in "${text}"`);
      const href = rest.slice(close + 2, end);
      if (!LINK.test(href)) throw new HelpSyntaxError(`${where}: a link goes to help:<id>, screen:<name> or https://, not "${href}"`);
      flush();
      out.push({ t: "link", href, c: inline(rest.slice(1, close), where) });
      at += end + 1;
      continue;
    }
    plain += ch;
    at += 1;
  }
  flush();
  return out;
}

/** The body's blocks. */
export function blocks(body: string, where = "article"): Block[] {
  const out: Block[] = [];
  const lines = body.split("\n");
  let paragraph: string[] = [];
  let list: { t: "ul" | "ol"; items: string[] } | null = null;

  const endParagraph = () => {
    if (paragraph.length) out.push({ t: "p", c: inline(paragraph.join(" "), where) });
    paragraph = [];
  };
  const endList = () => {
    if (list) out.push({ t: list.t, items: list.items.map((item) => inline(item, where)) });
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      endParagraph();
      endList();
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      endParagraph();
      endList();
      const level = heading[1].length;
      if (level !== 2 && level !== 3) throw new HelpSyntaxError(`${where}: only ## and ### headings ("${line}")`);
      out.push({ t: "h", level, c: inline(heading[2].trim(), where) });
      continue;
    }
    const bullet = line.match(/^-\s+(.+)$/);
    const numbered = line.match(/^\d+\.\s+(.+)$/);
    if (bullet || numbered) {
      endParagraph();
      const kind = bullet ? "ul" : "ol";
      if (list && list.t !== kind) endList();
      if (!list) list = { t: kind, items: [] };
      list.items.push((bullet ?? numbered)![1].trim());
      continue;
    }
    if (/^\s{2,}\S/.test(line) && list) {
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    if (/^\s/.test(line)) throw new HelpSyntaxError(`${where}: an indented line outside a list ("${line}")`);
    if (/^(>|\||```|!\[|\* )/.test(line)) throw new HelpSyntaxError(`${where}: not part of the format ("${line}")`);
    endList();
    paragraph.push(line.trim());
  }
  endParagraph();
  endList();
  return out;
}

/** Plain words of some inline content. */
export function plainText(content: Inline[]): string {
  return content
    .map((one) => (typeof one === "string" ? one : "c" in one ? plainText(one.c) : one.v))
    .join("");
}

/** Every word of a body, for the search index. */
export function bodyText(tree: Block[]): string {
  return tree
    .map((block) => ("items" in block ? block.items.map(plainText).join("\n") : plainText(block.c)))
    .join("\n");
}

/** One article file, compiled for the client. */
export function compile(source: string, where = "article"): Article {
  const { meta, body } = split(source, where);
  const tree = blocks(body, where);
  return { ...meta, blocks: tree, text: bodyText(tree) };
}

/** One article file, as the agent API serves it: the body as written. */
export function sourceOf(source: string, where = "article"): ArticleSource {
  const { meta, body } = split(source, where);
  // Compiled as well, so a file the client could not draw is refused here too.
  blocks(body, where);
  return { ...meta, markdown: body };
}

/** Every bold stretch of a tree: the UI labels the article quotes. */
export function labels(tree: Block[]): string[] {
  const found: string[] = [];
  const walk = (content: Inline[]) => {
    for (const one of content) {
      if (typeof one === "string" || !("c" in one)) continue;
      if (one.t === "strong") found.push(plainText(one.c));
      walk(one.c);
    }
  };
  for (const block of tree) {
    if ("items" in block) block.items.forEach(walk);
    else walk(block.c);
  }
  return found;
}

/** Every link target of a tree. */
export function links(tree: Block[]): string[] {
  const found: string[] = [];
  const walk = (content: Inline[]) => {
    for (const one of content) {
      if (typeof one === "string" || !("c" in one)) continue;
      if (one.t === "link") found.push(one.href);
      walk(one.c);
    }
  };
  for (const block of tree) {
    if ("items" in block) block.items.forEach(walk);
    else walk(block.c);
  }
  return found;
}
