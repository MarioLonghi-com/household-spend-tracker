/**
 * Money moving between the household's own accounts (issue #70).
 *
 * Two statements, imported days apart, each carry one leg of the same
 * transfer. An import links the ones it is sure of as it commits; this screen
 * is for the rest -- the sweep over rows imported before matching existed, the
 * pairs that need a person to say yes, and the legs still waiting for the other
 * bank's statement.
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Money, Problem, SortHeading, fixed, moneyKey, sortRows, useSort } from "../components/bits";
import type { Household } from "../lib/types";
import { UnprovenSection, WaitingSection, type LinkedPair } from "./TransferSections";
import { useIdentifierSuggestions } from "./Accounts";
import { formatDate } from "../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";

export interface Leg {
  id: string;
  account_id: string;
  account_name: string;
  date: string;
  amount: number;
  currency: string;
  description: string | null;
}

interface Pair {
  out_leg: Leg;
  in_leg: Leg;
  strength: "strong" | "suggested";
  why: string;
  /** Description words both rows carry (#127): these sort first by Why. */
  words?: string[];
}

interface Findings {
  strong: Pair[];
  suggested: Pair[];
  awaiting: { leg: Leg; why: string }[];
  /** Links no name vouches for and no person made (#131). */
  unproven?: LinkedPair[];
}

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

type PairSort = "out" | "into" | "amount" | "why";

const pairKey = (pair: Pair) => `${pair.out_leg.id}-${pair.in_leg.id}`;

/** Why a pair's tick box is off. A function, so it reads the active language. */
const sharesARow = () =>
  t({ message: "Shares a row with a pair you've ticked", comment: "Tooltip on the Transfers screen: why this pair cannot be ticked" });

/**
 * Pairs to link, one at a time or several ticked at once (#126).
 *
 * A row can only be one leg of one transfer, so two pairs that share a row
 * cannot both be linked: the server would link the first, refuse the second
 * with 409, and the whole batch would fail. Ticking a pair therefore disables
 * every other pair that shares a row with it, and select-all only ticks pairs
 * that do not clash with each other.
 *
 * `loadedAt` is when the findings last arrived. The ticks are cleared then --
 * after a link, whose success reloads them, and after any other reload --
 * because a tick on a list that has changed under it is a guess.
 */
