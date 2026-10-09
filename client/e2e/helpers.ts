import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";

// `client/package.json` is `"type": "module"`, so there is no __dirname.
const HERE = dirname(fileURLToPath(import.meta.url));

/** Where the signed-in session is kept between projects. */
export const STATE = join(HERE, ".auth", "state.json");

export type Credentials = { email: string; password: string; totp_secret: string };

export function credentials(): Credentials {
  return JSON.parse(readFileSync(join(HERE, ".credentials.json"), "utf8"));
}

/** RFC 4648 base32, which is what an authenticator secret is written in. */
function base32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    const index = alphabet.indexOf(char);
    if (index === -1) continue;
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let at = 0; at + 8 <= bits.length; at += 8) {
    bytes.push(parseInt(bits.slice(at, at + 8), 2));
  }
  return Buffer.from(bytes);
}

/**
 * The six digits an authenticator would be showing right now.
 *
 * RFC 6238 with the defaults this app uses: SHA-1, 30-second step, 6 digits.
 * Computed here rather than mocked, so the sign-in under test is the real one
 * -- the TOTP check is the gate every other test has to get through, and a
 * stubbed one would prove nothing about it.
 */
export function totp(secret: string, when: number = Date.now()): string {
  const counter = Math.floor(when / 1000 / 30);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", base32(secret)).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    (digest[offset + 1] << 16) |
    (digest[offset + 2] << 8) |
    digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

/** Milliseconds until the next 30-second TOTP window begins. */
function untilNextStep(): number {
  return 30_000 - (Date.now() % 30_000) + 750;
}

/** One pass at the front door. Returns whether it got through. */
async function attempt(page: Page, at: string): Promise<boolean> {
  const who = credentials();
  await page.goto(at);
  await page.getByLabel("Email").fill(who.email);
  await page.getByLabel("Password").fill(who.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await page.getByLabel("Code").fill(totp(who.totp_secret));
  await page.getByRole("button", { name: "Continue" }).click();

  const landed = page.getByRole("heading", { name: "Transactions", exact: true });
  return await landed
    .waitFor({ timeout: 8_000 })
    .then(() => true)
    .catch(() => false);
}

/**
 * Password, then a code. The whole front door, for real.
 *
 * The retry is not flake-handling -- it is two deliberate protections doing
 * their job to the one caller that is not an attacker:
 *
 * 1. `scripts/seed_demo` finishes the setup wizard headlessly, and the wizard
 *    will not finish without a code that verifies. So it presents one, and
 *    `totp.verify_and_consume` **burns that timestep**. A suite that seeds and
 *    immediately signs in is inside the same 30-second window, presenting a
 *    code the server has already seen.
 * 2. A wrong code does not just fail. `sessions.claim_pending` spends the
 *    pending sign-in row "whether or not the code that follows is right", so a
 *    stolen cookie is worth one attempt rather than five minutes of them --
 *    which means the retry cannot resubmit a code, it has to start again from
 *    the password.
 *
 * Hence: a whole fresh pass, in the next window. Both protections stay in the
 * path every test goes through, rather than being stubbed to make the suite
 * convenient.
 */
export async function signIn(page: Page, at: string = "/"): Promise<void> {
  if (await attempt(page, at)) return;
  await page.waitForTimeout(untilNextStep());
  if (await attempt(page, at)) return;
  // A third is not worth having: two failures in different windows is a real
  // failure, and the assertion below reports it with the page's own words.
  await page.getByRole("heading", { name: "Transactions", exact: true }).waitFor();
}

/**
 * The nav, whichever shape it is in.
 *
 * On a phone it is behind the menu button; on a desktop it is simply there.
 * Every test that navigates goes through this, so neither project needs to
 * know which layout it is running against.
 *
 * **Scoped to the nav, and that is not tidiness.** The menu's entries are named
 * after the things they open -- "Categories", "Transfers", "Income vs Expense" --
 * and so is half the furniture on the screen behind it: every sortable column
 * heading is a button called after its column, and the reports index is a card
 * called after its report. Unscoped, `go(page, "Payee")` -- as the payee list's
 * entry was called before #185 -- matched the menu entry *and* the register's
 * Payee column heading, and Playwright refused both.
 */
export async function go(page: Page, label: string): Promise<void> {
  const menu = page.getByRole("button", { name: /Open the menu/ });
  if (await menu.isVisible()) await menu.click();
  await page.locator("nav.side").getByRole("button", { name: label, exact: true }).click();
}

/**
 * The same server by name rather than by address: `localhost` is the one
 * host where passkeys work without HTTPS, and `e2e/serve.sh` makes it the
 * RP ID. At `127.0.0.1` -- the suite's `baseURL` -- they are not offered.
 */
export const BY_NAME = `http://localhost:${process.env.E2E_PORT ?? "8850"}`;

/** Where the passkey the passkey setup registered is kept for the specs. */
export const PASSKEY = join(HERE, ".auth", "passkey.json");

/**
 * A platform authenticator in software, through Chrome DevTools' WebAuthn
 * domain: discoverable credentials, user verification that succeeds, and a
 * user who is always there to touch it. Both e2e projects are Chromium.
 */
export async function virtualAuthenticator(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  return { cdp, authenticatorId };
}

/** A fresh code, in a window no earlier step of this run has spent. */
export async function nextCode(): Promise<string> {
  await new Promise((done) => setTimeout(done, untilNextStep()));
  return totp(credentials().totp_secret);
}
