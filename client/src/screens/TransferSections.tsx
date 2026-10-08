/**
 * The Transfers screen's lists of single rows, as opposed to its pairs.
 *
 * Its own file so the pair tables in `Transfers.tsx` and these can change
 * without stepping on each other.
 *
 * - **Waiting for the other statement** (#125): a row that names one of the
 *   household's accounts or members but has no other side yet. Some are not
 *   transfers at all -- money paid to a member's account that isn't tracked
 *   here -- and the way to say so is to give the row a category, because a
 *   categorised row is not a transfer. Clearing it puts the row back.
 * - **Linked by history only** (#131): links already made that no row's
 *   words vouch for and no person chose -- how the wrong links account
 *   history made are found and taken apart. Every link a program made
 *   through an agent key is here too (#134), until a person keeps it.
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Money, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import type { CategoryGroup, Household } from "../lib/types";
import type { Leg } from "./Transfers";
import { formatDate } from "../lib/locale";
import { Trans, useLingui } from "@lingui/react/macro";

export interface LinkedPair {
  out_leg: Leg;
  in_leg: Leg;
  /** `history`, `import` (made before links said how), `named` whose name has gone, or
   * `agent` -- a program linked it and no person has kept it yet (#134). */
  link_source: string | null;
  why: string;
}

export interface WaitingLeg {
  leg: Leg;
  why: string;
}

/** Every category that can be picked, grouped as the register groups them. */
function CategoryOptions({ groups }: { groups: CategoryGroup[] }) {
  return (
    <>
      {groups.map((group) => (
        <optgroup key={group.id} label={group.name}>
          {group.categories
            .filter((one) => !one.archived)
            .map((one) => (
              <option key={one.id} value={one.id}>
                {one.name}
              </option>
            ))}
        </optgroup>
      ))}
    </>
  );
}

type WaitingSort = "account" | "date" | "amount" | "description" | "why";

