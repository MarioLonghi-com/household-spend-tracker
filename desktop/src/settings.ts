/**
 * The shell's own settings, in `shell.json` beside the ledger (#62).
 *
 * Beside the ledger and not in Electron's `userData`, because on Windows that
 * is inside the MSIX package's private copy and goes with an uninstall; the
 * ledger's folder stays, and the backups folder someone chose should stay with
 * it. Only the shell's choices are here: everything about the household is in
 * the ledger.
 *
 * Read leniently -- a field that is missing or the wrong shape falls back to
 * its default rather than stopping the app -- and written whole, through a
 * temporary file and a rename, so a crash mid-write leaves the old file.
 */

import fs from "node:fs";

export type Settings = {
  /** The folder backups are written to; null for the default (`backups.ts`). */
  backupsFolder: string | null;
  /** Mac App Store: the security-scoped bookmark that lets the sandbox reach it. */
  backupsBookmark: string | null;
  /** Whether the person has been asked to choose a folder (Mac App Store). */
  askedForFolder: boolean;
  /** The local date (YYYY-MM-DD) of the last automatic daily backup. */
  lastDailyBackup: string | null;
  /** The audit log's position at the last backup: "changed since" is above it. */
  backedUpTo: number;
  /** The language the window last showed, for the menus before it loads. */
  language: string;
};

export const DEFAULTS: Settings = {
  backupsFolder: null,
  backupsBookmark: null,
  askedForFolder: false,
  lastDailyBackup: null,
  backedUpTo: -1,
  language: "en",
};

function pick<K extends keyof Settings>(raw: Record<string, unknown>, key: K, ok: (value: unknown) => boolean): Settings[K] {
  return ok(raw[key]) ? (raw[key] as Settings[K]) : DEFAULTS[key];
}

const text = (value: unknown) => typeof value === "string" && value.length > 0;
const textOrNull = (value: unknown) => value === null || text(value);

export function parse(raw: unknown): Settings {
  const found = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    backupsFolder: pick(found, "backupsFolder", textOrNull),
    backupsBookmark: pick(found, "backupsBookmark", textOrNull),
    askedForFolder: pick(found, "askedForFolder", (v) => typeof v === "boolean"),
    lastDailyBackup: pick(found, "lastDailyBackup", (v) => v === null || (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v))),
    backedUpTo: pick(found, "backedUpTo", (v) => Number.isInteger(v)),
    language: pick(found, "language", text),
  };
}

export function load(file: string): Settings {
  try {
    return parse(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch {
    return { ...DEFAULTS };
  }
}

export function save(file: string, settings: Settings): void {
  const temporary = `${file}.writing`;
  fs.writeFileSync(temporary, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, file);
}