function PairTable({
  pairs,
  baseCurrency,
  onLink,
  busy,
  loadedAt,
  initialSort = "out",
  onReject,
}: {
  pairs: Pair[];
  baseCurrency: string;
  onLink: (pairs: Pair[]) => void;
  busy: boolean;
  loadedAt: number;
  initialSort?: PairSort;
  /** "Not a transfer" (#131): never offer this pair again. */
  onReject?: (pair: Pair) => void;
}) {
  // Re-renders its words when the language changes.
  useLingui();
  const [ticked, setTicked] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => setTicked(new Set()), [loadedAt]);
  const { sort, direction, onSort } = useSort<PairSort>(initialSort);
  const rows = useMemo(
    () =>
      sortRows(pairs, sort, direction, (pair, column) => {
        switch (column) {
          case "out":
            return [pair.out_leg.date, pair.out_leg.account_name];
          case "into":
            return [pair.in_leg.date, pair.in_leg.account_name];
          // Money across currencies sorts by currency first, then figure --
          // base currency first, and the groups hold still when it turns (#129).
          case "amount":
            return moneyKey(pair.in_leg.currency, pair.in_leg.amount, baseCurrency);
          // Rows that say the same thing first in both directions (#127), then by what it says.
          case "why":
            return [fixed(pair.words?.length ? 0 : 1), pair.why];
        }
      }),
    [pairs, sort, direction, baseCurrency],
  );
  const heading = { sort, direction, onSort };

  // In the order shown, so "Link N selected" sends them the way they read.
  const chosen = rows.filter((pair) => ticked.has(pairKey(pair)));
  const taken = new Set(chosen.flatMap((pair) => [pair.out_leg.id, pair.in_leg.id]));
  const clashes = (pair: Pair) =>
    !ticked.has(pairKey(pair)) && (taken.has(pair.out_leg.id) || taken.has(pair.in_leg.id));
  // Everything that can be ticked is: each row is either ticked or blocked.
  const allTicked =
    chosen.length > 0 && rows.every((pair) => ticked.has(pairKey(pair)) || clashes(pair));

  function toggle(pair: Pair) {
    // `disabled` already stops a click, but the rule is the table's, not the
    // input's: a pair sharing a row with a ticked one is never added.
    if (clashes(pair)) return;
    setTicked((was) => {
      const next = new Set(was);
      if (!next.delete(pairKey(pair))) next.add(pairKey(pair));
      return next;
    });
  }

  // Top to bottom as shown, keeping what is already ticked and adding each
  // pair whose rows are still free -- so of two that clash, the one higher up
  // the list wins.
  function toggleAll() {
    if (allTicked) {
      setTicked(new Set());
      return;
    }
    const next = new Set(ticked);
    const used = new Set(taken);
    for (const pair of rows) {
      if (next.has(pairKey(pair))) continue;
      if (used.has(pair.out_leg.id) || used.has(pair.in_leg.id)) continue;
      next.add(pairKey(pair));
      used.add(pair.out_leg.id);
      used.add(pair.in_leg.id);
    }
    setTicked(next);
  }

  return (
    <>
      <p>
        <button disabled={busy || chosen.length === 0} onClick={() => onLink(chosen)}>
          {chosen.length === 0
            ? t({ message: "Link selected", comment: "Button on the Transfers screen: verb, link the ticked pairs as transfers" })
            : t({ message: plural(chosen.length, { one: `Link ${chosen.length} selected`, other: `Link ${chosen.length} selected` }), comment: "Button on the Transfers screen: verb, link the ticked pairs as transfers" })}
        </button>
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}>
                <input
                  type="checkbox"
                  checked={allTicked}
                  onChange={toggleAll}
                  aria-label={t`Select every pair that does not share a row with another`}
                  style={{ width: "auto" }}
                />
              </th>
              <SortHeading label={t({ message: "Out of", comment: "Column heading on the Transfers screen: preposition, the account money leaves" })} column="out" {...heading} />
              <SortHeading label={t({ message: "Into", comment: "Column heading on the Transfers screen: preposition, the account money arrives in" })} column="into" {...heading} />
              <SortHeading label={t({ message: "Amount", comment: "Column heading on the Transfers screen: noun, a sum of money. See GLOSSARY.md" })} column="amount" align="right" {...heading} />
              <SortHeading label={t({ message: "Why", comment: "Column heading on the Transfers screen: noun, the reason" })} column="why" {...heading} />
              <th aria-label={t({ message: "Actions", comment: "Screen-reader name on the Transfers screen" })} />
            </tr>
          </thead>
          <tbody>
            {rows.map((pair) => (
              <tr key={pairKey(pair)}>
                <td data-select="true" title={clashes(pair) ? sharesARow() : undefined}>
                  <input
                    type="checkbox"
                    checked={ticked.has(pairKey(pair))}
                    disabled={clashes(pair)}
                    onChange={() => toggle(pair)}
                    aria-label={t({ message: `Select ${pair.out_leg.account_name} to ${pair.in_leg.account_name}, ${formatDate(pair.out_leg.date)}`, comment: "Screen-reader name of a pair's tick box on the Transfers screen: from one account to another, and the date" })}
                    title={clashes(pair) ? sharesARow() : undefined}
                    style={{ width: "auto" }}
                  />
                </td>
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
                  <button className="link" disabled={busy} onClick={() => onLink([pair])}>
                    <Trans comment="Button on the Transfers screen: verb, link this pair as one transfer">Link</Trans>
                  </button>
                  {onReject && (
                    <button className="link" disabled={busy} onClick={() => onReject(pair)}>
                      <Trans comment="Button on the Transfers screen: these two rows are not one transfer, never offer them again">Not a transfer</Trans>
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function Transfers({ household }: { household: Household }) {
  // Re-renders its words when the language changes.
  useLingui();
  const client = useQueryClient();
  const key = ["transfer-findings", household.id];
  const findings = useQuery({
    queryKey: key,
    queryFn: () => api.get<Findings>(`/households/${household.id}/transfers/findings`),
  });
  const link = useMutation({
    mutationFn: (pairs: Pair[]) =>
      api.post<{ linked: number }>(`/households/${household.id}/transfers/link`, {
        pairs: pairs.map((pair) => ({ first_id: pair.out_leg.id, second_id: pair.in_leg.id })),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: key });
      client.invalidateQueries({ queryKey: ["register", household.id] });
    },
  });
  // "Link all" is not a person vouching for each pair, so the server records
  // each link as what its evidence is rather than as a person's (#131).
  const linkAll = useMutation({
    mutationFn: (pairs: Pair[]) =>
      api.post<{ linked: number }>(`/households/${household.id}/transfers/link`, {
        pairs: pairs.map((pair) => ({ first_id: pair.out_leg.id, second_id: pair.in_leg.id })),
        by: "evidence",
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: key });
      client.invalidateQueries({ queryKey: ["register", household.id] });
    },
  });
  const reject = useMutation({
    mutationFn: (pairs: Pair[]) =>
      api.post<{ rejected: number }>(`/households/${household.id}/transfers/reject`, {
        pairs: pairs.map((pair) => ({ first_id: pair.out_leg.id, second_id: pair.in_leg.id })),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  });
  const busy = link.isPending || linkAll.isPending || reject.isPending;
  const data = findings.data;
  // What adding the suggested identifiers would link (#130). Only said when
  // it is something: the list itself is on the Accounts screen.
  const suggested = useIdentifierSuggestions(household, true).data;
  const helping = (suggested?.items ?? []).filter((one) => (one.would_link ?? 0) > 0).length;

  return (
    <>
      <div className="card">
        <h2>
          <Trans comment="Heading of the Transfers screen: noun, money moved between your own accounts. See GLOSSARY.md">Transfers</Trans>
        </h2>
        <p className="muted small">
          <Trans>
            A transfer between two of your accounts shows up twice — once in each bank's
            statement — and until it is linked it counts as spending on one side and income on the
            other. Linked, it stays out of Income vs Expense. Imports link the ones they are sure of;
            these are the rest. To link a transfer between two currencies, select its two rows in
            the register and choose <em>Link as transfer</em>.
          </Trans>
        </p>
        <Problem error={findings.error ?? link.error ?? linkAll.error ?? reject.error} />
        {(link.data ?? linkAll.data) && (
          <p className="small">
            <Trans comment="Transfers screen, after linking: how many pairs were linked">Linked {(link.data ?? linkAll.data)!.linked}.</Trans>
          </p>
        )}
        {suggested && (suggested.would_link ?? 0) > 0 && (
          <p className="small" data-testid="suggested-identifiers">
            {t({
              message: plural(helping, {
                one: `${helping} suggested identifier would link`,
                other: `${helping} suggested identifiers would link`,
              }),
              comment: "Transfers screen, followed by how many pairs: identifiers are names and numbers that tell accounts apart",
            })}{" "}
            {t({
              message: plural(suggested.would_link ?? 0, {
                one: `${suggested.would_link} pair.`,
                other: `${suggested.would_link} pairs.`,
              }),
              comment: "Transfers screen, after how many suggested identifiers: how many transfer pairs they would link",
            })}{" "}
            <Trans>
              Add or ignore them under <em>Suggested identifiers</em> on the Accounts screen.
            </Trans>
          </p>
        )}
      </div>

      <div className="card">
        <h3>
          <Trans comment="Section heading on the Transfers screen: pairs the app is sure are transfers">Sure of these</Trans>
        </h3>
        <p className="small muted">
          <Trans>
            One row names the other account, or these two accounts have had transfers linked before,
            and neither row could be anything else — or, of the rows it could be, only this one
            says the same thing.
          </Trans>
        </p>
        {data && data.strong.length > 0 ? (
          <>
            <button
              className="primary"
              disabled={busy}
              onClick={() => linkAll.mutate(data.strong)}
            >
              <Trans comment="Button on the Transfers screen: verb, link every pair in this section; the number is how many">
                Link all {data.strong.length}
              </Trans>
            </button>
            <p />
            <PairTable
              pairs={data.strong}
              baseCurrency={household.base_currency}
              busy={busy}
              onLink={(pairs) => link.mutate(pairs)}
              onReject={(pair) => reject.mutate([pair])}
              loadedAt={findings.dataUpdatedAt}
            />
          </>
        ) : (
          <Empty>
            <Trans comment="Shown when a list is empty on the Transfers screen">Nothing waiting.</Trans>
          </Empty>
        )}
      </div>

      <div className="card">
        <h3>
          <Trans comment="Section heading on the Transfers screen: pairs that may be transfers, for a person to decide">Worth a look</Trans>
        </h3>
        <p className="small muted">
          <Trans>
            The amounts match and the dates are close, but nothing names the other account — or a
            row could pair with more than one. Pairs whose two descriptions say the same thing come
            first. Link the ones that are a transfer; mark the rest <em>Not a transfer</em> and they
            are not offered again.
          </Trans>
        </p>
        {data && data.suggested.length > 0 ? (
          <PairTable
            pairs={data.suggested}
            initialSort="why"
            baseCurrency={household.base_currency}
            busy={busy}
            onLink={(pairs) => link.mutate(pairs)}
            onReject={(pair) => reject.mutate([pair])}
            loadedAt={findings.dataUpdatedAt}
          />
        ) : (
          <Empty>
            <Trans comment="Shown when a list is empty on the Transfers screen">Nothing to look at.</Trans>
          </Empty>
        )}
      </div>

      <WaitingSection
        household={household}
        waiting={data?.awaiting ?? []}
        onChanged={() => client.invalidateQueries({ queryKey: key })}
      />

      <UnprovenSection
        household={household}
        linked={data?.unproven ?? []}
        onChanged={() => client.invalidateQueries({ queryKey: key })}
      />
    </>
  );
}
