/**
 * Proving an account against a statement.
 *
 * The screen is built around one number: the difference. You say what the bank
 * says the closing balance was, tick the rows that appear on the statement, and
 * the difference tells you how far you still are from agreeing. At zero, and
 * only at zero, you can finish.
 *
 * That is deliberate friction. A reconciliation is a claim that the ledger and
 * the bank agree as of a date, and its whole value is that someone later can
 * trust it and stop looking further back. One that did not actually balance
 * would be worse than never having reconciled at all.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { format, parse } from "../lib/money";
import { localToday } from "../lib/time";
import {
  Empty,
  Field,
  Hint,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
} from "../components/bits";
import type { Account, Reconciliation, Worksheet } from "../lib/types";
import { formatDate } from "../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

type WorksheetSort = "date" | "payee" | "memo" | "amount";

export function Reconcile({
  account,
  onClose,
  onDone,
}: {
  account: Account;
  onClose: () => void;
  onDone: () => void;
}) {
  const [closingDate, setClosingDate] = useState(localToday());
  const [closingBalance, setClosingBalance] = useState("");
  const [ticked, setTicked] = useState<Set<string>>(new Set());

  const sheet = useQuery({
    queryKey: ["reconciliation", account.id, closingDate],
    queryFn: () =>
      api.get<Worksheet>(`/accounts/${account.id}/reconciliation?until=${closingDate}`),
  });
  const past = useQuery({
    queryKey: ["reconciliations", account.id],
    queryFn: () => api.get<Reconciliation[]>(`/accounts/${account.id}/reconciliations`),
  });

  const finish = useMutation({
    mutationFn: () =>
      api.post<Reconciliation>(`/accounts/${account.id}/reconciliation`, {
        statement_date: closingDate,
        statement_balance: stated,
        transaction_ids: [...ticked],
      }),
    onSuccess: onDone,
  });

  // Oldest first: you tick a statement from the top down, and the statement
  // itself is in date order.
  const order = useSort<WorksheetSort>("date");
  const rows = useMemo(
    () =>
      sortRows(
        sheet.data?.candidates ?? [],
        order.sort,
        order.direction,
        (row, column) => {
          switch (column) {
            case "date":
              return row.date;
            case "payee":
              return row.payee;
            case "memo":
              return row.memo;
            // By size, not by sign: on a statement page you are looking for
            // "the €48 one", and splitting the ins from the outs would bury it.
            default:
              return Math.abs(row.amount);
          }
        },
        (a, b) => a.date.localeCompare(b.date),
      ),
    [sheet.data, order.sort, order.direction],
  );
  const locked = sheet.data?.locked_balance ?? 0;
  const stated = parse(closingBalance, account.currency);

  const tickedTotal = useMemo(
    () => rows.filter((row) => ticked.has(row.id)).reduce((sum, row) => sum + row.amount, 0),
    [rows, ticked],
  );

  // The number the whole screen is about. Null until the bank's figure is
  // typed, because "how far out are you" has no answer before then.
  const difference = stated === null ? null : stated - (locked + tickedTotal);
  const balanced = difference === 0 && ticked.size > 0;

  function toggle(id: string) {
    const next = new Set(ticked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setTicked(next);
  }

  function tickEverything() {
    setTicked(ticked.size === rows.length ? new Set() : new Set(rows.map((row) => row.id)));
  }

  return (
    <Panel title={t({ message: `Reconcile ${account.name}`, comment: "Title of the panel: reconcile (verb) the named account against a statement. See GLOSSARY.md" })} onClose={onClose} wide>
      <Problem error={sheet.error ?? finish.error} />

      <p className="muted small" style={{ marginTop: 0 }}>
        {sheet.data?.last_statement_date ? (
          <Trans>
            Last proved to <strong>{formatDate(sheet.data.last_statement_date)}</strong> at{" "}
            {format(sheet.data.last_statement_balance ?? 0, account.currency)}. Everything up to
            there is locked and is not counted again.
          </Trans>
        ) : (
          <Trans>
            This account has never been reconciled, so you are starting from the beginning of its
            history.
          </Trans>
        )}
      </p>

      <div className="row">
        <Field label={t`Statement closing date`}>
          <input
            type="date"
            value={closingDate}
            onChange={(e) => {
              setClosingDate(e.target.value);
              // Rows after the new date are gone from the sheet; a tick left
              // behind would be counted in a total nobody can see.
              setTicked(new Set());
            }}
          />
        </Field>
        <Field
          label={t({ message: `Closing balance (${account.currency})`, comment: "Label of a form field on the Reconcile screen" })}
          hint={
            <Hint label={t`the closing balance`}>
              <p>
                <Trans>
                  The figure printed on the statement for the day it closes — what the bank says you
                  had, not what this app thinks.
                </Trans>
              </p>
              <p className="muted small" style={{ marginBottom: 0 }}>
                <Trans>
                  For a credit card, type what you owe as a negative.
                </Trans>
              </p>
            </Hint>
          }
        >
          <input
            value={closingBalance}
            onChange={(e) => setClosingBalance(e.target.value)}
            inputMode="decimal"
            placeholder="0.00"
            autoFocus
          />
        </Field>
      </div>

      <Difference
        difference={difference}
        balanced={balanced}
        currency={account.currency}
        locked={locked}
        ticked={tickedTotal}
        typed={closingBalance.trim() !== ""}
      />

      {rows.length === 0 ? (
        <Empty>
          <Trans>
            Nothing left to prove on or before that date. Everything up to here is already locked.
          </Trans>
        </Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input
                    type="checkbox"
                    aria-label={t`Tick every row`}
                    checked={ticked.size === rows.length && rows.length > 0}
                    onChange={tickEverything}
                    style={{ width: "auto" }}
                  />
                </th>
                <SortHeading
                  label={t({ message: "Date", comment: "Column heading on the Reconcile screen: noun. See GLOSSARY.md" })}
                  column="date"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label={t({ message: "Payee", comment: "Column heading on the Reconcile screen: noun, who was paid or who paid. See GLOSSARY.md" })}
                  column="payee"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label={t({ message: "Memo", comment: "Column heading on the Reconcile screen: noun, the free-text line of a transaction. See GLOSSARY.md" })}
                  column="memo"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label={t({ message: "Amount", comment: "Column heading on the Reconcile screen: noun, a sum of money. See GLOSSARY.md" })}
                  column="amount"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                  align="right"
                />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={ticked.has(row.id) ? "selected" : undefined}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={t({ message: `Tick ${formatDate(row.date)}`, comment: "Screen-reader name on the Reconcile screen" })}
                      checked={ticked.has(row.id)}
                      onChange={() => toggle(row.id)}
                      style={{ width: "auto" }}
                    />
                  </td>
                  <td className="mono small">{formatDate(row.date)}</td>
                  <td>{row.payee ?? <span className="muted">—</span>}</td>
                  <td className="small muted">{row.memo}</td>
                  <td className={row.amount < 0 ? "amount neg" : "amount pos"}>
                    {format(row.amount, account.currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="row" style={{ marginTop: 16 }}>
        <button
          className="primary"
          disabled={!balanced || finish.isPending}
          onClick={() => finish.mutate()}
          title={balanced ? "" : t`The difference has to be zero first`}
        >
          {plural(ticked.size, {
            one: `Finish and lock ${ticked.size} row`,
            other: `Finish and lock ${ticked.size} rows`,
          })}
        </button>
        <button onClick={onClose}><Trans comment="Button on the Reconcile screen">Not now</Trans></button>
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>
        <Trans>
          Locking is one act, so History undoes the whole reconciliation in one click if you got the
          statement wrong.
        </Trans>
      </p>

      {(past.data ?? []).length > 0 && (
        <>
          <hr className="rule" />
          <h3 className="section-title"><Trans comment="Heading on the Reconcile screen">Already proved</Trans></h3>
          <table>
            <tbody>
              {(past.data ?? []).map((one) => (
                <tr key={one.id}>
                  <td className="mono small">{formatDate(one.statement_date)}</td>
                  <td className="amount">{format(one.statement_balance, account.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Panel>
  );
}

/**
 * How far the ledger and the bank still disagree.
 *
 * Shown as its own block rather than a line of text because it is the one thing
 * on this screen you act on: every tick is aimed at moving it to zero, and it
 * has to be findable without reading.
 */
