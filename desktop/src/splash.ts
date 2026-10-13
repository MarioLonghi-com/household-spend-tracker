/**
 * The small window shown while the backend starts (#62).
 *
 * A first start on Windows took 12.6 s in the spike (#59) -- the frozen
 * backend's first run, scanned on the way -- and an upgrade can take longer.
 * Without this, the app would show nothing at all for that long. It says
 * "Opening your ledger…", and "Upgrading your ledger…" when the backend
 * announces a migration.
 *
 * Plain HTML from a `data:` URL: no script, no preload, no access to anything.
 */

import { BrowserWindow, nativeTheme } from "electron";
import { language } from "./i18n";

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function page(title: string, detail: string): string {
  const dark = nativeTheme.shouldUseDarkColors;
  return `<!doctype html><html lang="${escape(language())}"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>
  html, body { margin: 0; height: 100%; font: 15px -apple-system, "Segoe UI", system-ui, sans-serif;
    background: ${dark ? "#1e1f22" : "#fafaf8"}; color: ${dark ? "#e8e8e6" : "#1d1d1b"}; -webkit-user-select: none; }
  main { height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 10px; padding: 0 28px; text-align: center; }
  h1 { font-size: 17px; font-weight: 600; margin: 0; }
  p { margin: 0; opacity: .7; }
  .bar { width: 160px; height: 3px; border-radius: 2px; overflow: hidden; background: ${dark ? "#33353a" : "#e4e4df"}; }
  .bar::after { content: ""; display: block; width: 40%; height: 100%; background: ${dark ? "#8fb3ff" : "#3a62c8"};
    animation: slide 1.1s ease-in-out infinite alternate; }
  @keyframes slide { from { transform: translateX(-10%); } to { transform: translateX(160%); } }
  @media (prefers-reduced-motion: reduce) { .bar::after { animation: none; width: 100%; } }
</style></head><body><main role="status" aria-live="polite">
<h1>${escape(title)}</h1>${detail ? `<p>${escape(detail)}</p>` : ""}<div class="bar"></div></main></body></html>`;
}

export class Splash {
  private readonly window: BrowserWindow;

  constructor(title: string) {
    this.window = new BrowserWindow({
      width: 420,
      height: 180,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      show: false,
      center: true,
      title: "Spend Tracker",
      webPreferences: { sandbox: true, contextIsolation: true, javascript: false },
    });
    this.window.setMenuBarVisibility(false);
    this.window.once("ready-to-show", () => {
      if (!this.window.isDestroyed()) this.window.show();
    });
    this.say(title);
  }

  say(title: string, detail = ""): void {
    if (this.window.isDestroyed()) return;
    void this.window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(page(title, detail))}`);
  }

  close(): void {
    if (!this.window.isDestroyed()) this.window.destroy();
  }
}
