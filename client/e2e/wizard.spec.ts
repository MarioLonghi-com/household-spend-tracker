import { expect, type Page, test } from "@playwright/test";
import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { totp } from "./helpers";

/**
 * The first-run wizard (#458), walked in a browser against an instance that
 * has never been set up.
 *
 * The rest of the suite shares one seeded instance, which is set up before
 * it starts; this spec needs one that is not, and a new one per test, since
 * finishing setup is something an instance does once. So each test starts
 * its own server on a fresh data directory, on `E2E_WIZARD_PORT` (the
 * suite's port plus one unless named), and stops it afterwards.
 *
 * Three runs: the whole wizard in Swedish, whose labels are the longest, with
 * a back-dated account; one that skips the account; and one at 360 px, the
 * narrow layout.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..");
const PY = process.env.PYTHON ?? join(ROOT, ".venv", "bin", "python");
const PORT = process.env.E2E_WIZARD_PORT ?? String(Number(process.env.E2E_PORT ?? "8850") + 1);
const BASE = `http://127.0.0.1:${PORT}`;
const PASSWORD = "a long enough test passphrase";

let server: ChildProcess | null = null;
let data = "";

async function healthy(): Promise<void> {
  for (let tries = 0; tries < 120; tries += 1) {
    try {
      const answer = await fetch(`${BASE}/api/health`);
      if (answer.ok) return;
    } catch {
      // Not up yet.
    }
    await new Promise((done) => setTimeout(done, 250));
  }
  throw new Error(`the wizard's server did not come up on ${PORT}`);
}

test.beforeEach(async () => {
  data = mkdtempSync(join(tmpdir(), "st-wizard-"));
  const env = {
    ...process.env,
    SPENDTRACKER_DATA_DIR: data,
    DATABASE_URL: `sqlite:///${join(data, "spendtracker.sqlite3")}`,
    SPENDTRACKER_ENV: "development",
    SPENDTRACKER_IN_CONTAINER: "0",
    PYTHONPATH: ROOT,
  };
  execFileSync(PY, ["-m", "alembic", "upgrade", "head"], { cwd: ROOT, env, stdio: "ignore" });
  server = spawn(PY, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT], {
    cwd: ROOT,
    env,
    stdio: "ignore",
  });
  await healthy();
});

test.afterEach(async () => {
  server?.kill();
  server = null;
  rmSync(data, { recursive: true, force: true });
});

const token = () => readFileSync(join(data, "setup-token"), "utf8").trim();
const next = (page: Page, name: string | RegExp) => page.getByRole("button", { name, exact: typeof name === "string" }).click();

/** Welcome to recovery: token, owner, an authenticator code, the codes stored. */
async function security(page: Page, words: { cont: string; stored: string }) {
  await next(page, words.cont);
  await next(page, words.cont);
  await page.locator('input[name="setup-token"]').fill(token());
  await next(page, words.cont);
  const fields = page.locator(".wizard-form input");
  await fields.nth(0).fill("Alex");
  await fields.nth(1).fill("alex@example.test");
  await page.locator('input[name="new-password"]').fill(PASSWORD);
  await next(page, words.cont);
  // Passkeys are never offered at an IP address: the authenticator it is.
  const secret = (await page.locator(".wizard-choice .mono").textContent())!.trim();
  await page.locator('input[name="one-time-code"]').fill(totp(secret));
  await next(page, words.cont);
  await page.getByLabel(words.stored).check();
  await next(page, words.cont);
}

/** A select by its label alone: a select's accessible name also holds its options. */
const field = (page: Page, label: string) =>
  page.locator("label.field", { has: page.locator(":scope > span", { hasText: new RegExp(`^${label}$`) }) }).locator("select");

async function api<T>(page: Page, path: string): Promise<T> {
  const answer = await page.request.get(`${BASE}/api${path}`);
  expect(answer.ok(), path).toBe(true);
  return answer.json() as Promise<T>;
}

type Household = { id: string; name: string; base_currency: string; date_format: string; week_start: string };

