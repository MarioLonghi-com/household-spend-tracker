import { expect, test as setup } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { BY_NAME, credentials, nextCode, PASSKEY, signIn, virtualAuthenticator } from "./helpers";

/**
 * Register one passkey, through the browser, and keep it for the specs (#121).
 *
 * Once, here, rather than in each spec: adding a passkey costs a step-up --
 * the password and a fresh authenticator code -- and every code burns its
 * 30-second window, so each registration would cost a window of its own.
 * The screens that add one come with #122; until then this drives the same
 * endpoints from the page: the step-up, the options,
 * `navigator.credentials.create()` against Chrome's virtual authenticator,
 * and the answer. Then the credential, private key included, is read back out
 * of the virtual authenticator so each spec can load it into its own.
 */
setup("register a passkey", async ({ page }) => {
  setup.setTimeout(120_000);
  // By name: passkeys are not offered at 127.0.0.1, an IP address.
  await signIn(page, BY_NAME);
  const { cdp, authenticatorId } = await virtualAuthenticator(page);

  const who = credentials();
  const code = await nextCode();
  const made = await page.evaluate(
    async ({ password, code }) => {
      const post = (path: string, body: unknown) =>
        fetch(`/api${path}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
      const granted = await post("/me/step-up", { password, code });
      if (!granted.ok) return { step: "step-up", status: granted.status, text: await granted.text() };
      const { token } = await granted.json();
      const offered = await post("/me/passkeys/options", { step_up_token: token });
      if (!offered.ok) return { step: "options", status: offered.status, text: await offered.text() };
      const klass = PublicKeyCredential as unknown as {
        parseCreationOptionsFromJSON: (json: unknown) => PublicKeyCredentialCreationOptions;
      };
      const credential = (await navigator.credentials.create({
        publicKey: klass.parseCreationOptionsFromJSON(await offered.json()),
      })) as PublicKeyCredential & { toJSON(): unknown };
      const stored = await post("/me/passkeys", { credential: credential.toJSON() });
      return { step: "register", status: stored.status, text: await stored.text() };
    },
    { password: who.password, code },
  );
  expect(made, JSON.stringify(made)).toMatchObject({ step: "register", status: 201 });

  const { credentials: held } = await cdp.send("WebAuthn.getCredentials", { authenticatorId });
  expect(held).toHaveLength(1);
  writeFileSync(PASSKEY, JSON.stringify(held[0]));
});
