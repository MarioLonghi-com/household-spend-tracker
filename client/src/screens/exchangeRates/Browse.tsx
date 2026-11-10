/**
 * Browse rates: the stored rates, a page at a time (#410).
 *
 * Years of daily rates for several currencies are tens of thousands of rows.
 * The server orders them and answers one page (`GET …/fx/rates` with `limit`
 * and `offset`), so this list never holds -- or draws -- more than a hundred.
 * Every column sorts at its heading, through the server, so each page is in
 * the order the arrow says; the headings stay in view while a page scrolls.
 *
 * By day it is the rows as stored. By month it is one line per currency per
 * month, the figure the monthly-average or month-end policy gives -- found by
 * the server exactly as a report finds it, so nothing is averaged here.
 *
 * The filters, the order and the page live in the address
 * (`browseView.ts`), so a reload or a bookmark opens the list where it was.
 *
 * A rate is shown exactly as the server sent it, a decimal string.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { api } from "../../lib/api";
import { Field, Problem, SortHeading } from "../../components/bits";
import { DateRange } from "../../components/DateRange";
import { formatCount, formatDate, monthLabel } from "../../lib/locale";
import { policyWord, sourceLabel, type FxRates, type FxStatus } from "../../lib/fx";
import type { Household } from "../../lib/types";
import {
  BROWSE_PAGE,
  OPEN_EXCHANGE_RATES,
  addressAskedForList,
  browseAddress,
  browseViewFromSearch,
  defaultBrowseView,
  serverQuery,
  type BrowseSort,
  type BrowseView,
  type Granularity,
} from "./browseView";

/**
 * The list's view, read from the address when the page was opened from one,
 * and written back to it as it changes. Leaving the page hands the shell its
 * bare address back, as the Net Worth report does.
 */
export function useBrowseView(householdId: string) {
  const [view, setView] = useState<BrowseView>(() =>
    new URLSearchParams(window.location.search).get("open") === OPEN_EXCHANGE_RATES
      ? browseViewFromSearch(window.location.search)
      : defaultBrowseView(),
  );
  // Read once: whether the page should open scrolled to the list.
  const [fromAddress] = useState(() => addressAskedForList(window.location.search));

  useEffect(() => {
    window.history.replaceState(null, "", browseAddress(view, householdId));
  }, [view, householdId]);
  useEffect(
    () => () => {
      if (new URLSearchParams(window.location.search).get("open") === OPEN_EXCHANGE_RATES) {
        window.history.replaceState(null, "", "/");
      }
    },
    [],
  );

  /** Change the filters or the order: the list starts again from its top. */
  const choose = useCallback(
    (next: Partial<BrowseView>) => setView((was) => ({ ...was, page: 1, at: null, ...next })),
    [],
  );
  return { view, setView, choose, fromAddress };
}

function granularityWord(granularity: Granularity): string {
  return granularity === "day"
    ? t({ message: "Every rate, by day", comment: "Option on Browse rates: list each stored rate on its own line" })
    : policyWord(granularity);
}

