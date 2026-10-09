/**
 * Income against expense, month by month, broken down by category.
 *
 * **One currency at a time, and the toggle at the top is the whole point.**
 * This ledger never converts -- currency lives on the account and there is no
 * rate anywhere in the schema -- so a column adding EUR to GBP would be a
 * number with no meaning. The previous build shipped three reports that did
 * exactly that. Here the currency is a *facet*: you pick one, and everything
 * on screen is about that one. There is no "all currencies" option, because
 * there is no answer to give.
 *
 * The report is **read-only**. Nothing here writes, so nothing here opens a
 * batch and there is no undo, because there is nothing to undo.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { Dialog, Empty, Hint, Problem } from "../../components/bits";
import { DateRange, monthsBack, type Range } from "../../components/DateRange";
import {
  GroupedPicker,
  type PickerGroup,
  type Selection,
} from "../../components/GroupedPicker";
import { accountGroups, type AccountGrouping } from "../../components/accountGroups";
import { format } from "../../lib/money";
import type {
  Account,
  CategoryGroup,
  Household,
  IncomeExpense as Report,
  ReportBehind,
  ReportRow,
  ReportSection,
} from "../../lib/types";
import { amountLang, formatDate, listText, monthLabel } from "../../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { categoryName } from "../../lib/labels";

/**
 * Which figure somebody clicked, in the terms the server narrows by.
 *
 * Every field is optional in the same way the corresponding cell is wide: no
 * `period` is the Total column, no `direction` is the Net row. `title` is
 * carried along rather than rebuilt from the parts, because the bubble's
 * heading should read the way the table reads -- "Groceries — Mar 2026" --
 * and the table is the only place that knows both halves.
 */
type Cell = {
  title: string;
  period?: string;
  rowCategoryId?: string | null;
  rowUncategorised?: boolean;
  direction?: "in" | "out";
};

/**
 * The element the Reports shell leaves beside an open report's `<h1>`.
 *
 * `Reports.tsx` renders it as the first child of the heading row; it belongs to
 * that file, and the id is the whole of the contract between the two.
 */
const HEAD_SLOT = "report-head-slot";

/**
 * Put the report's own heading controls on the heading's line.
 *
 * A portal rather than a prop, because what goes in there -- the currency
 * toggle -- is a control over state this component owns. Lifting the chosen
 * currency into `Reports.tsx` so it could render the toggle itself would move
 * a fact about *this* report into the shell that lists every report.
 *
 * **The fallback is not defensive decoration.** The slot is a feature of one
 * shell, and this report renders outside it -- in a test, and in whatever the
 * second place turns out to be. A missing slot has to leave the toggle on
 * screen rather than blank it: the toggle is what says which currency every
 * figure below is in, and a report that loses it is a report of unlabelled
 * numbers.
 */
export function HeadSlot({ children }: { children: ReactNode }) {
  const [slot, setSlot] = useState<HTMLElement | null>(null);

  // Layout, not passive. The slot is in the document only once the parent has
  // committed, so the first render has nowhere to portal to and draws the
  // fallback; running before paint is what stops anybody seeing it move.
  useLayoutEffect(() => {
    setSlot(document.getElementById(HEAD_SLOT));
  }, []);

  if (slot) return createPortal(children, slot);
  return <div className="report-head">{children}</div>;
}

/**
 * The fold state after clicking one heading.
 *
 * A function rather than two lines inside the setter so that the test drives
 * the same transition the screen does -- a harness with its own copy of "add
 * it if it is missing" proves the harness works.
 */