test.describe("the first-run wizard", () => {
  test.use({ locale: "sv-SE", viewport: { width: 1280, height: 800 } });

  test("in Swedish, with a back-dated first account", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByRole("heading", { name: "Nu ställer vi in ditt hushåll" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Installationssteg" })).toBeVisible();
    await security(page, { cont: "Fortsätt", stored: "Jag har sparat den någon annanstans än på den här datorn" });

    await expect(page.getByRole("heading", { name: "Namnge ditt hushåll" })).toBeVisible();
    await page.getByLabel("Hushållets namn").fill("Alex hushåll");
    await next(page, "Fortsätt");

    await expect(page.getByRole("heading", { name: "Var har du dina pengar?" })).toBeVisible();
    await field(page, "Land").selectOption("SE");
    await field(page, "Veckans första dag").selectOption("sunday");
    const preview = page.locator(".wizard-preview");
    await expect(preview).toContainText("kr");
    await expect(preview).toContainText(/\d{4}-\d{2}-\d{2}/);
    await expect(preview).toContainText("Matvaror");
    await next(page, "Fortsätt");

    await expect(page.getByRole("heading", { name: "Välj dina första kategorier" })).toBeVisible();
    await page.getByRole("button", { name: /Det viktigaste/ }).click();
    await next(page, "Fortsätt");

    await expect(page.getByRole("heading", { name: "Lägg till ditt första konto" })).toBeVisible();
    await page.getByLabel("Kontonamn").fill("Vardagskonto");
    await page.getByLabel("Startdatum").fill("2023-01-01");
    await page.getByLabel("Ingående saldo").fill("1234,56");
    await next(page, "Fortsätt");

    await expect(page.getByRole("heading", { name: "Kontrollera och skapa" })).toBeVisible();
    await next(page, "Skapa mitt hushåll");
    await expect(page.getByRole("heading", { name: "Klart" })).toBeVisible();

    const [household] = await api<Household[]>(page, "/households");
    expect([household.name, household.base_currency, household.date_format, household.week_start]).toEqual([
      "Alex hushåll", "SEK", "YYYY-MM-DD", "sunday",
    ]);
    const groups = await api<{ name: string; categories: { name: string }[] }[]>(page, `/households/${household.id}/categories`);
    expect(groups.map((group) => group.name)).toEqual(["Inkomster", "Räkningar", "Vardag"]);
    expect(groups.flatMap((group) => group.categories)).toHaveLength(11);
    const accounts = await api<{ id: string; name: string; currency: string }[]>(page, `/households/${household.id}/accounts`);
    expect(accounts.map((one) => [one.name, one.currency])).toEqual([["Vardagskonto", "SEK"]]);
    const register = await api<{ transactions: { date: string; amount: number }[] }>(
      page,
      `/households/${household.id}/transactions?account_id=${accounts[0].id}`,
    );
    expect(register.transactions.map((row) => [row.date, row.amount])).toEqual([["2023-01-01", 123456]]);
  });
});

test.describe("the first-run wizard, in English", () => {
  test.use({ locale: "en-US", viewport: { width: 1280, height: 800 } });

  test("skipping the first account makes none, and offers it next", async ({ page }) => {
    await page.goto(BASE);
    await security(page, { cont: "Continue", stored: "I have stored this somewhere other than this computer" });
    await expect(page.getByRole("heading", { name: "Name your household" })).toBeVisible();
    await next(page, "Continue");
    await next(page, "Continue");
    await next(page, "Continue");
    await expect(page.getByRole("heading", { name: "Add your first account" })).toBeVisible();
    await next(page, "Skip for now");
    await expect(page.getByText("Skipped — add one later from Accounts")).toBeVisible();
    await next(page, "Create my household");
    await expect(page.getByRole("heading", { name: "You're set up" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Add your first account" })).toBeVisible();

    const [household] = await api<Household[]>(page, "/households");
    expect(household.name).toBe("Alex's money");
    expect(await api<unknown[]>(page, `/households/${household.id}/accounts`)).toEqual([]);
  });
});

test.describe("the first-run wizard on a phone", () => {
  test.use({ locale: "pt-BR", viewport: { width: 360, height: 740 } });

  test("at 360 px: no rail, a progress bar, and nothing wider than the screen", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByRole("heading", { name: "Vamos configurar a sua casa" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Passos da configuração" })).toBeHidden();
    await expect(page.locator(".wizard-progress")).toContainText("Passo 1 de 11");
    await security(page, { cont: "Continuar", stored: "Guardei isto em outro lugar além deste computador" });
    await expect(page.locator(".wizard-progress")).toContainText("Passo 7 de 11");
    await expect(page.locator(".wizard-progress")).toContainText("Sua casa · Casa");
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(wide).toBe(false);
    await next(page, "Continuar");
    await next(page, "Continuar");
    await next(page, "Continuar");
    await next(page, "Pular por enquanto");
    await next(page, "Criar minha casa");
    await expect(page.getByRole("heading", { name: "Tudo pronto" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
});
