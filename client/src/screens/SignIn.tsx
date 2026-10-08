/**
 * Signing in: a password, then a code -- unless this browser passed one in the
 * last thirty days.
 *
 * In recovery mode (#287) -- the server's key cannot open this member's
 * authenticator -- the password step's answer says so, and the screen asks
 * for a recovery code straight away rather than for a code that cannot work.
 * (The code step refuses with the same flag, for an answer that never said.)
 * The recovery code goes straight on to setting up a new authenticator. The
 * sign-in's answer carries a grant that stands in for the old authenticator
 * there, so the whole way back costs one recovery code -- and the grant is
 * kept for this tab (`recoveryGrant.ts`), so "Not now" or a reload does not
 * turn it into two: the profile spends it later. A reload never shows the
 * "Not now" copy that says so, which is why the shell says it to any member
 * the key still cannot open (`ReenrolmentDue`).
 *
 * Passkeys (#121) are offered only where both the server's state answer and
 * this browser say they can work (#47, decision 4): then there is a "Sign in
 * with a passkey" button, and the email field offers passkeys itself
 * (conditional UI) where the browser supports that. Anywhere else the screen
 * is exactly as it was. A passkey is both factors, so it signs in at once.
 * After a recovery code, the screen says how many passkeys still work, since
 * one may be on the device that was lost (decision 1).
 */

