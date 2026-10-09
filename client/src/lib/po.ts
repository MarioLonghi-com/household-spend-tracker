/**
 * Enough of a PO reader for the client's own needs: the catalog checks
 * (`locales/catalogs.test.ts`) and review mode's search (#272), which shows a
 * reviewer each message's English, its translator notes and the draft.
 */

export interface PoEntry {
  /** The English source, `msgid`. */
  id: string;
  translation: string;
  fuzzy: boolean;
  /** The translator notes, `#.` lines: a source `comment` (#228). */
  notes: string[];
  /** `msgctxt`, or "" for a message with none. */
  context: string;
}

export function readPo(text: string): PoEntry[] {
  const unquote = (line: string) => JSON.parse(line.slice(line.indexOf('"')));
  const entries: PoEntry[] = [];
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.split("\n");
    let id = "";
    let translation = "";
    let field: "ctx" | "id" | "str" | null = null;
    let fuzzy = false;
    let context = "";
    const notes: string[] = [];
    for (const line of lines) {
      if (line.startsWith("#,")) fuzzy ||= line.includes("fuzzy");
      // Lingui's own "placeholder {0}: expr" lines are about the code, not a note.
      else if (line.startsWith("#. ") && !line.startsWith("#. placeholder ")) notes.push(line.slice(3));
      else if (line.startsWith("msgctxt ")) [field, context] = ["ctx", unquote(line)];
      else if (line.startsWith("msgid ")) [field, id] = ["id", unquote(line)];
      else if (line.startsWith("msgstr ")) [field, translation] = ["str", unquote(line)];
      else if (line.startsWith('"') && field === "ctx") context += unquote(line);
      else if (line.startsWith('"') && field === "id") id += unquote(line);
      else if (line.startsWith('"') && field === "str") translation += unquote(line);
    }
    if (field && id) entries.push({ id, translation, fuzzy, notes, context });
  }
  return entries;
}

/** The placeholder names and the tags a translation must keep, as the catalog check compares them. */
export function placeholdersOf(text: string): { names: string[]; tags: string[] } {
  const names = [...new Set([...text.matchAll(/\{(\w+)[,}]/g)].map((match) => match[1]))].sort();
  const tags = (text.match(/<\/?\d+>/g) ?? []).sort();
  return { names, tags };
}
