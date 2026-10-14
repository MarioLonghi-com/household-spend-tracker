/**
 * Spend Tracker on a desktop: the Electron shell (#61, #62).
 *
 * One window on `app://localhost`, every request forwarded to the frozen
 * backend over stdin/stdout (`scheme.ts`, `backend.ts`). The shell owns what a
 * server's operator would do by hand -- where the ledger lives, starting and
 * upgrading it, backups, restore -- and the backend owns everything else,
 * exactly as on a server.
 *
 * The life of one launch:
 *
 * 1. a second launch only focuses the first window (`requestSingleInstanceLock`);
 * 2. the splash says "Opening your ledger…" and the backend starts: it locks,
 *    checks, backs up and migrates the ledger first (`app/desktop/launch.py`),
 *    and says "upgrading" when it does. A start that stops -- a newer ledger,
 *    an upgrade to confirm or one that failed -- ends in a dialog;
 * 3. the window opens on the sign-in screen: the session lives in this
 *    process's memory only, so every launch asks for the password;
 * 4. the first launch of a day makes a backup;
 * 5. closing the window quits: a backup if anything changed since the last
 *    one, then the backend stops -- its shutdown cancels housekeeping and
 *    folds the WAL back in -- and the app exits.
 */

import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
  protocol,
  session,
  shell,
  type MessageBoxOptions,
  type OpenDialogOptions,
  type WebContents,
} from "electron";
import fs from "node:fs";
import path from "node:path";
import { Backend, type Launch } from "./backend";
import { bringIn, defaultFolder, deliver, localDay, type Made } from "./backups";
import { LANGUAGE_CHANNEL, SYSTEM_LOCALE_ARG } from "./bridge";
import { contextTemplate } from "./context";
import { CookieJar } from "./cookies";
import { language, say, useLanguage } from "./i18n";
import * as menu from "./menu";
import { placesIn, rootFor } from "./places";
import { handler, ORIGIN } from "./scheme";
import * as stored from "./settings";
import { Splash } from "./splash";
import { BUTTON, DIALOG, SPLASH } from "./words";

protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

// A test's root takes Chromium's profile with it -- local storage, caches --
// so a run never reads or leaves anything in the real app's folder.
if (process.env.SPENDTRACKER_DESKTOP_ROOT) {
  app.setPath("userData", path.join(path.resolve(process.env.SPENDTRACKER_DESKTOP_ROOT), "Chromium"));
}
const places = placesIn(rootFor({ platform: process.platform, env: process.env, userData: app.getPath("userData") }));
const sandboxed = Boolean(process.mas);
const jar = new CookieJar();
let settings = stored.load(places.settings);
useLanguage(settings.language);

let window: BrowserWindow | null = null;
let splash: Splash | null = null;
let log: fs.WriteStream | null = null;

/** A message box over the window when there is one, on its own when there is not. */
function ask(options: MessageBoxOptions) {
  return window ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options);
}

function pick(options: OpenDialogOptions) {
  return window ? dialog.showOpenDialog(window, options) : dialog.showOpenDialog(options);
}

// --------------------------------------------------------------------------- //
// The backend
// --------------------------------------------------------------------------- //

/** The running backend, or a promise of the next one while it restarts. */
let live: Backend | null = null;
let waiting: ((backend: Backend) => void)[] = [];
/** Set while the shell itself is stopping the backend, so its exit is expected. */
let stopping = false;

function current(): Promise<Backend> {
  return live ? Promise.resolve(live) : new Promise((resolve) => waiting.push(resolve));
}

function adopt(backend: Backend): void {
  live = backend;
  for (const resolve of waiting) resolve(backend);
  waiting = [];
  backend.onExit(() => {
    if (live === backend) live = null;
    if (!stopping) void diedWhileOpen();
  });
}

