/**
 * Net Worth: how much the household has, all accounts together (#397).
 *
 * Every account's balance at each month end, added up, with what moved it.
 * **The one report that may add two currencies, and only through a rate** --
 * which the server decides and says: `display_currency` is what the figures
 * are in. With one currency in scope they are exact. With several and no
 * exchange rates there is no total at all, and the page shows each currency's
 * own figures side by side (`currencies`), never added. Converted figures
 * (once exchange rates arrive, #396) wear "≈".
 *
 * Every figure on the page, the explanation at the foot included, comes from
 * the one response. Nothing is recomputed here: the client formats and draws.
 *
 * Read-only, like every report. Its one way out is an account's name, which
 * opens that account's register up to the "as of" date.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { Empty, Problem, SortHeading, useSort } from "../../components/bits";
import { DateRange } from "../../components/DateRange";
import { GroupedPicker, type PickerGroup } from "../../components/GroupedPicker";
import { accountGroups, type AccountGrouping } from "../../components/accountGroups";
import { format } from "../../lib/money";
import { moneyKey, sortRows, type SortKeyPart, type SortValue } from "../../lib/sorting";
import { ACCOUNT_TYPE_HEADINGS, ACCOUNT_TYPE_LABELS } from "../../lib/labels";
import { compareNames, formatDate, listText, monthLabel } from "../../lib/locale";
import type {
  Account,
  Country,
  Household,
  NetWorth as Report,
  NetWorthAccount,
  NetWorthBook,
  NetWorthHeld,
  NetWorthPoint,
  NetWorthSplit,
} from "../../lib/types";
import type { RegisterPreset } from "../Register";
import { countryName, useCountries } from "../Accounts";
import { RatesAsOf } from "../../components/RatesAsOf";
import { sourceLabel } from "../../lib/fx";
import {
  OPEN_NET_WORTH,
  UNSET_ID,
  addressFor,
  choosesNothing,
  defaultView,
  reportQuery,
  viewFromSearch,
  type NetWorthView,
} from "./netWorthView";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

/** Where the explanation's open-or-shut is remembered, per browser. */
const EXPLAIN_KEY = "net-worth-explanation-open";

// --------------------------------------------------------------------------- //
// Small formatting helpers
// --------------------------------------------------------------------------- //

/** An amount, marked "≈" when it was converted at a rate. */
function amount(minor: number, currency: string, approx: boolean): string {
  return `${approx ? "≈ " : ""}${format(minor, currency)}`;
}

/** A change: "+" in front when it went up, as `format` already puts "-" when down. */
function signed(minor: number, currency: string, approx: boolean): string {
  return `${minor > 0 ? "+" : ""}${amount(minor, currency, approx)}`;
}

/** "on 2026-10-31", under a figure or beside a heading. */
function onDate(date: string): string {
  return t({ message: `on ${date}`, comment: "Net Worth report: the date a figure or table is for, e.g. on 2026-10-31" });
}

/** "1 account", "3 accounts". */
function accountsText(count: number): string {
  return plural(count, { one: "# account", other: "# accounts" });
}

function tone(minor: number | null): string {
  if (minor === null || minor === 0) return "";
  return minor > 0 ? "pos" : "neg";
}

/** A point's name on the chart axis: its month, or "Today". */
function pointLabel(point: NetWorthPoint): string {
  return point.today
    ? t({ message: "Today", comment: "Net Worth report: the chart's last point when the running month is shown" })
    : monthLabel(point.date.slice(0, 7));
}

/** The words for one part of a split, in the reader's language. */
export function splitLabeller(split: NetWorthSplit, countries: Country[] | undefined) {
  return (key: string): string => {
    if (split === "currency") return key;
    if (split === "type") return ACCOUNT_TYPE_HEADINGS[key] ?? key;
    if (split === "institution") {
      return key || t({ message: "No bank set", comment: "Net Worth report: the accounts that do not say which bank holds them" });
    }
    if (!key) return t({ message: "No country set", comment: "Net Worth report: the accounts that do not say which country they are in" });
    const flag = countries?.find((one) => one.code === key)?.flag;
    return `${flag ? `${flag} ` : ""}${countryName(countries, key)}`;
  };
}

/**
 * Which colour each part wears, by the part, never by its rank.
 *
 * The slots are given out over **every** account in the household, not the
 * filtered ones, so narrowing a filter never repaints the parts that stay.
 * Seven hues in a fixed order; an eighth part and beyond share the neutral
 * "other" slot rather than a generated hue.
 */
export function partSlots(
  accounts: Account[],
  split: NetWorthSplit,
  label: (key: string) => string,
): Map<string, number> {
  const keys = new Set<string>();
  for (const one of accounts) {
    if (split === "currency") keys.add(one.currency);
    else if (split === "type") keys.add(one.type);
    else if (split === "institution") keys.add(one.institution?.trim() ?? "");
    else keys.add(one.country ?? "");
  }
  const ordered = [...keys].sort((a, b) => compareNames(label(a), label(b)));
  return new Map(ordered.map((key, at) => [key, at < 7 ? at + 1 : 8]));
}

// --------------------------------------------------------------------------- //
// The screen
// --------------------------------------------------------------------------- //

