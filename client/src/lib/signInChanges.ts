/**
 * Words for resets and new owners (#286), shared by the Admin screen's list
 * and the notice in the shell, so the two never describe one act two ways.
 *
 * The rows themselves are read from the audit log by the server
 * (`app/services/sign_in_changes.py`); nothing here decides what counts.
 */

import { asInstant } from "./time";
import type { SignInChange } from "./types";
import { t } from "@lingui/core/macro";

/** What a reset link resets, as a phrase: "password", "authenticator", or both. */
export function switchesText(password: boolean, authenticator: boolean): string {
  if (password && authenticator) return t`password and authenticator`;
  return password ? t({ message: "password", comment: "What was reset, inside a sentence: the password. Lower case. See GLOSSARY.md" }) : t({ message: "authenticator", comment: "What was reset, inside a sentence: the authenticator. Lower case. See GLOSSARY.md" });
}

/** The What column: a label, not a sentence. */
export function whatText(change: SignInChange): string {
  switch (change.what) {
    case "reset":
      if (change.password && change.authenticator) return t`Reset: password and authenticator`;
      return change.password ? t({ message: "Reset: password", comment: "A sign-in change in Admin's list: the person's password was reset" }) : t({ message: "Reset: authenticator", comment: "A sign-in change in Admin's list: the person's authenticator was reset" });
    case "promoted":
      return t`Made an owner`;
    case "authenticator_replaced":
      return t({ message: "New authenticator", comment: "A sign-in change in Admin's list: the person was given a new authenticator" });
    case "reenabled":
      return t({ message: "Re-enabled", comment: "A sign-in change in Admin's list: the person may sign in again" });
    default:
      return t`Added as an owner`;
  }
}

/** The By column. A command run beside the ledger is nobody's account. */
export function byText(change: SignInChange): string {
  return change.from_server ? t`from the server` : (change.by_name ?? t({ message: "somebody", comment: "Stands in for who made a change when the name is not known. Lower case" }));
}

/**
 * One line of the notice.
 *
 * Whole sentences, one per act and per kind of actor, rather than "{by}" and
 * "{what}" fragments joined here: a translator has to see the sentence to
 * write it, and "The server" and a person's name do not take the same verb
 * ending in every language.
 */
export function sentence(change: SignInChange): string {
  const user = change.user_name;
  if (!KNOWN.includes(change.what)) {
    // The first owner is the actor of their own creation: the wizard.
    if (change.by_id === change.user_id) return t`${user} set up this instance as its owner`;
  }
  if (change.from_server) {
    switch (change.what) {
      case "reset":
        if (change.password && change.authenticator)
          return t`The server reset ${user}'s password and authenticator`;
        return change.password
          ? t`The server reset ${user}'s password`
          : t`The server reset ${user}'s authenticator`;
      case "promoted":
        return t`The server made ${user} an owner`;
      case "authenticator_replaced":
        return t`The server gave ${user} a new authenticator`;
      case "reenabled":
        return t`The server re-enabled ${user}`;
      default:
        return t`The server added ${user} as an owner`;
    }
  }
  if (change.by_name === null || change.by_name === undefined) {
    switch (change.what) {
      case "reset":
        if (change.password && change.authenticator)
          return t`Somebody reset ${user}'s password and authenticator`;
        return change.password
          ? t`Somebody reset ${user}'s password`
          : t`Somebody reset ${user}'s authenticator`;
      case "promoted":
        return t`Somebody made ${user} an owner`;
      case "authenticator_replaced":
        return t`Somebody gave ${user} a new authenticator`;
      case "reenabled":
        return t({ message: `Somebody re-enabled ${user}`, comment: "A sign-in change, as a sentence; the placeholder is a person's name" });
      default:
        return t`Somebody added ${user} as an owner`;
    }
  }
  const by = change.by_name;
  switch (change.what) {
    case "reset":
      if (change.password && change.authenticator)
        return t`${by} reset ${user}'s password and authenticator`;
      return change.password ? t`${by} reset ${user}'s password` : t`${by} reset ${user}'s authenticator`;
    case "promoted":
      return t`${by} made ${user} an owner`;
    case "authenticator_replaced":
      return t`${by} gave ${user} a new authenticator`;
    case "reenabled":
      return t({ message: `${by} re-enabled ${user}`, comment: "A sign-in change, as a sentence: who re-enabled whom; both are people's names" });
    default:
      return t`${by} added ${user} as an owner`;
  }
}

/** The acts with a sentence of their own; anything else reads as "added". */
const KNOWN: readonly string[] = ["reset", "promoted", "authenticator_replaced", "reenabled"];

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