function backendCommand(): { command: string; args: string[]; cwd: string } {
  if (app.isPackaged) {
    const directory = path.join(process.resourcesPath, "backend");
    const executable = process.platform === "win32" ? "spend-tracker-backend.exe" : "spend-tracker-backend";
    return { command: path.join(directory, executable), args: [], cwd: directory };
  }
  // From source: `npm start` in desktop/, with the repository's own venv.
  const root = path.resolve(__dirname, "..", "..");
  const venv = process.platform === "win32" ? path.join(".venv", "Scripts", "python.exe") : path.join(".venv", "bin", "python");
  return {
    command: process.env.SPENDTRACKER_DESKTOP_PYTHON || path.join(root, venv),
    args: ["-m", "app.desktop.stdio_adapter"],
    cwd: root,
  };
}

function spawnBackend(extra: Record<string, string> = {}): Backend {
  const inherited = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !key.startsWith("SPENDTRACKER_") && key !== "DATABASE_URL"),
  );
  return new Backend({
    ...backendCommand(),
    env: {
      ...inherited,
      SPENDTRACKER_PROFILE: "desktop",
      SPENDTRACKER_DATA_DIR: places.ledger,
      // Windows pipes otherwise speak the ANSI code page (#319).
      PYTHONUTF8: "1",
      // The sandbox's own temporary folder on a Mac; harmless elsewhere.
      TMPDIR: app.getPath("temp"),
      ...extra,
    },
    log: log ?? undefined,
  });
}

/** Start the backend through its checks. Null when it did not start; the person has been told why. */
async function open(acceptLossy = false): Promise<Backend | null> {
  const backend = spawnBackend(acceptLossy ? { SPENDTRACKER_ACCEPT_LOSSY: "1" } : {});
  const started = await backend.started(() => splash?.say(say(SPLASH.upgrading), say(SPLASH.upgradingDetail)));
  if (started.kind === "ready") return backend;
  stopping = true;
  await backend.stop(10_000);
  stopping = false;
  if (started.kind === "died") {
    await couldNotStart(started.said);
    return null;
  }
  const launch = started.launch;
  if (launch.outcome === "declined") {
    return (await confirmLossy(launch)) ? open(true) : null;
  }
  await stoppedAtStart(launch);
  return null;
}

async function confirmLossy(launch: Launch): Promise<boolean> {
  splash?.close();
  splash = null;
  const answer = await ask({
    type: "warning",
    message: say(DIALOG.lossyTitle),
    detail: say(DIALOG.lossyDetail((launch.lossy ?? []).join("; "))),
    buttons: [say(BUTTON.update), say(BUTTON.quit)],
    defaultId: 1,
    cancelId: 1,
  });
  if (answer.response !== 0) return false;
  splash = new Splash(say(SPLASH.upgrading));
  return true;
}

async function stoppedAtStart(launch: Launch): Promise<void> {
  splash?.close();
  splash = null;
  if (launch.outcome === "newer") {
    await ask({ type: "error", message: say(DIALOG.newerTitle), detail: say(DIALOG.newerDetail), buttons: [say(BUTTON.quit)] });
    return;
  }
  if (launch.outcome === "busy") {
    await ask({ type: "error", message: say(DIALOG.busyTitle), detail: say(DIALOG.busyDetail), buttons: [say(BUTTON.quit)] });
    return;
  }
  if (launch.outcome === "migration-failed" || launch.outcome === "rows-dropped") {
    const backup = launch.backup ?? places.ledger;
    const answer = await ask({
      type: "error",
      message: say(launch.outcome === "rows-dropped" ? DIALOG.droppedTitle : DIALOG.failedTitle),
      detail: say(DIALOG.failedDetail(backup)),
      buttons: [say(BUTTON.showBackup), say(BUTTON.quit)],
      defaultId: 0,
      cancelId: 1,
    });
    if (answer.response === 0) shell.showItemInFolder(backup);
    return;
  }
  await couldNotStart((launch.log ?? []).join("\n"));
}

