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
 */

import { useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { Field, Problem } from "../components/bits";
import { dropGrant, keepGrant } from "../lib/recoveryGrant";
import type { User } from "../lib/types";

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
  const [recovered, setRecovered] = useState<{ user: User; keys: number } | null>(null);
  // The server's sentence when its key cannot open this member's authenticator.
  const [replaced, setReplaced] = useState<string | null>(null);
  const [owed, setOwed] = useState<Owed | null>(null);
  const [newCode, setNewCode] = useState("");

  /** Signed in: say what the recovery code revoked, if it revoked anything. */
  function finish(user: User, keys: number) {
    if (keys > 0) setRecovered({ user, keys });
    else onDone(user);
  }

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
        finish(state.user, state.keys_revoked ?? 0);
      } else if (state.needs_code) {
        setNeedsCode(true);
        if (state.key_replaced) {
          toRecoveryCode(
            state.detail ??
              "This server's secret key has been replaced. Use one of your recovery codes.",
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
          <h2>A new authenticator</h2>
          <p className="muted small">
            You're signed in. This server's secret key was replaced, so the authenticator you had
            no longer works here. Scan this with your authenticator app and type the six digits it
            shows — the recovery code you just used covers this, so it costs no other. Then delete
            the old entry for this account from your app.
          </p>
          {owed.offer ? (
            <>
              <div
                style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}
              >
                <QRCodeSVG value={owed.offer.uri} size={168} />
              </div>
              <p className="small muted">
                Can't scan? Enter this key by hand:{" "}
                <span className="mono">{owed.offer.secret}</span>
              </p>
              <Field label="The six digits">
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
                Check the code and finish
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
              Show a code to scan
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
            Not now
          </button>
          <p className="muted small">
            Until you do, every sign-in will ask for another recovery code. To do it later, open
            your account — your name in the menu — and set up a new authenticator there. In this
            tab, while you stay signed in and for up to a day, the code you just used still covers
            it; after that, or anywhere else, it takes one more recovery code.
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
          <h2>You're back in</h2>
          <p>
            Using a recovery code signed you out everywhere, forgot every trusted browser and
            revoked {recovered.keys === 1 ? "1 agent key" : `${recovered.keys} agent keys`}. A
            program that was using {recovered.keys === 1 ? "it" : "one"} will be refused from now
            on; issue a new key on your profile if it should keep working.
          </p>
          <button className="primary" autoFocus onClick={() => onDone(recovered.user)}>
            Continue
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
            <Field label="Email">
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus
                autoComplete="username"
              />
            </Field>
            <p />
            <Field label="Password">
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </Field>
            <p />
            <button className="primary" type="submit" disabled={busy || !email || !password}>
              Sign in
            </button>
          </>
        ) : (
          <>
            <h2>{useRecovery ? "A recovery code" : "Your authenticator"}</h2>
            {replaced && (
              <div className="banner warn" role="alert">
                {replaced}
              </div>
            )}
            <p className="muted small">
              {useRecovery
                ? "One of the ten you stored when this account was set up. Each works once, and using one signs you out everywhere, forgets every trusted browser and revokes every agent key."
                : "This browser hasn't been used here recently, so we need the six digits."}
            </p>
            <Field label={useRecovery ? "Recovery code" : "Code"}>
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
                  Don't ask on this browser for 30 days
                </label>
                <p />
              </>
            )}
            <button className="primary" type="submit" disabled={busy || code.length < 6}>
              Continue
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
                    ? "I have my authenticator after all"
                    : "I've lost my authenticator — use a recovery code"}
                </button>
              </>
            )}
          </>
        )}
      </div>
    </form>
  );
}
