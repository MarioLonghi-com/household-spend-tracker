import { expect, test } from "@playwright/test";

/**
 * One pass in the en-XA pseudo-locale, at the phone's width (#53).
 *
 * Every other spec runs pinned to English, and that is where the words are
 * checked. This one checks the opposite: that a second language reaches the
 * shell -- the menu and the tab's title -- and that nothing breaks when it
 * does. At the narrow width, because pseudo-localised words are longer, and a
 * layout that only fits English is a phone layout first.
 *
 * en-XA is never offered in the picker. It is reached the way CI and a
 * developer reach it: by storing it on the device before the app starts.
 */

/** Only printable ASCII: an English word nobody has extracted yet. */
const ascii = /^[\x20-\x7e]+$/;

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("spendtracker.locale", "en-XA");
  });
  await page.goto("/");
  // The register's own heading, pseudo-localised since #55: the screen behind
  // the shell rendered, in the second language.
  await page.locator(".register-card").waitFor();
  await expect(page.getByRole("heading", { level: 1 })).not.toHaveText("Transactions");
});

test("the shell speaks the pseudo-locale, and the screen still works", async ({ page }) => {
  await expect(page.locator("html")).toHaveAttribute("lang", "en-XA");

  // The tab title: the app's name stays, the page's name is translated.
  await expect(page).toHaveTitle(/^Spend Tracker - /);
  const title = await page.title();
  expect(title).not.toContain("Transactions");
  expect(title.replace(/^Spend Tracker - /, "").split(" - ")[0]).not.toMatch(ascii);

  // By what it controls, not by its name: in en-XA its name is pseudo-localised.
  const menu = page.locator('.topbar button[aria-controls="main-nav"]');
  if (await menu.isVisible()) await menu.click();
  const nav = page.locator("nav.side");
  await expect(nav.locator(".nav-head")).toHaveCount(4);
  const heads = await nav.locator(".nav-head").allInnerTexts();
  // Register, Reports and Admin are words; the third is the household's own
  // name, which is data and stays as typed.
  const english = ["Register", "Reports", "Admin"];
  [heads[0], heads[1], heads[3]].forEach((head, i) => {
    expect(head).not.toContain(english[i]);
    expect(head.trim()).not.toMatch(ascii);
  });

  // Moving through the menu still works in the pseudo-locale.
  await nav.locator(".nav-head").nth(1).click();
  await expect(page).not.toHaveTitle(title);
});
