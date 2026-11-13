/**
 * The build step that compiles the articles (#460): what it hands the client
 * and what it writes for the agent API.
 */

import { describe, expect, it } from "vitest";
import { helpArticles } from "../../help-articles";
import { REPORT_KEYS } from "../lib/screens";
import { REPORTS } from "../screens/Reports";

const ROOT = new URL(".", import.meta.url).pathname;

type Hooks = {
  resolveId: (id: string) => string | null;
  load: (this: unknown, id: string) => string | null;
  generateBundle: (this: unknown) => void;
};

function plugin(): Hooks {
  return helpArticles(ROOT) as unknown as Hooks;
}

const context = (emitted: { fileName: string; source: string }[] = []) => ({
  addWatchFile: () => {},
  error: (message: string) => {
    throw new Error(message);
  },
  emitFile: (file: { fileName: string; source: string }) => emitted.push(file),
});

describe("the help plugin", () => {
  it("gives each language a module of its own, with the tree and without the source", () => {
    const one = plugin();
    const id = one.resolveId("virtual:help-articles/es-ES")!;
    const code = one.load.call(context(), id)!;
    const articles = JSON.parse(code.replace(/^export default /, "").replace(/;$/, ""));
    const split = articles.find((article: { id: string }) => article.id === "split-transaction");
    expect(split.blocks.length).toBeGreaterThan(2);
    expect(split.markdown).toBeUndefined();
  });

  it("indexes which screens each article is about, without any of its words", () => {
    const one = plugin();
    const code = one.load.call(context(), one.resolveId("virtual:help-index")!)!;
    const index = JSON.parse(code.replace(/^export default /, "").replace(/;$/, ""));
    expect(index["snap-receipt"]).toEqual({ screens: ["receipts"], requires: "snap" });
    expect(index["split-transaction"]).toEqual({ screens: ["register"] });
    expect(code.length).toBeLessThan(3000);
  });

  it("writes every language as JSON with the body as written, for the agent API", () => {
    const emitted: { fileName: string; source: string }[] = [];
    plugin().generateBundle.call(context(emitted));
    expect(emitted.map((file) => file.fileName).sort()).toEqual([
      "help/en.json",
      "help/es-ES.json",
      "help/pt-BR.json",
      "help/sv-SE.json",
    ]);
    const english = JSON.parse(emitted.find((file) => file.fileName === "help/en.json")!.source);
    const split = english.articles.find((article: { id: string }) => article.id === "split-transaction");
    expect(split.markdown.startsWith("---")).toBe(false);
    expect(split.markdown).toContain("**");
  });

  it("leaves other modules alone, and refuses a language with no articles", () => {
    const one = plugin();
    expect(one.resolveId("react")).toBeNull();
    expect(() => one.load.call(context(), one.resolveId("virtual:help-articles/fr-FR")!)).toThrow(/fr-FR/);
  });
});

describe("the screens list", () => {
  it("has a screen for every report", () => {
    expect([...REPORT_KEYS].sort()).toEqual(REPORTS.map((one) => one.key).sort());
  });
});
