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
    <Panel title={`Reconcile ${account.name}`} onClose={onClose} wide>
      <Problem error={sheet.error ?? finish.error} />

      <p className="muted small" style={{ marginTop: 0 }}>
        {sheet.data?.last_statement_date ? (
          <>
            Last proved to <strong>{sheet.data.last_statement_date}</strong> at{" "}
            {format(sheet.data.last_statement_balance ?? 0, account.currency)}. Everything up to
            there is locked and is not counted again.
          </>
        ) : (
          <>
            This account has never been reconciled, so you are starting from the beginning of its
            history.
          </>
        )}
      </p>

      <div className="row">
        <Field label="Statement closing date">
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
          label={`Closing balance (${account.currency})`}
          hint={
            <Hint label="the closing balance">
              <p>
                The figure printed on the statement for the day it closes — what the bank says you
                had, not what this app thinks.
              </p>
              <p className="muted small" style={{ marginBottom: 0 }}>
                For a credit card, type what you owe as a negative.
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
          Nothing left to prove on or before that date. Everything up to here is already locked.
        </Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input
                    type="checkbox"
                    aria-label="Tick every row"
                    checked={ticked.size === rows.length && rows.length > 0}
                    onChange={tickEverything}
                    style={{ width: "auto" }}
                  />
                </th>
                <SortHeading
                  label="Date"
                  column="date"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Payee"
                  column="payee"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Memo"
                  column="memo"
                  sort={order.sort}
                  direction={order.direction}
                  onSort={order.onSort}
                />
                <SortHeading
                  label="Amount"
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
                      aria-label={`Tick ${row.date}`}
                      checked={ticked.has(row.id)}
                      onChange={() => toggle(row.id)}
                      style={{ width: "auto" }}
                    />
                  </td>
                  <td className="mono small">{row.date}</td>
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
          title={balanced ? "" : "The difference has to be zero first"}
        >
          Finish and lock {ticked.size} {ticked.size === 1 ? "row" : "rows"}
        </button>
        <button onClick={onClose}>Not now</button>
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>
        Locking is one act, so History undoes the whole reconciliation in one click if you got the
        statement wrong.
      </p>

      {(past.data ?? []).length > 0 && (
        <>
          <hr className="rule" />
          <h3 className="section-title">Already proved</h3>
          <table>
            <tbody>
              {(past.data ?? []).map((one) => (
                <tr key={one.id}>
                  <td className="mono small">{one.statement_date}</td>
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
        <strong>Type the closing balance</strong>
        <span className="small muted">
          {typed ? "That isn't an amount in " + currency : "then tick the rows that are on it"}
        </span>
      </div>
    );
  }

  return (
    <div className={balanced ? "difference agreed" : "difference apart"}>
      <div>
        <strong>{balanced ? "It balances" : `${format(difference, currency)} out`}</strong>
        <span className="small">
          {balanced
            ? "Every row on this statement is accounted for."
            : difference > 0
              ? "The bank says there is more than you have ticked — a payment in you have not entered?"
              : "You have ticked more than the bank says — a row that is not on this statement?"}
        </span>
      </div>
      <dl className="difference-sum">
        <dt>Already locked</dt>
        <dd className="amount">{format(locked, currency)}</dd>
        <dt>Ticked here</dt>
        <dd className="amount">{format(ticked, currency)}</dd>
      </dl>
    </div>
  );
}
