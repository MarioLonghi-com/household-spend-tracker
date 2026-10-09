import { expect, test, type Page } from "@playwright/test";
import { catalog, englishOnly, inspect, SEEDED_DATA } from "./l10n-check";

/**
 * Every screen in each draft language, before anyone reviews it (#271).
 *
 * A QA build (`vite build --mode qa`, which `npm run e2e` makes) lets a device
 * store a draft language the way en-XA is stored; no other build can. One
 * project per language and width (see playwright.config.ts), with the
 * browser's own locale set to the same language, so amounts are written the
 * way that language writes them.
 *
 * What a reviewer should never have to report: a message left in English, a
 * placeholder shown as `{0}`, a word cut off or pushing the page sideways on
 * a phone, an amount written the English way, a page whose `lang` is wrong.
 */

function localeOf(info: { project: { metadata: Record<string, unknown> } }): string {
  return String(info.project.metadata.locale);
}

async function openMenu(page: Page) {
  const menu = page.locator('.topbar button[aria-controls="main-nav"]');
  if (await menu.isVisible()) await menu.click();
}

/** Open a screen by its English menu label, said in this language. */
async function go(page: Page, locale: string, english: string) {
  await openMenu(page);
  const label = catalog(locale).get(english) || english;
  await page.locator("nav.side").getByRole("button", { name: label, exact: true }).click();
  await page.waitForLoadState("networkidle");
}

/** Signed in, in this project's language, on the register. */
async function start(page: Page, locale: string) {
  await page.addInitScript((one) => window.localStorage.setItem("spendtracker.locale", one), locale);
  await page.goto("/");
  await page.locator(".register-card").waitFor();
}

test("every screen speaks the language, keeps its placeholders and fits", async ({ page }, info) => {
  test.setTimeout(180_000);
  const locale = localeOf(info);
  await start(page, locale);
  await expect(page.locator("html")).toHaveAttribute("lang", locale);
  const english = englishOnly(locale).filter((one) => !SEEDED_DATA.has(one));

  const problems: string[] = [];
  const record = async (screen: string) => {
    const found = await inspect(page, english);
    for (const one of found.english) problems.push(`${screen}: in English: ${one}`);
    for (const one of found.sentences) problems.push(`${screen}: an English sentence: ${one.slice(0, 90)}`);
    for (const one of found.placeholders) problems.push(`${screen}: a placeholder left: ${one.slice(0, 90)}`);
    if (found.pageScroll) problems.push(`${screen}: the page scrolls sideways`);
    for (const one of found.overflowing) problems.push(`${screen}: wider than its box: ${one}`);
  };

  // Every screen the menu opens: by position, so the walk needs no English.
  const targets = page.locator("nav.side .nav-scroll button");
  const count = await targets.count();
  expect(count).toBeGreaterThan(10);
  for (let at = 0; at < count; at++) {
    await openMenu(page);
    const target = targets.nth(at);
    const name = (await target.innerText()).trim();
    await target.click();
    await page.waitForLoadState("networkidle");
    await record(name);
  }

  // Your own account, from your name under the menu.
  await openMenu(page);
  await page.locator("nav.side .presence button.link").first().click();
  await page.waitForLoadState("networkidle");
  await record("profile");

  expect(problems).toEqual([]);
});

test("amounts are written the way the language writes them", async ({ page }, info) => {
  const locale = localeOf(info);
  await start(page, locale);
  await go(page, locale, "Accounts");
  // The text exactly as drawn: `toHaveText` would fold the no-break spaces
  // into ordinary ones, and they are half of what is being checked.
  const balance = (name: string) =>
    expect.poll(() => page.locator("tr", { hasText: name }).first().locator("[data-figure] .amount").textContent());

  // U+00A0 where the language puts a space: between groups and before "kr" in
  // Swedish, after "R$" in Portuguese, before "€" in Spanish.
  if (locale === "sv-SE") {
    await balance("QA krona").toBe("1\u00a0234,56\u00a0kr");
    await balance("QA euro large").toBe("12\u00a0345,67\u00a0€");
  }
  if (locale === "pt-BR") {
    await balance("QA real").toBe("R$\u00a01.234,56");
  }
  if (locale === "es-ES") {
    // Spanish groups only from 10 000.
    await balance("QA euro").toBe("1234,56\u00a0€");
    await balance("QA euro large").toBe("12.345,67\u00a0€");
  }
  // Formatted in the language the words are in, so no amount says otherwise.
  await expect(page.locator(".amount[lang]")).toHaveCount(0);
});

test.describe("with the browser formatting in American English", () => {
  test.use({ locale: "en-US" });

  test("the page is in the language and each amount says it is English", async ({ page }, info) => {
    const locale = localeOf(info);
    await start(page, locale);
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    await go(page, locale, "Accounts");
    const amount = page.locator("tr", { hasText: "QA krona" }).first().locator("[data-figure] .amount");
    await expect.poll(() => amount.textContent()).toBe("SEK\u00a01,234.56");
    await expect(amount).toHaveAttribute("lang", "en-US");
  });
});

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("the sign-in page speaks the language too", async ({ page }, info) => {
    const locale = localeOf(info);
    await page.addInitScript((one) => window.localStorage.setItem("spendtracker.locale", one), locale);
    await page.goto("/");
    await page.locator("input[type=password]").waitFor();
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    const found = await inspect(page, englishOnly(locale));
    expect([found.english, found.sentences, found.placeholders, found.overflowing, found.pageScroll]).toEqual([
      [],
      [],
      [],
      [],
      false,
    ]);
  });
});
