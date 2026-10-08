/**
 * Work expenses, as the client reads them.
 *
 * Its own file with no React in it, for the reason `sorting.ts` is: these are
 * the pieces with a contract worth testing on their own. Two stored columns
 * -- `reimbursement` and `reimbursed_by_id` -- collapse into one of four
 * states a person sees, and the register's grouped view is an ordering over
 * rows already in hand. Both have to stay defined for inputs the server is
 * never supposed to send, because a pill that throws takes the register down
 * with it.
 */

import { t } from "@lingui/core/macro";

import type { ReimbursementState, Transaction } from "./types";

/** What the `W` pill says about a row. `none` draws nothing. */
export type WorkState = "none" | "owed" | "paid" | "off";

/**
 * The two columns as one state.
 *
 * Two combinations are impossible by the server's rules and are still given
 * an answer here rather than left to fall through:
 *
 * - `written_off` with a link. Written off means work will not pay; the state
 *   is the deliberate act, so it wins and the row reads as written off.
 * - No state with a link. Linking flags a row, and clearing the flag clears
 *   the link, so a link on an unflagged row is debris -- and the flag is the
 *   thing a person set, so the row reads as an ordinary one.
 *
 * Anything else the server might one day send as a state is `none` too: a
 * pill claiming a meaning nobody designed is worse than no pill.
 */
export function workState(
  txn: Partial<Pick<Transaction, "reimbursement" | "reimbursed_by_id">>,
): WorkState {
  const state: unknown = txn.reimbursement;
  if (state === "written_off") return "off";
  if (state === "expected") return txn.reimbursed_by_id ? "paid" : "owed";
  return "none";
}

/**
 * The pill for each state: its class and the words it stands for.
 *
 * Colour carries nothing on its own -- the letter is the same `W` in all
 * three, and the title and aria-label carry the word, exactly as the Source
 * and Cleared letters do. The colours are three of the eleven tokens a
 * household already has (`--warn`, `--positive`, `--muted`), because a
 * household cannot be given a twelfth.
 */
export const WORK_PILLS: Record<Exclude<WorkState, "none">, { className: string; word: string }> = {
  owed: {
    className: "tag work-owed",
    get word() {
      return t`Work expense — not reimbursed yet`;
    },
  },
  paid: {
    className: "tag work-paid",
    get word() {
      return t`Work expense — reimbursed`;
    },
  },
  off: {
    className: "tag work-off",
    get word() {
      return t`Work expense — written off`;
    },
  },
};

/**
 * What the panel's Reimbursement select offers, and what it sends.
 *
 * Sentences rather than words, the way the Cleared select reads: "Expected"
 * on its own does not say by whom or of what, and the choice is about what
 * work is going to do, so the label says that. The empty value is not a
 * state; it is the absence of one, and sends `clear_state`.
 */
export const REIMBURSEMENT_LABELS: Record<"" | ReimbursementState, string> = {
  // Getters, so each is read in the language active when it is shown.
  get ""() {
    return t`Not a work expense`;
  },
  get expected() {
    return t`Work should pay this back`;
  },
  get written_off() {
    return t`Written off — work will not pay it`;
  },
};

/**
 * Whole days from one ISO date to another.
 *
 * Both are calendar dates, so they are read as UTC midnights: read as local
 * time, a span across a daylight-saving change is 23 or 25 hours long and
 * rounds to the wrong day on one side of it. Negative when `from` is later,
 * which is what an advance paid before the expense looks like.
 */
export function ageInDays(from: string, to: string): number {
  const start = Date.parse(`${from.slice(0, 10)}T00:00:00Z`);
  const end = Date.parse(`${to.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return 0;
  return Math.round((end - start) / 86_400_000);
}

/** One row of the grouped view, with how far in it sits. */
export type Grouped<T> = { row: T; child: boolean };

/**
 * The register's rows with each payment followed by what it repaid.
 *
 * The rows keep the order they came in -- the register's own sort -- except
 * that an expense whose payment is also on screen moves to sit under that
 * payment, oldest first, and is marked as a child. An expense whose payment
 * is *not* on screen (another account's filter, a hidden currency) stays
 * where it was and is not indented: indenting it under nothing would say it
 * belongs to whichever row happens to be above it.
 */
export function groupByPayment<
  T extends Pick<Transaction, "id" | "date" | "reimbursed_by_id">,
>(rows: T[]): Grouped<T>[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  // Only one level deep. A payment is never itself flagged, so a row that
  // points at a row that points somewhere is not a shape the server makes --
  // but if one ever arrives, nesting it would drop a cycle's rows off the
  // screen entirely. So a row goes under its payment only when that payment
  // sits at the top level itself; anything else stays where it was.
  const parentOf = (row: T): string | null => {
    const payment = row.reimbursed_by_id;
    if (!payment || payment === row.id) return null;
    const parent = byId.get(payment);
    if (!parent) return null;
    const above = parent.reimbursed_by_id;
    return above && above !== parent.id && byId.has(above) ? null : payment;
  };

  const under = new Map<string, T[]>();
  for (const row of rows) {
    const parent = parentOf(row);
    if (!parent) continue;
    const list = under.get(parent) ?? [];
    list.push(row);
    under.set(parent, list);
  }

  const out: Grouped<T>[] = [];
  for (const row of rows) {
    if (parentOf(row)) continue;
    out.push({ row, child: false });
    const children = under.get(row.id);
    if (!children) continue;
    const oldestFirst = [...children].sort((a, b) =>
      a.date < b.date ? -1 : a.date > b.date ? 1 : 0,
    );
    for (const child of oldestFirst) out.push({ row: child, child: true });
  }
  return out;
}