export function NetWorth({
  household,
  onOpenRegister,
  onOpenExchangeRates,
}: {
  household: Household;
  onOpenRegister?: (preset: RegisterPreset) => void;
  /** Settings → Exchange rates, where rates are fetched or entered (#396). */
  onOpenExchangeRates?: () => void;
}) {
  // What the address asked for, when the report was opened from one;
  // otherwise its defaults.
  const [view, setView] = useState<NetWorthView>(() =>
    new URLSearchParams(window.location.search).get("open") === OPEN_NET_WORTH
      ? viewFromSearch(window.location.search)
      : defaultView(),
  );
  const [grouping, setGrouping] = useState<AccountGrouping>("country");
  const change = (next: Partial<NetWorthView>) => setView((was) => ({ ...was, ...next }));

  // The address follows the view, so the view can be bookmarked; leaving the
  // report gives the shell its bare address back.
  useEffect(() => {
    window.history.replaceState(null, "", addressFor(view, household.id));
  }, [view, household.id]);
  useEffect(
    () => () => {
      if (new URLSearchParams(window.location.search).get("open") === OPEN_NET_WORTH) {
        window.history.replaceState(null, "", "/");
      }
    },
    [],
  );

  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts`),
  });
  const countries = useCountries();
  const all = useMemo(() => accounts.data ?? [], [accounts.data]);

  const query = reportQuery(view);
  const nothing = choosesNothing(view);
  const report = useQuery({
    queryKey: ["net-worth", household.id, query],
    queryFn: () => api.get<Report>(`/households/${household.id}/reports/net-worth?${query}`),
    enabled: !nothing,
  });

  const label = useMemo(() => splitLabeller(view.split, countries.data), [view.split, countries.data]);
  const slots = useMemo(() => partSlots(all, view.split, label), [all, view.split, label]);
  const reset = () => setView(defaultView());

  return (
    <div className="net-worth">
      <Filters
        view={view}
        accounts={all}
        countries={countries.data}
        grouping={grouping}
        onGrouping={setGrouping}
        onChange={change}
      />

      <Problem error={report.error} />

      {nothing ? (
        <NoMatch onReset={reset} />
      ) : report.isLoading || !report.data ? (
        <div className="muted"><Trans comment="Text on the Net Worth report">Loading…</Trans></div>
      ) : report.data.explanation.accounts_counted === 0 ? (
        <NoMatch onReset={reset} />
      ) : (
        <Body
          report={report.data}
          view={view}
          onChange={change}
          label={label}
          slots={slots}
          countries={countries.data}
          onOpenRegister={onOpenRegister}
          onOpenExchangeRates={onOpenExchangeRates}
        />
      )}
    </div>
  );
}

function NoMatch({ onReset }: { onReset: () => void }) {
  return (
    <Empty>
      <Trans>No accounts match these filters.</Trans>{" "}
      <button type="button" className="link" onClick={onReset}>
        <Trans comment="Button on the Net Worth report: put every filter back to its default">Reset the filters</Trans>
      </button>
    </Empty>
  );
}

// --------------------------------------------------------------------------- //
// Filters
// --------------------------------------------------------------------------- //

/** One flat list of choices, each with how many accounts it holds. */
function choices(
  heading: string,
  counts: Map<string, number>,
  name: (id: string) => string,
): PickerGroup[] {
  const items = [...counts.entries()]
    .map(([id, n]) => ({ id, label: name(id), hint: `(${n})` }))
    .sort((a, b) => compareNames(a.label, b.label));
  return items.length ? [{ key: "all", label: heading, items }] : [];
}

function tally(accounts: Account[], keyOf: (one: Account) => string): Map<string, number> {
  const counts = new Map<string, number>();
  for (const one of accounts) counts.set(keyOf(one), (counts.get(keyOf(one)) ?? 0) + 1);
  return counts;
}

const bankOf = (one: Account) => one.institution?.trim() || UNSET_ID;
const countryOf = (one: Account) => one.country || UNSET_ID;

function Filters({
  view,
  accounts,
  countries,
  grouping,
  onGrouping,
  onChange,
}: {
  view: NetWorthView;
  accounts: Account[];
  countries: Country[] | undefined;
  grouping: AccountGrouping;
  onGrouping: (next: AccountGrouping) => void;
  onChange: (next: Partial<NetWorthView>) => void;
}) {
  const banks = choices(
    t({ message: "Banks", comment: "Heading in the bank filter on the Net Worth report" }),
    tally(accounts, bankOf),
    (id) => (id === UNSET_ID ? t({ message: "No bank set", comment: "Net Worth report: the accounts that do not say which bank holds them" }) : id),
  );
  const places = choices(
    t({ message: "Countries", comment: "Heading in the country filter on the Net Worth report" }),
    tally(accounts, countryOf),
    (id) =>
      id === UNSET_ID
        ? t({ message: "No country set", comment: "Net Worth report: the accounts that do not say which country they are in" })
        : countryName(countries, id),
  );
  const currencies = choices(
    t({ message: "Currencies", comment: "Heading in the currency filter on the Net Worth report" }),
    tally(accounts, (one) => one.currency),
    (id) => id,
  );
  // The accounts picker narrows inside the other three, so it offers only
  // the accounts they let through.
  const admitted = accounts.filter(
    (one) =>
      (view.banks === null || view.banks.includes(bankOf(one))) &&
      (view.countries === null || view.countries.includes(countryOf(one))) &&
      (view.currencies === null || view.currencies.includes(one.currency)),
  );

  return (
    <div className="report-bar nw-bar">
      <div className="report-choices">
        <GroupedPicker
          label={t({ message: "All banks", comment: "Text on a dropdown that picks several on the Net Worth report" })}
          groups={banks}
          value={view.banks}
          onChange={(ids) => onChange({ banks: ids })}
        />
        <GroupedPicker
          label={t({ message: "All countries", comment: "Text on a dropdown that picks several on the Net Worth report" })}
          groups={places}
          value={view.countries}
          onChange={(ids) => onChange({ countries: ids })}
        />
        <GroupedPicker
          label={t({ message: "All currencies", comment: "Text on a dropdown that picks several on the Net Worth report" })}
          groups={currencies}
          value={view.currencies}
          onChange={(ids) => onChange({ currencies: ids })}
        />
        <div className="picker-with-mode">
          <GroupedPicker
            label={t({ message: "All accounts", comment: "Text on a dropdown that picks several on the Net Worth report" })}
            groups={accountGroups(admitted, grouping)}
            value={view.accounts}
            onChange={(ids) => onChange({ accounts: ids })}
          />
          <select
            aria-label={t({ message: "Group accounts by", comment: "Screen-reader name on the Net Worth report" })}
            className="small"
            value={grouping}
            onChange={(e) => onGrouping(e.target.value as AccountGrouping)}
          >
            <option value="country"><Trans comment="Option in a dropdown on the Net Worth report">by country</Trans></option>
            <option value="type"><Trans comment="Option in a dropdown on the Net Worth report">by type</Trans></option>
          </select>
        </div>
      </div>

      <div className="report-filters nw-filters">
        <DateRange value={view.range} onChange={(range) => onChange({ range })} />
        <div className="nw-toggles">
          <label className="small">
            <input
              type="checkbox"
              checked={view.closedOnly}
              onChange={(e) => onChange({ closedOnly: e.target.checked })}
            />{" "}
            <Trans comment="Toggle on the Net Worth report: stop at the last month that has ended">Closed months only</Trans>
          </label>
          <label className="small">
            <input
              type="checkbox"
              checked={view.showClosed}
              onChange={(e) => onChange({ showClosed: e.target.checked })}
            />{" "}
            <Trans comment="Toggle on the Net Worth report: list accounts flagged closed in the tables">Show closed accounts</Trans>
          </label>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// The body
// --------------------------------------------------------------------------- //

function Body({
  report,
  view,
  onChange,
  label,
  slots,
  countries,
  onOpenRegister,
  onOpenExchangeRates,
}: {
  report: Report;
  view: NetWorthView;
  onChange: (next: Partial<NetWorthView>) => void;
  label: (key: string) => string;
  slots: Map<string, number>;
  countries: Country[] | undefined;
  onOpenRegister?: (preset: RegisterPreset) => void;
  onOpenExchangeRates?: () => void;
}) {
  const asOf = formatDate(report.as_of);
  const display = report.display_currency;
  const approx = report.converted;
  // The top-level figures as one book, when there is a total to give.
  const main: NetWorthBook | null =
    display && report.headline && report.bridge
      ? {
          currency: display,
          accounts: report.explanation.accounts_counted,
          points: report.points,
          headline: report.headline,
          bridge: report.bridge,
          held: report.held,
        }
      : null;
  const onlyOne = report.points.length === 1 && report.clipped_from !== null;
  const heldIn = listText(report.explanation.currencies);
  const unconverted = listText(report.not_converted.map((one) => one.currency));

  return (
    <>
      {/* "Rates as of" (#396 §5.2), from the same response as the figures.
          The server sends it only when several currencies are in scope and a
          source is set up; with one currency nothing converts and it is
          absent. With no rate stored yet it says so, and links to where they
          are fetched. */}
      {report.rates_status && (
        <RatesAsOf status={report.rates_status} onOpen={onOpenExchangeRates} />
      )}

      {report.clipped_from && (
        <p className="muted small nw-note">
          {onlyOne
            ? t`Showing ${asOf}, the last closed month.`
            : t`Stops at ${asOf}, the last closed month: the month still running is left out.`}
        </p>
      )}
      {!report.closed_only && report.points.some((one) => one.today) && (
        <p className="muted small nw-note">
          <Trans>As of {asOf}, with the month still open.</Trans>
        </p>
      )}

      {!main && (
        <div className="banner info" role="note">
          {t`These accounts are in ${heldIn}, and there are no exchange rates to convert between them, so each currency is shown on its own and nothing is added across them.`}{" "}
          {onOpenExchangeRates && (
            <button type="button" className="link" onClick={onOpenExchangeRates}>
              {report.rates_status
                ? t({ message: "Fetch or enter rates on the Exchange rates page", comment: "Link on the Net Worth report to Settings, Exchange rates, when no rate is stored yet" })
                : t({ message: "Choose a source of rates on the Exchange rates page", comment: "Link on the Net Worth report to Settings, Exchange rates, when the household's source list is empty" })}
            </button>
          )}
        </div>
      )}
      {report.not_converted.length > 0 && (
        <div className="banner warn" role="alert">
          {t`No exchange rate for ${unconverted}, so those accounts are left out of the total and listed below.`}
          <ul className="nw-not-converted">
            {report.not_converted.map((one) => (
              <li key={one.currency}>
                {format(one.native_minor, one.currency)} ·{" "}
                {accountsText(one.accounts)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="muted small nw-scope">
        <Trans>
          Counts every account these filters let through: current accounts, savings, cash, credit
          cards, other assets and other debts. Not counted: investments, property, vehicles,
          pensions, and loans not kept as an account.
        </Trans>
      </p>

      <div className="nw-split">
        <label className="small">
          <Trans comment="Label before the split choice on the Net Worth report: what the bars are divided by">Split by</Trans>{" "}
          <select
            value={view.split}
            onChange={(e) => onChange({ split: e.target.value as NetWorthSplit })}
          >
            <option value="currency"><Trans comment="Option: split the Net Worth chart by currency">Currency</Trans></option>
            <option value="country"><Trans comment="Option: split the Net Worth chart by country">Country</Trans></option>
            <option value="institution"><Trans comment="Option: split the Net Worth chart by bank">Bank</Trans></option>
            <option value="type"><Trans comment="Option: split the Net Worth chart by account type">Account type</Trans></option>
          </select>
        </label>
      </div>

      {main ? (
        <BookView book={main} approx={approx} showFx={approx} label={label} slots={slots} split={view.split} />
      ) : (
        report.currencies.map((book) => (
          <section key={book.currency} className="nw-book" aria-label={book.currency}>
            <h2 className="section-title">{book.currency}</h2>
            <BookView book={book} approx={false} showFx={false} label={label} slots={slots} split={view.split} />
          </section>
        ))
      )}

      <AccountsTable
        report={report}
        countries={countries}
        onOpen={(id) => onOpenRegister?.({ accounts: [id], range: { since: null, until: report.as_of } })}
      />

      <HowItAdds report={report} />
    </>
  );
}

/** Headline, tiles, chart, what changed and where it is held, in one currency. */
function BookView({
  book,
  approx,
  showFx,
  label,
  slots,
  split,
}: {
  book: NetWorthBook;
  approx: boolean;
  showFx: boolean;
  label: (key: string) => string;
  slots: Map<string, number>;
  split: NetWorthSplit;
}) {
  const { headline, currency } = book;
  const end = book.points[book.points.length - 1];
  const asOf = formatDate(end.date);
  const total = amount(headline.net_worth, currency, approx);
  const start = formatDate(headline.range_start);
  const held = accountsText(book.accounts);
  const prevDate = formatDate(headline.prev_date ?? "");

  let takeaway: string;
  if (headline.change_range === null || headline.change_range === 0) {
    takeaway = t`On ${asOf} the household held ${total} across ${held}.`;
  } else {
    const moved = amount(Math.abs(headline.change_range), currency, approx);
    takeaway =
      headline.change_range > 0
        ? t`On ${asOf} the household held ${total} across ${held}, up ${moved} on ${start}.`
        : t`On ${asOf} the household held ${total} across ${held}, down ${moved} on ${start}.`;
  }

  return (
    <>
      <p className="nw-takeaway">{takeaway}</p>

      <div className="nw-tiles">
        <Tile label={t({ message: "Net worth", comment: "Tile on the Net Worth report: the total" })}>
          <strong className={`amount ${tone(headline.net_worth < 0 ? -1 : 0)}`}>{total}</strong>
          <span className="small muted">{t({ message: `as of ${asOf}`, comment: "Net Worth report: under the total, the date it is for" })}</span>
        </Tile>
        <Tile label={t({ message: "Since the month before", comment: "Tile on the Net Worth report: change since the previous month end" })}>
          {headline.change_prev === null ? (
            <strong className="muted">—</strong>
          ) : (
            <>
              <strong className={`amount ${tone(headline.change_prev)}`}>
                {signed(headline.change_prev, currency, approx)}
              </strong>
              <span className="small muted">{onDate(prevDate)}</span>
            </>
          )}
        </Tile>
        <Tile label={t({ message: "Since the start of the range", comment: "Tile on the Net Worth report: change since the first point" })}>
          {headline.change_range === null ? (
            <strong className="muted">—</strong>
          ) : (
            <>
              <strong className={`amount ${tone(headline.change_range)}`}>
                {signed(headline.change_range, currency, approx)}
              </strong>
              <span className="small muted">{onDate(start)}</span>
            </>
          )}
        </Tile>
        <Tile label={t({ message: "Owed on cards and debts", comment: "Tile on the Net Worth report: credit cards and other liabilities, already subtracted" })}>
          <strong className={`amount ${tone(headline.liabilities)}`}>
            {amount(headline.liabilities, currency, approx)}
          </strong>
          <span className="small muted">
            <Trans comment="Under the owed figure on the Net Worth report">already subtracted</Trans>
          </span>
        </Tile>
      </div>

      <TrendChart points={book.points} currency={currency} approx={approx} label={label} slots={slots} />

      <WhatChanged book={book} approx={approx} showFx={showFx} />

      <WhereHeld book={book} approx={approx} label={label} split={split} />
    </>
  );
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="nw-tile">
      <span className="nw-tile-label">{label}</span>
      {children}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// The trend chart: hand-drawn SVG, one stacked bar per point
// --------------------------------------------------------------------------- //

/** Round steps for the value axis: 1, 2 or 5 times a power of ten. */
export function ticks(low: number, high: number, wanted = 4): number[] {
  if (high === low) return [low];
  const raw = (high - low) / wanted;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((one) => one * power).find((one) => one >= raw) ?? raw;
  const first = Math.floor(low / step) * step;
  const answer: number[] = [];
  for (let at = first; at <= high + step / 2; at += step) answer.push(Math.round(at));
  return answer;
}

function useWidth(fallback: number) {
  const holder = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const node = holder.current;
    if (!node) return;
    if (node.clientWidth > 0) setWidth(node.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(entry.contentRect.width);
    });
    watch.observe(node);
    return () => watch.disconnect();
  }, []);
  return { holder, width };
}

function TrendChart({
  points,
  currency,
  approx,
  label,
  slots,
}: {
  points: NetWorthPoint[];
  currency: string;
  approx: boolean;
  label: (key: string) => string;
  slots: Map<string, number>;
}) {
  const { holder, width } = useWidth(640);
  const [active, setActive] = useState<number | null>(null);
  const height = 240;
  const pad = { top: 14, right: 12, bottom: 30, left: 76 };
  const plotW = Math.max(width - pad.left - pad.right, 40);
  const plotH = height - pad.top - pad.bottom;

  const keys = useMemo(() => {
    const seen = new Set<string>();
    for (const point of points) for (const key of Object.keys(point.parts)) seen.add(key);
    return [...seen].sort((a, b) => (slots.get(a) ?? 9) - (slots.get(b) ?? 9) || compareNames(label(a), label(b)));
  }, [points, slots, label]);

  let low = 0;
  let high = 0;
  for (const point of points) {
    const up = Object.values(point.parts).filter((v) => v > 0).reduce((a, b) => a + b, 0);
    const down = Object.values(point.parts).filter((v) => v < 0).reduce((a, b) => a + b, 0);
    high = Math.max(high, up, point.net_worth);
    low = Math.min(low, down, point.net_worth);
  }
  const marks = ticks(low, high);
  const top = Math.max(high, marks[marks.length - 1] ?? high);
  const bottom = Math.min(low, marks[0] ?? low);
  const span = top - bottom || 1;
  const y = (value: number) => pad.top + ((top - value) / span) * plotH;

  const band = plotW / points.length;
  const bar = Math.max(Math.min(36, band * 0.6), 3);
  const every = Math.max(1, Math.ceil((points.length * 56) / plotW));
  const slotOf = (key: string) => slots.get(key) ?? 8;

  const tip = active === null ? null : points[active];
  const tipDate = tip ? formatDate(tip.date) : "";
  const withoutData = tip?.accounts_without_data ?? 0;

  return (
    <figure className="nw-chart">
      <figcaption className="sr-only">
        <Trans>Net worth at each month end, split into its parts.</Trans>
      </figcaption>
      {keys.length > 1 && (
        <ul className="nw-legend">
          {keys.map((key) => (
            <li key={key}>
              <span className={`nw-swatch nw-s${slotOf(key)}`} aria-hidden="true" />
              {label(key)}
            </li>
          ))}
        </ul>
      )}
      <div className="nw-chart-area" ref={holder} onMouseLeave={() => setActive(null)}>
        <svg width={width} height={height} role="img" aria-label={t`Net worth chart`}>
          {marks.map((mark) => (
            <g key={mark}>
              <line
                x1={pad.left}
                x2={pad.left + plotW}
                y1={y(mark)}
                y2={y(mark)}
                className={mark === 0 ? "nw-axis" : "nw-grid"}
              />
              <text x={pad.left - 6} y={y(mark)} dy="0.32em" textAnchor="end" className="nw-tick">
                {format(mark, currency)}
              </text>
            </g>
          ))}
          {points.map((point, at) => {
            const centre = pad.left + band * at + band / 2;
            let up = 0;
            let down = 0;
            const segments = keys
              .filter((key) => (point.parts[key] ?? 0) !== 0)
              .map((key) => {
                const value = point.parts[key];
                const from = value > 0 ? up : down;
                const to = from + value;
                if (value > 0) up = to;
                else down = to;
                const y1 = y(Math.max(from, to));
                const y2 = y(Math.min(from, to));
                // A 2px gap between stacked parts: each gives up a pixel at each end.
                const h = Math.max(y2 - y1 - 2, 1);
                return (
                  <rect
                    key={key}
                    x={centre - bar / 2}
                    y={y1 + 1}
                    width={bar}
                    height={h}
                    rx={2}
                    className={`nw-s${slotOf(key)}`}
                  />
                );
              });
            return (
              <g key={point.date} className={active === at ? "nw-bar on" : "nw-bar"}>
                {segments}
                <circle cx={centre} cy={y(point.net_worth)} r={4} className="nw-net" />
                {point.accounts_without_data > 0 && (
                  <text x={centre} y={pad.top + plotH + 26} textAnchor="middle" className="nw-gap-mark">
                    *
                  </text>
                )}
                {at % every === 0 && (
                  <text x={centre} y={pad.top + plotH + 16} textAnchor="middle" className="nw-tick">
                    {pointLabel(point)}
                  </text>
                )}
                {/* The hit target: the whole band, bigger than the bar. */}
                <rect
                  x={pad.left + band * at}
                  y={pad.top}
                  width={band}
                  height={plotH}
                  className="nw-hit"
                  tabIndex={0}
                  aria-label={`${pointLabel(point)}: ${amount(point.net_worth, currency, approx)}`}
                  onMouseEnter={() => setActive(at)}
                  onFocus={() => setActive(at)}
                  onBlur={() => setActive(null)}
                />
              </g>
            );
          })}
        </svg>
        {tip && active !== null && (
          <div
            className="nw-tip"
            role="status"
            style={{
              left: Math.min(Math.max(pad.left + band * active + band / 2 - 110, 0), Math.max(width - 220, 0)),
            }}
          >
            <strong>{tip.today ? t({ message: `Today, ${tipDate}`, comment: "Net Worth chart tooltip: the last point, which is today's date" }) : tipDate}</strong>
            <span>{amount(tip.net_worth, currency, approx)}</span>
            <ul>
              {keys
                .filter((key) => key in tip.parts)
                .map((key) => (
                  <li key={key}>
                    <span className={`nw-swatch nw-s${slotOf(key)}`} aria-hidden="true" />
                    {label(key)}: {amount(tip.parts[key], currency, approx)}
                  </li>
                ))}
            </ul>
            {tip.accounts_without_data > 0 && (
              <span className="small">
                {plural(withoutData, {
                  one: "# account had no data yet",
                  other: "# accounts had no data yet",
                })}
              </span>
            )}
            {tip.stale_rate && (
              <span className="small">
                <Trans>A rate used here is older than the limit set for it.</Trans>
              </span>
            )}
          </div>
        )}
      </div>
      {points.some((one) => one.accounts_without_data > 0) && (
        <p className="muted small">
          <Trans>
            * Some accounts had no rows yet at that date and count as nothing there: an early bar
            can be missing imports rather than missing money.
          </Trans>
        </p>
      )}
      <ChartTable points={points} keys={keys} currency={currency} approx={approx} label={label} />
    </figure>
  );
}

/** The chart's figures as a table, for a screen reader, a print, or checking. */
function ChartTable({
  points,
  keys,
  currency,
  approx,
  label,
}: {
  points: NetWorthPoint[];
  keys: string[];
  currency: string;
  approx: boolean;
  label: (key: string) => string;
}) {
  const { sort, direction, onSort } = useSort<string>("date", "asc");
  const rows = sortRows(points, sort, direction, (point, column): SortValue => {
    if (column === "date") return point.date;
    if (column === "total") return point.net_worth;
    return point.parts[column.slice(5)] ?? 0;
  });
  return (
    <details className="nw-chart-table">
      <summary className="small">
        <Trans comment="Net Worth report: opens the chart's figures as a table">The chart as a table</Trans>
      </summary>
      <div className="matrix-scroll">
        <table className="nw-table">
          <thead>
            <tr>
              <SortHeading label={t({ message: "Date", comment: "Column heading on the Net Worth report. See GLOSSARY.md" })} column="date" sort={sort} direction={direction} onSort={onSort} />
              <SortHeading label={t({ message: "Net worth", comment: "Column heading on the Net Worth report: the total" })} column="total" sort={sort} direction={direction} onSort={onSort} align="right" />
              {keys.map((key) => (
                <SortHeading key={key} label={label(key)} column={`part:${key}`} sort={sort} direction={direction} onSort={onSort} align="right" />
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((point) => (
              <tr key={point.date}>
                <td className="mono">{point.today ? `${formatDate(point.date)} (${pointLabel(point)})` : formatDate(point.date)}</td>
                <td className="amount">{amount(point.net_worth, currency, approx)}</td>
                {keys.map((key) => (
                  <td key={key} className="amount">{amount(point.parts[key] ?? 0, currency, approx)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// --------------------------------------------------------------------------- //
// What changed, and where it is held
// --------------------------------------------------------------------------- //

function WhatChanged({ book, approx, showFx }: { book: NetWorthBook; approx: boolean; showFx: boolean }) {
  const { bridge, currency, headline, points } = book;
  const startDate = formatDate(headline.range_start);
  const endDate = formatDate(points[points.length - 1].date);
  return (
    <section className="nw-section">
      <h3 className="section-title"><Trans comment="Section heading on the Net Worth report">What changed</Trans></h3>
      <dl className="nw-bridge">
        <dt>{t`Net worth on ${startDate}`}</dt>
        <dd className="amount">{amount(bridge.start, currency, approx)}</dd>
        {showFx && (
          <>
            <dt><Trans comment="Line on the Net Worth report: how much exchange rates moved the start balances">Exchange-rate effect</Trans></dt>
            <dd className={`amount ${tone(bridge.fx_effect)}`}>{signed(bridge.fx_effect, currency, approx)}</dd>
          </>
        )}
        <dt><Trans comment="Line on the Net Worth report: the change that was not exchange rates">Money added or spent</Trans></dt>
        <dd className={`amount ${tone(bridge.money_added_or_spent)}`}>
          {signed(bridge.money_added_or_spent, currency, approx)}
        </dd>
        <dt className="nw-bridge-end">{t`Net worth on ${endDate}`}</dt>
        <dd className="amount nw-bridge-end">{amount(bridge.end, currency, approx)}</dd>
      </dl>
    </section>
  );
}

type HeldKey = "name" | "native" | "rate" | "rate_date" | "value" | "share";

function WhereHeld({
  book,
  approx,
  label,
  split,
}: {
  book: NetWorthBook;
  approx: boolean;
  label: (key: string) => string;
  split: NetWorthSplit;
}) {
  const { sort, direction, onSort } = useSort<HeldKey>("value", "desc");
  const byCurrency = split === "currency";
  const rows = sortRows<NetWorthHeld, HeldKey>(book.held, sort, direction, (line, column) => {
    if (column === "name") return label(line.key);
    if (column === "native")
      return line.native_currency
        ? (moneyKey(line.native_currency, line.native_minor ?? 0, book.currency) as SortKeyPart[])
        : null;
    if (column === "rate") return line.rate === null ? null : Number(line.rate);
    if (column === "rate_date") return line.rate_date;
    if (column === "share") return line.share === null ? null : Number(line.share);
    return line.value;
  });
  const asOf = formatDate(book.points[book.points.length - 1].date);
  return (
    <section className="nw-section">
      <h3 className="section-title">
        <Trans comment="Section heading on the Net Worth report">Where it is held</Trans>{" "}
        <span className="muted small">{onDate(asOf)}</span>
      </h3>
      <div className="matrix-scroll">
        <table className="nw-table">
          <thead>
            <tr>
              <SortHeading label={t({ message: "Part", comment: "Column heading on the Net Worth report: one part of the split (a currency, country, bank or type)" })} column="name" sort={sort} direction={direction} onSort={onSort} />
              {byCurrency && (
                <SortHeading label={t({ message: "In its own currency", comment: "Column heading on the Net Worth report" })} column="native" sort={sort} direction={direction} onSort={onSort} align="right" />
              )}
              {approx && byCurrency && (
                <>
                  <SortHeading label={t({ message: "Rate", comment: "Column heading on the Net Worth report: an exchange rate" })} column="rate" sort={sort} direction={direction} onSort={onSort} align="right" />
                  <SortHeading label={t({ message: "Rate date", comment: "Column heading on the Net Worth report: when the exchange rate was published" })} column="rate_date" sort={sort} direction={direction} onSort={onSort} />
                </>
              )}
              <SortHeading label={t({ message: "Value", comment: "Column heading on the Net Worth report: the amount in the currency the page is in" })} column="value" sort={sort} direction={direction} onSort={onSort} align="right" />
              <SortHeading label={t({ message: "Share", comment: "Column heading on the Net Worth report: percentage of the total" })} column="share" sort={sort} direction={direction} onSort={onSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {rows.map((line) => (
              <tr key={line.key}>
                <td>{label(line.key)}</td>
                {byCurrency && (
                  <td className="amount">
                    {line.native_currency ? format(line.native_minor ?? 0, line.native_currency) : "—"}
                  </td>
                )}
                {approx && byCurrency && (
                  <>
                    <td className="amount mono">{line.rate ?? "—"}</td>
                    <td className="mono">{line.rate_date ? formatDate(line.rate_date) : "—"}</td>
                  </>
                )}
                <td className={`amount ${line.value < 0 ? "neg" : ""}`}>{amount(line.value, book.currency, approx)}</td>
                <td className="amount">{line.share === null ? "—" : percent(line.share)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** "0.1234" as "12.3%", from the server's decimal string. */
function percent(share: string): string {
  const [whole, fraction = ""] = share.replace("-", "").split(".");
  const digits = `${whole}${fraction.padEnd(4, "0")}`;
  const tenths = Number(digits.slice(0, -1)) / 10;
  const sign = share.startsWith("-") ? -1 : 1;
  return new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(
    (sign * tenths) / 100,
  );
}

// --------------------------------------------------------------------------- //
// Accounts
// --------------------------------------------------------------------------- //

type AccountKey = "name" | "bank" | "country" | "type" | "balance" | "value" | "change";

function AccountsTable({
  report,
  countries,
  onOpen,
}: {
  report: Report;
  countries: Country[] | undefined;
  onOpen: (id: string) => void;
}) {
  const display = report.display_currency;
  const approx = report.converted;
  const { sort, direction, onSort } = useSort<AccountKey>(display ? "value" : "balance", "desc");
  const rows = sortRows<NetWorthAccount, AccountKey>(
    report.accounts,
    sort,
    direction,
    (line, column) => {
      if (column === "name") return line.name;
      if (column === "bank") return line.institution;
      if (column === "country") return line.country ? countryName(countries, line.country) : null;
      if (column === "type") return ACCOUNT_TYPE_LABELS[line.type]?.label ?? line.type;
      if (column === "balance") return moneyKey(line.currency, line.native_minor, report.base_currency);
      if (column === "value") return line.value;
      return display ? line.change_range : moneyKey(line.currency, line.native_change, report.base_currency);
    },
    (a, b) => compareNames(a.name, b.name),
  );
  const asOf = formatDate(report.as_of);
  const hidden = report.explanation.accounts_hidden_closed;

  return (
    <section className="nw-section">
      <h3 className="section-title">
        <Trans comment="Section heading on the Net Worth report: noun, bank or cash accounts. See GLOSSARY.md">Accounts</Trans>{" "}
        <span className="muted small">{onDate(asOf)}</span>
      </h3>
      <div className="matrix-scroll">
        <table className="nw-table nw-accounts">
          <thead>
            <tr>
              <SortHeading label={t({ message: "Account", comment: "Column heading on the Net Worth report: noun, a bank or cash account. See GLOSSARY.md" })} column="name" sort={sort} direction={direction} onSort={onSort} />
              <SortHeading label={t({ message: "Bank", comment: "Column heading on the Net Worth report: the institution holding the account" })} column="bank" sort={sort} direction={direction} onSort={onSort} />
              <SortHeading label={t({ message: "Country", comment: "Column heading on the Net Worth report" })} column="country" sort={sort} direction={direction} onSort={onSort} />
              <SortHeading label={t({ message: "Type", comment: "Column heading on the Net Worth report: the account type" })} column="type" sort={sort} direction={direction} onSort={onSort} />
              <SortHeading label={t({ message: "Balance", comment: "Column heading on the Net Worth report: in the account's own currency" })} column="balance" sort={sort} direction={direction} onSort={onSort} align="right" />
              {display && (
                <SortHeading label={t({ message: `Value in ${display}`, comment: "Column heading on the Net Worth report: the balance in the page's currency" })} column="value" sort={sort} direction={direction} onSort={onSort} align="right" />
              )}
              <SortHeading label={t({ message: "Change in the range", comment: "Column heading on the Net Worth report: since the start of the range" })} column="change" sort={sort} direction={direction} onSort={onSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {rows.map((line) => {
              const change = display ? line.change_range : line.native_change;
              const changeCurrency = display ?? line.currency;
              const name = line.name;
              return (
                <tr key={line.id}>
                  <td>
                    <button
                      type="button"
                      className="link"
                      title={t`Open ${name} in the register, up to ${asOf}`}
                      onClick={() => onOpen(line.id)}
                    >
                      {line.name}
                    </button>
                    {line.closed && (
                      <span className="muted small"> · <Trans comment="Beside an account flagged closed on the Net Worth report">closed</Trans></span>
                    )}
                  </td>
                  <td>{line.institution ?? <span className="muted">—</span>}</td>
                  <td>{line.country ? countryName(countries, line.country) : <span className="muted">—</span>}</td>
                  <td>{ACCOUNT_TYPE_LABELS[line.type]?.label ?? line.type}</td>
                  <td className={`amount ${line.native_minor < 0 ? "neg" : ""}`}>{format(line.native_minor, line.currency)}</td>
                  {display && (
                    <td className={`amount ${line.value !== null && line.value < 0 ? "neg" : ""}`}>
                      {line.value === null ? <span className="muted">—</span> : amount(line.value, display, approx && line.currency !== display)}
                    </td>
                  )}
                  <td className={`amount ${tone(change)}`}>
                    {change === null ? <span className="muted">—</span> : signed(change, changeCurrency, Boolean(display) && approx)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {hidden > 0 && (
        <p className="muted small">
          {plural(hidden, {
            one: "# closed account is not listed. Its balances still count in the months it held money; tick Show closed accounts to list it.",
            other: "# closed accounts are not listed. Their balances still count in the months they held money; tick Show closed accounts to list them.",
          })}
        </p>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// How the amounts are put together (§4.6)
// --------------------------------------------------------------------------- //

function readOpen(): boolean {
  try {
    const stored = window.localStorage.getItem(EXPLAIN_KEY);
    return stored === null ? true : stored === "true";
  } catch {
    return true;
  }
}

function HowItAdds({ report }: { report: Report }) {
  const [open, setOpen] = useState(readOpen);
  const ex = report.explanation;
  const display = report.display_currency;
  const asOf = formatDate(report.as_of);
  const counted = accountsText(ex.accounts_counted);
  const bridge = report.bridge;
  const fxEffect = bridge && display ? signed(bridge.fx_effect, display, true) : "";
  const moneyMoved = bridge && display ? signed(bridge.money_added_or_spent, display, report.converted) : "";
  const sources = listText((report.rate_source?.chain ?? []).map((one) => sourceLabel(one)));
  const inCurrencies = listText(ex.currencies);
  const missing = listText(ex.missing);
  const emptyAtStart = ex.accounts_without_data_at_start;

  return (
    <details
      className="nw-explain"
      open={open}
      onToggle={(event) => {
        const next = (event.currentTarget as HTMLDetailsElement).open;
        setOpen(next);
        try {
          window.localStorage.setItem(EXPLAIN_KEY, String(next));
        } catch {
          // Private windows refuse; the section still opens and shuts.
        }
      }}
    >
      <summary>
        <Trans comment="Heading of the explanation at the foot of the Net Worth report">How the amounts are put together</Trans>
      </summary>
      <ol>
        <li>
          <strong><Trans comment="Net Worth explanation, point 1">Which accounts are counted.</Trans></strong>{" "}
          {t`The filters above let through ${counted}, in ${inCurrencies}.`}{" "}
          <Trans>
            Investments, property, vehicles, pensions, and loans not kept as an account are left out by
            design: the tracker does not record them.
          </Trans>
        </li>
        <li>
          <strong><Trans comment="Net Worth explanation, point 2">How each balance is found.</Trans></strong>{" "}
          <Trans>
            Every transaction on the account up to and including the date, the opening balance
            included. A transfer between two of your accounts moves money from one balance to the
            other and does not change the total; between two currencies, the total moves only by
            the difference in conversion.
          </Trans>
        </li>
        <li>
          <strong><Trans comment="Net Worth explanation, point 3">How each foreign balance is converted.</Trans></strong>{" "}
          {report.converted ? (
            <>
              {t`Rates come from ${sources}, the last one published on or before each date.`}
              <ul>
                {ex.rates.map((rate) => {
                  const code = rate.currency;
                  const figure = rate.rate;
                  const base = display ?? "";
                  const published = formatDate(rate.rate_date);
                  const from = sourceLabel(rate.source);
                  return (
                    <li key={code}>
                      {t({ message: `${code}: ${figure} per 1 ${base}, published ${published} (${from})`, comment: "Net Worth explanation: one rate, e.g. GBP: 0.85 per 1 EUR, published 2026-10-30 (ECB)" })}
                      {rate.before_as_of && ` — ${t`the last rate before ${asOf}`}`}
                      {rate.stale && ` — ${t`older than the limit set for rates`}`}
                    </li>
                  );
                })}
              </ul>
              {ex.largest_foreign && display && (
                <p className="mono">
                  {format(ex.largest_foreign.native_minor, ex.largest_foreign.currency)} ÷{" "}
                  {ex.largest_foreign.rate} = {format(ex.largest_foreign.value, display)}
                </p>
              )}
            </>
          ) : display ? (
            t`Nothing is converted: every account here is in ${display}, so the figures are exact.`
          ) : (
            t`Nothing is converted: there are no exchange rates, so each currency keeps its own figures and they are never added together.`
          )}
        </li>
        <li>
          <strong><Trans comment="Net Worth explanation, point 4">How the total is built.</Trans></strong>{" "}
          <Trans>
            Each account's value is rounded to the cent, then the values are added. Credit cards and
            other debts already hold a negative balance, so they subtract by themselves.
          </Trans>
        </li>
        {bridge && display && (
          <li>
            <strong><Trans comment="Net Worth explanation, point 5">How the change is split.</Trans></strong>{" "}
            {report.converted
              ? t`The exchange-rate effect is what the balances held at the start would be worth at the end rates, less what they were worth at the start rates: ${fxEffect}. The rest of the change, ${moneyMoved}, is money added or spent, including money that arrived during the range and then moved with the rate.`
              : t`In one currency there is no exchange-rate effect: the whole change, ${moneyMoved}, is money added or spent.`}
          </li>
        )}
        {report.converted && (
          <li>
            <strong><Trans comment="Net Worth explanation, point 6">What "≈" means.</Trans></strong>{" "}
            <Trans>
              Converted figures depend on the rate source: a different source gives slightly different
              numbers. The amounts in each account's own currency are exact.
            </Trans>
          </li>
        )}
        <li>
          <strong><Trans comment="Net Worth explanation, point 7">Closed months.</Trans></strong>{" "}
          {report.closed_only
            ? t`Closed months only is on, so the page stops at ${asOf}, the end of the last month that has finished. The running month is left out because its imports are usually incomplete and its card bills unpaid.`
            : t`Closed months only is off, so the last point is today, ${asOf}, in a month still running.`}
        </li>
        {(ex.accounts_without_data > 0 || ex.accounts_without_data_at_start > 0 || ex.missing.length > 0 || ex.stale) && (
          <li>
            <strong><Trans comment="Net Worth explanation, point 8">Gaps.</Trans></strong>{" "}
            {ex.accounts_without_data_at_start > 0 &&
              plural(emptyAtStart, {
                one: "# account had no rows yet at the start of the range and counts as nothing until its first one. ",
                other: "# accounts had no rows yet at the start of the range and count as nothing until their first one. ",
              })}
            {ex.missing.length > 0 && t`No rate for ${missing}, so those accounts are left out of the total. `}
            {ex.stale && t`At least one rate is older than the limit set for rates; it is still used, and marked.`}
          </li>
        )}
      </ol>
    </details>
  );
}
