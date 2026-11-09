/**
 * A link that opens Transactions in a new tab, filtered, on one row (#401).
 *
 * The Income vs Expense bubble lists the rows behind a figure; each of them
 * opens the register showing the same slice the figure was -- its category or
 * group, its month, the report's currency and accounts -- with that row
 * highlighted and its panel open. A new tab, so the report stays where it was.
 *
 * Both halves live here: `registerHref` writes the address and
 * `registerPresetFrom` reads it back when the new tab loads, so the names of
 * the parameters are coined in one place. Everything read arrives in a query
 * string, so every value is checked before the register is handed it, the way
 * `?household=` is (#197); anything malformed is dropped, never trusted.
 */

import type { Range } from "../components/DateRange";
import type { RegisterPreset } from "../screens/Register";

/** What the new tab should show. `null`/absent means "no filter" for that facet. */
export type RegisterLink = {
  household: string;
  /** The transaction to highlight and open. */
  open: string;
  since?: string | null;
  until?: string | null;
  /** Category ids to show; `[]` with `backlog` is "Needs a category" alone. */
  categories?: string[] | null;
  /** Whether rows that still need a category are shown too. */
  backlog?: boolean;
  currency?: string | null;
  accounts?: string[] | null;
};

const ID = /^[\w-]{1,64}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const CURRENCY = /^[A-Z]{3}$/;
/** More ids than any household has categories or accounts is not a real link. */
const MOST = 500;

export function registerHref(link: RegisterLink): string {
  const query = new URLSearchParams();
  query.set("open", "register");
  query.set("household", link.household);
  query.set("txn", link.open);
  if (link.since) query.set("since", link.since);
  if (link.until) query.set("until", link.until);
  for (const id of link.categories ?? []) query.append("category", id);
  if (link.categories) query.set("backlog", link.backlog ? "1" : "0");
  else if (link.backlog === false) query.set("backlog", "0");
  if (link.currency) query.set("currency", link.currency);
  for (const id of link.accounts ?? []) query.append("account", id);
  return `/?${query.toString()}`;
}

function ids(query: URLSearchParams, name: string): string[] | null {
  const all = query.getAll(name);
  if (all.length === 0 || all.length > MOST || !all.every((one) => ID.test(one))) return null;
  return all;
}

/**
 * The register preset an address asks for, or `null` when it asks for none.
 *
 * Only a well-formed transaction id makes it a link at all; each facet is
 * then kept if it is well formed and dropped if not, so a mangled link still
 * lands on the row rather than on an error.
 */
export function registerPresetFrom(search: string): RegisterPreset | null {
  const query = new URLSearchParams(search);
  if (query.get("open") !== "register") return null;
  const open = query.get("txn") ?? "";
  if (!ID.test(open)) return null;

  const since = query.get("since");
  const until = query.get("until");
  const range: Range = {
    since: since && DATE.test(since) ? since : null,
    until: until && DATE.test(until) ? until : null,
  };

  const categories = ids(query, "category");
  const backlogRaw = query.get("backlog");
  const backlog = backlogRaw === "0" ? false : true;
  // "category" absent and backlog=1 is the backlog alone: no ids, ticked.
  const picked = categories ?? (backlogRaw === "1" ? [] : null);

  const currency = query.get("currency");
  return {
    open,
    range,
    categories: picked,
    backlog,
    currencies: currency && CURRENCY.test(currency) ? [currency] : null,
    accounts: ids(query, "account") ?? undefined,
  };
}
