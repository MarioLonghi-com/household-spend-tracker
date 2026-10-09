/**
 * Review mode: an owner reviewing the draft languages on their own device (#272).
 *
 * Per device, like the language itself (#48): a switch in Application
 * management, kept in this browser's storage, off unless turned on. It only
 * does anything for an owner -- the shell asks `reviewAllowed` with who is
 * signed in -- so a member who found the key would still be offered nothing.
 */

/** Where this device keeps the switch. */
export const REVIEW_KEY = "spendtracker.review";

/** Whether this device has review mode on. A blocked store reads as off. */
export function reviewStored(): boolean {
  try {
    return window.localStorage.getItem(REVIEW_KEY) === "on";
  } catch {
    return false;
  }
}

/** Turn it on or off on this device. A blocked store means it lasts the session. */
export function storeReview(on: boolean): void {
  try {
    if (on) window.localStorage.setItem(REVIEW_KEY, "on");
    else window.localStorage.removeItem(REVIEW_KEY);
  } catch {
    // Private window or blocked site data: the choice still applies now.
  }
}

/** Whether this person, on this device, gets the drafts as previews. */
export function reviewAllowed(role: string | undefined): boolean {
  return role === "owner" && reviewStored();
}
