/**
 * The desktop shell, when the client runs inside it (#62); undefined in a
 * browser.
 *
 * `desktop/src/preload.ts` puts this on `window` and says what crosses. Two
 * things about language, and one fact about this computer -- whether it can
 * prompt for Touch ID (#432) -- none of them a capability: what the app
 * offers is `/api/capabilities` (`lib/capabilities.ts`), the same in a window
 * as in a browser, and a sensor only matters where the server offers
 * `device_unlock`. No screen asks whether it is on a desktop.
 *
 * No imports: `lib/locale.ts` reads this, and that module has to load under
 * plain Node as well.
 */

export type DesktopBridge = {
  /** The operating system's regional format, e.g. `en-SE`. */
  systemLocale?: string;
  /** Tell the shell which language the words are now in, for its menus. */
  setLanguage(locale: string): void;
  /**
   * How this computer can gate the launch: `touch_id` on a Mac with a sensor
   * the shell can prompt and a keychain it can keep the secret in; absent
   * otherwise. The shell's answer alone -- the page never prompts.
   */
  unlock?: "touch_id";
};

export function desktop(): DesktopBridge | undefined {
  const found = (globalThis as { spendTrackerDesktop?: DesktopBridge }).spendTrackerDesktop;
  return found && typeof found.setLanguage === "function" ? found : undefined;
}
