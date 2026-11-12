/**
 * Both factors again, for an act whose effect outlives this session.
 *
 * The same bar and the same shape as issuing a key on the Profile screen: the
 * password and a live authenticator code, turned into a single-use step-up
 * grant *inside the click that spends it*, so the token never sits in the page
 * waiting to be used. See `app/auth/stepup.py` for the argument.
 *
 * A code is burned by the grant it buys, so a second act needs the next code
 * from the authenticator. `spent` clears the field to say so.
 *
 * In recovery mode (#287) -- the server's key cannot open this member's
 * authenticator -- no code can be checked, and a recovery code is not taken
 * here either. The refusal says so, with `key_replaced`, and says to set up a
 * new authenticator from the account first; this adds where the account is
 * (`setUpANewOne`). It used to carry the sign-in's sentence, "use one of
 * your recovery codes", which sent the member to type one into a field that
 * would only refuse it again.
 *
 * **A passkey stands in for the code** (#456). An account with a passkey
 * that works here is offered "Use a passkey" first, and the code as the other
 * choice when it also has an authenticator; an account with no authenticator
 * is never asked for a code. The browser's passkey prompt runs inside the
 * same click that spends the grant, like everything else here.
 */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import { Field } from "./bits";
import { offeredNow, useCapabilities } from "../lib/capabilities";
import { getPasskey, passkeyState } from "../lib/passkeys";
import type { AuthenticatorStatus } from "../lib/types";
import { t } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";

/** Which second factor a step-up is passed with (#456). */
export type StepUpFactor = "code" | "passkey";

export interface StepUpProof {
  password: string;
  code: string;
  /** Unset until the account's factors are known; `StepUpFields` sets it. */
  factor?: StepUpFactor;
}

export const NO_PROOF: StepUpProof = { password: "", code: "" };

export function stepUpReady(proof: StepUpProof): boolean {
  // The password alone on the desktop, where it is the only factor (#320),
  // and with a passkey, whose prompt comes when the button is pressed (#456).
  const needsCode = offeredNow().second_factor && proof.factor !== "passkey";
  return proof.password.length > 0 && (!needsCode || proof.code.trim().length >= 6);
}

/**
 * The caller's second factors, as a step-up needs them: whether a passkey
 * can be offered here and whether there is an authenticator to ask a code
 * from. Shares its query with Sign-in methods, so adding or removing one
 * there is seen here.
 */
export function useStepUpFactors(): { passkey: boolean; code: boolean; known: boolean } {
  const offered = useCapabilities();
  const status = useQuery({
    queryKey: ["authenticator"],
    queryFn: () => api.get<AuthenticatorStatus>("/me/authenticator"),
    enabled: offered.second_factor,
  });
  const state = useQuery({ queryKey: ["passkey-state"], queryFn: passkeyState, staleTime: Infinity });
  if (!offered.second_factor) return { passkey: false, code: false, known: true };
  const known = !!status.data && !!state.data;
  return {
    passkey: (status.data?.passkeys ?? 0) > 0 && state.data?.available === true,
    // Asked for unless the server has said there is none, so an answer that
    // has not arrived yet still shows today's field.
    code: status.data?.enrolled !== false,
    known,
  };
}

/**
 * Said after the server's sentence when the key cannot open your
 * authenticator. A function, so it is read in the language active when it is
 * said rather than the one active when this module loaded.
 */
export function setUpANewOne(): string {
  return t`Your account is under your name in the menu.`;
}

/** Buy one grant. Spend it on the very next request. With a passkey, the
 * browser's prompt runs here, between the options and the grant. */
export async function stepUpToken(proof: StepUpProof): Promise<string> {
  try {
    if (proof.factor === "passkey") {
      const options = await api.post<unknown>("/me/step-up/options");
      const credential = await getPasskey(options);
      const grant = await api.post<{ token: string }>("/me/step-up", {
        password: proof.password,
        credential,
      });
      return grant.token;
    }
    const grant = await api.post<{ token: string }>("/me/step-up", {
      password: proof.password,
      code: proof.code,
    });
    return grant.token;
  } catch (problem) {
    const body = (problem as { body?: Record<string, unknown> } | null)?.body;
    if (problem instanceof Error && body?.key_replaced === true) {
      throw new ApiError(`${problem.message} ${setUpANewOne()}`, 401, undefined, body);
    }
    throw problem;
  }
}

/** The code is dead once a grant has been bought with it. */
export function spent(proof: StepUpProof): StepUpProof {
  return { ...proof, code: "" };
}

export function StepUpFields({
  proof,
  onChange,
  why,
}: {
  proof: StepUpProof;
  onChange: (next: StepUpProof) => void;
  /** One sentence on why this act asks for both. */
  why: string;
}) {
  const { t } = useLingui();
  const offered = useCapabilities();
  const can = useStepUpFactors();
  // The passkey first when there is one (#456); the code otherwise.
  const preferred: StepUpFactor = can.passkey ? "passkey" : "code";
  useEffect(() => {
    if (offered.second_factor && can.known && proof.factor === undefined) {
      onChange({ ...proof, factor: preferred });
    }
  }, [offered.second_factor, can.known, proof, preferred, onChange]);
  const factor = proof.factor ?? preferred;
  const other: StepUpFactor | null =
    factor === "passkey" ? (can.code ? "code" : null) : can.passkey ? "passkey" : null;
  return (
    <>
      <p className="small muted" style={{ marginTop: 0 }}>
        {!offered.second_factor ? (
          <Trans>{why} So this asks for your password again.</Trans>
        ) : factor === "passkey" ? (
          <Trans>{why} So this asks for your password and your passkey — the same as signing in.</Trans>
        ) : (
          <Trans>{why} So this asks for your password and a code — the same as signing in.</Trans>
        )}
      </p>
      <Field label={t({ message: "Your password", comment: "Label of a form field on the password-and-code check" })}>
        <input
          type="password"
          value={proof.password}
          autoComplete="current-password"
          onChange={(e) => onChange({ ...proof, password: e.target.value })}
        />
      </Field>
      {offered.second_factor && factor === "code" ? (
        <>
          <p />
          <Field label={t`The six digits from your authenticator`}>
            <input
              value={proof.code}
              inputMode="numeric"
              autoComplete="one-time-code"
              onChange={(e) => onChange({ ...proof, code: e.target.value })}
            />
          </Field>
        </>
      ) : null}
      {offered.second_factor && factor === "passkey" ? (
        <p className="small muted">
          <Trans>Your browser asks for your passkey when you continue.</Trans>
        </p>
      ) : null}
      {offered.second_factor && other ? (
        <p className="small">
          <button type="button" className="link" onClick={() => onChange({ ...proof, factor: other, code: "" })}>
            {other === "passkey"
              ? t({ message: "Use a passkey instead", comment: "Link on the password-and-code check: switch to the passkey. See GLOSSARY.md" })
              : t({ message: "Use a code from your authenticator instead", comment: "Link on the password-and-code check: switch to the authenticator code. See GLOSSARY.md" })}
          </button>
        </p>
      ) : null}
    </>
  );
}
