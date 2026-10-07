/**
 * Following an account reset link (#284).
 *
 * The account was shut when the link was issued: every session ended, and the
 * password, the authenticator or both were reset. This page says who did it
 * and when, sets whatever the link is for, and shows the new recovery codes
 * once. It does not sign anybody in -- a session needs a password and a
 * second factor, and the link supplies at most one -- so it ends at the
 * sign-in page.
 */

import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { formatInstant } from "../lib/time";
import { Field, Problem } from "../components/bits";
import type { ResetState } from "../lib/types";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { useLingui } from "@lingui/react";

interface Offer {
  blob: string;
  otpauth_uri: string;
  secret: string;
}

/**
 * Who reset what, when, and what to do now: whole sentences, one per case,
 * so a translator sees each as it is read.
 */
function resetText(state: ResetState, when: string): string {
  const by = state.reset_by;
  const email = state.email;
  const both = state.password && state.authenticator;
  if (by) {
    if (both)
      return t`${by} reset your password and your authenticator for ${email} on ${when}. Every browser that was signed in as you has been signed out. Set both again below.`;
    return state.password
      ? t`${by} reset your password for ${email} on ${when}. Every browser that was signed in as you has been signed out. Set it again below.`
      : t`${by} reset your authenticator for ${email} on ${when}. Every browser that was signed in as you has been signed out. Set it again below.`;
  }
  if (both)
    return t`Your password and your authenticator for ${email} was reset from the server on ${when}. Every browser that was signed in as you has been signed out. Set both again below.`;
  return state.password
    ? t`Your password for ${email} was reset from the server on ${when}. Every browser that was signed in as you has been signed out. Set it again below.`
    : t`Your authenticator for ${email} was reset from the server on ${when}. Every browser that was signed in as you has been signed out. Set it again below.`;
}

export function ResetAccount({ token, onDone }: { token: string; onDone: () => void }) {
  // Re-renders in a language that arrives after the first render.
  useLingui();
  const path = `/reset/${encodeURIComponent(token)}`;
  const [state, setState] = useState<ResetState | null>(null);
  const [looking, setLooking] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const [password, setPassword] = useState("");
  const [offer, setOffer] = useState<Offer | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const found = await api.get<ResetState>(path);
        setState(found);
        if (found.authenticator) setOffer(await api.post<Offer>(`${path}/authenticator`, {}));
      } catch (problem) {
        setError(problem);
      } finally {
        setLooking(false);
      }
    })();
  }, [path]);

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  }

  const anotherOffer = () =>
    run(async () => {
      setCode("");
      setOffer(await api.post<Offer>(`${path}/authenticator`, {}));
    });

  const finish = () =>
    run(async () => {
      const done = await api.post<{ recovery_codes: string[] }>(path, {
        password: state?.password ? password : null,
        blob: offer?.blob ?? null,
        code: state?.authenticator ? code : null,
      });
      // The secret has done its job; it does not sit in state while the codes
      // are on screen (#198).
      setOffer(null);
      setCode("");
      setPassword("");
      setCodes(done.recovery_codes);
    });

  if (looking)
    return (
      <div className="centred muted">
        <Trans>Looking up your reset link…</Trans>
      </div>
    );

  if (!state)
    return (
      <div className="centred">
        <h1>
          <Trans>That link doesn't work</Trans>
        </h1>
        <Problem error={error} />
        <p className="muted small">
          <Trans>
            Reset links work once and they expire. Ask an owner of this instance for a new one.
          </Trans>
        </p>
      </div>
    );

  if (codes)
    return (
      <div className="centred">
        <h1>
          <Trans>Done</Trans>
        </h1>
        {codes.length > 0 ? (
          <div className="card">
            <h2>
              <Trans>Your new recovery codes</Trans>
            </h2>
            <p className="muted small">
              <Trans>
                Each one works once, and they are shown only now. Your old ones no longer work.
                They are how you get back in if you lose your phone but still know your password.
              </Trans>
            </p>
            <div className="codes">
              {codes.map((one) => (
                <span key={one}>{one}</span>
              ))}
            </div>
            <label className="small">
              <input
                type="checkbox"
                checked={saved}
                onChange={(e) => setSaved(e.target.checked)}
                style={{ width: "auto", marginRight: 8 }}
              />
              <Trans>I have stored these somewhere that is not this browser</Trans>
            </label>
            <p />
            <button className="primary" disabled={!saved} onClick={onDone}>
              <Trans>Sign in</Trans>
            </button>
          </div>
        ) : (
          <div className="card">
            <p>
              <Trans>Your new password is set. Sign in with it and your authenticator as usual.</Trans>
            </p>
            <button className="primary" onClick={onDone}>
              <Trans>Sign in</Trans>
            </button>
          </div>
        )}
      </div>
    );

  const ready =
    (!state.password || password.length >= 12) &&
    (!state.authenticator || (offer !== null && code.length >= 6));

  return (
    <div className="centred">
      <h1>
        <Trans>Reset your sign-in</Trans>
      </h1>
      <p className="muted small">{resetText(state, formatInstant(state.created_at))}</p>
      <p className="muted small">
        <Trans>If you did not expect this, tell an owner of this instance before you go on.</Trans>
      </p>

      <Problem error={error} />

      {state.password && (
        <div className="card">
          <h2>
            <Trans>A new password</Trans>
          </h2>
          <Field label={t`Password`}>
            <input
              type="password"
              name="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              autoFocus
            />
          </Field>
          <p className="muted small">
            <Trans>At least 12 characters. Length is what matters.</Trans>
          </p>
        </div>
      )}

      {state.authenticator && (
        <div className="card">
          <h2>
            <Trans>A new authenticator</Trans>
          </h2>
          {offer ? (
            <>
              <p className="muted small">
                <Trans>
                  Scan this with your authenticator app, then type the six digits it shows. We
                  check the code now, so you find out it works here rather than the next time you
                  sign in. The entry your app had for this account no longer works; delete it.
                </Trans>
              </p>
              <div
                style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}
              >
                <QRCodeSVG value={offer.otpauth_uri} size={168} />
              </div>
              <p className="small muted">
                <Trans>
                  Can't scan? Enter this key by hand: <span className="mono">{offer.secret}</span>
                </Trans>
              </p>
              <Field label={t`The six digits`}>
                <input
                  name="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  inputMode="numeric"
                  maxLength={6}
                  autoComplete="one-time-code"
                  autoFocus={!state.password}
                />
              </Field>
              <p className="small">
                <button className="link" disabled={busy} onClick={anotherOffer}>
                  <Trans>Show a new code to scan</Trans>
                </button>
              </p>
            </>
          ) : (
            <button disabled={busy} onClick={anotherOffer}>
              <Trans>Show a code to scan</Trans>
            </button>
          )}
        </div>
      )}

      <button className="primary" disabled={busy || !ready} onClick={finish}>
        {state.authenticator ? t`Check the code and finish` : t`Set the password`}
      </button>
    </div>
  );
}
