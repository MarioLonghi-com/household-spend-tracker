/**
 * Reimbursements: what work owes you, what came back, what was written off.
 *
 * **Several currencies side by side, never added up** (#142). The ledger
 * never converts, so a figure adding a EUR hotel to a GBP train would be a
 * number with nothing behind it -- but work repays across currencies often
 * enough that one currency per view hid half of a trip. So the currencies are
 * checkboxes: each headline figure gets a line per ticked currency, and the
 * two lists merge, every amount in its own currency. A GBP expense repaid in
 * EUR is one claim listed once, and it still says "mixed currencies, not
 * compared" rather than inventing the rate that would compare them.
 *
 * **One request per ticked currency**, merged here. The server's answer is
 * per currency on purpose -- every figure in it is a sum that must never
 * cross one -- so asking it for "EUR and GBP" would mean teaching it to
 * return a list of the answers it already gives, one at a time.
 *
 * Read-only like every report. The one thing it does besides read is send
 * somebody to the register -- with the Work expenses filter already set, and
 * for an outstanding row with that row open -- because what you do about an
 * outstanding claim is done there.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { Dialog, Empty, Hint, Problem, SortHeading, type SortDirection } from "../../components/bits";
import { ALL_DATES, DateRange, type Range } from "../../components/DateRange";
import { ReceiptMark } from "../../components/Receipts";
import { format } from "../../lib/money";
import { ageInDays } from "../../lib/reimbursement";
import { fixed, moneyKey, sortRows } from "../../lib/sorting";
import { useSticky } from "../../lib/sticky";
import { localToday } from "../../lib/time";
import type {
  Household,
  ReimbursementClaim,
  ReimbursementClaimExpense,
  ReimbursementMonth,
  ReimbursementOutstanding,
  ReimbursementsReport,
  ReimbursementView,
} from "../../lib/types";
import type { RegisterPreset } from "../Register";
import { HeadSlot } from "./IncomeExpense";
import { formatDate, listText, monthLabel, orText } from "../../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

/** "1 expense", "3 expenses". */
function expenses(count: number): string {
  return plural(count, { one: `${count} expense`, other: `${count} expenses` });
}

/** "3 days": how long something has waited. */
function days(count: number): string {
  return plural(count, { one: `${count} day`, other: `${count} days` });
}

/** "EUR", "EUR or GBP", "EUR, GBP or USD" -- for "nothing outstanding in …". */
function either(codes: string[]): string {
  return orText(codes);
}

/**
 * Which currencies the checkboxes offer.
 *
 * The report's own list, busiest first, which only holds currencies with a
 * flagged row or a payment in them -- so a currency with no work money is not
 * a box that ticks onto nothing. The ones on screen are kept even when the
 * list does not name them, so the boxes always say what the figures are in.
 */
export function checkOptions(available: string[], shown: string[]): string[] {
  return [...available, ...shown.filter((code) => !available.includes(code))];
}

/**
 * The currencies to ask for, from what was ticked and what has work money.
 *
 * `ticked` is null until somebody ticks something: then it is the busiest
 * currency with work money in it. Until the first answer says which those
 * are, the household's busiest currency (`guess`) stands in. A ticked
 * currency that no longer has work money is dropped without a word -- a box
 * for it would open onto nothing -- and when that drops all of them, the
 * busiest one takes over, as on a first visit. Busiest first, always, so the
 * figures' lines keep one order whatever order the boxes were ticked in.
 */
export function chooseCurrencies(
  ticked: string[] | null,
  available: string[] | undefined,
  guess: string,
): string[] {
  if (!available) return ticked && ticked.length > 0 ? ticked : [guess];
  const kept = available.filter((code) => ticked?.includes(code));
  if (kept.length > 0) return kept;
  return available.length > 0 ? [available[0]] : [guess];
}

/**
 * What this screen remembers, per browser and household (#142).
 *
 * The ticked currencies, the dates and each table's order -- the things a
 * person set and would have to set again. Every one is read through a check
 * of its own, because `localStorage` holds whatever an older version, or a
 * hand in the console, left there, and a value of the wrong shape must read
 * as the default rather than as a request for currency "undefined".
 */
const STICKY = "reimbursements";

const CODE = /^[A-Z]{3}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Null (nothing ticked yet) or a list of ISO codes. */
export function isTicked(raw: unknown): raw is string[] | null {
  return (
    raw === null ||
    (Array.isArray(raw) && raw.every((one) => typeof one === "string" && CODE.test(one)))
  );
}

/** A `Range`: each end a `YYYY-MM-DD` or null for open. */
export function isRange(raw: unknown): raw is Range {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return false;
  const { since, until } = raw as Record<string, unknown>;
  const end = (value: unknown) => value === null || (typeof value === "string" && DAY.test(value));
  return end(since) && end(until);
}

type Order<K extends string> = { sort: K; direction: SortDirection };

