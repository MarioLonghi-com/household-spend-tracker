import { describe, expect, it, vi } from "vitest";
import { contextTemplate } from "../src/context";

const flags = { canUndo: true, canRedo: true, canCut: true, canCopy: true, canPaste: true, canDelete: true, canSelectAll: true, canEditRichly: false };
const nothing = { isEditable: false, selectionText: "", linkURL: "", misspelledWord: "", dictionarySuggestions: [] as string[], editFlags: flags };
const contents = () => ({ replaceMisspelling: vi.fn(), session: { addWordToSpellCheckerDictionary: vi.fn() } as never });

describe("the right-click menu (#62)", () => {
  it("offers cut, copy, paste and select all in a text field, enabled as Chromium says", () => {
    const items = contextTemplate({ ...nothing, isEditable: true, editFlags: { ...flags, canCut: false, canCopy: false } }, contents(), vi.fn());
    expect(items.map((item) => item.role ?? item.type)).toEqual(["cut", "copy", "paste", "separator", "selectAll"]);
    expect(items.map((item) => item.label ?? null)).toEqual(["Cut", "Copy", "Paste", null, "Select all"]);
    expect(items.filter((item) => item.enabled === false).map((item) => item.role)).toEqual(["cut", "copy"]);
  });

  it("puts spelling suggestions first, and they replace the word", () => {
    const into = contents();
    const items = contextTemplate({ ...nothing, isEditable: true, misspelledWord: "groceires", dictionarySuggestions: ["groceries", "grocers"] }, into, vi.fn());
    expect(items.slice(0, 3).map((item) => item.label)).toEqual(["groceries", "grocers", "Add to dictionary"]);
    items[0].click?.({} as never, undefined, {} as never);
    expect(into.replaceMisspelling).toHaveBeenCalledWith("groceries");
  });

  it("offers copy for selected text outside a field, and nothing at all for a plain click", () => {
    expect(contextTemplate({ ...nothing, selectionText: "Padaria Ñandú" }, contents(), vi.fn()).map((item) => item.role)).toEqual(["copy"]);
    expect(contextTemplate(nothing, contents(), vi.fn())).toEqual([]);
  });

  it("copies a web link's address, and ignores the app's own and odd schemes", () => {
    const copy = vi.fn();
    const items = contextTemplate({ ...nothing, linkURL: "https://example.test/help" }, contents(), copy);
    expect(items.map((item) => item.label)).toEqual(["Copy link"]);
    items[0].click?.({} as never, undefined, {} as never);
    expect(copy).toHaveBeenCalledWith("https://example.test/help");
    expect(contextTemplate({ ...nothing, linkURL: "javascript:alert(1)" }, contents(), copy)).toEqual([]);
  });
});
