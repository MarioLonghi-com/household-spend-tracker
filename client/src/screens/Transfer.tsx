/**
 * Moving money between two of your own accounts.
 *
 * A transfer is one act that writes two rows, so it gets one form rather than
 * two transactions you remember to keep in step. The server mirrors the legs
 * and links them; the register shows both, each as "Transfer : <the other
 * account>".
 *
 * Across currencies it asks for **both** amounts. That is not a missing feature:
 * the service refuses to invent a rate, because a rate it guessed would be a
 * number nobody could later reconcile against what the bank actually did. Each
 * leg stays exact in its own currency and the implied rate is stored on the pair.
 */

import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Field, Panel, Problem } from "../components/bits";
import { exponent, format, parse } from "../lib/money";
import type { Account, Household } from "../lib/types";

const today = () => new Date().toISOString().slice(0, 10);

export function Transfer({
  household,
  accounts,
  onClose,
  onDone,
}: {
  household: Household;
  accounts: Account[];
  onClose: () => void;
  onDone: () => void;
}) {
  const open = useMemo(() => accounts.filter((one) => !one.closed), [accounts]);

  const [fromId, setFromId] = useState(open[0]?.id ?? "");
  const [toId, setToId] = useState(open[1]?.id ?? "");
  const [date, setDate] = useState(today());
  const [leaving, setLeaving] = useState("");
  const [arriving, setArriving] = useState("");
  const [memo, setMemo] = useState("");

  const from = open.find((one) => one.id === fromId);
  const to = open.find((one) => one.id === toId);
  const crossCurrency = Boolean(from && to && from.currency !== to.currency);

  const out = from ? parse(leaving, from.currency) : null;
  const inn = to ? parse(arriving, to.currency) : null;

  const sameAccount = Boolean(fromId && toId && fromId === toId);
  const ready =
    Boolean(from && to) &&
    !sameAccount &&
    out !== null &&
    out > 0 &&
    (!crossCurrency || (inn !== null && inn > 0));

  const send = useMutation({
    mutationFn: () =>
      api.post(`/households/${household.id}/transfers`, {
        from_account_id: fromId,
        to_account_id: toId,
        date,
        amount: Math.abs(out ?? 0),
        // Only when the currencies differ: for a same-currency transfer the
        // server insists both sides match, and sending it again is one more
        // thing that can disagree.
        to_amount: crossCurrency ? Math.abs(inn ?? 0) : null,
        memo: memo.trim() || null,
      }),
    onSuccess: onDone,
  });

  // What the two amounts imply, shown before it is saved rather than after.
  const impliedRate =
    crossCurrency && out && inn
      ? (inn / 10 ** exponent(to!.currency) / (out / 10 ** exponent(from!.currency))).toFixed(4)
      : null;

  if (open.length < 2) {
    return (
      <Panel title="Add transfer" onClose={onClose}>
        <p className="muted small">
          A transfer needs two open accounts. Add another on the Accounts screen first.
        </p>
      </Panel>
    );
  }

  return (
    <Panel title="Add transfer between accounts" onClose={onClose}>
      <Problem error={send.error} />

      <Field label="From">
        <select value={fromId} onChange={(e) => setFromId(e.target.value)} autoFocus>
          {open.map((one) => (
            <option key={one.id} value={one.id}>
              {one.name} ({one.currency})
            </option>
          ))}
        </select>
      </Field>
      <p />
      <Field label="To">
        <select value={toId} onChange={(e) => setToId(e.target.value)}>
          {open.map((one) => (
            <option key={one.id} value={one.id}>
              {one.name} ({one.currency})
            </option>
          ))}
        </select>
      </Field>
      {sameAccount ? (
        <p className="small neg">Pick two different accounts — money cannot move to itself.</p>
      ) : (
        <p />
      )}

      <Field label="Date">
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
      </Field>
      <p />

      <Field label={from ? `Amount leaving (${from.currency})` : "Amount"}>
        <input
          value={leaving}
          onChange={(e) => setLeaving(e.target.value)}
          inputMode="decimal"
          placeholder="12,34"
        />
      </Field>
      {crossCurrency ? (
        <>
          <p className="muted small">
            No minus signs — the direction is the two accounts above.
          </p>
          <Field label={`Amount arriving (${to!.currency})`}>
            <input
              value={arriving}
              onChange={(e) => setArriving(e.target.value)}
              inputMode="decimal"
              placeholder="12,34"
            />
          </Field>
          <p className="muted small">
            Both, because the two accounts are in different currencies and we will not invent a
            rate — take the figure your bank actually credited.
            {impliedRate ? (
              <>
                {" "}
                That works out at <span className="mono">{impliedRate}</span> {to!.currency} per{" "}
                {from!.currency}.
              </>
            ) : null}
          </p>
        </>
      ) : (
        <p className="muted small">
          No minus signs — the direction is the two accounts above.
          {out !== null && from ? (
            <>
              {" "}
              <span className="mono">{format(out, from.currency)}</span> leaves {from.name} and the
              same arrives in {to?.name}.
            </>
          ) : null}
        </p>
      )}

      <Field label="Memo">
        <input value={memo} onChange={(e) => setMemo(e.target.value)} />
      </Field>
      <p />

      <button className="primary" disabled={!ready || send.isPending} onClick={() => send.mutate()}>
        Transfer
      </button>
      <p className="muted small" style={{ marginTop: 10 }}>
        This writes both rows as one act, so undoing it from History takes both back together.
      </p>
    </Panel>
  );
}