function Difference({
  difference,
  balanced,
  currency,
  locked,
  ticked,
  typed,
}: {
  difference: number | null;
  balanced: boolean;
  currency: string;
  locked: number;
  ticked: number;
  typed: boolean;
}) {
  if (difference === null) {
    return (
      <div className="difference waiting">
        <strong>
          <Trans>Type the closing balance</Trans>
        </strong>
        <span className="small muted">
          {typed ? t`That isn't an amount in ${currency}` : t`then tick the rows that are on it`}
        </span>
      </div>
    );
  }

  return (
    <div className={balanced ? "difference agreed" : "difference apart"}>
      <div>
        <strong>{balanced ? t({ message: "It balances", comment: "Text on the Reconcile screen" }) : t({ message: `${format(difference, currency)} out`, comment: "Text on the Reconcile screen" })}</strong>
        <span className="small">
          {balanced
            ? t`Every row on this statement is accounted for.`
            : difference > 0
              ? t`The bank says there is more than you have ticked — a payment in you have not entered?`
              : t`You have ticked more than the bank says — a row that is not on this statement?`}
        </span>
      </div>
      <dl className="difference-sum">
        <dt><Trans comment="Name of a fact on the Reconcile screen">Already locked</Trans></dt>
        <dd className="amount">{format(locked, currency)}</dd>
        <dt><Trans comment="Name of a fact on the Reconcile screen">Ticked here</Trans></dt>
        <dd className="amount">{format(ticked, currency)}</dd>
      </dl>
    </div>
  );
}
