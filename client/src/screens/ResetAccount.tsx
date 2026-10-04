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

interface Offer {
  blob: string;
  otpauth_uri: string;
  secret: string;
}

export function ResetAccount({ token, onDone }: { token: string; onDone: () => void }) {
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

  if (looking) return <div className="centred muted">Looking up your reset link…</div>;

  if (!state)
    return (
      <div className="centred">
        <h1>That link doesn't work</h1>
        <Problem error={error} />
        <p className="muted small">
          Reset links work once and they expire. Ask an owner of this instance for a new one.
        </p>
      </div>
    );

  const what =
    state.password && state.authenticator
      ? "your password and your authenticator"
      : state.password
        ? "your password"
        : "your authenticator";

  if (codes)
    return (
      <div className="centred">
        <h1>Done</h1>
        {codes.length > 0 ? (
          <div className="card">
            <h2>Your new recovery codes</h2>
            <p className="muted small">
              Each one works once, and they are shown only now. Your old ones no longer work. They
              are how you get back in if you lose your phone but still know your password.
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
              I have stored these somewhere that is not this browser
            </label>
            <p />
            <button className="primary" disabled={!saved} onClick={onDone}>
              Sign in
            </button>
          </div>
        ) : (
          <div className="card">
            <p>Your new password is set. Sign in with it and your authenticator as usual.</p>
            <button className="primary" onClick={onDone}>
              Sign in
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
      <h1>Reset your sign-in</h1>
      <p className="muted small">
        {state.reset_by
          ? `${state.reset_by} reset ${what} for ${state.email}`
          : `${what[0].toUpperCase()}${what.slice(1)} for ${state.email} was reset from the server`}{" "}
        on {formatInstant(state.created_at)}. Every browser that was signed in as you has been
        signed out. Set {state.password && state.authenticator ? "both" : "it"} again below.
      </p>
      <p className="muted small">
        If you did not expect this, tell an owner of this instance before you go on.
      </p>

      <Problem error={error} />

      {state.password && (
        <div className="card">
          <h2>A new password</h2>
          <Field label="Password">
            <input
              type="password"
              name="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              autoFocus
            />
          </Field>
          <p className="muted small">At least 12 characters. Length is what matters.</p>
        </div>
      )}

      {state.authenticator && (
        <div className="card">
          <h2>A new authenticator</h2>
          {offer ? (
            <>
              <p className="muted small">
                Scan this with your authenticator app, then type the six digits it shows. We check
                the code now, so you find out it works here rather than the next time you sign in.
                The entry your app had for this account no longer works; delete it.
              </p>
              <div
                style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}
              >
                <QRCodeSVG value={offer.otpauth_uri} size={168} />
              </div>
              <p className="small muted">
                Can't scan? Enter this key by hand: <span className="mono">{offer.secret}</span>
              </p>
              <Field label="The six digits">
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
                  Show a new code to scan
                </button>
              </p>
            </>
          ) : (
            <button disabled={busy} onClick={anotherOffer}>
              Show a code to scan
            </button>
          )}
        </div>
      )}

      <button className="primary" disabled={busy || !ready} onClick={finish}>
        {state.authenticator ? "Check the code and finish" : "Set the password"}
      </button>
    </div>
  );
}
