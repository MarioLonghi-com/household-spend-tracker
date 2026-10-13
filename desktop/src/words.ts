/**
 * Every word the shell shows: menus, dialogs, the splash (#62).
 *
 * Descriptors, not strings: `say()` in `i18n.ts` renders one in the language
 * active at that moment, so a menu rebuilt after the window changes language
 * comes out in it. A message of one or two words carries a translator comment,
 * as every one in the client does (`client/src/locales/README.md`).
 */

import { msg } from "@lingui/core/macro";

export const MENU = {
  file: msg({ message: "File", comment: "Desktop app menu bar: the File menu" }),
  edit: msg({ message: "Edit", comment: "Desktop app menu bar: the Edit menu" }),
  view: msg({ message: "View", comment: "Desktop app menu bar: the View menu" }),
  window: msg({ message: "Window", comment: "Desktop app menu bar: the Window menu" }),
  help: msg({ message: "Help", comment: "Desktop app menu bar: the Help menu" }),

  backUpNow: msg({ message: "Back up now", comment: "Desktop app File menu item: make a backup of the ledger now" }),
  restore: msg`Restore from backup…`,
  chooseFolder: msg`Choose backups folder…`,
  showBackups: msg`Show backups folder`,
  showLedger: msg`Show ledger folder`,
  closeWindow: msg({ message: "Close window", comment: "Desktop app File menu item" }),

  about: msg`About Spend Tracker`,
  hide: msg`Hide Spend Tracker`,
  hideOthers: msg({ message: "Hide others", comment: "macOS app menu item: hide every other app's windows" }),
  showAll: msg({ message: "Show all", comment: "macOS app menu item: show every app's windows again" }),
  quit: msg`Quit Spend Tracker`,

  undo: msg({ message: "Undo", comment: "Desktop app Edit menu item: undo the last edit in a text field" }),
  redo: msg({ message: "Redo", comment: "Desktop app Edit menu item: redo the edit just undone in a text field" }),
  cut: msg({ message: "Cut", comment: "Desktop app Edit menu item (clipboard)" }),
  copy: msg({ message: "Copy", comment: "Desktop app Edit menu item (clipboard)" }),
  paste: msg({ message: "Paste", comment: "Desktop app Edit menu item (clipboard)" }),
  selectAll: msg({ message: "Select all", comment: "Desktop app Edit menu item: select all the text in a field" }),

  actualSize: msg({ message: "Actual size", comment: "Desktop app View menu item: reset the zoom" }),
  zoomIn: msg({ message: "Zoom in", comment: "Desktop app View menu item: make everything larger" }),
  zoomOut: msg({ message: "Zoom out", comment: "Desktop app View menu item: make everything smaller" }),
  fullScreen: msg({ message: "Full screen", comment: "Desktop app View menu item: switch full screen on or off" }),
  minimise: msg({ message: "Minimise", comment: "Desktop app Window menu item: minimise the window to the Dock or taskbar" }),
  zoomWindow: msg({ message: "Zoom", comment: "macOS Window menu item: make the window fill the screen, not full screen" }),
};

export const SPLASH = {
  opening: msg`Opening your ledger…`,
  upgrading: msg`Upgrading your ledger…`,
  upgradingDetail: msg`A backup was made first. This can take a minute.`,
};

export const BUTTON = {
  ok: msg({ message: "OK", comment: "Desktop app dialog button: close the dialog" }),
  quit: msg({ message: "Quit", comment: "Desktop app dialog button: close Spend Tracker" }),
  cancel: msg({ message: "Cancel", comment: "Desktop app dialog button: do nothing and close the dialog" }),
  later: msg({ message: "Later", comment: "Desktop app dialog button: not now, ask again another time" }),
  showBackup: msg({ message: "Show the backup", comment: "Desktop app dialog button: open the folder holding the backup" }),
  showLog: msg({ message: "Show the log", comment: "Desktop app dialog button: open the folder holding the log file" }),
  showInFolder: msg({ message: "Show in folder", comment: "Desktop app dialog button: open the folder holding the new backup" }),
  update: msg({ message: "Update the ledger", comment: "Desktop app dialog button: go ahead with an upgrade that cannot be undone" }),
  restore: msg({ message: "Restore", comment: "Desktop app dialog button: replace the ledger with the chosen backup" }),
  chooseFolder: msg({ message: "Choose a folder…", comment: "Desktop app dialog button: open a folder picker" }),
  useFolder: msg({ message: "Use this folder", comment: "Desktop app folder picker's confirm button" }),
};

export const DIALOG = {
  newerTitle: msg`This ledger was last opened by a newer version of Spend Tracker`,
  newerDetail: msg`Update Spend Tracker, then open it again. Nothing was changed.`,

  lossyTitle: msg`This update changes your ledger in a way that cannot be undone`,
  lossyDetail: (titles: string) =>
    msg`A backup is made first, so you can go back to it. What changes: ${titles}.`,

  failedTitle: msg`The ledger could not be upgraded`,
  droppedTitle: msg`The upgrade did not keep every row, so Spend Tracker stopped`,
  failedDetail: (backup: string) =>
    msg`Nothing is lost: the ledger as it was is in the backup made just before, in ${backup}. Please report this.`,

  busyTitle: msg`Spend Tracker is already using this ledger`,
  busyDetail: msg`Another copy of the app has it open. Close that one, then try again.`,

  couldNotStartTitle: msg`Spend Tracker could not open the ledger`,
  stoppedTitle: msg`The ledger stopped unexpectedly`,
  stoppedDetail: msg`Spend Tracker will close. Everything saved before this is kept.`,

  backedUpTitle: msg({ message: "Backup saved", comment: "Desktop app dialog title after Back up now succeeded" }),
  backedUpDetail: (name: string, folder: string) =>
    msg({ message: `${name} in ${folder}`, comment: "Desktop app dialog: the backup's file name, then the folder it was saved in" }),
  backupFailedTitle: msg`The backup could not be made`,

  restorePick: msg`Choose a backup to restore`,
  restoreFilter: msg`Spend Tracker backups`,
  restoreTitle: msg`Replace the ledger with this backup?`,
  restoreDetail: (name: string) =>
    msg`${name} will replace everything in the ledger now. The ledger as it is now is backed up first, and you will sign in again.`,
  restoredTitle: msg`The backup is restored`,
  restoredDetail: msg`Sign in to carry on.`,
  notRestoredTitle: msg`This backup was not restored`,
  notRestoredNewer: msg`It was made by a newer version of Spend Tracker. Update this one first. Nothing was changed.`,

  chooseFolderTitle: msg`Choose where backups go`,
  askFolderTitle: msg`Backups are kept inside the app's own folder for now`,
  askFolderDetail: msg`Choose a folder you can see, in Documents or one that syncs, so you can find your backups and copy them elsewhere.`,

  aboutDetail: (version: string) => msg`Version ${version}. Licensed under the GNU AGPL, version 3 or later.`,
};
