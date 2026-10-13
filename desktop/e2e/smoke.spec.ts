/**
 * The packaged desktop app, from a first launch to a relaunch (#61).
 *
 * The data goes in through the window's own `fetch` -- through the shell's
 * scheme handler, its cookie jar and the frozen backend, which is what is
 * under test -- rather than by clicking through screens the client's suite
 * already walks. Native dialogs cannot be clicked from here, so the one or two
 * a step raises are answered by replacing `dialog` in the main process.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));
const DIST = path.join(HERE, "..", "dist");

/** The packaged app: SPENDTRACKER_DESKTOP_APP, else where electron-builder put it. */
function executable(): string {
  if (process.env.SPENDTRACKER_DESKTOP_APP) return process.env.SPENDTRACKER_DESKTOP_APP;
  if (process.platform === "win32") return path.join(DIST, "win-unpacked", "Spend Tracker.exe");
  return path.join(DIST, "mac-arm64", "Spend Tracker.app", "Contents", "MacOS", "Spend Tracker");
}

const PASSWORD = "a long and entirely invented passphrase";
const EMAIL = "jane@example.test";

function freshRoot(): { root: string; backups: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "st-desktop-"));
  const backups = path.join(root, "Chosen backups");
  // A backups folder chosen in advance, so nothing is written to Documents.
  fs.writeFileSync(path.join(root, "shell.json"), JSON.stringify({ backupsFolder: backups, askedForFolder: true }));
  return { root, backups };
}

async function launch(root: string): Promise<ElectronApplication> {
  return electron.launch({
    executablePath: executable(),
    env: { ...process.env, SPENDTRACKER_DESKTOP_ROOT: root },
    timeout: 120_000,
  });
}

/** The app's window, not the splash. */
async function appWindow(app: ElectronApplication): Promise<Page> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const found = app.windows().find((page) => page.url().startsWith("app://"));
    if (found) {
      await found.waitForLoadState("domcontentloaded");
      return found;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("the app's window never opened");
}

/** Answer every native dialog with `response` (and `picked` for a file picker). */
async function answerDialogs(app: ElectronApplication, response: number, picked?: string): Promise<void> {
  await app.evaluate(
    ({ dialog }, { response, picked }) => {
      const asked: string[] = [];
      (globalThis as Record<string, unknown>).askedInDialogs = asked;
      dialog.showMessageBox = (async (...args: unknown[]) => {
        const options = args[args.length - 1] as { message: string };
        asked.push(options.message);
        return { response, checkboxChecked: false };
      }) as typeof dialog.showMessageBox;
      dialog.showOpenDialog = (async () => ({ canceled: !picked, filePaths: picked ? [picked] : [] })) as typeof dialog.showOpenDialog;
    },
    { response, picked },
  );
}

async function asked(app: ElectronApplication): Promise<string[]> {
  return app.evaluate(() => ((globalThis as Record<string, unknown>).askedInDialogs as string[]) ?? []);
}

async function clickMenu(app: ElectronApplication, id: string): Promise<void> {
  await app.evaluate(({ Menu }, id) => Menu.getApplicationMenu()?.getMenuItemById(id)?.click(), id);
}

async function menuTitles(app: ElectronApplication): Promise<string[]> {
  return app.evaluate(({ Menu }) => (Menu.getApplicationMenu()?.items ?? []).map((item) => item.label));
}

type Answer = { status: number; data: any };