export function WaitingSection({
  household,
  waiting,
  onChanged,
}: {
  household: Household;
  waiting: WaitingLeg[];
  /** The findings are stale: a row just stopped being a candidate. */
  onChanged: () => void;
}) {
  const { t } = useLingui();
  const client = useQueryClient();
  const categories = useQuery({
    queryKey: ["categories", household.id, false],
    queryFn: () => api.get<CategoryGroup[]>(`/households/${household.id}/categories`),
  });
  const groups = categories.data ?? [];

  const [ticked, setTicked] = useState<Set<string>>(new Set());
  // A reload that drops a row drops its tick too: a tick on a row that has
  // gone would be counted in "Categorise N selected" and then do nothing.
  useEffect(() => {
    setTicked((was) => {
      const present = new Set(waiting.map(({ leg }) => leg.id));
      const kept = new Set([...was].filter((id) => present.has(id)));
      return kept.size === was.size ? was : kept;
    });
  }, [waiting]);

  const done = () => {
    onChanged();
    client.invalidateQueries({ queryKey: ["register", household.id] });
  };
  // One row: the ordinary transaction update, so History shows it as an edit
  // of that row and one undo takes it back.
  const one = useMutation({
    mutationFn: ({ id, category_id }: { id: string; category_id: string }) =>
      api.patch(`/transactions/${id}`, { category_id }),
    onSuccess: done,
  });
  // Many rows: one bulk update, so one undo.
  const many = useMutation({
    mutationFn: ({ ids, category_id }: { ids: string[]; category_id: string }) =>
      api.post(`/households/${household.id}/transactions/bulk`, {
        transaction_ids: ids,
        category_id,
      }),
    onSuccess: () => {
      setTicked(new Set());
      done();
    },
  });
  const busy = one.isPending || many.isPending;

  const { sort, direction, onSort } = useSort<WaitingSort>("date");
  const rows = useMemo(
    () =>
      sortRows(waiting, sort, direction, ({ leg, why }, column) => {
        switch (column) {
          case "account":
            return leg.account_name;
          case "date":
            return [leg.date, leg.account_name];
          // Money across currencies sorts by currency first, then figure.
          case "amount":
            return [leg.currency, leg.amount];
          case "description":
            return leg.description;
          case "why":
            return why;
        }
      }),
    [waiting, sort, direction],
  );
  const heading = { sort, direction, onSort };
  const allTicked = waiting.length > 0 && ticked.size === waiting.length;

  return (
    <div className="card">
      <h3>
        <Trans>Waiting for the other statement</Trans>
      </h3>
      <p className="small muted">
        <Trans>
          These name one of your accounts or a member of the household, and their other side is
          not in the ledger yet. They are linked when that statement is imported. Give a row a
          category if it isn't a transfer.
        </Trans>
      </p>
      <Problem error={categories.error ?? one.error ?? many.error} />
      {waiting.length > 0 ? (
        <>
          <div className="row-actions">
            <select
              aria-label={t`Categorise ${ticked.size} selected as…`}
              value=""
              disabled={busy || ticked.size === 0}
              onChange={(event) => {
                if (event.target.value) {
                  many.mutate({ ids: [...ticked], category_id: event.target.value });
                }
              }}
            >
              <option value="">{t`Categorise ${ticked.size} selected as…`}</option>
              <CategoryOptions groups={groups} />
            </select>
          </div>
          <p />
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      aria-label={t`Select every waiting row`}
                      checked={allTicked}
                      disabled={busy}
                      onChange={() =>
                        setTicked(
                          allTicked ? new Set() : new Set(waiting.map(({ leg }) => leg.id)),
                        )
                      }
                      style={{ width: "auto" }}
                    />
                  </th>
                  <SortHeading label={t({ message: "Account", comment: "Column heading on the Transfers screen: noun, a bank or cash account. See GLOSSARY.md" })} column="account" {...heading} />
                  <SortHeading label={t({ message: "Date", comment: "Column heading on the Transfers screen: noun. See GLOSSARY.md" })} column="date" {...heading} />
                  <SortHeading label={t({ message: "Amount", comment: "Column heading on the Transfers screen: noun, a sum of money. See GLOSSARY.md" })} column="amount" align="right" {...heading} />
                  <SortHeading label={t({ message: "Description", comment: "Column heading on the Transfers screen" })} column="description" {...heading} />
                  <SortHeading label={t({ message: "Why", comment: "Column heading on the Transfers screen: noun, the reason" })} column="why" {...heading} />
                  <th aria-label={t`Not a transfer`} />
                </tr>
              </thead>
              <tbody>
                {rows.map(({ leg, why }) => (
                  <tr key={leg.id}>
                    <td data-select="true">
                      <input
                        type="checkbox"
                        aria-label={t({ message: `Select ${leg.account_name} ${formatDate(leg.date)}`, comment: "Screen-reader name on the Transfers screen" })}
                        checked={ticked.has(leg.id)}
                        disabled={busy}
                        onChange={() =>
                          setTicked((was) => {
                            const next = new Set(was);
                            if (next.has(leg.id)) next.delete(leg.id);
                            else next.add(leg.id);
                            return next;
                          })
                        }
                        style={{ width: "auto" }}
                      />
                    </td>
                    <td data-label={t({ message: "Account", comment: "Column name shown beside a value on phones on the Transfers screen: noun, a bank or cash account. See GLOSSARY.md" })}>
                      <strong>{leg.account_name}</strong>
                    </td>
                    <td data-label={t({ message: "Date", comment: "Column name shown beside a value on phones on the Transfers screen: noun. See GLOSSARY.md" })}>{formatDate(leg.date)}</td>
                    <td className="amount" data-label={t({ message: "Amount", comment: "Column name shown beside a value on phones on the Transfers screen: noun, a sum of money. See GLOSSARY.md" })}>
                      <Money minor={leg.amount} currency={leg.currency} />
                    </td>
                    <td className="small" data-label={t({ message: "Description", comment: "Column name shown beside a value on phones on the Transfers screen" })}>
                      {leg.description ?? "—"}
                    </td>
                    <td className="small muted" data-label={t({ message: "Why", comment: "Column name shown beside a value on phones on the Transfers screen: noun, the reason" })}>
                      {why}
                    </td>
                    <td className="row-actions">
                      <select
                        aria-label={t`Not a transfer — categorise as…`}
                        value=""
                        disabled={busy}
                        onChange={(event) => {
                          if (event.target.value) {
                            one.mutate({ id: leg.id, category_id: event.target.value });
                          }
                        }}
                      >
                        <option value="">{t`Not a transfer — categorise as…`}</option>
                        <CategoryOptions groups={groups} />
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <Empty>
          <Trans comment="Shown when a list is empty on the Transfers screen">Nothing waiting.</Trans>
        </Empty>
      )}
    </div>
  );
}

type LinkedSort = "out" | "into" | "amount" | "why";

function LegCell({ leg }: { leg: Leg }) {
  return (
    <>
      <div>
        <strong>{leg.account_name}</strong> · {formatDate(leg.date)}
      </div>
      <div className="small muted">{leg.description ?? "—"}</div>
    </>
  );
}

export function UnprovenSection({
  household,
  linked,
  onChanged,
}: {
  household: Household;
  linked: LinkedPair[];
  onChanged: () => void;
}) {
  const { t } = useLingui();
  const client = useQueryClient();
  const done = () => {
    onChanged();
    client.invalidateQueries({ queryKey: ["register", household.id] });
  };
  // Unlinking also records the pair as not a transfer, so the next sweep does
  // not offer it straight back; undoing the unlink in History undoes both.
  const unlink = useMutation({
    mutationFn: (pair: LinkedPair) => api.post(`/transactions/${pair.out_leg.id}/unlink`),
    onSuccess: done,
  });
  const keep = useMutation({
    mutationFn: (pair: LinkedPair) => api.post(`/transactions/${pair.out_leg.id}/confirm-link`),
    onSuccess: done,
  });
  const busy = unlink.isPending || keep.isPending;

  const { sort, direction, onSort } = useSort<LinkedSort>("out");
  const rows = useMemo(
    () =>
      sortRows(linked, sort, direction, (pair, column) => {
        switch (column) {
          case "out":
            return [pair.out_leg.date, pair.out_leg.account_name];
          case "into":
            return [pair.in_leg.date, pair.in_leg.account_name];
          // Money across currencies sorts by currency first, then figure.
          case "amount":
            return [pair.in_leg.currency, pair.in_leg.amount];
          case "why":
            return pair.why;
        }
      }),
    [linked, sort, direction],
  );
  const heading = { sort, direction, onSort };

  return (
    <div className="card">
      <h3>
        <Trans>Linked by history only</Trans>
      </h3>
      <p className="small muted">
        <Trans>
          Linked as transfers although no person chose them — because the two accounts had been
          linked before and neither row names the other, or because a program linked them through
          an agent key. A purchase and the refund of exactly its amount can look like that. Unlink
          the ones that are not transfers; they are not offered again. Keep the ones that are.
        </Trans>
      </p>
      <Problem error={unlink.error ?? keep.error} />
      {linked.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label={t({ message: "Out of", comment: "Column heading on the Transfers screen: preposition, the account money leaves" })} column="out" {...heading} />
                <SortHeading label={t({ message: "Into", comment: "Column heading on the Transfers screen: preposition, the account money arrives in" })} column="into" {...heading} />
                <SortHeading label={t({ message: "Amount", comment: "Column heading on the Transfers screen: noun, a sum of money. See GLOSSARY.md" })} column="amount" align="right" {...heading} />
                <SortHeading label={t({ message: "Why", comment: "Column heading on the Transfers screen: noun, the reason" })} column="why" {...heading} />
                <th aria-label={t({ message: "Actions", comment: "Screen-reader name on the Transfers screen" })} />
              </tr>
            </thead>
            <tbody>
              {rows.map((pair) => (
                <tr key={`${pair.out_leg.id}-${pair.in_leg.id}`}>
                  <td data-label={t({ message: "Out of", comment: "Column name shown beside a value on phones on the Transfers screen: preposition, the account money leaves" })}>
                    <LegCell leg={pair.out_leg} />
                  </td>
                  <td data-label={t({ message: "Into", comment: "Column name shown beside a value on phones on the Transfers screen: preposition, the account money arrives in" })}>
                    <LegCell leg={pair.in_leg} />
                  </td>
                  <td className="amount" data-label={t({ message: "Amount", comment: "Column name shown beside a value on phones on the Transfers screen: noun, a sum of money. See GLOSSARY.md" })}>
                    <Money minor={pair.in_leg.amount} currency={pair.in_leg.currency} />
                  </td>
                  <td className="small" data-label={t({ message: "Why", comment: "Column name shown beside a value on phones on the Transfers screen: noun, the reason" })}>
                    {pair.why}
                  </td>
                  <td className="amount row-actions">
                    <button className="link" disabled={busy} onClick={() => unlink.mutate(pair)}>
                      <Trans comment="Button on the Transfers screen: verb, undo a link between two rows">Unlink</Trans>
                    </button>
                    <button className="link" disabled={busy} onClick={() => keep.mutate(pair)}>
                      <Trans comment="Button on the Transfers screen: verb, leave it as it is">Keep</Trans>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>
          <Trans>Every link has a name or a person behind it.</Trans>
        </Empty>
      )}
    </div>
  );
}
