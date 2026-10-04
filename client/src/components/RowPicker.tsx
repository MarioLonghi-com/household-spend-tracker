/**
 * Pick one row of the register from a window of dates around a moment.
 *
 * Two callers ask the same question -- "which transaction is this about?" --
 * from opposite ends: a receipt looking for the purchase it records, and a
 * work expense looking for the payment that repaid it. Both open on a window
 * around one date, let the person widen it, sort at the headings and pick one
 * row with a button. What differs is the window's width, which rows are
 * candidates at all, and what picking does, so those are the props.
 *
 * It was the receipts screen's `Matcher` and it keeps that screen's behaviour
 * exactly: the same query key, the same table, the same empty state.
 */

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { format } from "../lib/money";
import { sortRows } from "../lib/sorting";
import { Empty, Field, Panel, Problem, SortHeading, useSort } from "./bits";
import type { Account, Household, RegisterPage, Transaction } from "../lib/types";

/** An ISO date moved by whole days, read and written as UTC so no DST shift. */
export function shiftDays(iso: string, days: number): string {
  const when = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  when.setUTCDate(when.getUTCDate() + days);
  return when.toISOString().slice(0, 10);
}

export function RowPicker({
  household,
  title,
  anchor,
  days,
  intro,
  accept,
  action,
  busy = false,
  error,
  empty = "Nothing in that window. Widen the dates above.",
  onPick,
  onClose,
}: {
  household: Household;
  title: string;
  /** The date the window opens around, `YYYY-MM-DD` or a longer ISO string. */
  anchor: string;
  /** How far either side of `anchor` the window opens. */
  days: number;
  /** A sentence saying why the window is where it is. */
  intro?: ReactNode;
  /** Which rows are candidates. Everything in the window, when absent. */
  accept?: (txn: Transaction) => boolean;
  /** The button on each row: "Attach", "Link". */
  action: string;
  /** While the pick is being saved, so a second click cannot send it twice. */
  busy?: boolean;
  error?: unknown;
  empty?: ReactNode;
  onPick: (txn: Transaction) => void;
  onClose: () => void;
}) {
  const [since, setSince] = useState(() => shiftDays(anchor, -days));
  const [until, setUntil] = useState(() => shiftDays(anchor, days));
  const order = useSort<"date" | "payee" | "amount">("date", "desc");

  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts`),
  });

  // Keyed by the window alone, not by the filter: two pickers over the same
  // dates read the same rows, and the filter is applied to them in hand.
  const page = useQuery({
    queryKey: ["match", household.id, since, until],
    queryFn: () =>
      api.get<RegisterPage>(
        `/households/${household.id}/transactions?since=${since}&until=${until}`,
      ),
  });

  const currencyOf = (txn: Transaction & { currency?: string | null }) =>
    txn.currency ??
    (accounts.data ?? []).find((one) => one.id === txn.account_id)?.currency ??
    household.base_currency;

  const rows = useMemo(() => {
    const candidates = (page.data?.transactions ?? []).filter((txn) => !accept || accept(txn));
    return sortRows(candidates, order.sort, order.direction, (txn, column) =>
      column === "payee" ? (txn.payee_name ?? "") : column === "amount" ? txn.amount : txn.date,
    );
  }, [page.data, accept, order.sort, order.direction]);

  return (
    <Panel title={title} onClose={onClose} wide>
      <Problem error={page.error ?? error} />
      {intro ? <p className="muted small">{intro}</p> : null}

      <div className="row">
        <Field label="From">
          <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        </Field>
        <Field label="To">
          <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </Field>
      </div>

      {rows.length === 0 ? (
        <Empty>{empty}</Empty>
      ) : (
        <table>
          <thead>
            <tr>
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
                label="Amount"
                column="amount"
                sort={order.sort}
                direction={order.direction}
                onSort={order.onSort}
                align="right"
              />
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((txn) => (
              <tr key={txn.id}>
                {/* Payee first on a phone, then the amount: matching a row is
                    looking for who the money went to or came from. */}
                <td data-label="Date" data-detail-first="true">{txn.date}</td>
                <td data-primary="true">
                  {txn.payee_name ?? <span className="muted">—</span>}
                  {txn.has_receipt ? <span className="receipt-mark">📎</span> : null}
                </td>
                <td className="amount" data-figure="true">
                  {format(txn.amount, currencyOf(txn))}
                </td>
                <td>
                  <button disabled={busy} onClick={() => onPick(txn)}>
                    {action}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
