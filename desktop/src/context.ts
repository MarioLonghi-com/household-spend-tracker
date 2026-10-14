/**
 * The right-click menu (#62). Electron shows none unless the app builds one,
 * so the desktop app had no Cut, Copy or Paste on a right click -- found in
 * the first hands-on test. In the window's language, like the menu bar.
 *
 * What it offers follows what was clicked, from Chromium's own `params`: a
 * misspelt word gets suggestions; a text field gets Cut, Copy, Paste and
 * Select all, each enabled only when it would do something; a selection
 * elsewhere gets Copy; a link gets Copy link. Nothing else -- no Inspect.
 */

import type { ContextMenuParams, MenuItemConstructorOptions, WebContents } from "electron";
import { say } from "./i18n";
import { MENU } from "./words";

type Clicked = Pick<ContextMenuParams, "isEditable" | "selectionText" | "linkURL" | "misspelledWord" | "dictionarySuggestions" | "editFlags">;

export function contextTemplate(params: Clicked, contents: Pick<WebContents, "replaceMisspelling" | "session">, copy: (text: string) => void): MenuItemConstructorOptions[] {
  const items: MenuItemConstructorOptions[] = [];
  if (params.isEditable && params.misspelledWord) {
    for (const suggestion of params.dictionarySuggestions.slice(0, 5)) {
      items.push({ label: suggestion, click: () => contents.replaceMisspelling(suggestion) });
    }
    items.push({
      label: say(MENU.addToDictionary),
      click: () => contents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
    });
    items.push({ type: "separator" });
  }
  if (params.isEditable) {
    items.push(
      { role: "cut", label: say(MENU.cut), enabled: params.editFlags.canCut },
      { role: "copy", label: say(MENU.copy), enabled: params.editFlags.canCopy },
      { role: "paste", label: say(MENU.paste), enabled: params.editFlags.canPaste },
      { type: "separator" },
      { role: "selectAll", label: say(MENU.selectAll), enabled: params.editFlags.canSelectAll },
    );
  } else if (params.selectionText.trim()) {
    items.push({ role: "copy", label: say(MENU.copy) });
  }
  if (/^(https?:|mailto:)/i.test(params.linkURL)) {
    if (items.length) items.push({ type: "separator" });
    items.push({ label: say(MENU.copyLink), click: () => copy(params.linkURL) });
  }
  return items;
}