/** An order naming one of `columns` and a direction. */
export function isOrder<K extends string>(columns: readonly K[]) {
  return (raw: unknown): raw is Order<K> => {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return false;
    const { sort, direction } = raw as Record<string, unknown>;
    return (
      typeof sort === "string" &&
      (columns as readonly string[]).includes(sort) &&
      (direction === "asc" || direction === "desc")
    );
  };
}

/**
 * `useSort`, remembered under `<table>.order`.
 *
 * The column and its direction are one value, because they are one choice:
 * stored apart, a damaged direction would turn a remembered column round.
 * The shared `useSort` is left as it is -- every other list still forgets.
 */
function useStickySort<K extends string>(
  household: string,
  table: string,
  columns: readonly K[],
  initial: K,
  initialDirection: SortDirection = "asc",
) {
  const [order, setOrder] = useSticky<Order<K>>(
    STICKY,
    household,
    `${table}.order`,
    { sort: initial, direction: initialDirection },
    isOrder(columns),
  );
  const onSort = useCallback(
    (column: K, direction: SortDirection) => setOrder({ sort: column, direction }),
    [setOrder],
  );
  return { sort: order.sort, direction: order.direction, onSort };
}

export function Reimbursements({
  household,
  onOpenRegister,
}: {
  household: Household;
  /** Go to the register with a Work expenses view and, maybe, a row open. */
  onOpenRegister?: (preset: RegisterPreset) => void;
}) {
  //: Null until somebody ticks: the busiest currency with work money in it
  //: is chosen for them, and re-chosen if the first guess had none. What
  //: was ticked last time comes back, less any currency whose work money
  //: has since gone -- `chooseCurrencies` drops it without a word.
  const [ticked, setTicked] = useSticky<string[] | null>(
    STICKY,
    household.id,
    "currencies",
    null,
    isTicked,
  );
  const [range, setRange] = useSticky<Range>(STICKY, household.id, "range", ALL_DATES, isRange);

  // The first request has to name a currency before the report can say which
  // ones have anything in them, so it asks the household's own list -- the
  // same cached read the register and Income v Expense make.
  const currencies = useQuery({
    queryKey: ["report-currencies", household.id],
    queryFn: () =>
      api.get<{ currencies: string[] }>(`/households/${household.id}/reports/currencies`),
  });
  const guess = currencies.data?.currencies[0] ?? household.base_currency;

  // Which currencies have work money, as the last answer said. Held apart
  // from the answers because it decides which answers to ask for: read off
  // the current ones, ticking a currency not yet fetched would blank the
  // list while it loads and the choice would snap back. Tagged with the
  // household, so a switch never offers the other household's currencies.
  const [known, setKnown] = useState<{ household: string; list: string[] } | null>(null);
  const available = known?.household === household.id ? known.list : undefined;
  const shown = chooseCurrencies(ticked, available, guess);

  const rangeQuery = useMemo(() => {
    const params = new URLSearchParams();
    if (range.since) params.set("since", range.since);
    if (range.until) params.set("until", range.until);
    return params.toString();
  }, [range]);

  const answers = useQueries({
    queries: shown.map((code) => {
      const query = new URLSearchParams(rangeQuery);
      query.set("currency", code);
      const text = query.toString();
      return {
        queryKey: ["reimbursements", household.id, text],
        queryFn: () =>
          api.get<ReimbursementsReport>(`/households/${household.id}/reports/reimbursements?${text}`),
        enabled: !currencies.isLoading,
      };
    }),
  });

  const heard = answers.find((one) => one.data)?.data?.available_currencies;
  useEffect(() => {
    if (!heard) return;
    setKnown((before) =>
      before?.household === household.id && before.list.join() === heard.join()
        ? before
        : { household: household.id, list: heard },
    );
  }, [heard, household.id]);

  const flip = (code: string) => {
    const next = shown.includes(code) ? shown.filter((one) => one !== code) : [...shown, code];
    // The last box stays ticked: a report of no currency is a blank page
    // that looks like one with nothing owed.
    if (next.length === 0) return;
    setTicked(checkOptions(available ?? [], next).filter((one) => next.includes(one)));
  };

  const loading = currencies.isLoading || answers.some((one) => one.isLoading);
  const error = currencies.error ?? answers.find((one) => one.error)?.error ?? null;
  const reports = answers.every((one) => one.data)
    ? answers.map((one) => one.data as ReimbursementsReport)
    : null;
  const nothingFlagged = reports !== null && reports[0]?.available_currencies.length === 0;

  return (
    <>
      <HeadSlot>
        <CurrencyChecks
          ticked={shown}
          options={checkOptions(available ?? [], shown)}
          onFlip={flip}
        />
      </HeadSlot>

      <div className="report-bar">
        <div className="report-choices">
          {/* Where you act on what this shows. The filter is the owed view,
              which is the list a person works through. */}
          <button type="button" onClick={() => onOpenRegister?.({ reimbursement: "owed" })}>
            <Trans>
              Show in Transactions
            </Trans>
          </button>
        </div>
        <div className="report-filters">
          <DateRange value={range} onChange={setRange} />
        </div>
      </div>
      {range.since || range.until ? (
        <p className="muted small" style={{ marginTop: 0 }}>
          <Trans>
            Dates narrow by when the expense happened, not when it was repaid.
          </Trans>
        </p>
      ) : null}

      <Problem error={error} />

      {loading ? (
        <div className="muted"><Trans comment="Text on the Reimbursements report">Loading…</Trans></div>
      ) : !reports ? null : nothingFlagged ? (
        <Empty>
          <Trans>
            Nothing is marked as a work expense yet. Open a transaction in the register and set
            Reimbursement to “Work should pay this back” — or tick several and use Work expense in
            the bar that appears.
          </Trans>
        </Empty>
      ) : (
        <ReimbursementsBody
          household={household.id}
          reports={reports}
          baseCurrency={household.base_currency}
          onOpen={(id, view = "owed") => onOpenRegister?.({ reimbursement: view, open: id })}
        />
      )}
    </>
  );
}

