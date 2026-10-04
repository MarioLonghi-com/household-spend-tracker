/**
 * Accepting an invitation.
 *
 * The same four steps the first owner walked, minus the setup token — the link
 * is the credential. Nothing is written until the last step, so a person who
 * closes the tab halfway has not half-created an account.
 */

import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { Field, Problem } from "../components/bits";
import type { InviteState, User } from "../lib/types";
import { RecoveryCodeSheet } from "../components/RecoveryCodes";

interface Started {
  blob: string;
  otpauth_uri: string;
  secret: string;
  recovery_codes: string[];
}

export function AcceptInvite({ token, onDone }: { token: string; onDone: (user: User) => void }) {
  const [state, setState] = useState<InviteState | null>(null);
  const [looking, setLooking] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const [step, setStep] = useState(1);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [started, setStarted] = useState<Started | null>(null);
  const [blob, setBlob] = useState("");
  const [code, setCode] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const found = await api.get<InviteState>(`/invite/${encodeURIComponent(token)}`);
        setState(found);
        if (found.email) setEmail(found.email);
      } catch (problem) {
        setError(problem);
      } finally {
        setLooking(false);
      }
    })();
  }, [token]);

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

  const begin = () =>
    run(async () => {
      const result = await api.post<Started>("/invite/begin", {
        token,
        email,
        display_name: name,
        password,
      });
      setStarted(result);
      setBlob(result.blob);
      setStep(2);
    });

  const enrol = () =>
    run(async () => {
      const result = await api.post<{ blob: string }>("/invite/enrol", { blob, code });
      setBlob(result.blob);
      // Step 3 shows only the recovery codes. The authenticator's secret has
      // done its job, so it does not sit in state until the page closes (#198).
      setCode("");
      setStarted((was) => was && { ...was, secret: "", otpauth_uri: "" });
      setStep(3);
    });

  const finish = () =>
    run(async () => {
      const user = await api.post<User>("/invite/complete", { blob, codes_saved: true });
      onDone(user);
    });

  if (looking) return <div className="centred muted">Looking up your invitation…</div>;

  if (!state)
    return (
      <div className="centred">
        <h1>That link doesn't work</h1>
        <Problem error={error} />
        <p className="muted small">
          Invitations are single-use and they expire. Ask whoever sent it for a fresh one.
        </p>
      </div>
    );

  return (
    <div className="centred">
      <h1>You've been invited</h1>
      <p className="muted small">
        {state.invited_by} invited you to Spend Tracker as
        {state.role === "owner" ? " an owner" : " a member"}
        {state.households.length > 0 ? `, in ${state.households.join(" and ")}` : null}.
        {state.role === "owner"
          ? " An owner can administer this instance: people, households and invitations."
          : null}
      </p>

      <Problem error={error} />

      {step === 1 && (
        <div className="card">
          <h2>1. You</h2>
          <Field label="Email">
            <input
              type="email"
              name="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus={!state.email}
              autoComplete="username"
            />
          </Field>
          {state.email ? (
            <p className="muted small">
              Filled in from the invitation. Change it if you would rather use another address.
            </p>
          ) : (
            <p />
          )}
          <Field label="Your name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus={Boolean(state.email)}
            />
          </Field>
          <p />
          <Field label="Password">
            <input
              type="password"
              name="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
          <p className="muted small">At least 12 characters. Length is what matters.</p>
          <button
            className="primary"
            disabled={busy || !email || !name || password.length < 12}
            onClick={begin}
          >
            Continue
          </button>
        </div>
      )}

      {step === 2 && started && (
        <div className="card">
          <h2>2. Your authenticator</h2>
          <p className="muted small">
            Scan this with your authenticator app, then type the six digits it shows. We check the
            code now, so you find out it works here rather than the next time you sign in.
          </p>
          <div style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}>
            <QRCodeSVG value={started.otpauth_uri} size={168} />
          </div>
          <p className="small muted">
            Can't scan? Enter this key by hand: <span className="mono">{started.secret}</span>
          </p>
          <Field label="The six digits">
            <input
              name="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoFocus
              maxLength={6}
              autoComplete="one-time-code"
            />
          </Field>
          <p />
          <button className="primary" disabled={busy || code.length < 6} onClick={enrol}>
            Check the code
          </button>
        </div>
      )}

      {step === 3 && started && (
        <div className="card">
          <h2>3. Recovery codes</h2>
          <RecoveryCodeSheet
            codes={started.recovery_codes}
            action="Finish"
            busy={busy}
            onStored={finish}
          />
        </div>
      )}
    </div>
  );
}
