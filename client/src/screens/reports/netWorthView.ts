/**
 * The Net Worth report's choices, and how they live in the address (#397).
 *
 * The shell never changes its URL, so this report is the exception that
 * writes one: `/?open=report-net-worth&household=…&since=…` names the report
 * and every filter on it, so a filtered view can be bookmarked and a reload
 * comes back to it. `App.tsx` reads `?open=` as it does for the screens that
 * open in a new tab and leaves the rest of the address for this file.
 *
 * Pure functions, so the round trip is tested without a browser.
 */

import { ALL_DATES, monthsBack, type Range } from "../../components/DateRange";
import type { Selection } from "../../components/GroupedPicker";
import type { NetWorthSplit } from "../../lib/types";

/** The `?open=` value, and the shell's screen name. */
export const OPEN_NET_WORTH = "report-net-worth";

/**
 * A filter's id for "not set" -- no bank, no country. The server says it with
 * an empty value (`institution=`); inside the picker an id has to be
 * something, and this is no bank's name.
 */
export const UNSET_ID = "∅";

export type NetWorthView = {
  range: Range;
  closedOnly: boolean;
  banks: Selection;
  countries: Selection;
  currencies: Selection;
  accounts: Selection;
  showClosed: boolean;
  split: NetWorthSplit;
};

const SPLITS: NetWorthSplit[] = ["currency", "country", "institution", "type"];

/** What the report opens on: the latest 12 months, closed months only, everything. */
export function defaultView(today = new Date()): NetWorthView {
  return {
    range: monthsBack(12, today),
    closedOnly: true,
    banks: null,
    countries: null,
    currencies: null,
    accounts: null,
    showClosed: false,
    split: "currency",
  };
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;

function many(query: URLSearchParams, key: string): Selection {
  if (!query.has(key)) return null;
  return query.getAll(key).map((one) => (one === "" ? UNSET_ID : one));
}

/** The view an address asks for, anything it leaves out at its default. */
export function viewFromSearch(search: string, today = new Date()): NetWorthView {
  const query = new URLSearchParams(search);
  const view = defaultView(today);
  if (query.get("range") === "all") view.range = ALL_DATES;
  else {
    const since = query.get("since");
    const until = query.get("until");
    if (since && ISO.test(since) && until && ISO.test(until)) view.range = { since, until };
  }
  view.closedOnly = query.get("closed_only") !== "false";
  view.showClosed = query.get("include_closed_accounts") === "true";
  view.banks = many(query, "institution");
  view.countries = many(query, "country");
  view.currencies = many(query, "currency");
  view.accounts = many(query, "account_id");
  const split = query.get("split");
  if (split && (SPLITS as string[]).includes(split)) view.split = split as NetWorthSplit;
  return view;
}

/**
 * The report's own query string: what the server is asked, and -- with the
 * screen and the household in front -- what the address says.
 */
export function reportQuery(view: NetWorthView): string {
  const query = new URLSearchParams();
  if (!view.range.since && !view.range.until) query.set("range", "all");
  if (view.range.since) query.set("since", view.range.since);
  if (view.range.until) query.set("until", view.range.until);
  if (!view.closedOnly) query.set("closed_only", "false");
  const put = (key: string, chosen: Selection) => {
    for (const id of chosen ?? []) query.append(key, id === UNSET_ID ? "" : id);
  };
  put("institution", view.banks);
  put("country", view.countries);
  put("currency", view.currencies);
  put("account_id", view.accounts);
  if (view.showClosed) query.set("include_closed_accounts", "true");
  if (view.split !== "currency") query.set("split", view.split);
  return query.toString();
}

/** The address for this view of this household's report. */
export function addressFor(view: NetWorthView, householdId: string): string {
  const head = new URLSearchParams({ open: OPEN_NET_WORTH, household: householdId });
  const rest = reportQuery(view);
  return `/?${head.toString()}${rest ? `&${rest}` : ""}`;
}

/** A filter ticked down to nothing: no account can pass it, so there is nothing to ask. */
export function choosesNothing(view: NetWorthView): boolean {
  return [view.banks, view.countries, view.currencies, view.accounts].some(
    (chosen) => chosen !== null && chosen.length === 0,
  );
}
