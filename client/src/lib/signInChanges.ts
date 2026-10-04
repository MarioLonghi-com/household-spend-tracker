/**
 * Words for resets and new owners (#286), shared by the Admin screen's list
 * and the notice in the shell, so the two never describe one act two ways.
 *
 * The rows themselves are read from the audit log by the server
 * (`app/services/sign_in_changes.py`); nothing here decides what counts.
 */

import { asInstant } from "./time";
import type { SignInChange } from "./types";

/** What a reset link resets, as a phrase: "password", "authenticator", or both. */
export function switchesText(password: boolean, authenticator: boolean): string {
  if (password && authenticator) return "password and authenticator";
  return password ? "password" : "authenticator";
}

/** The What column: a label, not a sentence. */
export function whatText(change: SignInChange): string {
  switch (change.what) {
    case "reset":
      return `Reset: ${switchesText(change.password, change.authenticator)}`;
    case "promoted":
      return "Made an owner";
    case "authenticator_replaced":
      return "New authenticator";
    case "reenabled":
      return "Re-enabled";
    default:
      return "Added as an owner";
  }
}

/** The By column. A command run beside the ledger is nobody's account. */
export function byText(change: SignInChange): string {
  return change.from_server ? "from the server" : (change.by_name ?? "somebody");
}

/** One line of the notice. */
export function sentence(change: SignInChange): string {
  const by = change.from_server ? "The server" : (change.by_name ?? "Somebody");
  switch (change.what) {
    case "reset":
      return `${by} reset ${change.user_name}'s ${switchesText(change.password, change.authenticator)}`;
    case "promoted":
      return `${by} made ${change.user_name} an owner`;
    case "authenticator_replaced":
      return `${by} gave ${change.user_name} a new authenticator`;
    case "reenabled":
      return `${by} re-enabled ${change.user_name}`;
    default:
      // The first owner is the actor of their own creation: the wizard.
      return change.by_id === change.user_id
        ? `${change.user_name} set up this instance as its owner`
        : `${by} added ${change.user_name} as an owner`;
  }
}

/**
 * How far a viewer has dismissed: the newest item they dismissed, by its
 * position in the audit log and by its time.
 *
 * Both, because each can go backwards on its own. The position is
 * `changes.seq`, a plain rowid: after `make restore` it carries on from the
 * backup's last row, below a mark kept in this browser from before the
 * restore, and a position alone would hide every reset made since until the
 * log caught up. The time is the batch's start, which a batch that started
 * first and committed second puts behind an item already dismissed. An item
 * newer by either is news; one older by both was there to be dismissed.
 */
export interface Dismissed {
  seq: number;
  at: string;
}

export const NOTHING_DISMISSED: Dismissed = { seq: 0, at: "" };

export function isDismissed(raw: unknown): raw is Dismissed {
  if (typeof raw !== "object" || raw === null) return false;
  const mark = raw as Record<string, unknown>;
  return typeof mark.seq === "number" && typeof mark.at === "string";
}

function instant(serverTimestamp: string): number {
  return serverTimestamp ? asInstant(serverTimestamp).getTime() : Number.NEGATIVE_INFINITY;
}

/** The mark that covers every one of `changes`. */
export function dismissing(changes: SignInChange[]): Dismissed {
  return changes.reduce<Dismissed>(
    (mark, one) => ({
      seq: Math.max(mark.seq, one.id),
      at: instant(one.at) > instant(mark.at) ? one.at : mark.at,
    }),
    NOTHING_DISMISSED,
  );
}

/**
 * What the notice owes this viewer: done by somebody else -- another owner or
 * the server -- and newer than whatever they last dismissed.
 */
export function owedTo(viewerId: string, changes: SignInChange[], dismissed: Dismissed): SignInChange[] {
  return changes.filter(
    (one) =>
      one.by_id !== viewerId &&
      // A time that does not parse is NaN, never newer: the position decides.
      (one.id > dismissed.seq || instant(one.at) > instant(dismissed.at)),
  );
}
