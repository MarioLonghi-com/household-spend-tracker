import { useSyncExternalStore } from "react";
import { api } from "./api";

/**
 * What this deployment offers (#316): the server's `GET /api/capabilities`,
 * decided by its profile in `app/capabilities.py`. The desktop app is the same
 * client with the features that need a network, a second device or a second
 * person turned off -- and the server has already left their routes out, so
 * hiding them here is what keeps a button from leading to a 404.
 *
 * A screen asks whether a capability is on, never which profile it is in.
 */
export type Capabilities = {
  snap: boolean;
  agents: boolean;
  invitations: boolean;
  account_resets: boolean;
  passkeys: boolean;
  second_factor: boolean;
  /** A forgotten password is replaced with the recovery code: the desktop's way back in (#320). */
  password_reset_by_code: boolean;
  self_update: boolean;
  upstream_check: boolean;
  db_view: boolean;
  ynab_import: boolean;
};

export type Capability = keyof Capabilities;

/**
 * A server's set: what every deployment offered before capabilities existed,
 * and what is assumed if the server cannot say -- one older than the endpoint.
 */
export const EVERYTHING: Capabilities = {
  snap: true,
  agents: true,
  invitations: true,
  account_resets: true,
  passkeys: true,
  second_factor: true,
  password_reset_by_code: false,
  self_update: true,
  upstream_check: true,
  db_view: true,
  ynab_import: true,
};

// Kept here rather than in the query cache, which is the signed-in person's
// and is cleared whenever that changes (`App.setUser`). These belong to the
// deployment: they are the same for everyone and do not change while it runs.
let offered: Capabilities | null = null;
const listeners = new Set<() => void>();

function publish(next: Capabilities): Capabilities {
  offered = next;
  for (const listener of listeners) listener();
  return next;
}

/**
 * Ask once, at boot, before the first screen is drawn (`App`): a screen that
 * read the default first would already have asked for something the
 * deployment does not have -- the desktop's Admin asked for the pending
 * resets, a route that is not there.
 */
export async function loadCapabilities(): Promise<Capabilities> {
  try {
    const answer = await api.get<Capabilities & { product_name?: unknown }>("/capabilities");
    if (!answer) return publish(EVERYTHING);
    const { product_name: name, ...switches } = answer;
    setProductName(typeof name === "string" && name.trim() ? name : null);
    return publish(switches);
  } catch {
    return publish(EVERYTHING);
  }
}

/**
 * What the app is called here (#346): "Household Spend Tracker" in the desktop
 * app, the name both stores list it under, and "Spend Tracker" on a server.
 * The server says which, beside the capabilities, in the same boot request; a
 * server too old to say is a server, and keeps the name it always had.
 */
export const SERVER_NAME = "Spend Tracker";
let named: string | null = null;
const nameListeners = new Set<() => void>();

/** For the boot, and for tests. Null goes back to the server's name. */
export function setProductName(next: string | null): void {
  named = next;
  for (const listener of nameListeners) listener();
}

/** The name, for code that is not a component (the tab title). */
export function productNameNow(): string {
  return named ?? SERVER_NAME;
}

function subscribeName(listener: () => void): () => void {
  nameListeners.add(listener);
  return () => nameListeners.delete(listener);
}

/** The name, for a component: it redraws if the boot answers after the first paint. */
export function useProductName(): string {
  return useSyncExternalStore(subscribeName, productNameNow);
}

/** For tests: what a deployment offers, as if the boot had asked. Null forgets. */
export function setCapabilities(next: Capabilities | null): void {
  offered = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The same, for code that is not a component (a form's "ready" check). */
export function offeredNow(): Capabilities {
  return offered ?? EVERYTHING;
}

/** The deployment's capabilities, or everything until the boot has asked. */
export function useCapabilities(): Capabilities {
  return useSyncExternalStore(subscribe, () => offered ?? EVERYTHING);
}