async function couldNotStart(said: string): Promise<void> {
  splash?.close();
  splash = null;
  const answer = await ask({
    type: "error",
    message: say(DIALOG.couldNotStartTitle),
    detail: said.trim().split("\n").slice(-12).join("\n"),
    buttons: [say(BUTTON.showLog), say(BUTTON.quit)],
    defaultId: 1,
    cancelId: 1,
  });
  if (answer.response === 0) shell.showItemInFolder(places.log);
}

async function diedWhileOpen(): Promise<void> {
  await ask({ type: "error", message: say(DIALOG.stoppedTitle), detail: say(DIALOG.stoppedDetail), buttons: [say(BUTTON.ok)] });
  app.exit(1);
}

// --------------------------------------------------------------------------- //
// Backups
// --------------------------------------------------------------------------- //

function backupsFolder(): string {
  return settings.backupsFolder ?? defaultFolder(app.getPath("documents"), path.join(places.root, "Backups"), sandboxed);
}

/** Run `work` with the sandbox's access to the chosen folder open, when there is one to open. */
function inFolder<T>(work: () => T): T {
  const stop = sandboxed && settings.backupsBookmark ? app.startAccessingSecurityScopedResource(settings.backupsBookmark) : null;
  try {
    return work();
  } finally {
    if (typeof stop === "function") stop();
  }
}

function remember(change: Partial<stored.Settings>): void {
  settings = { ...settings, ...change };
  try {
    stored.save(places.settings, settings);
  } catch (problem) {
    log?.write(`shell: settings not saved: ${String(problem)}\n`);
  }
}

type Reason = "manual" | "daily" | "quit" | "restore";

/** One backup into the backups folder. The path, or null -- said in a dialog when somebody asked for it. */
async function backUp(reason: Reason): Promise<string | null> {
  const backend = live;
  const tell = reason === "manual" || reason === "restore";
  const fail = async (detail: string) => {
    log?.write(`shell: ${reason} backup failed: ${detail}\n`);
    if (tell) await ask({ type: "error", message: say(DIALOG.backupFailedTitle), detail, buttons: [say(BUTTON.ok)] });
    return null;
  };
  if (!backend?.alive) return fail("the ledger is not running");
  const made = await backend.control<Made & { detail?: string }>("backup");
  if (made.status !== 200) return fail(made.data.detail ?? `status ${made.status}`);
  let file: string;
  try {
    file = inFolder(() => deliver(made.data, backupsFolder()).file);
  } catch (problem) {
    return fail(String(problem));
  }
  remember({ backedUpTo: Math.max(settings.backedUpTo, made.data.changes_head) });
  log?.write(`shell: ${reason} backup ${file}\n`);
  if (reason === "manual") {
    const answer = await ask({
      type: "info",
      message: say(DIALOG.backedUpTitle),
      detail: say(DIALOG.backedUpDetail(path.basename(file), path.dirname(file))),
      buttons: [say(BUTTON.ok), say(BUTTON.showInFolder)],
      defaultId: 0,
    });
    if (answer.response === 1) inFolder(() => shell.showItemInFolder(file));
  }
  if (sandboxed && !settings.backupsFolder && !settings.askedForFolder && reason !== "quit") await askForFolder();
  return file;
}

/** The Mac App Store build cannot reach Documents until the person chooses it: ask, once. */
async function askForFolder(): Promise<void> {
  remember({ askedForFolder: true });
  const answer = await ask({
    type: "info",
    message: say(DIALOG.askFolderTitle),
    detail: say(DIALOG.askFolderDetail),
    buttons: [say(BUTTON.chooseFolder), say(BUTTON.later)],
    defaultId: 0,
    cancelId: 1,
  });
  if (answer.response === 0) await chooseFolder();
}

async function chooseFolder(): Promise<void> {
  const picked = await pick({
    title: say(DIALOG.chooseFolderTitle),
    buttonLabel: say(BUTTON.useFolder),
    defaultPath: settings.backupsFolder ?? app.getPath("documents"),
    properties: ["openDirectory", "createDirectory"],
    securityScopedBookmarks: sandboxed,
  });
  if (picked.canceled || picked.filePaths.length === 0) return;
  remember({ backupsFolder: picked.filePaths[0], backupsBookmark: picked.bookmarks?.[0] || null, askedForFolder: true });
}

