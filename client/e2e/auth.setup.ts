import { test as setup } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { signIn, STATE } from "./helpers";

/**
 * Sign in once, and let every test reuse the session.
 *
 * Not a speed optimisation -- it is the only thing that works. `totp.verify_
 * and_consume` burns the timestep a code belongs to, so the same six digits
 * cannot be presented twice. Signing in per test meant every test that began
 * inside the same 30-second window as the previous one was refused, and the
 * suite failed in a pattern that looked like flakiness and was in fact the
 * replay protection doing exactly its job.
 */
setup("authenticate", async ({ page }) => {
  // Longer than the default 30s, because the retry path deliberately waits out
  // a 30-second TOTP window before its second attempt. With the default this
  // test failed exactly when it was doing the right thing, and passed when the
  // seed happened to land early in a window -- which reads as flake and is the
  // timeout being wrong.
  setup.setTimeout(90_000);
  mkdirSync(dirname(STATE), { recursive: true });
  await signIn(page);
  await page.context().storageState({ path: STATE });
});
