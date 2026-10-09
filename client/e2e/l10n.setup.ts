import { test as setup } from "@playwright/test";
import { QA_ACCOUNTS } from "./l10n-check";

/**
 * The amounts the per-language pass reads its formatting traps from (#271):
 * one account each in SEK, BRL and EUR, opened with a balance that shows the
 * grouping. Made after every English spec has run, so none of them sees four
 * accounts it did not expect; through the app's own API, from the page, as
 * the signed-in owner.
 */
setup("accounts with amounts worth formatting", async ({ page }) => {
  await page.goto("/");
  await page.locator(".register-card").waitFor();
  await page.evaluate(async (accounts) => {
    const households: { id: string }[] = await (await fetch("/api/households")).json();
    const id = households[0].id;
    const have: { name: string }[] = await (await fetch(`/api/households/${id}/accounts`)).json();
    for (const account of accounts) {
      if (have.some((one) => one.name === account.name)) continue;
      const made = await fetch(`/api/households/${id}/accounts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: account.name, type: "savings", currency: account.currency, opening_balance: account.minor }),
      });
      if (!made.ok) throw new Error(`${account.name}: ${made.status} ${await made.text()}`);
    }
  }, QA_ACCOUNTS);
});
