/**
 * Backups the person can see (#62, decided 2026-10-13).
 *
 * The backend writes each copy (`VACUUM INTO`, reopened, checked and counted)
 * into the ledger's private `.outgoing` folder; this moves it to the backups
 * folder. Two steps, because the Mac App Store sandbox lets the shell reach a
 * folder the person chose and does not let the backend.
 *
 * A backup is a bare `.sqlite3`, whole on its own -- the desktop has no
 * authenticators, so no `secret.key` goes with it -- and safe in a synced
 * folder: it is written under a temporary name and renamed when complete, so a
 * sync client never uploads half of one.
 *
 * **The folder:** `Documents/Spend Tracker Backups` by default, or the one
 * chosen with "Choose backups folder…". The Mac App Store build cannot write to
 * Documents until the person has chosen it, so there the default is a
 * `Backups` folder beside the ledger, and the app asks once for a better one.
 *
 * **When:** "Back up now"; the first launch of each day; on quit when
 * anything changed since the last backup; and before a restore.
 */

import fs from "node:fs";
import path from "node:path";
import { isOurs, toRemove } from "./retention";

export const DEFAULT_FOLDER_NAME = "Spend Tracker Backups";

/** What `app/desktop/control.py:backup` answers. */
export type Made = {
  path: string;
  name: string;
  bytes: number;
  taken_at: string;
  revision: string;
  rows: number;
  changes_head: number;
};

export type Delivered = { file: string; removed: string[] };

/**
 * The default folder: `documents/Spend Tracker Backups`, or, where the sandbox
 * cannot reach Documents before it is chosen, `fallback`.
 */
export function defaultFolder(documents: string, fallback: string, sandboxed: boolean): string {
  return sandboxed ? fallback : path.join(documents, DEFAULT_FOLDER_NAME);
}

/**
 * Move a finished copy into `folder`, then prune it. The copy is first
 * written as `<name>.partial`, then renamed, then the outgoing file removed --
 * and only then are old backups removed (`retention.ts`).
 */
export function deliver(made: Pick<Made, "path" | "name">, folder: string): Delivered {
  fs.mkdirSync(folder, { recursive: true });
  // One app writes here at a time (a second launch only focuses the first),
  // so a `.partial` of ours already there is one a crash left behind.
  for (const name of fs.readdirSync(folder)) {
    if (name.endsWith(".partial") && isOurs(name.slice(0, -".partial".length))) {
      fs.rmSync(path.join(folder, name), { force: true });
    }
  }
  const file = freeName(folder, made.name);
  const partial = `${file}.partial`;
  fs.copyFileSync(made.path, partial, fs.constants.COPYFILE_EXCL);
  fs.renameSync(partial, file);
  fs.rmSync(made.path, { force: true });
  const removed = toRemove(fs.readdirSync(folder));
  for (const name of removed) fs.rmSync(path.join(folder, name), { force: true });
  return { file, removed };
}

/**
 * `name`, or `…-2`, `…-3` when a backup from the same second is already there.
 * Never the name of one that exists: a second backup within a second -- "Back
 * up now", then a restore, which backs up first -- once overwrote the first,
 * and the restore then put back the wrong one.
 */
export function freeName(folder: string, name: string): string {
  const stem = name.replace(/\.sqlite3$/, "");
  let candidate = path.join(folder, name);
  for (let again = 2; fs.existsSync(candidate); again += 1) candidate = path.join(folder, `${stem}-${again}.sqlite3`);
  return candidate;
}

/** A local date as YYYY-MM-DD, which is what "the first launch of a day" compares. */
export function localDay(when: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

/** Copy a backup chosen for restore to where the sandboxed backend can read it. */
export function bringIn(chosen: string, incoming: string): string {
  fs.rmSync(incoming, { recursive: true, force: true });
  fs.mkdirSync(incoming, { recursive: true });
  const copy = path.join(incoming, path.basename(chosen));
  fs.copyFileSync(chosen, copy);
  return copy;
}