/**
 * A checkbox per currency with work money, as many ticked as you like.
 *
 * Its own control rather than Income v Expense's `CurrencyToggle`, which is
 * radio buttons on purpose: that report's columns *are* one currency, and a
 * second tick there would be a sum. Here nothing is summed -- each figure
 * grows a line per currency instead -- so the control says "several" in the
 * one way a screen reader and a pointer both understand.
 */
function CurrencyChecks({
  ticked,
  options,
  onFlip,
}: {
  ticked: string[];
  options: string[];
  onFlip: (code: string) => void;
}) {
  return (
    <div className="currency-toggle reimb-currencies">
      <span className="daterange-label">
        <Trans comment="Text on the Reimbursements report: noun. See GLOSSARY.md">Currency</Trans>
      </span>
      <div role="group" aria-label={t({ message: "Currency", comment: "Screen-reader name on the Reimbursements report: noun. See GLOSSARY.md" })} className="daterange-presets">
        {options.map((code) => {
          const on = ticked.includes(code);
          const last = on && ticked.length === 1;
          return (
            <label
              key={code}
              className={on ? "chip active" : "chip"}
              title={last ? t`At least one currency stays ticked` : undefined}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={last}
                onChange={() => onFlip(code)}
              />
              {code}
            </label>
          );
        })}
      </div>
      {options.length > 1 && (
        <Hint label={t`why the currencies are never added up`}>
          <p>
            <Trans>
              This ledger never converts. Currency lives on the account, and there is no exchange
              rate stored anywhere — so a total mixing {listText(options)} would be a number with
              nothing behind it.
            </Trans>
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            <Trans>
              Tick several and each figure gets a line per currency; the lists show every amount
              in its own. Nothing on this screen ever adds two together.
            </Trans>
          </p>
        </Hint>
      )}
    </div>
  );
}

/** An outstanding row with the currency its report was in: the row does not say. */
type OwedRow = ReimbursementOutstanding & { currency: string };
/** A month's figures with the currency they are in. */
type MonthRow = ReimbursementMonth & { currency: string };

/**
 * The payments across every ticked currency, each once.
 *
 * A GBP hotel repaid in EUR is in both answers -- the server lists a claim
 * under every currency it touches -- and ticking both must not list it
 * twice. Newest payment first, as the server orders each answer.
 */
export function mergeClaims(reports: ReimbursementsReport[]): ReimbursementClaim[] {
  const seen = new Set<string>();
  const merged: ReimbursementClaim[] = [];
  for (const report of reports)
    for (const claim of report.claims) {
      if (seen.has(claim.settlement.id)) continue;
      seen.add(claim.settlement.id);
      merged.push(claim);
    }
  return merged.sort(
    (a, b) =>
      b.settlement.date.localeCompare(a.settlement.date) ||
      a.settlement.id.localeCompare(b.settlement.id),
  );
}

/**
 * Everything below the controls, for the reports already in hand -- one per
 * ticked currency, busiest first.
 *
 * Exported so the test can hand it a fixture without a server, the way the
 * Income v Expense test renders `ReportTable` directly.
 */