async function dailyBackup(): Promise<void> {
  const today = localDay(new Date());
  if (settings.lastDailyBackup === today || !live) return;
  const state = await live.control<{ changes_head: number }>("state");
  // A ledger nobody has written to yet has nothing worth keeping.
  if (state.status !== 200 || state.data.changes_head === 0) return;
  if (await backUp("daily")) remember({ lastDailyBackup: today });
}

async function restoreFromBackup(): Promise<void> {
  const picked = await inFolder(() =>
    pick({
      title: say(DIALOG.restorePick),
      defaultPath: backupsFolder(),
      properties: ["openFile"],
      filters: [{ name: say(DIALOG.restoreFilter), extensions: ["sqlite3", "zip"] }],
      securityScopedBookmarks: sandboxed,
    }),
  );
  if (picked.canceled || picked.filePaths.length === 0) return;
  const chosen = picked.filePaths[0];
  const confirm = await ask({
    type: "warning",
    message: say(DIALOG.restoreTitle),
    detail: say(DIALOG.restoreDetail(path.basename(chosen))),
    buttons: [say(BUTTON.restore), say(BUTTON.cancel)],
    defaultId: 1,
    cancelId: 1,
  });
  if (confirm.response !== 0) return;

  // First, before anything else can touch the backups folder: the backend
  // cannot read a file picked outside its sandbox, so the shell, which the
  // picker gave it to, copies it in.
  const access = sandboxed && picked.bookmarks?.[0] ? app.startAccessingSecurityScopedResource(picked.bookmarks[0]) : null;
  let copy: string;
  try {
    copy = inFolder(() => bringIn(chosen, places.incoming));
  } finally {
    if (typeof access === "function") access();
  }
  // Then the ledger as it is now, so the restore can be undone by restoring this.
  if (!(await backUp("restore"))) {
    fs.rmSync(places.incoming, { recursive: true, force: true });
    return;
  }

  const running = live;
  live = null;
  stopping = true;
  await running?.stop();
  jar.clear();
  const restoring = spawnBackend({ SPENDTRACKER_RESTORE_FROM: copy });
  const answer = await restoring.restored();
  await restoring.stop(10_000);
  stopping = false;
  fs.rmSync(places.incoming, { recursive: true, force: true });

  window?.hide();
  splash = new Splash(say(SPLASH.opening));
  const next = await open();
  if (!next) {
    app.exit(1);
    return;
  }
  adopt(next);
  await window?.loadURL(`${ORIGIN}/`);
  window?.show();
  splash?.close();
  splash = null;

  if (answer.kind === "restored") {
    await ask({ type: "info", message: say(DIALOG.restoredTitle), detail: say(DIALOG.restoredDetail), buttons: [say(BUTTON.ok)] });
  } else {
    const detail =
      answer.kind === "stopped" && answer.restore.outcome === "newer" ? say(DIALOG.notRestoredNewer) : answer.kind === "died" ? answer.said.trim().split("\n").slice(-8).join("\n") : answer.restore.reason;
    await ask({ type: "error", message: say(DIALOG.notRestoredTitle), detail, buttons: [say(BUTTON.ok)] });
  }
}

// --------------------------------------------------------------------------- //
// Windows and what they may do
// --------------------------------------------------------------------------- //

function isOurs(url: string): boolean {
  return url === ORIGIN || url.startsWith(`${ORIGIN}/`);
}

/** Outside links go to the system browser; nothing else leaves the app. */
function openOutside(url: string): void {
  if (/^(https?:|mailto:)/i.test(url)) void shell.openExternal(url);
}

function webPreferences(): Electron.WebPreferences {
  return {
    preload: path.join(__dirname, "preload.cjs"),
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    webviewTag: false,
    spellcheck: true,
    additionalArguments: [`${SYSTEM_LOCALE_ARG}${app.getSystemLocale()}`],
  };
}

