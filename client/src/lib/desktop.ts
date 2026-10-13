/**
 * The desktop shell, when the client runs inside it (#62); undefined in a
 * browser.
 *
 * `desktop/src/preload.ts` puts this on `window` and says what crosses. Two
 * things, both about language, and neither a capability: what the app offers
 * is `/api/capabilities` (`lib/capabilities.ts`), the same in a window as in a
 * browser. No screen asks whether it is on a desktop.
 *
 * No imports: `lib/locale.ts` reads this, and that module has to load under
 * plain Node as well.
 */

export type DesktopBridge = {
  /** The operating system's regional format, e.g. `en-SE`. */
  systemLocale?: string;
  /** Tell the shell which language the words are now in, for its menus. */
  setLanguage(locale: string): void;
};

export function desktop(): DesktopBridge | undefined {
  const found = (globalThis as { spendTrackerDesktop?: DesktopBridge }).spendTrackerDesktop;
  return found && typeof found.setLanguage === "function" ? found : undefined;
}
