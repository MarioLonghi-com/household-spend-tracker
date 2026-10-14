/**
 * Where the desktop app keeps things (#317, decided 2026-10-13).
 *
 * The shell decides every place and tells the backend; Python never guesses.
 *
 *     <root>/Ledger/        the backend's data directory: the live ledger, its
 *                           logs, the backups an upgrade takes first
 *     <root>/shell.json     the shell's own settings (`settings.ts`)
 *     <root>/shell.log      the backend's standard error, this launch
 *
 * `<root>` is the operating system's per-user application data, never a
 * synced folder and never inside the app:
 *
 * - **macOS:** `Application Support/Spend Tracker`, which for the Mac App
 *   Store build is inside the app's sandbox container. The container stays
 *   when the app is deleted.
 * - **Windows:** `%LOCALAPPDATA%\Spend Tracker`, which is not Roaming (a WAL
 *   database must not roam) and is excluded from MSIX virtualisation in the
 *   manifest, so it is the real folder and survives an uninstall (#59).
 *
 * **The folder's name is fixed, not the product's** (#346). Electron's
 * `userData` is Application Support/<productName>, and the app was renamed
 * "Household Spend Tracker" for the stores: following the product name would
 * have moved the ledger, and the app would have opened as a fresh install
 * with the old ledger still on disk. Both platforms use `DATA_FOLDER`.
 *
 * The backups the person keeps go elsewhere, to a folder they can see
 * (`backups.ts`).
 *
 * `SPENDTRACKER_DESKTOP_ROOT` moves `<root>` for the tests, and only them: a
 * real install never sets it, and nothing in the app writes it.
 */

import path from "node:path";

export type Places = {
  root: string;
  ledger: string;
  settings: string;
  log: string;
  /** Where a backup chosen for restore is copied before the backend reads it. */
  incoming: string;
};

/** The ledger's folder under the platform's application data, whatever the app is called. */
export const DATA_FOLDER = "Spend Tracker";

export type Environment = {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  /** Electron's `app.getPath("appData")`: Application Support on a Mac. */
  appData: string;
};

export function rootFor({ platform, env, appData }: Environment): string {
  if (env.SPENDTRACKER_DESKTOP_ROOT) return path.resolve(env.SPENDTRACKER_DESKTOP_ROOT);
  if (platform === "win32" && env.LOCALAPPDATA) return path.win32.join(env.LOCALAPPDATA, DATA_FOLDER);
  return path.join(appData, DATA_FOLDER);
}

export function placesIn(root: string): Places {
  const ledger = path.join(root, "Ledger");
  return {
    root,
    ledger,
    settings: path.join(root, "shell.json"),
    log: path.join(root, "shell.log"),
    incoming: path.join(ledger, ".incoming"),
  };
}