export function Browse({
  household,
  data,
  view,
  onView,
  onChoose,
  fromAddress,
}: {
  household: Household;
  data: FxStatus;
  view: BrowseView;
  onView: (next: BrowseView) => void;
  onChoose: (next: Partial<BrowseView>) => void;
  /** The address asked for the list: open the page scrolled to it. */
  fromAddress: boolean;
}) {
  const query = serverQuery(view);
  const found = useQuery({
    queryKey: ["fx-rates", household.id, "browse", query],
    queryFn: ({ signal }) => api.get<FxRates>(`/households/${household.id}/fx/rates?${query}`, { signal }),
    // The page in hand stays while the next one comes, rather than the table
    // emptying and the headings jumping on every press of Next.
    placeholderData: keepPreviousData,
  });
  const answer = found.data;
  const monthly = view.granularity !== "day";

  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (fromAddress) heading.current?.scrollIntoView?.({ block: "start" });
  }, [fromAddress]);

  // A jump lands on a row: it is brought into view and marked.
  const focused = useRef<HTMLTableRowElement>(null);
  const focus = answer && !found.isPlaceholderData ? answer.focus : null;
  useEffect(() => {
    if (focus !== null) focused.current?.scrollIntoView?.({ block: "center" });
  }, [focus, answer]);

  const [jumpTo, setJumpTo] = useState("");
  const jump = () => {
    if (!jumpTo) return;
    const at = jumpTo.length === 7 ? `${jumpTo}-01` : jumpTo;
    // A jump is to a date, so the list is put in date order if it was not.
    onView({ ...view, at, page: 1, ...(view.sort === "date" ? {} : { sort: "date", direction: "desc" }) });
  };

  const onSort = (column: BrowseSort, direction: "asc" | "desc") => onChoose({ sort: column, direction });
  const sorting = { sort: view.sort, direction: view.direction, onSort };
  const base = household.base_currency;
  // Each heading is also its cell's label on a phone, where the headings are hidden.
  const label = {
    date: monthly
      ? t({ message: "Month", comment: "Column heading on Browse rates: the month a line is for" })
      : t({ message: "Date", comment: "Column heading on Browse rates: the rate's date" }),
    currency: t({ message: "Currency", comment: "Column heading on Browse rates. See GLOSSARY.md" }),
    source: t({ message: "Source", comment: "Column heading on Browse rates: where the rate came from" }),
    rate: t({ message: "As published", comment: "Column heading on Browse rates: the rate as the source quotes it" }),
    inBase: t({ message: `Per 1 ${base}`, comment: "Column heading: the rate as units per one unit of the base currency" }),
    rateDate: t({ message: "Rate's date", comment: "Column heading on Browse rates by month: the date of the rate the month's figure used" }),
    own: t({ message: "Your transfers", comment: "Column heading on Browse rates: the rate your own transfers gave that day" }),
  };

  const offset = answer?.offset ?? 0;
  const total = answer?.total ?? 0;
  const page = Math.floor(offset / BROWSE_PAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / BROWSE_PAGE));
  const goTo = (next: number) => onView({ ...view, at: null, page: Math.min(Math.max(next, 1), pages) });
  const count = (n: number) => formatCount(n);

  const pager = answer && total > BROWSE_PAGE && (
    <div className="row fx-browse-pager" role="group" aria-label={t({ message: "Pages of rates", comment: "Accessible name of the page buttons under Browse rates" })}>
      <button type="button" disabled={page <= 1} onClick={() => goTo(1)}>
        {t({ message: "First page", comment: "Button on Browse rates" })}
      </button>
      <button type="button" disabled={page <= 1} onClick={() => goTo(page - 1)}>
        {t({ message: "Previous page", comment: "Button on Browse rates" })}
      </button>
      <span className="small muted">
        {t({ message: `Page ${count(page)} of ${count(pages)}`, comment: "Where Browse rates is in its list of pages" })}
      </span>
      <button type="button" disabled={page >= pages} onClick={() => goTo(page + 1)}>
        {t({ message: "Next page", comment: "Button on Browse rates" })}
      </button>
      <button type="button" disabled={page >= pages} onClick={() => goTo(pages)}>
        {t({ message: "Last page", comment: "Button on Browse rates" })}
      </button>
    </div>
  );

  const policyNote = (granularity: Granularity) =>
    (granularity === "day" ? "spot" : granularity) === data.monthly_policy
      ? t({ message: `${granularityWord(granularity)} (as reports use)`, comment: "Option on Browse rates: this granularity is the household's rate policy" })
      : granularityWord(granularity);

  return (
    <>
      <h2 className="section-title" ref={heading}>
        <Trans comment="Heading on the Exchange rates page: the stored rates, filtered">Browse rates</Trans>
      </h2>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t({ message: "Currency", comment: "Label of a filter on the Exchange rates page. See GLOSSARY.md" })}>
          <select value={view.currency} onChange={(e) => onChoose({ currency: e.target.value })}>
            <option value="">{t({ message: "Every currency", comment: "Option of a filter: no currency chosen" })}</option>
            {data.currencies.map((one) => (
              <option key={one.currency} value={one.currency}>
                {one.currency}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t({ message: "Source", comment: "Label of a filter on the Exchange rates page: where rates come from" })}>
          <select
            value={view.source}
            onChange={(e) => onChoose({ source: e.target.value as BrowseView["source"] })}
          >
            <option value="">{t({ message: "Every source", comment: "Option of a filter: no source chosen" })}</option>
            <option value="ecb">{sourceLabel("ecb")}</option>
            <option value="manual">{sourceLabel("manual")}</option>
          </select>
        </Field>
        <Field label={t({ message: "Show", comment: "Label of a choice on Browse rates: a line per day, or a line per month" })}>
          <select
            value={view.granularity}
            onChange={(e) => {
              const granularity = e.target.value as Granularity;
              // "As published" and the rate's own date belong to one kind of line each.
              const sort =
                (granularity === "day" && view.sort === "rate_date") || (granularity !== "day" && view.sort === "rate")
                  ? { sort: "date" as const, direction: "desc" as const }
                  : {};
              setJumpTo("");
              onChoose({ granularity, ...sort });
            }}
          >
            {(["day", "month_avg", "month_end"] as const).map((one) => (
              <option key={one} value={one}>
                {policyNote(one)}
              </option>
            ))}
          </select>
        </Field>
        <form
          className="row"
          style={{ gap: 6, alignItems: "flex-end" }}
          onSubmit={(e) => {
            e.preventDefault();
            jump();
          }}
        >
          <Field
            label={
              monthly
                ? t({ message: "Jump to month", comment: "Label on Browse rates: open the list at this month" })
                : t({ message: "Jump to date", comment: "Label on Browse rates: open the list at this day" })
            }
          >
            <input
              type={monthly ? "month" : "date"}
              value={jumpTo}
              onChange={(e) => setJumpTo(e.target.value)}
            />
          </Field>
          <button type="submit" disabled={!jumpTo}>
            {t({ message: "Go", comment: "Button on Browse rates: jump to the date or month typed beside it" })}
          </button>
        </form>
      </div>
      <DateRange value={view.range} onChange={(range) => onChoose({ range })} />
      <Problem error={found.error} />
      {answer && (
        <>
          <p className="small muted" aria-live="polite">
            {total > BROWSE_PAGE && answer.rates.length > 0
              ? t({
                  message: `${count(offset + 1)}–${count(offset + answer.rates.length)} of ${count(total)} rates`,
                  comment: "Which rows of the list Browse rates is showing",
                })
              : plural(total, { one: "# rate", other: "# rates" })}
          </p>
          {pager}
          {answer.rates.length > 0 && (
            <div className="table-scroll fx-browse-scroll">
              <table className="fx-browse-table">
                <thead>
                  <tr>
                    <SortHeading label={label.date} column="date" {...sorting} />
                    <SortHeading label={label.currency} column="currency" {...sorting} />
                    <SortHeading label={label.source} column="source" {...sorting} />
                    {!monthly && <SortHeading label={label.rate} column="rate" align="right" {...sorting} />}
                    <SortHeading label={label.inBase} column="in_base" align="right" {...sorting} />
                    {monthly && <SortHeading label={label.rateDate} column="rate_date" {...sorting} />}
                    {data.show_own_rates && <SortHeading label={label.own} column="own" align="right" {...sorting} />}
                  </tr>
                </thead>
                <tbody>
                  {answer.rates.map((row, index) => {
                    const isFocus = index === focus;
                    return (
                      <tr
                        key={`${row.source}${row.quote_currency}${row.currency}${row.date}${row.period}`}
                        ref={isFocus ? focused : undefined}
                        className={isFocus ? "selected" : undefined}
                        aria-current={isFocus ? "true" : undefined}
                      >
                        <td className="mono small" data-label={label.date}>
                          {row.period === "month" ? monthLabel(row.date.slice(0, 7)) : formatDate(row.date)}
                        </td>
                        <td className="mono" data-primary data-label={label.currency}>
                          {row.currency}
                        </td>
                        <td className="small" data-label={label.source}>
                          {sourceLabel(row.source)}
                        </td>
                        {!monthly && (
                          <td className="amount mono" data-label={label.rate}>
                            {row.rate} <span className="muted">/ {row.quote_currency}</span>
                          </td>
                        )}
                        <td className="amount mono" data-figure data-label={label.inBase}>
                          {row.rate_in_base ?? "—"}
                        </td>
                        {monthly && (
                          <td className="mono small" data-label={label.rateDate}>
                            {row.rate_date ? formatDate(row.rate_date) : "—"}
                            {row.stale && (
                              <>
                                {" "}
                                <span className="tag">
                                  {t({ message: "stale", comment: "Beside a month's rate on Browse rates: older than the household's stale limit" })}
                                </span>
                              </>
                            )}
                          </td>
                        )}
                        {data.show_own_rates && (
                          <td className="amount mono" data-label={label.own}>
                            {row.own_rate ?? "—"}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {answer.rates.length > 10 && pager}
        </>
      )}
    </>
  );
}
