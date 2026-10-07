import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { BY_NAME, credentials, nextCode, PASSKEY, virtualAuthenticator } from "./helpers";

/**
 * Signing in with a passkey, at both widths (#121).
 *
 * Runs signed out: the projects that run this file carry no stored session.
 * The passkey `passkey.setup.ts` registered is loaded into a fresh virtual
 * authenticator here, as if the same passkey had synced to this device. Then
 * Sign-in methods (#122) adds a second through the screen, renames it and
 * removes it.
 */

test("a passkey signs in, and Sign-in methods adds, renames and removes one", async ({ page }) => {
  // Adding costs a fresh authenticator code, which can mean waiting out a
  // 30-second window the earlier steps of the run already spent.
  test.setTimeout(120_000);
  // At the IP address the screen is exactly today's: no passkey offered.
  await page.goto("/");
  await expect(page.getByLabel("Password")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toHaveCount(0);

  const { cdp, authenticatorId } = await virtualAuthenticator(page);
  const credential = JSON.parse(readFileSync(PASSKEY, "utf8"));
  // The counter only ever goes up across runs and projects: the server
  // refuses one that goes backwards, as it would from a cloned authenticator.
  credential.signCount = Math.floor(Date.now() / 1000);
  await cdp.send("WebAuthn.addCredential", { authenticatorId, credential });

  await page.goto(BY_NAME);
  const button = page.getByRole("button", { name: "Sign in with a passkey" });
  const landed = page.getByRole("heading", { name: "Transactions", exact: true });
  // Chrome's virtual authenticator answers the email field's conditional
  // request by itself, so the suggestion may sign in before anybody presses
  // anything. Give it a moment; only if the screen is still there, use the
  // button. Pressing while that sign-in is under way would wait on a button
  // that is about to leave the page.
  await expect(button.or(landed)).toBeVisible();
  const bySuggestion = await landed
    .waitFor({ timeout: 5_000 })
    .then(() => true)
    .catch(() => false);
  if (!bySuggestion) await button.click();
  await expect(landed).toBeVisible();

  // Signed in by that passkey: the server counted the use.
  const listed = await page.evaluate(async () => (await fetch("/api/me/passkeys")).json());
  expect(listed).toHaveLength(1);
  expect(listed[0].last_used_at).not.toBeNull();
  expect(listed[0].usable_here).toBe(true);

  // Sign-in methods, under your name in the menu.
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();
  await page.locator("nav").getByRole("button", { name: "Demo", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign-in methods" })).toBeVisible();
  await expect(
    page.getByText("You sign in with a passkey, or with your password and authenticator code."),
  ).toBeVisible();
  const table = page.locator("table.passkeys");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByText("this device", { exact: true })).toBeVisible();

  // The browser would refuse to make a second passkey in an authenticator
  // already holding one for this account -- the options exclude it -- so the
  // copy loaded above leaves this one. The server still has it.
  await cdp.send("WebAuthn.removeCredential", { authenticatorId, credentialId: credential.credentialId });

  await page.getByRole("button", { name: "Add a passkey" }).click();
  await page.getByLabel("Your password").fill(credentials().password);
  await page.getByLabel("The six digits from your authenticator").fill(await nextCode());
  await page.getByRole("button", { name: "Continue to the passkey" }).click();

  // The new row, highlighted, its name ready to type over.
  const name = page.getByLabel(/^New name for /);
  await expect(name).toBeFocused();
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await name.fill("Second key");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(table.getByText("Second key")).toBeVisible();

  await page.getByRole("button", { name: "Remove Second key" }).click();
  const question = page.getByRole("alertdialog");
  await expect(question).toContainText("You will still be able to sign in with your other passkey");
  await question.getByRole("button", { name: "Remove" }).click();
  await expect(table.getByText("Second key")).toHaveCount(0);
  await expect(table.locator("tbody tr")).toHaveCount(1);

  // What the server holds, not only what the screen says.
  const after = await page.evaluate(async () => (await fetch("/api/me/passkeys")).json());
  expect(after.map((one: { label: string }) => one.label)).not.toContain("Second key");
  expect(after).toHaveLength(1);
});
