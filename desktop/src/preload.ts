/**
 * What the window may know of the shell, and all it may ask (#62).
 *
 * `window.spendTrackerDesktop`, read by `client/src/lib/desktop.ts`:
 *
 * - `systemLocale`: the operating system's regional format (Electron's
 *   `app.getSystemLocale()`), the default for how numbers, money and dates are
 *   written. Chromium's own default is the UI language, which is not the same
 *   thing: an English Mac set to Swedish formats writes "1 234,50 kr".
 * - `setLanguage(locale)`: the language the window just activated, so the
 *   menus and dialogs follow it.
 *
 * Nothing else crosses: no file access, no backend, no other IPC.
 */

import { contextBridge, ipcRenderer } from "electron";
import { LANGUAGE_CHANNEL, SYSTEM_LOCALE_ARG } from "./bridge";

const systemLocale = process.argv.find((arg) => arg.startsWith(SYSTEM_LOCALE_ARG))?.slice(SYSTEM_LOCALE_ARG.length) || undefined;

contextBridge.exposeInMainWorld("spendTrackerDesktop", {
  systemLocale,
  setLanguage(locale: unknown): void {
    if (typeof locale === "string" && locale.length <= 35) ipcRenderer.send(LANGUAGE_CHANNEL, locale);
  },
});
