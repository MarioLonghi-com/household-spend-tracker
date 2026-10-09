import { expect, test, type Page } from "@playwright/test";

/**
 * Review mode, the way the household will use it (#272): an owner turns it on
 * for this device, picks a draft language in their profile, suggests a better
 * wording for a message, finds it in Application management's list and its
 * download, and turns review mode off again -- after which English is back
 * and no language can be chosen.
 *
 * Runs after the English specs (see playwright.config.ts): it leaves a
 * suggestion behind, which is a row in History.
 */

/** The menu, on a phone too, found by what it controls: its name is translated. */
async function openMenu(page: Page) {
  const menu = page.locator('.topbar button[aria-controls="main-nav"]');
  if (await menu.isVisible()) await menu.click();
}

async function go(page: Page, label: string) {
  await openMenu(page);
  await page.locator("nav.side").getByRole("button", { name: label, exact: true }).click();
}

async function openProfile(page: Page) {
  await openMenu(page);
  await page.locator("nav.side .presence button.link").first().click();
}

test("an owner reviews a draft in the app and suggests a better wording", async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto("/");
  await page.locator(".register-card").waitFor();

  // Off: the profile offers no language at all.
  await openProfile(page);
  await expect(page.getByRole("combobox", { name: "Language" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // On, for this device, from Application management.
  await go(page, "Application management");
  await page.getByRole("checkbox", { name: /Review translations on this device/ }).check();

  // The previews are offered beside English; Portuguese is chosen.
  await openProfile(page);
  const picker = page.getByRole("combobox", { name: "Language" });
  await expect(picker.locator("option")).toHaveText([
    "English",
    /Preview — machine translated, under review/,
    /Preview — machine translated, under review/,
    /Preview — machine translated, under review/,
  ]);
  await picker.selectOption("pt-BR");
  await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
  await page.keyboard.press("Escape");

  // A better wording for one message, found by its English.
  const words = `Fotografe um comprovante (${info.project.name})`;
  await page.getByRole("button", { name: "Sugerir uma redação melhor" }).click();
  await page.getByRole("searchbox").fill("Snap a Receipt");
  await page.getByRole("button", { name: "Fotografar um comprovante", exact: true }).click();
  await page.getByRole("textbox", { name: "Sua redação" }).fill(words);
  await page.getByRole("button", { name: "Salvar a sugestão" }).click();
  await expect(page.getByRole("status")).toContainText("Salva.");
  await page.keyboard.press("Escape");

  // Listed, and in the download, as it was written.
  await go(page, "Gerenciamento do aplicativo");
  await expect(page.locator("#translations tr", { hasText: words })).toHaveCount(1);
  const href = await page.getByRole("link", { name: "Todos os idiomas (JSON)" }).getAttribute("href");
  // Fetched by the page itself, as the link's download is: with its cookie.
  const exported = await page.evaluate(async (url) => (await fetch(url)).json(), href!);
  expect(
    exported.suggestions.filter((one: { suggested: string }) => one.suggested === words),
  ).toEqual([expect.objectContaining({ locale: "pt-BR", message: "Snap a Receipt", context: "", suggested: words })]);

  // Off again: English is back, and the profile offers no language.
  await page.getByRole("checkbox", { name: /Revisar traduções neste dispositivo/ }).uncheck();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("heading", { name: "Application management", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Suggest a better wording" })).toHaveCount(0);
  await openProfile(page);
  await expect(page.getByRole("combobox", { name: "Language" })).toHaveCount(0);
});
