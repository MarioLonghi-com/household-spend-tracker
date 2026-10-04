/**
 * What a browser still holds after somebody signs out of it (#198).
 *
 * The query cache is cleared by the shell. This is the rest: the remembered
 * register and reimbursements filters, and `/snap`'s queue of photos waiting
 * to upload -- IndexedDB, with the photo's bytes and any location fix -- plus
 * the household it last sent to. Keep the names in step with `DB_NAME` and
 * `HOUSEHOLD_KEY` in `app/static/snap/snap.js`. And a recovery sign-in's
 * grant to set up a new authenticator (#287), which the session it was bound
 * to has just taken with it.
 */

import { dropGrant } from "./recoveryGrant";
import { forgetScreens } from "./sticky";

export const SNAP_DB = "snap";
export const SNAP_HOUSEHOLD_KEY = "snap.household";

export function forgetTheBrowser(): void {
  forgetScreens();
  dropGrant();
  try {
    window.localStorage.removeItem(SNAP_HOUSEHOLD_KEY);
  } catch {
    /* a private window */
  }
  try {
    window.indexedDB?.deleteDatabase(SNAP_DB);
  } catch {
    /* storage refused; there is nothing there to forget */
  }
}
