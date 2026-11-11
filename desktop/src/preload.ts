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
 * - `unlock`: `"touch_id"` when this computer can prompt for Touch ID and keep
 *   the secret (#432), absent otherwise. A fact about the machine, decided
 *   once at launch; the server's `device_unlock` capability is the other half.
 *
 * Nothing else crosses: no file access, no backend, no other IPC.
 */

import { contextBridge, ipcRenderer } from "electron";
import { LANGUAGE_CHANNEL, SYSTEM_LOCALE_ARG, UNLOCK_ARG } from "./bridge";

const systemLocale = process.argv.find((arg) => arg.startsWith(SYSTEM_LOCALE_ARG))?.slice(SYSTEM_LOCALE_ARG.length) || undefined;
const unlock = process.argv.find((arg) => arg.startsWith(UNLOCK_ARG))?.slice(UNLOCK_ARG.length) || undefined;

contextBridge.exposeInMainWorld("spendTrackerDesktop", {
  systemLocale,
  unlock: unlock === "touch_id" ? unlock : undefined,
  setLanguage(locale: unknown): void {
    if (typeof locale === "string" && locale.length <= 35) ipcRenderer.send(LANGUAGE_CHANNEL, locale);
  },
});