function guard(contents: WebContents): void {
  contents.on("will-navigate", (event, url) => {
    if (!isOurs(url)) {
      event.preventDefault();
      openOutside(url);
    }
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.on("context-menu", (_event, params) => {
    const items = contextTemplate(params, contents, (text) => clipboard.writeText(text));
    if (items.length) Menu.buildFromTemplate(items).popup({ window: BrowserWindow.fromWebContents(contents) ?? undefined });
  });
  // A same-origin `_blank` (a receipt, a report) opens an app window; anything
  // else opens in the browser.
  contents.setWindowOpenHandler(({ url }) => {
    if (isOurs(url)) return { action: "allow", overrideBrowserWindowOptions: { width: 1000, height: 800, webPreferences: webPreferences() } };
    openOutside(url);
    return { action: "deny" };
  });
}

function setMenu(): void {
  Menu.setApplicationMenu(
    menu.build({
      backUpNow: () => void backUp("manual"),
      restore: () => void restoreFromBackup(),
      chooseFolder: () => void chooseFolder(),
      showBackups: () => {
        const folder = backupsFolder();
        inFolder(() => {
          fs.mkdirSync(folder, { recursive: true });
          void shell.openPath(folder);
        });
      },
      showLedger: () => void shell.openPath(places.ledger),
      about: () =>
        void ask({
          type: "info",
          message: "Spend Tracker",
          detail: say(DIALOG.aboutDetail(app.getVersion())),
          buttons: [say(BUTTON.ok)],
        }),
    }),
  );
}

function createWindow(): BrowserWindow {
  const made = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 380,
    minHeight: 520,
    show: false,
    title: "Spend Tracker",
    webPreferences: webPreferences(),
  });
  made.once("ready-to-show", () => {
    made.show();
    splash?.close();
    splash = null;
  });
  made.webContents.on("render-process-gone", (_event, details) => {
    log?.write(`shell: the page stopped (${details.reason}); reloading\n`);
    if (details.reason !== "clean-exit") made.reload();
  });
  void made.loadURL(`${ORIGIN}/`);
  return made;
}

// --------------------------------------------------------------------------- //
// The app
// --------------------------------------------------------------------------- //

let quitting = false;

async function shutdown(): Promise<void> {
  const backend = live;
  if (!backend?.alive) return;
  const state = await backend.control<{ changes_head: number }>("state");
  if (state.status === 200 && state.data.changes_head > Math.max(settings.backedUpTo, 0)) await backUp("quit");
  stopping = true;
  await backend.stop();
}

async function start(): Promise<void> {
  await app.whenReady();
  fs.mkdirSync(places.root, { recursive: true });
  log = fs.createWriteStream(places.log, { flags: "w" });

  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) =>
    callback(permission === "clipboard-sanitized-write" || permission === "fullscreen"),
  );
  protocol.handle("app", handler(current, jar));
  app.on("web-contents-created", (_event, contents) => guard(contents));
  ipcMain.on(LANGUAGE_CHANNEL, (event, locale: unknown) => {
    if (typeof locale !== "string" || !isOurs(event.senderFrame?.url ?? "")) return;
    const now = useLanguage(locale);
    if (settings.language !== now) remember({ language: now });
    setMenu();
  });
  setMenu();

  splash = new Splash(say(SPLASH.opening));
  const backend = await open();
  if (!backend) {
    app.exit(1);
    return;
  }
  adopt(backend);
  window = createWindow();
  window.on("closed", () => {
    window = null;
  });
  void dailyBackup();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!window) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    void shutdown().finally(() => {
      log?.end();
      app.exit(0);
    });
  });
  void start();
}

/** For the end-to-end smoke test, which cannot reach a module's state otherwise. */
(globalThis as Record<string, unknown>).spendTrackerShell = {
  places,
  backupsFolder,
  language,
  settings: () => settings,
  backendExecutable: () => live?.executable ?? null,
};
