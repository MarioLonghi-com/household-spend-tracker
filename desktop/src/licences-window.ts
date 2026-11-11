/**
 * The Licences window itself (#63): one at a time, in the language the app
 * shows at the moment it opens, over the main window when there is one.
 * What it shows is `licences.ts`, which has no Electron in it and is what
 * the unit tests cover.
 */

import { app, BrowserWindow, nativeTheme } from "electron";
import path from "node:path";
import { language, say } from "./i18n";
import { loadNotices, noticesPath, page, sourceLink } from "./licences";
import { LICENCES, MENU, PRODUCT_NAME } from "./words";

export class Licences {
  private window: BrowserWindow | null = null;

  show(parent: BrowserWindow | null): void {
    if (this.window && !this.window.isDestroyed()) {
      this.window.focus();
      return;
    }
    const file = noticesPath(app.isPackaged ? process.resourcesPath : null);
    const html = page({
      lang: language(),
      dark: nativeTheme.shouldUseDarkColors,
      title: say(MENU.licences),
      free: say(LICENCES.free),
      sourceLabel: say(LICENCES.source),
      link: sourceLink(app.getVersion()),
      noticesHeading: say(LICENCES.notices),
      notices: loadNotices(file),
      missing: say(LICENCES.missing),
      where: say(LICENCES.where(path.dirname(file))),
    });
    this.window = new BrowserWindow({
      width: 760,
      height: 640,
      minWidth: 420,
      minHeight: 320,
      show: false,
      title: `${PRODUCT_NAME}: ${say(MENU.licences)}`,
      parent: parent ?? undefined,
      webPreferences: { sandbox: true, contextIsolation: true, javascript: false },
    });
    this.window.setMenuBarVisibility(false);
    this.window.once("ready-to-show", () => {
      if (this.window && !this.window.isDestroyed()) this.window.show();
    });
    this.window.on("closed", () => {
      this.window = null;
    });
    void this.window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  }
}