export function toggleFold(current: ReadonlySet<string>, key: string): ReadonlySet<string> {
  const next = new Set(current);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

export function IncomeExpense({ household }: { household: Household }) {
  const [currency, setCurrency] = useState<string | null>(null);
  // Six months back, inclusive of this one. The default the report opens on:
  // long enough that a pattern is visible, short enough to fit on a screen.
  const [range, setRange] = useState<Range>(() => monthsBack(6));
  const [grouping, setGrouping] = useState<AccountGrouping>("country");
  const [chosenAccounts, setChosenAccounts] = useState<Selection>(null);
  const [chosenCategories, setChosenCategories] = useState<Selection>(null);
  const [withUncategorised, setWithUncategorised] = useState(true);

  /**
   * Which bands and which category groups are folded shut.
   *
   * Held here rather than inside `ReportTable` because that component unmounts
   * every time the query goes back to loading -- changing the currency, the
   * range or a filter -- and a fold that reopened itself whenever you narrowed
   * the report would be worse than no fold at all. A set of keys rather than a
   * boolean per section, so a group added tomorrow starts open without anybody
   * adding a field for it.
   *
   * Nothing is written anywhere: this is what is on screen, not a preference,
   * and a report stores nothing by design.
   */
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const toggleCollapsed = useCallback((key: string) => {
    setCollapsed((was) => toggleFold(was, key));
  }, []);

  const currencies = useQuery({
    queryKey: ["report-currencies", household.id],
    queryFn: () =>
      api.get<{ currencies: string[] }>(
        `/households/${household.id}/reports/currencies`,
      ),
  });

  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts`),
  });

  const categories = useQuery({
    queryKey: ["categories", household.id, false],
    queryFn: () => api.get<CategoryGroup[]>(`/households/${household.id}/categories`),
  });

  const available = currencies.data?.currencies ?? [];

  // The busiest currency, until somebody picks another. Kept in an effect
  // rather than in the query's initial data because the list arrives after the
  // first render, and defaulting to `available[0]` inline would re-pick it
  // every time the list refetched -- overriding a choice the person had made.
  useEffect(() => {
    if (currency === null && available.length > 0) setCurrency(available[0]);
  }, [available, currency]);

  /**
   * Only the accounts in the currency on screen.
   *
   * A EUR report cannot say anything about a GBP account, so offering one in
   * the filter invites a person to tick it and watch nothing change -- which
   * reads as a broken filter rather than as the currency rule working.
   */
  const inCurrency = useMemo(
    () => (accounts.data ?? []).filter((one) => one.currency === currency),
    [accounts.data, currency],
  );

  const accountOptions = useMemo(
    () => accountGroups(inCurrency, grouping),
    [inCurrency, grouping],
  );

  const categoryOptions: PickerGroup[] = useMemo(
    () =>
      (categories.data ?? []).map((group) => ({
        key: group.id,
        label: group.name,
        items: group.categories.map((one) => ({ id: one.id, label: one.name })),
      })),
    [categories.data],
  );

  const query = useMemo(() => {
    const params = new URLSearchParams();
    if (currency) params.set("currency", currency);
    if (range.since) params.set("since", range.since);
    if (range.until) params.set("until", range.until);
    for (const id of chosenAccounts ?? []) params.append("account_id", id);
    for (const id of chosenCategories ?? []) params.append("category_id", id);
    if (!withUncategorised) params.set("include_uncategorised", "false");
    return params.toString();
  }, [currency, range, chosenAccounts, chosenCategories, withUncategorised]);

  const report = useQuery({
    queryKey: ["income-expense", household.id, query],
    queryFn: () =>
      api.get<Report>(`/households/${household.id}/reports/income-expense?${query}`),
    enabled: Boolean(currency),
  });

  if (currencies.isLoading)
    return (
      <div className="muted">
        <Trans comment="Text on the Income vs Expense report">Loading…</Trans>
      </div>
    );

  if (available.length === 0)
    return (
      <Empty>
        <Trans>
          There are no accounts yet, so there is nothing to report on. Add one on the Accounts
          screen and the figures will follow.
        </Trans>
      </Empty>
    );

  return (
    <>
      {/* The currency toggle goes on the heading's line, at the left of it.

          It says what every figure below is denominated in, which is part of
          the title of the thing you are looking at rather than one filter
          among several -- so it belongs beside the name of the report, not in
          the filter bar. `Reports.tsx` leaves a slot in front of the `<h1>`
          for exactly this; `HeadSlot` falls back to a line of its own where
          there is no slot. */}
      <HeadSlot>
        <CurrencyToggle value={currency} options={available} onChange={setCurrency} />
      </HeadSlot>

      <div className="report-bar">
        {/* The two pickers take the place the toggle vacated: top left, ahead
            of `.report-filters`, which now carries the date range alone. They
            are what you reach for repeatedly while reading a report, so they
            sit at the edge you read from rather than tucked under the
            calendar. */}
        <div className="report-choices">
          <GroupedPicker
            label={t({ message: "All categories", comment: "Text on a dropdown that picks several on the Income vs Expense report" })}
            groups={categoryOptions}
            value={chosenCategories}
            onChange={setChosenCategories}
            extra={{
              label: t({ message: "Uncategorised", comment: "Label on the Income vs Expense report: having no category. See GLOSSARY.md" }),
              hint: t`(rows with no category yet)`,
              checked: withUncategorised,
              onChange: setWithUncategorised,
            }}
          />

          <div className="picker-with-mode">
            <GroupedPicker
              label={t({ message: "All accounts", comment: "Text on a dropdown that picks several on the Income vs Expense report" })}
              groups={accountOptions}
              value={chosenAccounts}
              onChange={setChosenAccounts}
            />
            {/* Which way the accounts are gathered. Two answers, both
                useful, and neither is a filter -- it changes the headings
                you tick, not the accounts that exist. */}
            <select
              aria-label={t`Group accounts by`}
              className="small"
              value={grouping}
              onChange={(e) => setGrouping(e.target.value as AccountGrouping)}
            >
              <option value="country"><Trans comment="Option in a dropdown on the Income vs Expense report">by country</Trans></option>
              <option value="type"><Trans comment="Option in a dropdown on the Income vs Expense report">by type</Trans></option>
            </select>
          </div>
        </div>

        <div className="report-filters">
          <DateRange value={range} onChange={setRange} />
        </div>
      </div>

      <Problem error={report.error} />

      {report.isLoading || !report.data ? (
        <div className="muted"><Trans comment="Text on the Income vs Expense report">Loading…</Trans></div>
      ) : (
        <>
          {/* "All dates" sends no ends and the server resolves them to where
              this household's flow actually starts and stops. Saying which
              dates it settled on is not decoration: the columns are calendar
              months, so the window has to close somewhere, and a report whose
              range was decided for it should say what was decided. */}
          {(!range.since || !range.until) && (
            <p className="muted small" style={{ marginTop: 0 }}>
              <Trans>
                All dates: {report.data.since} to {report.data.until}.
              </Trans>
            </p>
          )}
          <ReportTable
            report={report.data}
            household={household}
            query={query}
            collapsed={collapsed}
            onToggle={toggleCollapsed}
          />
        </>
      )}
    </>
  );
}

/**
 * The currency switch.
 *
 * Radio buttons in spirit -- exactly one is always chosen and there is no
 * "all". A select would imply a list you could leave unanswered, and a
 * checkbox set would imply you could tick two, which is the one thing this
 * report must never do.
 */
export function CurrencyToggle({
  value,
  options,
  onChange,
}: {
  value: string | null;
  options: string[];
  onChange: (next: string) => void;
}) {
  return (
    <div className="currency-toggle">
      <span className="daterange-label">
        <Trans comment="Text on the Income vs Expense report: noun. See GLOSSARY.md">Currency</Trans>
      </span>
      <div role="radiogroup" aria-label={t({ message: "Currency", comment: "Screen-reader name on the Income vs Expense report: noun. See GLOSSARY.md" })} className="daterange-presets">
        {options.map((code) => (
          <button
            key={code}
            type="button"
            role="radio"
            aria-checked={code === value}
            className={code === value ? "chip active" : "chip"}
            onClick={() => onChange(code)}
          >
            {code}
          </button>
        ))}
      </div>
      {options.length > 1 && (
        <Hint label={t`why one currency at a time`}>
          <p>
            <Trans>
              This ledger never converts. Currency lives on the account, and there is no exchange
              rate stored anywhere — so a total mixing {listText(options)} would be a number with
              nothing behind it.
            </Trans>
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            <Trans>
              Each currency gets its own report instead. Nothing on this screen ever adds two
              together.
            </Trans>
          </p>
        </Hint>
      )}
    </div>
  );
}

/** "3 transfer legs", for the line saying what the report left out. */
function legs(count: number): string {
  return plural(count, { one: `${count} transfer leg`, other: `${count} transfer legs` });
}

/** "2 opening balances", likewise. */
function openings(count: number): string {
  return plural(count, { one: `${count} opening balance`, other: `${count} opening balances` });
}

export function ReportTable({
  report,
  household,
  query,
  collapsed,
  onToggle,
}: {
  report: Report;
  household: Household;
  query: string;
  /** The keys of the bands and groups that are folded shut. */
  collapsed: ReadonlySet<string>;
  onToggle: (key: string) => void;
}) {
  const [cell, setCell] = useState<Cell | null>(null);
  const onOpen = setCell;
  const { months, currency } = report;
  const gaps = report.coverage.months_in_range - report.coverage.months_with_activity;
  const hidden = report.excluded.transfers + report.excluded.opening_balances;
  // Its own sentence rather than a third clause of the one above: a ledger
  // with work expenses and no transfers in the window must still say so.
  const work = report.excluded.reimbursements;

  return (
    <>
      {/* `matrix-scroll`, not the shared `table-scroll`.

          Below 768px `table-scroll` turns every row into a card -- the right
          answer for the register, where a row is one transaction and the
          column headings are repeated onto each line. It is the wrong answer
          here: in a matrix the *column* carries half the meaning, and the
          card transform drops the month headings, leaving eight figures in a
          stack with nothing to say which month any of them is. A report
          stays a grid on a phone and scrolls sideways, with the category
          column pinned. */}
      <div className="matrix-scroll">
        <table className="report">
          <thead>
            <tr>
              <th className="report-label">&nbsp;</th>
              {months.map((period) => (
                <th key={period} className="amount">
                  {monthLabel(period)}
                </th>
              ))}
              <th className="amount"><Trans comment="Column heading on the Income vs Expense report: noun, per month">Average</Trans></th>
              <th className="amount"><Trans comment="Column heading on the Income vs Expense report: noun, the sum">Total</Trans></th>
            </tr>
          </thead>

          <Band
            title={t({ message: "Income", comment: "Tooltip on the Income vs Expense report: noun, money that came in" })}
            tone="in"
            section={report.income}
            months={months}
            currency={currency}
            collapsed={collapsed}
            onToggle={onToggle}
            onOpen={setCell}
          />
          <Band
            title={t({ message: "Expense", comment: "Tooltip on the Income vs Expense report: noun, money that went out" })}
            tone="out"
            section={report.expense}
            months={months}
            currency={currency}
            collapsed={collapsed}
            onToggle={onToggle}
            onOpen={setCell}
          />

          <tfoot>
            {/* The bottom line, and the only line on the report that is
                coloured by its sign: green when the household kept money,
                red when it did not. Everywhere else a sign is arithmetic --
                expense is negative by construction -- so colouring it would
                paint a whole band red and say nothing. Here it is the
                answer.

                Colour is never the only thing carrying it. `format` keeps
                the "-" on a negative, which is what survives a monochrome
                print and a reader who cannot tell the two hues apart, and
                `Figure` puts the word beside it for a screen reader, which
                is told no colour at all. */}
            <tr className="report-net">
              <th scope="row" className="report-label">
                <Trans comment="Column heading on the Income vs Expense report: income minus spending">
                  Net
                </Trans>
              </th>
              {months.map((period) => (
                <td key={period} className="amount">
                  <Figure
                    minor={report.net_by_month[period] ?? 0}
                    currency={currency}
                    signed
                    onOpen={onOpen}
                    cell={{ title: t({ message: `Net — ${monthLabel(period)}`, comment: "Label on the Income vs Expense report" }), period }}
                  />
                </td>
              ))}
              {/* The average of a net is not a figure with rows behind it --
                  it is arithmetic over two other averages -- so there is
                  nothing to drill into and the cell says so rather than
                  offering a bubble that would have to invent its contents. */}
              <td className="amount muted">—</td>
              <td className="amount">
                <Figure
                  minor={report.net_total_minor}
                  currency={currency}
                  strong
                  signed
                  onOpen={onOpen}
                  cell={{ title: t`Net — whole period` }}
                />
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      {/* What the report left out, said out loud. A report that quietly drops
          a fifth of the register is a report nobody can check -- and counting
          transfers as spend overstated outflow by 41.5% on the real ledger,
          which is exactly the kind of thing this line surfaces. */}
      <p className="muted small report-notes">
        {hidden > 0 ? (
          <>
            {t`Not counted: ${legs(report.excluded.transfers)} and ${openings(report.excluded.opening_balances)}. Moving money between your own accounts is not income or spending, and what an account held when you started is not money you earned.`}{" "}
          </>
        ) : null}
        {work > 0 ? (
          <>
            {plural(work, {
              one: `${work} work expense or repayment left out: what work pays you back was never your spending, and the repayment is not income.`,
              other: `${work} work expenses and repayments left out: what work pays you back was never your spending, and the repayment is not income.`,
            })}{" "}
          </>
        ) : null}
        {gaps > 0 ? (
          <>
            {plural(report.coverage.months_in_range, {
              one: `${report.coverage.months_with_activity} of ${report.coverage.months_in_range} months have any transactions — an empty column is a month with nothing imported, not a month you spent nothing.`,
              other: `${report.coverage.months_with_activity} of ${report.coverage.months_in_range} months have any transactions — an empty column is a month with nothing imported, not a month you spent nothing.`,
            })}
          </>
        ) : null}
      </p>

      {cell && (
        <Behind
          cell={cell}
          household={household}
          query={query}
          currency={currency}
          onClose={() => setCell(null)}
        />
      )}
    </>
  );
}

/**
 * The rows behind one figure.
 *
 * Opened by clicking the figure, and it asks the server rather than filtering
 * anything it already has: the report carries sums, not rows, and a bubble
 * that reconstructed the rows from the register would be composing a
 * different predicate from the one that made the number -- transfer legs and
 * opening balances included -- and would hand somebody checking a figure the
 * evidence that it was wrong when it was right.
 *
 * It prints its own total, and that total is counted over the whole match
 * rather than over the rows shown, so a capped list still adds up to the cell
 * it came from.
 */
function Behind({
  cell,
  household,
  query,
  currency,
  onClose,
}: {
  cell: Cell;
  household: Household;
  query: string;
  currency: string;
  onClose: () => void;
}) {
  const params = useMemo(() => {
    const next = new URLSearchParams(query);
    if (cell.period) next.set("period", cell.period);
    if (cell.rowUncategorised) next.set("row_uncategorised", "true");
    else if (cell.rowCategoryId) next.set("row_category_id", cell.rowCategoryId);
    if (cell.direction) next.set("direction", cell.direction);
    return next.toString();
  }, [query, cell]);

  const rows = useQuery({
    queryKey: ["income-expense-behind", household.id, params],
    queryFn: () =>
      api.get<ReportBehind>(
        `/households/${household.id}/reports/income-expense/behind?${params}`,
      ),
  });

  return (
    <Dialog title={cell.title} onClose={onClose}>
      <Problem error={rows.error} />
      {rows.isLoading || !rows.data ? (
        <p className="muted"><Trans comment="Sentence on the Income vs Expense report">Loading…</Trans></p>
      ) : rows.data.entries.length === 0 ? (
        <Empty><Trans>Nothing went into this one.</Trans></Empty>
      ) : (
        <>
          <div className="behind-scroll">
            <table className="behind">
              <thead>
                <tr>
                  <th><Trans comment="Column heading on the Income vs Expense report: noun, a bank or cash account. See GLOSSARY.md">Account</Trans></th>
                  <th><Trans comment="Column heading on the Income vs Expense report: noun. See GLOSSARY.md">Date</Trans></th>
                  <th><Trans comment="Column heading on the Income vs Expense report: noun, who was paid or who paid. See GLOSSARY.md">Payee</Trans></th>
                  <th><Trans comment="Column heading on the Income vs Expense report: noun, the free-text line of a transaction. See GLOSSARY.md">Memo</Trans></th>
                  <th className="amount"><Trans comment="Column heading on the Income vs Expense report: noun, a sum of money. See GLOSSARY.md">Amount</Trans></th>
                </tr>
              </thead>
              <tbody>
                {rows.data.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td>{entry.account_name}</td>
                    <td className="mono">{formatDate(entry.date)}</td>
                    <td>{entry.payee_name ?? <span className="muted">—</span>}</td>
                    <td className="muted small">{entry.memo ?? ""}</td>
                    <td className="amount">
                      <Figure minor={entry.amount_minor} currency={currency} />
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row" colSpan={4}>
                    {plural(rows.data.count, {
                      one: `${rows.data.count} transaction`,
                      other: `${rows.data.count} transactions`,
                    })}
                  </th>
                  <td className="amount">
                    <Figure minor={rows.data.total_minor} currency={currency} strong />
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
          {rows.data.capped && (
            <p className="muted small">
              <Trans>
                Showing the {rows.data.entries.length} most recent of {rows.data.count}. The total
                above is over all of them — narrow the report, or open one month rather than the
                whole period.
              </Trans>
            </p>
          )}
        </>
      )}
    </Dialog>
  );
}

/** A band's rows, gathered under the category group they belong to. */
export type RowGroup = {
  /** The group's name, or `""` for the rows that belong to no group. */
  key: string;
  /** `null` for the rows that belong to no group -- they get no heading. */
  label: string | null;
  rows: ReportRow[];
};

/**
 * Gather a band's rows under their category groups.
 *
 * **Groups come out in the order their first row arrived in**, and rows keep
 * the order they arrived in inside a group. The server sorts rows by size, so
 * this puts the group holding the biggest figure first and leaves the biggest
 * figure at the top of it -- which is as much of "largest first" as survives
 * grouping at all. Sorting the groups alphabetically instead would scatter the
 * numbers somebody opened the report to see.
 *
 * The rows with no group go last and get no heading. On this report that is
 * "Uncategorised", which is the *absence* of a category: a heading over it
 * would give that absence a name it does not have.
 */
export function groupRows(rows: ReportRow[]): RowGroup[] {
  const groups: RowGroup[] = [];
  const seen = new Map<string, RowGroup>();
  const loose: ReportRow[] = [];

  for (const row of rows) {
    if (row.group_name === null) {
      loose.push(row);
      continue;
    }
    let group = seen.get(row.group_name);
    if (!group) {
      group = { key: row.group_name, label: row.group_name, rows: [] };
      seen.set(row.group_name, group);
      groups.push(group);
    }
    group.rows.push(row);
  }

  if (loose.length > 0) groups.push({ key: "", label: null, rows: loose });
  return groups;
}

/** The key a band folds under. */
export const bandKey = (tone: "in" | "out") => `band:${tone}`;
/** The key one category group folds under, within its band. */
export const groupKey = (tone: "in" | "out", group: string) => `group:${tone}:${group}`;

/**
 * A fold control, sitting in a heading's label cell.
 *
 * A real `<button>` with `aria-expanded`, not a clickable row: the state has to
 * be announced and reachable from the keyboard, and the caret alone is decoration.
 */
function Fold({
  open,
  label,
  onToggle,
}: {
  open: boolean;
  label: string;
  onToggle: () => void;
}) {
  return (
    <button type="button" className="fold" aria-expanded={open} onClick={onToggle}>
      <span className="fold-caret" aria-hidden="true">
        {open ? "▾" : "▸"}
      </span>
      {label}
    </button>
  );
}

/**
 * One half of the report: a heading row, the categories under their group
 * headings, and a total.
 *
 * A `tbody` per band rather than one long one, so the heading row and its
 * categories stay together for a screen reader and the two halves can be
 * styled apart without a class on every row.
 *
 * Folding the band hides its categories and **keeps its total row**: the
 * question "how much did we spend" has to stay answered when the breakdown is
 * shut, or folding would be hiding the figure rather than the detail.
 */
function Band({
  title,
  tone,
  section,
  months,
  currency,
  collapsed,
  onToggle,
  onOpen,
}: {
  title: string;
  tone: "in" | "out";
  section: ReportSection;
  months: string[];
  currency: string;
  collapsed: ReadonlySet<string>;
  onToggle: (key: string) => void;
  onOpen: (cell: Cell) => void;
}) {
  const key = bandKey(tone);
  const shut = collapsed.has(key);
  const groups = useMemo(() => groupRows(section.rows), [section.rows]);
  // A whole phrase per band, not "Total" plus the band's name lower-cased:
  // case and word order are the translator's.
  const total = tone === "in" ? t({ message: "Total income", comment: "Label on the Income vs Expense report" }) : t({ message: "Total expense", comment: "Label on the Income vs Expense report" });

  return (
    <tbody className={`report-band report-${tone}`}>
      <tr className="report-heading">
        {/* The heading sits in the label column alone, with the rest of the
            row as one plain cell beside it. A `th` spanning every column
            cannot be made sticky -- a cell wider than the scrollport does not
            stick, it just scrolls out of the container and gets clipped, so
            at twelve months the band headings vanished the moment anybody
            scrolled sideways. One column wide, it sticks like every other row
            label does. */}
        <th scope="rowgroup" className="report-label">
          <Fold open={!shut} label={title} onToggle={() => onToggle(key)} />
        </th>
        <td colSpan={months.length + 2}>
          {shut && section.rows.length > 0 && (
            <span className="muted small">
              {plural(section.rows.length, {
                one: `${section.rows.length} category folded away — the total is on the line below.`,
                other: `${section.rows.length} categories folded away — the total is on the line below.`,
              })}
            </span>
          )}
        </td>
      </tr>

      {shut ? null : section.rows.length === 0 ? (
        <tr>
          <td className="muted small" colSpan={months.length + 3}>
            <Trans>
              Nothing in this window.
            </Trans>
          </td>
        </tr>
      ) : (
        groups.map((group) => (
          <Group
            key={group.key}
            group={group}
            tone={tone}
            months={months}
            currency={currency}
            collapsed={collapsed}
            onToggle={onToggle}
            onOpen={onOpen}
          />
        ))
      )}

      <tr className="report-total">
        <th scope="row" className="report-label">
          {total}
        </th>
        {months.map((period) => (
          <td key={period} className="amount">
            <Figure
              minor={section.by_month[period] ?? 0}
              currency={currency}
              onOpen={onOpen}
              cell={{
                title: `${total} — ${monthLabel(period)}`,
                period,
                direction: tone,
              }}
            />
          </td>
        ))}
        {/* An average has no rows behind it -- it is the total divided by the
            months on screen -- so it is not offered as something to open. */}
        <td className="amount">
          <Figure minor={section.average_minor} currency={currency} />
        </td>
        <td className="amount">
          <Figure
            minor={section.total_minor}
            currency={currency}
            strong
            onOpen={onOpen}
            cell={{ title: total, direction: tone }}
          />
        </td>
      </tr>
    </tbody>
  );
}

/**
 * One category group: a heading that breaks the list, and its categories.
 *
 * The heading is a band of its own rather than an indent, because what it has
 * to do is **separate** -- a reader going down a column of twenty categories
 * needs to see where Housing stops and Transport starts, and an indent does
 * not say that at a glance.
 *
 * It carries no figures. Everything else on this report is a number the server
 * computed and can be asked to justify row by row; a subtotal added up here
 * would be the one figure on screen with no drill-through behind it, and its
 * average would round differently from every other average in the table. The
 * group totals belong in the response before they belong on the screen.
 */
function Group({
  group,
  tone,
  months,
  currency,
  collapsed,
  onToggle,
  onOpen,
}: {
  group: RowGroup;
  tone: "in" | "out";
  months: string[];
  currency: string;
  collapsed: ReadonlySet<string>;
  onToggle: (key: string) => void;
  onOpen: (cell: Cell) => void;
}) {
  const key = groupKey(tone, group.key);
  // Rows with no group have no heading, so there is nothing to fold them with
  // and they are always shown.
  const shut = group.label !== null && collapsed.has(key);

  return (
    <>
      {group.label !== null && (
        <tr className="report-group">
          <th className="report-label">
            <Fold open={!shut} label={group.label} onToggle={() => onToggle(key)} />
          </th>
          <td colSpan={months.length + 2}>
            {shut && (
              <span className="muted small">
                {plural(group.rows.length, {
                  one: `${group.rows.length} category folded away`,
                  other: `${group.rows.length} categories folded away`,
                })}
              </span>
            )}
          </td>
        </tr>
      )}

      {!shut &&
        group.rows.map((row) => (
          <Line
            key={row.key ?? "none"}
            row={row}
            months={months}
            currency={currency}
            tone={tone}
            onOpen={onOpen}
          />
        ))}
    </>
  );
}

function Line({
  row,
  months,
  currency,
  tone,
  onOpen,
}: {
  row: ReportRow;
  months: string[];
  currency: string;
  tone: "in" | "out";
  onOpen: (cell: Cell) => void;
}) {
  const which = { rowCategoryId: row.key, rowUncategorised: row.key === null };
  const name = categoryName(row.key, row.name);
  return (
    <tr className={row.key === null ? "report-unset" : undefined}>
      {/* The group used to be printed small beside every category name. It is
          the heading above the row now, so repeating it on each line would be
          saying the same thing twice in the narrowest column on screen. */}
      <th scope="row" className="report-label">
        {name}
      </th>
      {months.map((period) => (
        <td key={period} className="amount">
          <Figure
            minor={row.by_month[period] ?? 0}
            currency={currency}
            onOpen={onOpen}
            cell={{
              title: `${name} — ${monthLabel(period)}`,
              period,
              direction: tone,
              ...which,
            }}
          />
        </td>
      ))}
      <td className="amount">
        <Figure minor={row.average_minor} currency={currency} />
      </td>
      <td className="amount">
        <Figure
          minor={row.total_minor}
          currency={currency}
          strong
          onOpen={onOpen}
          cell={{ title: name, direction: tone, ...which }}
        />
      </td>
    </tr>
  );
}

/**
 * One figure in the grid.
 *
 * A zero is rendered muted rather than omitted. The cell has to be there --
 * the column exists whether or not this row reached it -- and a blank cell
 * reads as missing data where a grey 0.00 reads as "nothing here", which is
 * what it is.
 */
function Figure({
  minor,
  currency,
  strong,
  signed,
  onOpen,
  cell,
}: {
  minor: number;
  currency: string;
  strong?: boolean;
  /** Colour this one by its sign, and say the sign in words for a reader. */
  signed?: boolean;
  onOpen?: (cell: Cell) => void;
  cell?: Cell;
}) {
  const text = format(minor, currency);
  const lang = amountLang();

  // A zero has nothing behind it, so it is not offered as something to open.
  // Making it clickable would promise a list and then show an empty one,
  // which teaches people the feature is broken on the cells where it is
  // simply not applicable.
  if (minor === 0) return <span className="muted" lang={lang}>{text}</span>;

  const classes = [strong ? "report-strong" : "", signed ? (minor > 0 ? "net-up" : "net-down") : ""]
    .filter(Boolean)
    .join(" ");
  const body = (
    <span className={classes || undefined}>
      {lang ? <span lang={lang}>{text}</span> : text}
      {/* The word a screen reader hears, because it hears no colour. The "-"
          in front of the figure is the cue that is there for everybody else. */}
      {signed && <span className="sr-only"> {minor > 0 ? t({ message: "surplus", comment: "Screen-reader text on the Income vs Expense report: the household kept money (for screen readers)" }) : t({ message: "shortfall", comment: "Screen-reader text on the Income vs Expense report: the household spent more than came in (for screen readers)" })}</span>}
    </span>
  );
  if (!onOpen || !cell) return body;

  return (
    <button
      type="button"
      className="figure-open"
      onClick={() => onOpen(cell)}
      title={t`What went into ${cell.title}`}
    >
      {body}
    </button>
  );
}