export function ReimbursementsBody({
  household,
  reports,
  onOpen,
  baseCurrency = reports[0]?.currency ?? "",
  asOf = localToday(),
}: {
  /** Whose remembered table orders to read and write. */
  household: string;
  reports: ReimbursementsReport[];
  /**
   * To the register with this row open, under the Work expenses view that
   * lists it: `owed` for an expense still waiting, `paid` for a payment or
   * an expense it repaid.
   */
  onOpen: (transactionId: string, view?: ReimbursementView) => void;
  /** Whose money groups first when a column sorts across currencies. */
  baseCurrency?: string;
  /** Today, unless a test says otherwise: "waiting" is counted to it. */
  asOf?: string;
}) {
  const codes = reports.map((one) => one.currency);
  const owed = useMemo<OwedRow[]>(
    () =>
      reports.flatMap((one) => one.outstanding_rows.map((row) => ({ ...row, currency: one.currency }))),
    [reports],
  );
  const claims = useMemo(() => mergeClaims(reports), [reports]);
  const months = useMemo<MonthRow[]>(
    () => reports.flatMap((one) => one.months.map((month) => ({ ...month, currency: one.currency }))),
    [reports],
  );
  //: The row whose details are open, if any (#142).
  const [picked, setPicked] = useState<Picked | null>(null);
  const line = (report: ReimbursementsReport, minor: number, note: string, owes = false) => ({
    currency: report.currency,
    value: format(minor, report.currency),
    note,
    owes,
  });

  return (
    <>
      <div className="reimb-figures">
        <Figure
          label={t`Work owes you`}
          lines={reports.map((one) => {
            const oldest =
              one.oldest_outstanding !== null ? ageInDays(one.oldest_outstanding, asOf) : null;
            return line(
              one,
              one.outstanding,
              one.outstanding_count === 0
                ? t({ message: "Nothing owed", comment: "Text on the Reimbursements report" })
                : `${expenses(one.outstanding_count)}${
                    oldest !== null ? ` · ${t({ message: `oldest ${days(oldest)}`, comment: "Text on the Reimbursements report" })}` : ""
                  }`,
              one.outstanding > 0,
            );
          })}
        />
        <Figure
          label={t({ message: "Recovered", comment: "Name of a figure on the Reimbursements report: paid back by work" })}
          lines={reports.map((one) =>
            line(
              one,
              one.recovered,
              plural(one.recovered_count, {
                one: `${one.recovered_count} expense repaid`,
                other: `${one.recovered_count} expenses repaid`,
              }),
            ),
          )}
        />
        <Figure
          label={t({ message: "Written off", comment: "Name of a figure on the Reimbursements report: a work expense work will not pay; counted as your own spending. See GL…" })}
          lines={reports.map((one) =>
            line(
              one,
              one.written_off,
              one.written_off_count === 0
                ? t({ message: "None", context: "written off", comment: "Text on the Reimbursements report (written off)" })
                : plural(one.written_off_count, {
                    one: `${one.written_off_count} expense, counted as your spending`,
                    other: `${one.written_off_count} expenses, counted as your spending`,
                  }),
            ),
          )}
        />
        <Figure
          label={t`Paid, not matched`}
          lines={reports.map((one) =>
            line(
              one,
              one.unmatched,
              one.unmatched > 0
                ? t`An advance not spent yet, or an overpayment`
                : t`Every payment is accounted for`,
            ),
          )}
        />
      </div>

      <Outstanding
        household={household}
        rows={owed}
        codes={codes}
        base={baseCurrency}
        asOf={asOf}
        onOpen={onOpen}
        onPick={setPicked}
      />
      <Payments household={household} claims={claims} base={baseCurrency} onPick={setPicked} />
      <ByMonth household={household} months={months} codes={codes} base={baseCurrency} />

      <p className="muted small">
        <Trans>
          Each currency keeps its own figures and nothing here adds two together — the ledger
          stores no exchange rates. A claim repaid in another currency is listed once, with every
          amount in its own currency and no difference worked out between them.
        </Trans>
      </p>

      {picked ? (
        <Details
          picked={picked}
          asOf={asOf}
          onOpen={onOpen}
          onClose={() => setPicked(null)}
        />
      ) : null}
    </>
  );
}

/** Which row's details are open, with what the dialog needs to describe it. */
type Picked =
  | { kind: "owed"; row: OwedRow }
  | { kind: "payment"; claim: ReimbursementClaim }
  | { kind: "repaid"; expense: ReimbursementClaimExpense; claim: ReimbursementClaim };

/**
 * What a row needs to open its details: a click anywhere but on a control in
 * it, and Enter or Space while the row itself has focus.
 *
 * The row is a Tab stop of its own so the dialog is not a pointer-only
 * feature. A click that lands on a button or link inside the row is that
 * control's -- the Outstanding date goes to the register, and opening a
 * dialog over it as well would be two answers to one click. The key check is
 * on the row itself for the same reason: Enter on the date button bubbles up
 * here, and it already means "go".
 */
function pickable(open: () => void) {
  return {
    tabIndex: 0,
    onClick: (event: MouseEvent<HTMLTableRowElement>) => {
      const control = (event.target as HTMLElement).closest(
        "button, a, input, select, textarea, label",
      );
      if (control && event.currentTarget.contains(control)) return;
      open();
    },
    onKeyDown: (event: KeyboardEvent<HTMLTableRowElement>) => {
      if (event.target !== event.currentTarget) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      open();
    },
  };
}

