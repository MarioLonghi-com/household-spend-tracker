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
 * (`SET_UP_A_NEW_ONE`). It used to carry the sign-in's sentence, "use one of
 * your recovery codes", which sent the member to type one into a field that
 * would only refuse it again.
 */

import { ApiError, api } from "../lib/api";
import { Field } from "./bits";

export interface StepUpProof {
  password: string;
  code: string;
}

export const NO_PROOF: StepUpProof = { password: "", code: "" };

export function stepUpReady(proof: StepUpProof): boolean {
  return proof.password.length > 0 && proof.code.trim().length >= 6;
}

/** Said after the server's sentence when the key cannot open your authenticator. */
export const SET_UP_A_NEW_ONE = "Your account is under your name in the menu.";

/** Buy one grant. Spend it on the very next request. */
export async function stepUpToken(proof: StepUpProof): Promise<string> {
  try {
    const grant = await api.post<{ token: string }>("/me/step-up", {
      password: proof.password,
      code: proof.code,
    });
    return grant.token;
  } catch (problem) {
    const body = (problem as { body?: Record<string, unknown> } | null)?.body;
    if (problem instanceof Error && body?.key_replaced === true) {
      throw new ApiError(`${problem.message} ${SET_UP_A_NEW_ONE}`, 401, undefined, body);
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
  return (
    <>
      <p className="small muted" style={{ marginTop: 0 }}>
        {why} So this asks for your password and a code — the same as signing in.
      </p>
      <Field label="Your password">
        <input
          type="password"
          value={proof.password}
          autoComplete="current-password"
          onChange={(e) => onChange({ ...proof, password: e.target.value })}
        />
      </Field>
      <p />
      <Field label="The six digits from your authenticator">
        <input
          value={proof.code}
          inputMode="numeric"
          autoComplete="one-time-code"
          onChange={(e) => onChange({ ...proof, code: e.target.value })}
        />
      </Field>
    </>
  );
}