import { useEffect, useRef, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { Field, Problem } from "../components/bits";
import {
  conditionalMediationAvailable,
  getPasskey,
  noteRecoveryReminder,
  passkeyState,
  rememberThisDevice,
  wasDismissed,
} from "../lib/passkeys";
import { dropGrant, keepGrant } from "../lib/recoveryGrant";
import type { User } from "../lib/types";
import { Trans, useLingui } from "@lingui/react/macro";

interface SignInState {
  authenticated: boolean;
  needs_code: boolean;
  user: User | null;
  /** Only after a recovery code: the live agent keys it revoked (#210). */
  keys_revoked?: number;
  /** Only after a recovery code, in recovery mode: leave to re-enrol (#287). */
  reenrolment_grant?: string | null;
  /**
   * Only after the right password, in recovery mode (#287): no code from this
   * member's authenticator can be checked. `detail` is the sentence that says
   * why -- the pair the code step's refusal carries too.
   */
  key_replaced?: boolean;
  detail?: string | null;
  /** Only after a recovery code: passkeys that still work here (#121). */
  passkeys_live?: number;
  /** Only after a passkey sign-in: which one, for "this device" (#47 §3). */
  passkey_id?: string | null;
}

interface Offer {
  token: string;
  secret: string;
  uri: string;
}

/** A new authenticator owed after a recovery sign-in in recovery mode. */
interface Owed {
  user: User;
  keys: number;
  grant: string;
  offer: Offer | null;
}

/** The code step's refusal for a member the server's key cannot open (#287). */
function keyReplaced(problem: unknown): problem is Error {
  const body = (problem as { body?: { key_replaced?: unknown } } | null)?.body;
  return problem instanceof Error && body?.key_replaced === true;
}

export function SignIn({ onDone }: { onDone: (user: User) => void }) {
  const { t } = useLingui();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [trust, setTrust] = useState(true);
  const [needsCode, setNeedsCode] = useState(false);
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  // A recovery code that revoked keys holds the screen long enough to say so:
  // a key found not working a week later reads as a bug, not as this.
  const [recovered, setRecovered] = useState<{
    user: User;
    keys: number;
    passkeys: number;
  } | null>(null);
  // Offered only when the server and this browser both say passkeys work here.
  const [passkeysHere, setPasskeysHere] = useState(false);
  // The email field's passkey suggestions, waiting in the background.
  const waiting = useRef<AbortController | null>(null);
  // The server's sentence when its key cannot open this member's authenticator.
  const [replaced, setReplaced] = useState<string | null>(null);
  const [owed, setOwed] = useState<Owed | null>(null);
  const [newCode, setNewCode] = useState("");

  /**
   * Signed in: say what the recovery code revoked, and which passkeys it left
   * working, if either is anything.
   */
  function finish(user: User, keys: number, passkeys = 0) {
    waiting.current?.abort();
    if (passkeys > 0) noteRecoveryReminder(passkeys);
    if (keys > 0 || passkeys > 0) setRecovered({ user, keys, passkeys });
    else onDone(user);
  }

  /** A passkey's answer, to the server; both factors in one. */
  async function withPasskey(credential: unknown) {
    const state = await api.post<SignInState>("/session/passkey", { credential });
    if (state.authenticated && state.user) {
      if (state.passkey_id) rememberThisDevice(state.passkey_id);
      finish(state.user, 0);
    }
  }

  useEffect(() => {
    let gone = false;
    (async () => {
      const state = await passkeyState();
      if (gone || !state.available) return;
      setPasskeysHere(true);
      if (!(await conditionalMediationAvailable()) || gone) return;
      // The email field offers this browser's passkeys for this site. The
      // request waits until one is picked, and is abandoned when the member
      // signs in some other way or leaves.
      const controller = new AbortController();
      waiting.current = controller;
      try {
        const options = await api.post<unknown>("/session/passkey/options", {});
        const credential = await getPasskey(options, { conditional: true, signal: controller.signal });
        if (!gone) await run(() => withPasskey(credential));
      } catch (problem) {
        if (!gone && !wasDismissed(problem) && !controller.signal.aborted) setError(problem);
      }
    })();
    return () => {
      gone = true;
      waiting.current?.abort();
    };
    // Once, on arrival. `run` and `withPasskey` only read setters.
  }, []);

  /** The button: one prompt, now, instead of waiting in the email field. */
  const passkeyNow = () =>
    run(async () => {
      waiting.current?.abort();
      const options = await api.post<unknown>("/session/passkey/options", {});
      let credential: unknown;
      try {
        credential = await getPasskey(options);
      } catch (problem) {
        if (wasDismissed(problem)) return;
        throw problem;
      }
      await withPasskey(credential);
    });

  /**
   * A new authenticator to scan, asked for with the password this screen
   * still holds from a moment ago. Dropped once it has done that (#198).
   */
  async function offer(next: Omit<Owed, "offer">) {
    setOwed({ ...next, offer: null });
    const made = await api.post<Offer>("/me/authenticator", { current_password: password });
    setPassword("");
    setOwed({ ...next, offer: made });
  }

  /**
   * Recovery mode: the recovery code, and no way back to an authenticator
   * code that cannot work here. The half-finished sign-in is good, so the
   * recovery code goes on from it.
   */
  function toRecoveryCode(sentence: string) {
    setReplaced(sentence);
    setUseRecovery(true);
    setCode("");
  }

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (problem) {
      if (keyReplaced(problem)) {
        // Not an error to show and retry: the code step's refusal, for an
        // answer to the password that did not already say so.
        toRecoveryCode(problem.message);
      } else {
        setError(problem);
      }
    } finally {
      setBusy(false);
    }
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    // Signing in with the password: the email field's passkey offer is done.
    waiting.current?.abort();
    return run(async () => {
      const state = !needsCode
        ? await api.post<SignInState>("/session", { email, password })
        : useRecovery
          ? await api.post<SignInState>("/session/recovery", { code })
          : await api.post<SignInState>("/session/code", { code, trust_this_browser: trust });
      if (state.authenticated && state.user && state.reenrolment_grant) {
        // Kept before anything else can go wrong, so the profile can spend it
        // if this screen never gets to.
        keepGrant(state.user.id, state.reenrolment_grant);
        await offer({
          user: state.user,
          keys: state.keys_revoked ?? 0,
          grant: state.reenrolment_grant,
        });
      } else if (state.authenticated && state.user) {
        finish(state.user, state.keys_revoked ?? 0, state.passkeys_live ?? 0);
      } else if (state.needs_code) {
        setNeedsCode(true);
        if (state.key_replaced) {
          toRecoveryCode(
            state.detail ??
              t`This server's secret key has been replaced. Use one of your recovery codes.`,
          );
        }
      }
    });
  };

  const enrol = (event: React.FormEvent) => {
    event.preventDefault();
    if (!owed?.offer) return;
    const { user, keys, grant, offer: made } = owed;
    return run(async () => {
      await api.post("/me/authenticator/confirm", { token: made.token, code: newCode, grant });
      // Spent: the key opens the new authenticator, so the grant is dead.
      dropGrant();
      // The secret and the code have done their job; neither stays in state.
      setOwed(null);
      setNewCode("");
      finish(user, keys);
    });
  };

  if (owed) {
    return (
      <form className="centred" onSubmit={enrol}>
        <h1>Spend Tracker</h1>
        <Problem error={error} />
        <div className="card">
          <h2>
            <Trans>A new authenticator</Trans>
          </h2>
          <p className="muted small">
            <Trans>
              You're signed in. This server's secret key was replaced, so the authenticator you had
              no longer works here. Scan this with your authenticator app and type the six digits
              it shows — the recovery code you just used covers this, so it costs no other. Then
              delete the old entry for this account from your app.
            </Trans>
          </p>
          {owed.offer ? (
            <>
              <div
                style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}
              >
                <QRCodeSVG value={owed.offer.uri} size={168} />
              </div>
              <p className="small muted">
                <Trans>
                  Can't scan? Enter this key by hand:{" "}
                  <span className="mono">{owed.offer.secret}</span>
                </Trans>
              </p>
              <Field label={t`The six digits`}>
                <input
                  name="one-time-code"
                  value={newCode}
                  onChange={(e) => setNewCode(e.target.value)}
                  inputMode="numeric"
                  maxLength={6}
                  autoComplete="one-time-code"
                  autoFocus
                />
              </Field>
              <p />
              <button className="primary" type="submit" disabled={busy || newCode.length < 6}>
                <Trans>Check the code and finish</Trans>
              </button>
            </>
          ) : (
            <button
              type="button"
              disabled={busy || !password}
              onClick={() =>
                run(() => offer({ user: owed.user, keys: owed.keys, grant: owed.grant }))
              }
            >
              <Trans>Show a code to scan</Trans>
            </button>
          )}
          <p />
          <button
            type="button"
            className="link"
            onClick={() => {
              setOwed(null);
              setPassword("");
              finish(owed.user, owed.keys);
            }}
          >
            <Trans comment="Button on the sign-in page">Not now</Trans>
          </button>
          <p className="muted small">
            <Trans>
              Until you do, every sign-in will ask for another recovery code. To do it later, open
              your account — your name in the menu — and set up a new authenticator there. In this
              tab, while you stay signed in and for up to a day, the code you just used still
              covers it; after that, or anywhere else, it takes one more recovery code.
            </Trans>
          </p>
        </div>
      </form>
    );
  }

  if (recovered) {
    return (
      <div className="centred">
        <h1>Spend Tracker</h1>
        <div className="card" role="status">
          <h2>
            <Trans>You're back in</Trans>
          </h2>
          {recovered.keys > 0 && (
            <p>
              {recovered.keys === 1 ? (
                <Trans>
                  Using a recovery code signed you out everywhere, forgot every trusted browser and
                  revoked 1 agent key. A program that was using it will be refused from now on;
                  issue a new key on your profile if it should keep working.
                </Trans>
              ) : (
                <Trans>
                  Using a recovery code signed you out everywhere, forgot every trusted browser and
                  revoked {recovered.keys} agent keys. A program that was using one will be refused
                  from now on; issue a new key on your profile if it should keep working.
                </Trans>
              )}
            </p>
          )}
          {recovered.passkeys > 0 && (
            <p>
              {recovered.passkeys === 1 ? (
                <Trans>
                  You still have 1 passkey, and a recovery code leaves them working. If one was on
                  the device you lost, remove it in Sign-in methods on your profile — your name in
                  the menu.
                </Trans>
              ) : (
                <Trans>
                  You still have {recovered.passkeys} passkeys, and a recovery code leaves them
                  working. If one was on the device you lost, remove it in Sign-in methods on your
                  profile — your name in the menu.
                </Trans>
              )}
            </p>
          )}
          <button className="primary" autoFocus onClick={() => onDone(recovered.user)}>
            <Trans comment="Button on the sign-in page: go on to the next step">Continue</Trans>
          </button>
        </div>
      </div>
    );
  }

  return (
    <form className="centred" onSubmit={submit}>
      <h1>Spend Tracker</h1>
      <Problem error={error} />

      <div className="card">
        {!needsCode ? (
          <>
            <Field label={t({ message: "Email", comment: "Label of a form field on the sign-in page: noun, an email address" })}>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus
                // `webauthn` lets the browser offer passkeys from this field,
                // and only where passkeys can work at all.
                autoComplete={passkeysHere ? "username webauthn" : "username"}
              />
            </Field>
            <p />
            <Field label={t({ message: "Password", comment: "Label of a form field on the sign-in page: noun. See GLOSSARY.md" })}>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </Field>
            <p />
            <button className="primary" type="submit" disabled={busy || !email || !password}>
              <Trans comment="Button on the sign-in page. See GLOSSARY.md">Sign in</Trans>
            </button>
            {passkeysHere && (
              <>
                <p className="muted small">
                  <Trans comment="Sentence on the sign-in page: conjunction between two choices">or</Trans>
                </p>
                <button type="button" disabled={busy} onClick={passkeyNow}>
                  <Trans>Sign in with a passkey</Trans>
                </button>
              </>
            )}
          </>
        ) : (
          <>
            <h2>{useRecovery ? t`A recovery code` : t({ message: "Your authenticator", comment: "Heading on the sign-in page" })}</h2>
            {replaced && (
              <div className="banner warn" role="alert">
                {replaced}
              </div>
            )}
            <p className="muted small">
              {useRecovery
                ? t`One of the ten you stored when this account was set up. Each works once, and using one signs you out everywhere, forgets every trusted browser and revokes every agent key.`
                : t`This browser hasn't been used here recently, so we need the six digits.`}
            </p>
            <Field label={useRecovery ? t({ message: "Recovery code", comment: "Text on the sign-in page. See GLOSSARY.md" }) : t({ message: "Code", comment: "Text on the sign-in page: noun, a code typed in" })}>
              <input
                name={useRecovery ? "recovery-code" : "one-time-code"}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode={useRecovery ? "text" : "numeric"}
                maxLength={useRecovery ? 64 : 10}
                autoFocus
                spellCheck={false}
                autoComplete={useRecovery ? "off" : "one-time-code"}
              />
            </Field>
            <p />
            {!useRecovery && (
              <>
                <label className="small">
                  <input
                    type="checkbox"
                    checked={trust}
                    onChange={(e) => setTrust(e.target.checked)}
                    style={{ width: "auto", marginRight: 8 }}
                  />
                  <Trans>Don't ask on this browser for 30 days</Trans>
                </label>
                <p />
              </>
            )}
            <button className="primary" type="submit" disabled={busy || code.length < 6}>
              <Trans comment="Button on the sign-in page: go on to the next step">Continue</Trans>
            </button>
            {/* Not after the key was replaced: no code from it can work here. */}
            {!replaced && (
              <>
                <p />
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    setUseRecovery((was) => !was);
                    setCode("");
                    setError(null);
                  }}
                >
                  {useRecovery
                    ? t`I have my authenticator after all`
                    : t`I've lost my authenticator — use a recovery code`}
                </button>
              </>
            )}
          </>
        )}
      </div>
    </form>
  );
}