/**
 * One row's details, in the Income v Expense report's dialog (#142).
 *
 * Everything in it came with the report -- memo and category included -- so
 * it opens at once, and what it says is what the table beside it says. What
 * it adds is the link: which payment repaid this expense, or which expenses
 * this payment repaid, which the table can only show by position.
 */
function Details({
  picked,
  asOf,
  onOpen,
  onClose,
}: {
  picked: Picked;
  asOf: string;
  onOpen: (transactionId: string, view?: ReimbursementView) => void;
  onClose: () => void;
}) {
  const describe = (one: { date: string; payee_name: string | null }, amount: string) =>
    `${formatDate(one.date)} · ${one.payee_name ?? t({ message: "no payee", comment: "Label on the Reimbursements report" })} · ${amount}`;

  let row: {
    id: string;
    date: string;
    account_name: string;
    payee_name: string | null;
    memo: string | null;
    category_name: string | null;
  };
  let amountLabel: string;
  let amount: string;
  let state: string;
  let link: ReactNode;
  let view: ReimbursementView;
  if (picked.kind === "owed") {
    const waited = ageInDays(picked.row.date, asOf);
    row = picked.row;
    amountLabel = t({ message: "Spent", comment: "Label on the Reimbursements report: money that went out" });
    amount = format(picked.row.amount, picked.row.currency);
    state = plural(waited, {
      one: `Work should pay this back — not repaid yet, ${waited} day waiting`,
      other: `Work should pay this back — not repaid yet, ${waited} days waiting`,
    });
    link = (
      <span className="muted">
        <Trans>No payment linked yet</Trans>
      </span>
    );
    view = "owed";
  } else if (picked.kind === "repaid") {
    const pay = picked.claim.settlement;
    row = picked.expense;
    amountLabel = t({ message: "Spent", comment: "Label on the Reimbursements report: money that went out" });
    amount = format(picked.expense.amount, picked.expense.currency);
    state = t`Work should pay this back — repaid`;
    const others = picked.claim.expenses.length - 1;
    const payment = describe(pay, format(pay.amount, pay.currency));
    link = (
      <>
        {t({ message: `Repaid by ${payment}`, comment: "Label on the Reimbursements report" })}
        {others > 0 ? (
          <span className="muted">
            {" "}
            {plural(others, {
              one: `with ${others} other expense`,
              other: `with ${others} other expenses`,
            })}
          </span>
        ) : null}
      </>
    );
    view = "paid";
  } else {
    const claim = picked.claim;
    row = claim.settlement;
    amountLabel = t({ message: "Received", comment: "Label on the Reimbursements report: money that came in" });
    amount = format(claim.settlement.amount, claim.settlement.currency);
    state = t`A payment from work`;
    link = (
      <>
        <ul className="reimb-detail-list">
          {claim.expenses.map((one) => (
            <li key={one.id}>{describe(one, format(one.amount, one.currency))}</li>
          ))}
        </ul>
        <span className="small">
          <Trans comment="Text on the Reimbursements report">
            Difference: <DifferenceText claim={claim} />
          </Trans>
        </span>
      </>
    );
    view = "paid";
  }

  const blank = <span className="muted">—</span>;
  return (
    <Dialog title={`${row.payee_name ?? t({ message: "No payee", comment: "Text in a dialog on the Reimbursements report" })} · ${formatDate(row.date)}`} onClose={onClose}>
      <dl className="reimb-detail">
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun. See GLOSSARY.md">Date</Trans></dt>
        <dd className="mono">{formatDate(row.date)}</dd>
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun, a bank or cash account. See GLOSSARY.md">Account</Trans></dt>
        <dd>{row.account_name}</dd>
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun, who was paid or who paid. See GLOSSARY.md">Payee</Trans></dt>
        <dd>{row.payee_name ?? blank}</dd>
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun, the free-text line of a transaction. See GLOSSARY.md">Memo</Trans></dt>
        <dd>{row.memo ?? blank}</dd>
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun, what a transaction was for. See GLOSSARY.md">Category</Trans></dt>
        <dd>{row.category_name ?? blank}</dd>
        <dt>{amountLabel}</dt>
        <dd className="amount">{amount}</dd>
        <dt><Trans comment="Name of a fact on the Reimbursements report: noun, being paid back by work">Reimbursement</Trans></dt>
        <dd>{state}</dd>
        <dt>{picked.kind === "payment" ? t({ message: "Repaid", comment: "Name of a fact on the Reimbursements report: paid back by work" }) : t({ message: "Linked to", comment: "Name of a fact on the Reimbursements report" })}</dt>
        <dd>{link}</dd>
      </dl>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          onClick={() => {
            onClose();
            onOpen(row.id, view);
          }}
        >
          <Trans>
            Open in Transactions
          </Trans>
        </button>
      </div>
    </Dialog>
  );
}

