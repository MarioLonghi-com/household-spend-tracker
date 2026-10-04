/**
 * The grant a recovery sign-in hands back in recovery mode (#287), kept until
 * the profile spends it.
 *
 * Signing in with a recovery code while the server's key cannot open the
 * member's authenticator returns a `reenrolment_grant`: leave to set up a new
 * authenticator without a second recovery code. It used to live only in the
 * sign-in screen's state, so "Not now" or a reload lost it, and setting up the
 * authenticator later from the profile cost the second code it exists to
 * save. It is kept here instead.
 *
 * `sessionStorage`, not `localStorage`: this tab and no other, gone when the
 * tab is. Keyed to the member it was given to, so whoever signs in next at
 * this tab never sends somebody else's. The grant is worth nothing beside any
 * session but the one its sign-in issued, and the server checks that, so this
 * is a convenience -- every read and write is in a `try`, and a browser that
 * will not store it still signs in and still re-enrols, with a recovery code.
 *
 * Dropped once a re-enrolment succeeds, and on sign-out (`signedOut.ts`).
 */

const KEY = "spendtracker.reenrolment-grant";

interface Held {
  user: string;
  grant: string;
}

export function keepGrant(userId: string, grant: string): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({ user: userId, grant } satisfies Held));
  } catch {
    /* storage refused: the sign-in screen still offers the new authenticator */
  }
}

/** This member's grant, if this tab holds one. Never another member's. */
export function heldGrant(userId: string): string | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const held = JSON.parse(raw) as Partial<Held>;
    return held.user === userId && typeof held.grant === "string" ? held.grant : null;
  } catch {
    return null;
  }
}

export function dropGrant(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* nothing could be stored, so there is nothing to forget */
  }
}
