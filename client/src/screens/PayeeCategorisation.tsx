/**
 * What each payee gets filed under, and the door to changing it.
 *
 * Split out of the payee rules screen (#65). The two halves were one screen on
 * the argument that you set them in the same sitting -- a rule decides which
 * payee a statement line belongs to, the payee then decides what it was for.
 * On a real ledger that put a table of several hundred payees above the dozen
 * rules, and the rules were the thing you could no longer find. They are
 * still one job; they are no longer one scroll.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Panel, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { PayeeCategorisationPanel } from "./Categories";
import type { Tally } from "./Categories";
import { useWindowed } from "../lib/useWindowed";
import type { Household, Payee } from "../lib/types";
import { compareNames, formatCount } from "../lib/locale";
import { categoryName } from "../lib/labels";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

/** `GET /households/{id}/stats/payees`, one entry per payee with transactions. */
export interface PayeeStat {
  payee_id: string;
  transaction_count: number;
  /** Distinct categories among them. "Uncategorised" is not one of them. */
  category_count: number;
  /** Every one of them, biggest first. Not truncated by the server. */
  categories: Tally[];
}

interface PayeeStats {
  payees: PayeeStat[];
}

/** How many category names fit on one line before the rest become a count. */
const INLINE_CATEGORIES = 3;

/**
 * `categories` sorts on **how many** categories a payee spreads across, not on
 * the text of the summary: the column is a fact about the payee, and sorting
 * the sentence would order by whichever name happens to come first.
 */
type PayeeSort = "name" | "transactions" | "categories";