/**
 * One headline figure: a line per ticked currency, never a total.
 *
 * Each line carries its own sentence, because "2 expenses · oldest 7 days"
 * is about the euros and not the pounds. The owed colour goes on the lines
 * that owe something, so a zero in the second currency does not wear the
 * warning the first one earned.
 */
function Figure({
  label,
  lines,
}: {
  label: string;
  lines: { currency: string; value: string; note: string; owes?: boolean }[];
}) {
  const owes = lines.some((one) => one.owes);
  return (
    <div className={owes ? "reimb-figure owed" : "reimb-figure"}>
      <span className="reimb-figure-label">{label}</span>
      {lines.map((one) => (
        <div key={one.currency} className={one.owes ? "reimb-figure-line owed" : "reimb-figure-line"}>
          <strong className="amount">{one.value}</strong>
          <span className="small muted">{one.note}</span>
        </div>
      ))}
    </div>
  );
}

/** `amount` rather than "owed" so the heading opens largest-first, as every
 *  money column does (`SortHeading` keys its natural direction on the name). */
const OUTSTANDING_COLUMNS = ["date", "payee", "account", "memo", "amount", "waiting"] as const;
type OutstandingColumn = (typeof OUTSTANDING_COLUMNS)[number];

/** The worklist: every expense still owed, oldest first until a heading says otherwise. */
function Outstanding({
  household,
  rows: unsorted,
  codes,
  base,
  asOf,
  onOpen,
  onPick,
}: {
  household: string;
  rows: OwedRow[];
  /** The ticked currencies, for saying what "nothing" was about. */
  codes: string[];
  base: string;
  asOf: string;
  onOpen: (transactionId: string, view?: ReimbursementView) => void;
  onPick: (picked: Picked) => void;
}) {
  const order = useStickySort(household, "outstanding", OUTSTANDING_COLUMNS, "date", "asc");
  const rows = useMemo(
    () =>
      sortRows(
        unsorted,
        order.sort,
        order.direction,
        (row, column) => {
          if (column === "payee") return row.payee_name;
          if (column === "account") return row.account_name;
          if (column === "memo") return row.memo;
          if (column === "amount") return moneyKey(row.currency, row.amount, base);
          if (column === "waiting") return ageInDays(row.date, asOf);
          return row.date;
        },
        (a, b) => a.id.localeCompare(b.id),
      ),
    [unsorted, base, order.sort, order.direction, asOf],
  );
  const heading = (label: string, column: OutstandingColumn, right = false) => (
    <SortHeading
      label={label}
      column={column}
      sort={order.sort}
      direction={order.direction}
      onSort={order.onSort}
      align={right ? "right" : undefined}
    />
  );

  return (
    <section className="reimb-section" aria-labelledby="reimb-outstanding">
      <h2 id="reimb-outstanding" className="section-title">
        <Trans comment="Heading on the Reimbursements report: still owed">Outstanding</Trans>{" "}
        <span className="muted small">
          <Trans>
            Click one for its details, or its date to find its payment or write it off.
          </Trans>
        </span>
      </h2>
      {rows.length === 0 ? (
        <Empty>{t`Nothing outstanding in ${either(codes)}.`}</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {heading(t({ message: "Date", comment: "Column heading on the Reimbursements report: noun. See GLOSSARY.md" }), "date")}
                {heading(t({ message: "Payee", comment: "Column heading on the Reimbursements report: noun, who was paid or who paid. See GLOSSARY.md" }), "payee")}
                {heading(t({ message: "Account", comment: "Column heading on the Reimbursements report: noun, a bank or cash account. See GLOSSARY.md" }), "account")}
                {heading(t({ message: "Memo", comment: "Column heading on the Reimbursements report: noun, the free-text line of a transaction. See GLOSSARY.md" }), "memo")}
                {heading(t({ message: "Owed", comment: "Column heading on the Reimbursements report: adjective, still to be paid back by work" }), "amount", true)}
                {heading(t({ message: "Waiting", comment: "Column heading on the Reimbursements report: how long it has waited, or a queue state" }), "waiting", true)}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const waited = ageInDays(row.date, asOf);
                return (
                  <tr
                    key={row.id}
                    className="row-pick"
                    {...pickable(() => onPick({ kind: "owed", row }))}
                  >
                    <td data-label={t({ message: "Date", comment: "Column name shown beside a value on phones on the Reimbursements report: noun. See GLOSSARY.md" })} data-detail-first="true">
                      <button
                        className="link"
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpen(row.id);
                        }}
                        title={t`Open it in Transactions`}
                      >
                        {formatDate(row.date)}
                      </button>
                    </td>
                    <td data-primary="true">
                      {row.payee_name ?? <span className="muted">—</span>}
                      {row.has_receipt ? <ReceiptMark /> : null}
                    </td>
                    <td className="small muted" data-label={t({ message: "Account", comment: "Column name shown beside a value on phones on the Reimbursements report: noun, a bank or cash account. See GLOSSARY.md" })}>
                      {row.account_name}
                    </td>
                    <td className="small reimb-memo" data-label={t({ message: "Memo", comment: "Column name shown beside a value on phones on the Reimbursements report: noun, the free-text line of a transaction. S…" })}>
                      {row.memo ?? ""}
                    </td>
                    <td className="amount" data-figure="true">
                      {format(row.amount, row.currency)}
                    </td>
                    <td
                      className={waited > 30 ? "amount work-waiting" : "amount"}
                      data-label={t({ message: "Waiting", comment: "Column name shown beside a value on phones on the Reimbursements report: how long it has waited, or a queue state" })}
                    >
                      {days(waited)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** `server` is the order the answers came in -- newest payment first -- with no arrow lit. */
const PAYMENT_COLUMNS = ["server", "date", "payee", "account", "memo", "amount", "difference"] as const;
type PaymentColumn = Exclude<(typeof PAYMENT_COLUMNS)[number], "server">;

/** The difference cell: a figure, a figure that wants looking at, or the reason there is none. */
export function DifferenceText({ claim }: { claim: ReimbursementClaim }) {
  const own = claim.settlement.currency;
  if (claim.difference === null)
    return (
      <span className="work-waiting">
        <Trans>mixed currencies, not compared</Trans>
      </span>
    );
  if (claim.difference === 0) return <>{format(0, own)}</>;
  return <span className="work-waiting">{format(claim.difference, own)}</span>;
}

/**
 * One row per payment, and under each the expenses it repaid.
 *
 * Sorted at its headings like every list; the expenses travel with their
 * payment, so Memo sorts by the payment's memo and each expense keeps its own
 * beside it. "Received" is money in the payment's own currency, whichever
 * currencies are ticked, so it sorts by currency first, then figure.
 */
function Payments({
  household,
  claims: unsorted,
  base,
  onPick,
}: {
  household: string;
  claims: ReimbursementClaim[];
  base: string;
  onPick: (picked: Picked) => void;
}) {
  const order = useStickySort(household, "payments", PAYMENT_COLUMNS, "server");
  const claims = useMemo(
    () =>
      sortRows(unsorted, order.sort, order.direction, (claim, column) => {
        const pay = claim.settlement;
        if (column === "date") return pay.date;
        if (column === "payee") return pay.payee_name;
        if (column === "account") return pay.account_name;
        if (column === "memo") return pay.memo;
        if (column === "amount") return moneyKey(pay.currency, pay.amount, base);
        if (column === "difference")
          return claim.difference === null
            ? null
            : moneyKey(pay.currency, claim.difference, base);
        return null;
      }),
    [unsorted, base, order.sort, order.direction],
  );
  const heading = (label: string, column: PaymentColumn, right = false) => (
    <SortHeading
      label={label}
      column={column}
      sort={order.sort}
      direction={order.direction}
      onSort={order.onSort}
      align={right ? "right" : undefined}
    />
  );

  return (
    <section className="reimb-section" aria-labelledby="reimb-payments">
      <h2 id="reimb-payments" className="section-title">
        <Trans>Payments from work</Trans>{" "}
        <span className="muted small">
          <Trans>Each one with the expenses linked to it.</Trans>
        </span>
      </h2>
      {claims.length === 0 ? (
        <Empty><Trans>No payments linked yet.</Trans></Empty>
      ) : (
        <div className="matrix-scroll">
          <table className="reimb-claims">
            <thead>
              <tr>
                {heading(t({ message: "Paid on", comment: "Column heading on the Reimbursements report" }), "date")}
                {heading(t({ message: "From", comment: "Column heading on the Reimbursements report: the start of a range, or where money comes from" }), "payee")}
                {heading(t({ message: "Into", comment: "Column heading on the Reimbursements report: where it goes, or what it is merged into" }), "account")}
                {heading(t({ message: "Memo", comment: "Column heading on the Reimbursements report: noun, the free-text line of a transaction. See GLOSSARY.md" }), "memo")}
                {heading(t({ message: "Received", comment: "Column heading on the Reimbursements report: money that came in" }), "amount", true)}
                <th className="amount"><Trans comment="Column heading on the Reimbursements report: how much of the payment the expenses account for">Covered</Trans></th>
                {heading(t({ message: "Difference", comment: "Column heading on the Reimbursements report: noun, what is left after subtracting" }), "difference", true)}
              </tr>
            </thead>
            <tbody>
              {claims.map((claim) => (
                <ClaimRows key={claim.settlement.id} claim={claim} onPick={onPick} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ClaimRows({
  claim,
  onPick,
}: {
  claim: ReimbursementClaim;
  onPick: (picked: Picked) => void;
}) {
  const pay = claim.settlement;
  return (
    <>
      <tr className="reimb-claim row-pick" {...pickable(() => onPick({ kind: "payment", claim }))}>
        <td className="mono">{formatDate(pay.date)}</td>
        <td>{pay.payee_name ?? <span className="muted">—</span>}</td>
        <td className="small muted">{pay.account_name}</td>
        <td className="small reimb-memo">{pay.memo ?? ""}</td>
        <td className="amount pos">{format(pay.amount, pay.currency)}</td>
        <td className="amount">
          {claim.covered === null
            ? claim.expenses.map((one) => format(one.amount, one.currency)).join(" + ")
            : format(claim.covered, pay.currency)}
        </td>
        <td className="amount">
          <DifferenceText claim={claim} />
        </td>
      </tr>
      {claim.expenses.map((one) => (
        <tr
          key={one.id}
          className="reimb-claim-expense row-pick"
          {...pickable(() => onPick({ kind: "repaid", expense: one, claim }))}
        >
          <td colSpan={3}>
            <span className="work-child" aria-hidden="true">
              ↳
            </span>
            {formatDate(one.date)} · {one.payee_name ?? "—"} · {one.account_name}
          </td>
          <td className="small reimb-memo">{one.memo ?? ""}</td>
          <td />
          <td className="amount">{format(one.amount, one.currency)}</td>
          <td />
        </tr>
      ))}
    </>
  );
}

const MONTH_COLUMNS = ["month", "flagged", "recovered", "written_off", "outstanding"] as const;
type MonthColumn = (typeof MONTH_COLUMNS)[number];

/**
 * By the month the expense happened, so a claim repaid late stays in its own month.
 *
 * A row per month *and currency*: September in euros and September in pounds
 * are two rows, never one. A month sorts with its currencies held together in
 * one order; a money column sorts by currency first, then figure.
 */
function ByMonth({
  household,
  months: unsorted,
  codes,
  base,
}: {
  household: string;
  months: MonthRow[];
  codes: string[];
  base: string;
}) {
  const order = useStickySort(household, "months", MONTH_COLUMNS, "month", "asc");
  const months = useMemo(
    () =>
      sortRows(
        unsorted,
        order.sort,
        order.direction,
        (month, column) =>
          column === "month"
            ? [month.month, fixed(codes.indexOf(month.currency))]
            : moneyKey(month.currency, month[column], base),
      ),
    [unsorted, codes, base, order.sort, order.direction],
  );
  const several = codes.length > 1;
  const heading = (label: string, column: MonthColumn, right = true) => (
    <SortHeading
      label={label}
      column={column}
      sort={order.sort}
      direction={order.direction}
      onSort={order.onSort}
      align={right ? "right" : undefined}
    />
  );
  const cell = (minor: number, currency: string, warn = false) =>
    minor === 0 ? (
      <span className="muted">—</span>
    ) : warn ? (
      <span className="work-waiting">{format(minor, currency)}</span>
    ) : (
      format(minor, currency)
    );

  return (
    <section className="reimb-section" aria-labelledby="reimb-months">
      <h2 id="reimb-months" className="section-title">
        <Trans comment="Heading on the Reimbursements report">By month</Trans>{" "}
        <span className="muted small">
          <Trans>By when the expense happened.</Trans>
        </span>
      </h2>
      {months.length === 0 ? (
        <Empty>{t`No work expenses in ${either(codes)} yet.`}</Empty>
      ) : (
        <div className="matrix-scroll">
          <table>
            <thead>
              <tr>
                {heading(t({ message: "Month", comment: "Column heading on the Reimbursements report: noun, a calendar month" }), "month", false)}
                {heading(t({ message: "Flagged", comment: "Column heading on the Reimbursements report: marked as a work expense" }), "flagged")}
                {heading(t({ message: "Recovered", comment: "Column heading on the Reimbursements report: paid back by work" }), "recovered")}
                {heading(t({ message: "Written off", comment: "Column heading on the Reimbursements report: a work expense work will not pay; counted as your own spending. See GLOS…" }), "written_off")}
                {heading(t({ message: "Still owed", comment: "Column heading on the Reimbursements report: still to be paid back by work" }), "outstanding")}
              </tr>
            </thead>
            <tbody>
              {months.map((month) => (
                <tr key={`${month.month} ${month.currency}`}>
                  <td>
                    {monthLabel(month.month)}
                    {several ? <span className="muted small"> · {month.currency}</span> : null}
                  </td>
                  <td className="amount">{cell(month.flagged, month.currency)}</td>
                  <td className="amount">{cell(month.recovered, month.currency)}</td>
                  <td className="amount">{cell(month.written_off, month.currency)}</td>
                  <td className="amount">{cell(month.outstanding, month.currency, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
