/**
 * First boot.
 *
 * Four steps, and nothing is written until the last one. Step 3 will not let
 * you past without a code that actually verifies -- so the authenticator is
 * known to pair before it is the only thing between you and the ledger.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../lib/api";
import { Field, Problem } from "../components/bits";
import type { User } from "../lib/types";
import { RecoveryCodeSheet } from "../components/RecoveryCodes";

interface Started {
  blob: string;
  otpauth_uri: string;
  secret: string;
  recovery_codes: string[];
}

interface SetupState {
  setup_required: boolean;
  /** Where the token was written, from the server. The screen used to say
   *  `data/setup-token`, which is wrong in every container and wrong for any
   *  install since the default data directory moved out of the checkout. */
  token_path?: string | null;
}

export function Setup({ onDone }: { onDone: (user: User) => void }) {
  const state = useQuery({
    queryKey: ["setup", "state"],
    queryFn: () => api.get<SetupState>("/setup/state"),
  });
  const tokenPath = state.data?.token_path ?? null;

  const [step, setStep] = useState(1);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const [token, setToken] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [started, setStarted] = useState<Started | null>(null);
  const [blob, setBlob] = useState("");
  const [code, setCode] = useState("");

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
      const result = await api.post<Started>("/setup/begin", {
        token,
        email,
        display_name: name,
        password,
      });
      setStarted(result);
      setBlob(result.blob);
      setStep(3);
    });

  const enrol = () =>
    run(async () => {
      const result = await api.post<{ blob: string }>("/setup/enrol", { blob, code });
      setBlob(result.blob);
      // Step 4 shows only the recovery codes. The authenticator's secret has
      // done its job, so it does not sit in state until the page closes (#198).
      setCode("");
      setStarted((was) => was && { ...was, secret: "", otpauth_uri: "" });
      setStep(4);
    });

  const finish = () =>
    run(async () => {
      const user = await api.post<User>("/setup/complete", { blob, codes_saved: true });
      onDone(user);
    });

  return (
    <div className="centred">
      <h1>Set up Spend Tracker</h1>
      <p className="muted small">
        Nothing is saved until the last step. If you close this page, this instance is still
        untouched and you can start again.
      </p>

      <Problem error={error} />

      {step === 1 && (
        <div className="card">
          <h2>1. The setup token</h2>
          <p className="muted small">
            It was printed to the server log when this instance started, and written to{" "}
            <span className="mono">{tokenPath ?? "the data directory"}</span>. It changes every
            time the server restarts until setup is finished.
          </p>
          {/* The image has no shell and no `cat`, so the obvious command does
              not work in it. This one uses the interpreter that is always
              there. */}
          <p className="muted small">
            In Docker:{" "}
            <span className="mono">
              docker compose exec app python -c &quot;print(open(&apos;
              {tokenPath ?? "/var/lib/spend-tracker/setup-token"}&apos;).read())&quot;
            </span>
          </p>
          <Field label="Setup token">
            <input
              name="setup-token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoFocus
              spellCheck={false}
              autoComplete="off"
            />
          </Field>
          <p />
          <button className="primary" disabled={!token.trim()} onClick={() => setStep(2)}>
            Continue
          </button>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <h2>2. You</h2>
          <Field label="Email">
            <input
              type="email"
              name="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              autoComplete="username"
            />
          </Field>
          <p />
          <Field label="Your name">
            <input value={name} onChange={(e) => setName(e.target.value)} />
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

      {step === 3 && started && (
        <div className="card">
          <h2>3. Your authenticator</h2>
          <p className="muted small">
            Scan this with your authenticator app, then type the six digits it shows. We check
            the code now, so you find out it works here rather than the next time you sign in.
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

      {step === 4 && started && (
        <div className="card">
          <h2>4. Recovery codes</h2>
          <RecoveryCodeSheet
            codes={started.recovery_codes}
            action="Finish setup"
            busy={busy}
            onStored={finish}
          />
        </div>
      )}
    </div>
  );
}
