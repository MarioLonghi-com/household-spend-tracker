/**
 * What the Browse rates list is showing, and how it is written in the address
 * (#410).
 *
 * The same bargain as the Net Worth report (`reports/netWorthView.ts`): one
 * query string is both what the server is asked and, with the screen and the
 * household in front, what the address says -- so a reload, a bookmark or a
 * link opens the list exactly where it was, and the filters cannot say one
 * thing while the rows show another. No React here, so it is tested alone.
 */

import { ALL_DATES, type Range } from "../../components/DateRange";

/** `?open=` for the Exchange rates page (`App.tsx`'s `OPENS_IN_NEW_TAB`). */
export const OPEN_EXCHANGE_RATES = "exchange-rates";

/** Rows per page. A page is drawn in one go, so it is kept to what draws at once. */
export const BROWSE_PAGE = 100;

export const GRANULARITIES = ["day", "month_avg", "month_end"] as const;
export type Granularity = (typeof GRANULARITIES)[number];

export const BROWSE_SORTS = ["date", "currency", "source", "rate", "in_base", "rate_date", "own"] as const;
export type BrowseSort = (typeof BROWSE_SORTS)[number];

export type BrowseView = {
  currency: string;
  source: "" | "ecb" | "manual";
  granularity: Granularity;
  range: Range;
  sort: BrowseSort;
  direction: "asc" | "desc";
  /** 1-based. Ignored while `at` is set: the server says which page that is. */
  page: number;
  /** A jump: the day (or month's first day) whose nearest row to open on. */
  at: string | null;
};

export function defaultBrowseView(): BrowseView {
  return {
    currency: "",
    source: "",
    granularity: "day",
    range: ALL_DATES,
    sort: "date",
    direction: "desc",
    page: 1,
    at: null,
  };
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;
const CODE = /^[A-Z]{3}$/;

/** The view an address asks for. Anything unreadable is left at its default. */
export function browseViewFromSearch(search: string): BrowseView {
  const query = new URLSearchParams(search);
  const view = defaultBrowseView();
  const currency = (query.get("currency") ?? "").toUpperCase();
  if (CODE.test(currency)) view.currency = currency;
  const source = query.get("source");
  if (source === "ecb" || source === "manual") view.source = source;
  const granularity = query.get("granularity");
  if ((GRANULARITIES as readonly string[]).includes(granularity ?? "")) {
    view.granularity = granularity as Granularity;
  }
  const since = query.get("since");
  const until = query.get("until");
  view.range = {
    since: since && ISO.test(since) ? since : null,
    until: until && ISO.test(until) ? until : null,
  };
  const sort = query.get("sort");
  if ((BROWSE_SORTS as readonly string[]).includes(sort ?? "")) view.sort = sort as BrowseSort;
  const direction = query.get("direction");
  if (direction === "asc" || direction === "desc") view.direction = direction;
  const page = Number(query.get("page"));
  if (Number.isInteger(page) && page > 1) view.page = page;
  const at = query.get("at");
  if (at && ISO.test(at)) view.at = at;
  return view;
}

/**
 * The view's own query string: the filters, the order and where in the list.
 * Defaults are left out, so a plain visit has a plain address.
 */
export function browseQuery(view: BrowseView): string {
  const query = new URLSearchParams();
  if (view.currency) query.set("currency", view.currency);
  if (view.source) query.set("source", view.source);
  if (view.granularity !== "day") query.set("granularity", view.granularity);
  if (view.range.since) query.set("since", view.range.since);
  if (view.range.until) query.set("until", view.range.until);
  if (view.sort !== "date" || view.direction !== "desc") {
    query.set("sort", view.sort);
    query.set("direction", view.direction);
  }
  if (view.at) query.set("at", view.at);
  else if (view.page > 1) query.set("page", String(view.page));
  return query.toString();
}

/** What the server is asked: the view, with the page turned into an offset. */
export function serverQuery(view: BrowseView): string {
  const query = new URLSearchParams(browseQuery(view));
  query.delete("page");
  query.set("limit", String(BROWSE_PAGE));
  if (!view.at) query.set("offset", String((view.page - 1) * BROWSE_PAGE));
  return query.toString();
}

/** The address while the page is open, so the list can be bookmarked or reloaded. */
export function browseAddress(view: BrowseView, householdId: string): string {
  const head = new URLSearchParams({ open: OPEN_EXCHANGE_RATES, household: householdId });
  const rest = browseQuery(view);
  return `/?${head.toString()}${rest ? `&${rest}` : ""}`;
}

/** Whether the address asked for anything beyond the page itself. */
export function addressAskedForList(search: string): boolean {
  const query = new URLSearchParams(search);
  return query.get("open") === OPEN_EXCHANGE_RATES && browseQuery(browseViewFromSearch(search)) !== "";
}