/** Requests from inside the window: the same path the SPA's own take. */
async function call(page: Page, method: string, url: string, body?: unknown): Promise<Answer> {
  return page.evaluate(
    async ({ method, url, body }) => {
      const response = await fetch(url, {
        method,
        headers: body === undefined ? {} : { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      let data: unknown = text;
      try {
        data = JSON.parse(text);
      } catch {
        // Not JSON: kept as text.
      }
      return { status: response.status, data };
    },
    { method, url, body },
  );
}

async function setUp(page: Page): Promise<string> {
  const begun = await call(page, "POST", "/api/setup/begin", { token: "", email: EMAIL, display_name: "Jane", password: PASSWORD });
  expect(begun.status, JSON.stringify(begun.data)).toBe(200);
  expect(begun.data.recovery_codes).toHaveLength(1);
  expect((await call(page, "POST", "/api/setup/complete", { blob: begun.data.blob, codes_saved: true })).status).toBe(200);
  const house = await call(page, "POST", "/api/households", { name: "Casa Ñandú", base_currency: "EUR" });
  expect(house.status).toBe(201);
  const account = await call(page, "POST", `/api/households/${house.data.id}/accounts`, { name: "Current", type: "checking" });
  expect(account.status).toBe(201);
  await addTransaction(page, house.data.id, account.data.id, "Padaria Ñandú", -1234);
  return house.data.id;
}

async function addTransaction(page: Page, house: string, account: string, payee: string, amount: number): Promise<void> {
  const made = await call(page, "POST", `/api/households/${house}/transactions`, { account_id: account, date: "2026-10-01", amount, payee_name: payee });
  expect(made.status, JSON.stringify(made.data)).toBe(201);
}

async function payees(page: Page, house: string): Promise<string[]> {
  const register = await call(page, "GET", `/api/households/${house}/transactions`);
  expect(register.status).toBe(200);
  return register.data.transactions.map((row: { payee_name: string | null }) => row.payee_name).sort();
}

async function inLanguage(page: Page, locale: string): Promise<void> {
  if (locale === "en") return;
  await page.evaluate((locale) => window.localStorage.setItem("spendtracker.locale", locale), locale);
  await page.reload();
}

function ours(folder: string): string[] {
  return fs.existsSync(folder) ? fs.readdirSync(folder).filter((name) => /^spendtracker-.*\.sqlite3$/.test(name)).sort() : [];
}

test("set up, add, back up, relaunch: the ledger, the language and the backups are all still there", async ({}, info) => {
  const locale = String(info.project.metadata.locale);
  const { root, backups } = freshRoot();

  let app = await launch(root);
  let page = await appWindow(app);
  await inLanguage(page, locale);
  await expect(page.locator("html")).toHaveAttribute("lang", locale);
  const house = await setUp(page);

  // The shell's words follow the window's language.
  const titles = await menuTitles(app);
  if (locale === "en") expect(titles).toContain("File");
  else expect(titles.some((title) => /[^\x00-\x7f]/.test(title) && !title.includes("Spend Tracker"))).toBe(true);

  await answerDialogs(app, 0);
  await clickMenu(app, "back-up-now");
  await expect.poll(() => ours(backups).length, { timeout: 30_000 }).toBe(1);
  expect((await asked(app)).length).toBe(1);

  const executablePath = await app.evaluate(() => (globalThis as any).spendTrackerShell.backendExecutable());
  expect(executablePath.toLowerCase()).toContain(path.join("resources", "backend").toLowerCase());
  await app.close();

  // Closed cleanly: the WAL folded back in, the process gone.
  const wal = path.join(root, "Ledger", "spendtracker.sqlite3-wal");
  expect(!fs.existsSync(wal) || fs.statSync(wal).size === 0).toBe(true);

  app = await launch(root);
  page = await appWindow(app);
  await expect(page.locator("html")).toHaveAttribute("lang", locale);
  // A password at every launch: the session did not survive.
  expect((await call(page, "GET", "/api/me")).status).toBe(401);
  await expect(page.locator('input[type="password"]')).toBeVisible();
  expect((await call(page, "POST", "/api/session", { email: EMAIL, password: PASSWORD })).status).toBe(200);
  expect(await payees(page, house)).toEqual(["Padaria Ñandú"]);
  if (locale !== "en") expect(await menuTitles(app)).not.toContain("File");
  await app.close();
});

test("restore from a backup puts its ledger back, and asks for the password again", async ({}, info) => {
  const locale = String(info.project.metadata.locale);
  const { root, backups } = freshRoot();
  const app = await launch(root);
  const page = await appWindow(app);
  await inLanguage(page, locale);
  const house = await setUp(page);

  await answerDialogs(app, 0);
  await clickMenu(app, "back-up-now");
  await expect.poll(() => ours(backups).length, { timeout: 30_000 }).toBe(1);
  const kept = path.join(backups, ours(backups)[0]);

  const accounts = await call(page, "GET", `/api/households/${house}/accounts`);
  await addTransaction(page, house, accounts.data[0].id, "After the backup", -500);
  expect(await payees(page, house)).toEqual(["After the backup", "Padaria Ñandú"]);

  // Both dialogs -- "replace the ledger?" and "restored" -- answered with their first button.
  await answerDialogs(app, 0, kept);
  await clickMenu(app, "restore");
  await expect.poll(async () => (await asked(app)).length, { timeout: 120_000 }).toBeGreaterThanOrEqual(2);

  const window = await appWindow(app);
  await expect.poll(async () => (await call(window, "GET", "/api/me")).status, { timeout: 60_000 }).toBe(401);
  expect((await call(window, "POST", "/api/session", { email: EMAIL, password: PASSWORD })).status).toBe(200);
  expect(await payees(window, house)).toEqual(["Padaria Ñandú"]);
  // The ledger as it was before the restore was backed up first.
  expect(ours(backups).length).toBe(2);
  await app.close();
});
