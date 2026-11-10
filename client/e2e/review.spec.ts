import { expect, test, type Page } from "@playwright/test";

/**
 * Review mode, the way the household uses it (#272): an owner turns it on
 * for this device, picks a language in their profile, suggests a better
 * wording for a message, finds it in Application management's list and its
 * download, and turns review mode off again -- after which the language
 * stays (every served language is anyone's, #58) and only the suggesting is
 * gone.
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

test("an owner reviews a language in the app and suggests a better wording", async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto("/");
  await page.locator(".register-card").waitFor();

  // Review mode off: the four languages are offered all the same (#58), and
  // nothing offers to suggest a wording.
  await openProfile(page);
  const languages = ["English", "português (Brasil)", "español de España", "svenska (Sverige)"];
  await expect(page.getByRole("combobox", { name: "Language" }).locator("option")).toHaveText(languages);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Suggest a better wording" })).toHaveCount(0);

  // On, for this device, from Application management.
  await go(page, "Application management");
  await page.getByRole("checkbox", { name: /Review translations on this device/ }).check();

  // Portuguese is chosen, as anyone could.
  await openProfile(page);
  const picker = page.getByRole("combobox", { name: "Language" });
  await expect(picker.locator("option")).toHaveText(languages);
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

  // Off again: the language stays, the suggesting goes.
  await page.getByRole("checkbox", { name: /Revisar traduções neste dispositivo/ }).uncheck();
  await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
  await expect(page.getByRole("heading", { name: "Gerenciamento do aplicativo", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sugerir uma redação melhor" })).toHaveCount(0);

  // Back to English, through the picker, for the specs that follow.
  await openProfile(page);
  await page.getByRole("combobox", { name: "Idioma" }).selectOption("en");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("heading", { name: "Application management", level: 1 })).toBeVisible();
});
