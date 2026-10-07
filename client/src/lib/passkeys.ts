/**
 * Passkeys in the browser (#47, #121): whether this browser and this server
 * can use them here, and the two ceremonies.
 *
 * The browser's own WebAuthn JSON API, no library (decision 3):
 * `PublicKeyCredential.parseCreationOptionsFromJSON`,
 * `parseRequestOptionsFromJSON` and `credential.toJSON()`. A browser without
 * them is not offered passkeys at all, rather than offered a flow that breaks
 * (decision 4). The server's half of that check is
 * `GET /api/session/passkey/state`; both have to say yes.
 */

import { api } from "./api";

/** Why the server does not offer passkeys here; see `app/auth/passkeys.py`. */
export type PasskeyReason =
  | "not_configured"
  | "ip_address"
  | "wrong_host"
  | "insecure"
  | "no_origins"
  /** This browser's half: no secure context, no WebAuthn, or no JSON helpers. */
  | "browser";

export interface PasskeyState {
  available: boolean;
  reason: PasskeyReason | null;
  detail?: string | null;
  /** The public URL, where passkeys do work, when one is configured. */
  address?: string | null;
}

/** The parts of `PublicKeyCredential` this file relies on. Typed here
 * because the DOM typings lag behind the JSON helpers. */
interface JsonCapable {
  parseCreationOptionsFromJSON?: (json: unknown) => CredentialCreationOptions["publicKey"];
  parseRequestOptionsFromJSON?: (json: unknown) => CredentialRequestOptions["publicKey"];
  isConditionalMediationAvailable?: () => Promise<boolean>;
}

function credentialClass(): JsonCapable | null {
  const found = (globalThis as { PublicKeyCredential?: JsonCapable }).PublicKeyCredential;
  return found ?? null;
}

/** This browser's half of "conditions are good" (#47 §1.1). */
export function browserSupportsPasskeys(): boolean {
  const klass = credentialClass();
  return (
    typeof window !== "undefined" &&
    window.isSecureContext === true &&
    klass !== null &&
    typeof klass.parseCreationOptionsFromJSON === "function" &&
    typeof klass.parseRequestOptionsFromJSON === "function" &&
    typeof navigator !== "undefined" &&
    !!navigator.credentials
  );
}

/** Whether the email field may offer passkeys itself (conditional UI). */
export async function conditionalMediationAvailable(): Promise<boolean> {
  const klass = credentialClass();
  if (!klass?.isConditionalMediationAvailable) return false;
  try {
    return await klass.isConditionalMediationAvailable();
  } catch {
    return false;
  }
}

/** Both halves at once. Never throws: a state that cannot be read is
 * "unavailable", and the screen is exactly today's. */
export async function passkeyState(): Promise<PasskeyState> {
  let server: PasskeyState | undefined;
  try {
    server = await api.get<PasskeyState>("/session/passkey/state");
  } catch {
    server = undefined;
  }
  if (!server) return { available: false, reason: "not_configured" };
  if (server.available && !browserSupportsPasskeys()) {
    return { ...server, available: false, reason: "browser" };
  }
  return server;
}

function asJson(credential: Credential | null): unknown {
  const json = (credential as { toJSON?: () => unknown } | null)?.toJSON;
  if (!credential || typeof json !== "function") throw new Error("the browser gave no passkey");
  return json.call(credential);
}

/** `navigator.credentials.get()` for the server's sign-in options. */
export async function getPasskey(
  options: unknown,
  { conditional = false, signal }: { conditional?: boolean; signal?: AbortSignal } = {},
): Promise<unknown> {
  const publicKey = credentialClass()!.parseRequestOptionsFromJSON!(options);
  const credential = await navigator.credentials.get({
    publicKey,
    signal,
    ...(conditional ? { mediation: "conditional" as CredentialMediationRequirement } : {}),
  });
  return asJson(credential);
}

/** `navigator.credentials.create()` for the server's registration options. */
export async function createPasskey(options: unknown): Promise<unknown> {
  const publicKey = credentialClass()!.parseCreationOptionsFromJSON!(options);
  return asJson(await navigator.credentials.create({ publicKey }));
}

/** A cancelled or timed-out prompt: the member said no, which is not an error to show. */
export function wasDismissed(problem: unknown): boolean {
  const name = (problem as { name?: string } | null)?.name;
  return name === "NotAllowedError" || name === "AbortError";
}

const THIS_DEVICE = "spend-tracker.passkey-this-device";

/** Which passkey this browser last signed in with, for "this device" in
 * Sign-in methods (#47 §3). This browser's own note, never sent anywhere. */
export function rememberThisDevice(passkeyId: string): void {
  try {
    window.localStorage.setItem(THIS_DEVICE, passkeyId);
  } catch {
    // Private mode or storage switched off: the mark is a nicety.
  }
}

export function thisDevicePasskey(): string | null {
  try {
    return window.localStorage.getItem(THIS_DEVICE);
  } catch {
    return null;
  }
}
