/**
 * The menu bar, in the window's language (#62). Rebuilt whenever the language
 * changes, because a menu's labels are fixed once it is built.
 *
 * Explicit labels on the standard items rather than Electron's own: those are
 * in the operating system's language, and a menu half in Swedish and half in
 * the language the person chose in the app is the thing #62 asks to avoid.
 */

import { Menu, type MenuItemConstructorOptions } from "electron";
import { say } from "./i18n";
import { MENU, PRODUCT_NAME } from "./words";

export type Actions = {
  backUpNow: () => void;
  restore: () => void;
  chooseFolder: () => void;
  showBackups: () => void;
  showLedger: () => void;
  about: () => void;
};

/** Ids the end-to-end smoke test clicks, since it cannot open a native menu. */
export const IDS = {
  backUpNow: "back-up-now",
  restore: "restore",
  chooseFolder: "choose-backups-folder",
  showBackups: "show-backups-folder",
  showLedger: "show-ledger-folder",
} as const;

export function build(actions: Actions, platform: NodeJS.Platform = process.platform): Menu {
  const mac = platform === "darwin";
  const file: MenuItemConstructorOptions = {
    label: say(MENU.file),
    submenu: [
      { id: IDS.backUpNow, label: say(MENU.backUpNow), accelerator: "CmdOrCtrl+Shift+B", click: actions.backUpNow },
      { id: IDS.restore, label: say(MENU.restore), click: actions.restore },
      { type: "separator" },
      { id: IDS.chooseFolder, label: say(MENU.chooseFolder), click: actions.chooseFolder },
      { id: IDS.showBackups, label: say(MENU.showBackups), click: actions.showBackups },
      { id: IDS.showLedger, label: say(MENU.showLedger), click: actions.showLedger },
      { type: "separator" },
      mac ? { role: "close", label: say(MENU.closeWindow) } : { role: "quit", label: say(MENU.quit) },
    ],
  };
  const edit: MenuItemConstructorOptions = {
    label: say(MENU.edit),
    submenu: [
      { role: "undo", label: say(MENU.undo) },
      { role: "redo", label: say(MENU.redo) },
      { type: "separator" },
      { role: "cut", label: say(MENU.cut) },
      { role: "copy", label: say(MENU.copy) },
      { role: "paste", label: say(MENU.paste) },
      { role: "selectAll", label: say(MENU.selectAll) },
    ],
  };
  const view: MenuItemConstructorOptions = {
    label: say(MENU.view),
    submenu: [
      { role: "resetZoom", label: say(MENU.actualSize) },
      { role: "zoomIn", label: say(MENU.zoomIn) },
      { role: "zoomOut", label: say(MENU.zoomOut) },
      { type: "separator" },
      { role: "togglefullscreen", label: say(MENU.fullScreen) },
    ],
  };
  const window: MenuItemConstructorOptions = {
    label: say(MENU.window),
    submenu: mac
      ? [
          { role: "minimize", label: say(MENU.minimise) },
          { role: "zoom", label: say(MENU.zoomWindow) },
        ]
      : [{ role: "minimize", label: say(MENU.minimise) }],
  };
  const help: MenuItemConstructorOptions = {
    label: say(MENU.help),
    submenu: [{ label: say(MENU.about), click: actions.about }],
  };
  const template: MenuItemConstructorOptions[] = [file, edit, view, window, help];
  if (mac) {
    template.unshift({
      label: PRODUCT_NAME,
      submenu: [
        { label: say(MENU.about), click: actions.about },
        { type: "separator" },
        { role: "hide", label: say(MENU.hide) },
        { role: "hideOthers", label: say(MENU.hideOthers) },
        { role: "unhide", label: say(MENU.showAll) },
        { type: "separator" },
        { role: "quit", label: say(MENU.quit) },
      ],
    });
    // On a Mac "About" lives in the app menu; Help keeps only what is left.
    help.submenu = [];
    template.pop();
  }
  return Menu.buildFromTemplate(template);
}
