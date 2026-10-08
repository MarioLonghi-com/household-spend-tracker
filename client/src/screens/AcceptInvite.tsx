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
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { useLingui } from "@lingui/react";
import { listText } from "../lib/locale";

interface Started {
  blob: string;
  otpauth_uri: string;
  secret: string;
  recovery_codes: string[];
}

/** The product's name, which stays as it is in every language. */
const PRODUCT = "Spend Tracker";

/** Who invited you, as what, and to which households: one sentence per case. */
function invitedText(state: InviteState): string {
  const inviter = state.invited_by;
  const households = listText(state.households);
  if (state.role === "owner") {
    return state.households.length > 0
      ? t`${inviter} invited you to ${PRODUCT} as an owner, in ${households}.`
      : t`${inviter} invited you to ${PRODUCT} as an owner.`;
  }
  return state.households.length > 0
    ? t`${inviter} invited you to ${PRODUCT} as a member, in ${households}.`
    : t`${inviter} invited you to ${PRODUCT} as a member.`;
}

export function AcceptInvite({ token, onDone }: { token: string; onDone: (user: User) => void }) {
  // Re-renders in a language that arrives after the first render.
  useLingui();
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

  if (looking)
    return (
      <div className="centred muted">
        <Trans>Looking up your invitation…</Trans>
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
          <Trans>Invitations are single-use and they expire. Ask whoever sent it for a fresh one.</Trans>
        </p>
      </div>
    );

  return (
    <div className="centred">
      <h1>
        <Trans>You've been invited</Trans>
      </h1>
      <p className="muted small">
        {invitedText(state)}
        {state.role === "owner"
          ? ` ${t`An owner can administer this instance: people, households and invitations.`}`
          : null}
      </p>

      <Problem error={error} />

      {step === 1 && (
        <div className="card">
          <h2>
            <Trans comment="Heading on the invitation page">1. You</Trans>
          </h2>
          <Field label={t({ message: "Email", comment: "Label of a form field on the invitation page: noun, an email address" })}>
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
              <Trans>
                Filled in from the invitation. Change it if you would rather use another address.
              </Trans>
            </p>
          ) : (
            <p />
          )}
          <Field label={t({ message: "Your name", comment: "Label of a form field on the invitation page" })}>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus={Boolean(state.email)}
            />
          </Field>
          <p />
          <Field label={t({ message: "Password", comment: "Label of a form field on the invitation page: noun. See GLOSSARY.md" })}>
            <input
              type="password"
              name="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
          <p className="muted small">
            <Trans>At least 12 characters. Length is what matters.</Trans>
          </p>
          <button
            className="primary"
            disabled={busy || !email || !name || password.length < 12}
            onClick={begin}
          >
            <Trans comment="Button on the invitation page: go on to the next step">Continue</Trans>
          </button>
        </div>
      )}

      {step === 2 && started && (
        <div className="card">
          <h2>
            <Trans comment="Heading on the invitation page">2. Your authenticator</Trans>
          </h2>
          <p className="muted small">
            <Trans>
              Scan this with your authenticator app, then type the six digits it shows. We check
              the code now, so you find out it works here rather than the next time you sign in.
            </Trans>
          </p>
          <div style={{ background: "#fff", padding: 12, width: "fit-content", margin: "8px 0" }}>
            <QRCodeSVG value={started.otpauth_uri} size={168} />
          </div>
          <p className="small muted">
            <Trans>
              Can't scan? Enter this key by hand: <span className="mono">{started.secret}</span>
            </Trans>
          </p>
          <Field label={t`The six digits`}>
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
            <Trans>Check the code</Trans>
          </button>
        </div>
      )}

      {step === 3 && started && (
        <div className="card">
          <h2>
            <Trans comment="Heading on the invitation page">3. Recovery codes</Trans>
          </h2>
          <RecoveryCodeSheet
            codes={started.recovery_codes}
            action={t({ message: "Finish", comment: "Button on the invitation page: finish the steps" })}
            busy={busy}
            onStored={finish}
          />
        </div>
      )}
    </div>
  );
}