export function PayeeCategorisation({ household }: { household: Household }) {
  const [categorising, setCategorising] = useState<string | null>(null);
  /** The payee whose whole category list is open. Null is the ordinary case. */
  const [breakdown, setBreakdown] = useState<Payee | null>(null);
  const [search, setSearch] = useState("");

  const payees = useQuery({
    queryKey: ["payees", household.id],
    queryFn: () => api.get<Payee[]>(`/households/${household.id}/payees`),
  });

  /**
   * The stat column, in one request for the whole screen.
   *
   * One grouped query on the server, not one per payee: this list is thousands
   * of rows on a real ledger, which is exactly where a per-row request stops
   * being merely wasteful.
   */
  const stats = useQuery({
    queryKey: ["payee-stats", household.id],
    queryFn: () => api.get<PayeeStats>(`/households/${household.id}/stats/payees`),
  });
  const byPayee = useMemo(() => {
    const found = new Map<string, PayeeStat>();
    for (const one of stats.data?.payees ?? []) found.set(one.payee_id, one);
    return found;
  }, [stats.data]);

  const payeeOrder = useSort<PayeeSort>("name");
  /**
   * Filtered, then sorted, then windowed -- in that order.
   *
   * The search runs here rather than at the server because the whole payee
   * list is already in hand: this screen fetches it once and the register's
   * own search is the one that has to be exact and server-side. Typing here
   * costs no request and the count under the table stays truthful, because
   * `useWindowed` is given the filtered list rather than the whole one.
   */
  const query = search.trim().toLowerCase();
  const realPayees = useMemo(
    () =>
      sortRows(
        (payees.data ?? [])
          .filter((one) => !one.transfer_account_id)
          .filter((one) => !query || one.name.toLowerCase().includes(query)),
        payeeOrder.sort,
        payeeOrder.direction,
        (payee, column) => {
          if (column === "transactions") return byPayee.get(payee.id)?.transaction_count ?? 0;
          if (column === "categories") return byPayee.get(payee.id)?.category_count ?? 0;
          return payee.name;
        },
        (a, b) => compareNames(a.name, b.name),
      ),
    [payees.data, byPayee, query, payeeOrder.sort, payeeOrder.direction],
  );
  // Payees grow with every new shop, so this list is windowed.
  const page = useWindowed(realPayees);

  return (
    <>
      {categorising && (
        <PayeeCategorisationPanel
          payeeId={categorising}
          household={household}
          onClose={() => setCategorising(null)}
        />
      )}
      {breakdown && (
        <Panel title={breakdown.name} onClose={() => setBreakdown(null)}>
          <h3 className="section-title"><Trans comment="Heading on the Payee Categorisation screen">Categorised as</Trans></h3>
          <ul className="breakdown">
            {(byPayee.get(breakdown.id)?.categories ?? []).map((one) => (
              <li key={one.key ?? "none"}>
                <span className={one.key ? "" : "muted"}>{categoryName(one.key, one.name)}</span>
                <span className="small muted">{formatCount(one.transaction_count)}</span>
              </li>
            ))}
          </ul>
          <p className="muted small">
            <Trans>
              Every category this payee has been filed under, biggest first. The transactions with
              no category are listed too, because they are what this payee still costs you to
              classify.
            </Trans>
          </p>
        </Panel>
      )}
      <h1><Trans comment="Screen title on the Payee Categorisation screen. See GLOSSARY.md">Payee categorisation</Trans></h1>
      <p className="muted small">
        <Trans>
          What each payee has been filed under, and what a new transaction for it will be. Which
          payee a statement line becomes is decided first, by the payee naming rules.
        </Trans>
      </p>

      <Problem error={payees.error ?? stats.error} />

      <div className="card">
        <div className="row payee-search-row">
          <h2 className="card-title" style={{ margin: 0 }}>
            <Trans>
              What each payee is categorised as
            </Trans>
          </h2>
          {/* `type="search"` rather than a text box: it gets the browser's own
              clear button and the phone keyboard's search key, and neither is
              worth rebuilding. `role="searchbox"` is what it already is. */}
          <label className="payee-search">
            <span className="sr-only"><Trans comment="Screen-reader text on the Payee Categorisation screen">Search payees</Trans></span>
            <input
              type="search"
              value={search}
              placeholder={t`type any part of a payee`}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        {payees.data?.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            <Trans>
              No payees yet. They appear as you enter or import transactions.
            </Trans>
          </p>
        ) : realPayees.length === 0 ? (
          /* A filter that matches nothing says so, rather than looking like a
             household with no payees in it. */
          <p className="muted small" style={{ margin: 0 }}>
            {t`No payee matches “${search.trim()}”.`}
          </p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label={t({ message: "Payee", comment: "Column heading on the Payee Categorisation screen: noun, who was paid or who paid. See GLOSSARY.md" })}
                    column="name"
                    sort={payeeOrder.sort}
                    direction={payeeOrder.direction}
                    onSort={payeeOrder.onSort}
                  />
                  <SortHeading
                    label={t({ message: "Transactions", comment: "Column heading on the Payee Categorisation screen. See GLOSSARY.md" })}
                    column="transactions"
                    sort={payeeOrder.sort}
                    direction={payeeOrder.direction}
                    onSort={payeeOrder.onSort}
                    align="right"
                  />
                  <SortHeading
                    label={t({ message: "Categorised as", comment: "Column heading on the Payee Categorisation screen" })}
                    column="categories"
                    sort={payeeOrder.sort}
                    direction={payeeOrder.direction}
                    onSort={payeeOrder.onSort}
                    className="col-tallies"
                  />
                  <th className="amount" />
                </tr>
              </thead>
              <tbody>
                {page.visible.map((payee) => (
                  <tr key={payee.id}>
                    {/* The phone card's headline; see "Tables on a phone". */}
                    <td data-primary="true">{payee.name}</td>
                    <td className="small muted amount" data-figure="true">
                      {stats.isPending
                        ? "…"
                        : formatCount(byPayee.get(payee.id)?.transaction_count ?? 0)}
                    </td>
                    <td className="small" data-label={t({ message: "Categorised as", comment: "Column name shown beside a value on phones on the Payee Categorisation screen" })} data-detail-first="true">
                      <CategorySummary
                        stat={byPayee.get(payee.id)}
                        onMore={() => setBreakdown(payee)}
                      />
                    </td>
                    <td className="amount">
                      <button className="link" onClick={() => setCategorising(payee.id)}>
                        <Trans comment="Button on the Payee Categorisation screen">
                          Categorisation
                        </Trans>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!page.allShown && (
              <button
                type="button"
                className="more-rows"
                ref={page.sentinelRef}
                onClick={page.extend}
              >
                {t`Showing ${formatCount(page.shown)} of ${formatCount(page.total)} — show more`}
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}

/**
 * What a payee gets filed under, on one line.
 *
 * Three names and their counts, then a count of the rest -- because the useful
 * fact is usually "this is always Groceries" or "this is all over the place",
 * and both are visible in three names. The remainder is a button rather than a
 * number so the whole list is still reachable: a summary that hides four
 * categories with no way to see them is a summary that cannot be checked.
 */
function CategorySummary({
  stat,
  onMore,
}: {
  stat: PayeeStat | undefined;
  onMore: () => void;
}) {
  if (!stat || stat.categories.length === 0) {
    return <span className="muted">—</span>;
  }
  const shown = stat.categories.slice(0, INLINE_CATEGORIES);
  const rest = stat.categories.length - shown.length;
  return (
    <span className="tallies">
      {shown.map((one, at) => (
        <span key={one.key ?? "none"} className={one.key ? "tally" : "tally unset"}>
          {at > 0 ? <span className="tally-gap" aria-hidden="true"> · </span> : null}
          {categoryName(one.key, one.name)} <span className="muted">{formatCount(one.transaction_count)}</span>
        </span>
      ))}
      {rest > 0 && (
        <button type="button" className="link tally-more" onClick={onMore}>
          {t({ message: `+${formatCount(rest)} more`, comment: "Button on the Payee Categorisation screen" })}
        </button>
      )}
    </span>
  );
}
