import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { BY_NAME, PASSKEY, virtualAuthenticator } from "./helpers";

/**
 * Signing in with a passkey, at both widths (#121).
 *
 * Runs signed out: the projects that run this file carry no stored session.
 * The passkey `passkey.setup.ts` registered is loaded into a fresh virtual
 * authenticator here, as if the same passkey had synced to this device.
 */

test("a passkey signs in, and only where passkeys can work", async ({ page }) => {
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
  // The email field may already have offered it (conditional UI); if the
  // screen is still there, the button does it.
  await expect(button.or(landed)).toBeVisible();
  if (await button.isVisible()) await button.click();
  await expect(landed).toBeVisible();

  // Signed in by that passkey: the server counted the use.
  const listed = await page.evaluate(async () => (await fetch("/api/me/passkeys")).json());
  expect(listed).toHaveLength(1);
  expect(listed[0].last_used_at).not.toBeNull();
  expect(listed[0].usable_here).toBe(true);
});
