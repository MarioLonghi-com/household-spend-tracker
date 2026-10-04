/**
 * The household's payees: who the money went to, as one list.
 *
 * Payees have existed since the register did and have never had a screen —
 * they could be created (by typing one into a transaction, or by an import
 * matching a rule) and merged (by nothing at all, though the endpoint has been
 * there the whole time). So the two things this screen does are the two that
 * had no way in: seeing the list, and folding a duplicate into the real one.
 *
 * Deliberately modest. Per-payee figures — how many transactions, how much,
 * when last seen — belong next to the rules that produce them and are being
 * built there; a second, differently-computed copy of the same numbers here is
 * the copy that eventually disagrees.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  Dialog,
  Empty,
  Field,
  Problem,
  SortHeading,
  sortRows,
  useSort,
  useToasts,
  Toasts,
} from "../components/bits";
import type { Account, Household, Payee, PayeeCollision } from "../lib/types";

type PayeeSort = "name" | "kind";
type CollisionSort = "spellings" | "transactions";

export function Payees({ household }: { household: Household }) {
  const client = useQueryClient();
  const toasts = useToasts();
  const [adding, setAdding] = useState(false);
  const [merging, setMerging] = useState<Payee | null>(null);
  const [search, setSearch] = useState("");

  const payees = useQuery({
    queryKey: ["payees", household.id],
    queryFn: () => api.get<Payee[]>(`/households/${household.id}/payees`),
  });

  // Only to name the account a transfer payee stands for. A transfer payee is
  // not a shop; calling it one in a list of shops is how somebody comes to
  // merge "Transfer : Visa" into a supermarket.
  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts?include_closed=true`),
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["payees", household.id] });

  const create = useMutation({
    mutationFn: (name: string) =>
      api.post<Payee>(`/households/${household.id}/payees`, { name }),
    onSuccess: (made) => {
      setAdding(false);
      refresh();
      toasts.say(`${made.name} added`);
    },
  });

  const merge = useMutation({
    mutationFn: ({ from, into }: { from: Payee; into: string }) =>
      api.post<Payee>(`/payees/${from.id}/merge`, { into_payee_id: into }),
    onSuccess: (kept) => {
      setMerging(null);
      // The register and the rules both name payees, and a merge moved both.
      client.invalidateQueries();
      toasts.say(`Folded into ${kept.name}`);
    },
  });

  const order = useSort<PayeeSort>("name");
  const accountName = (id: string | null) =>
    (accounts.data ?? []).find((one) => one.id === id)?.name ?? "another account";

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const shown = (payees.data ?? []).filter(
      (one) => !needle || one.name.toLowerCase().includes(needle),
    );
    return sortRows(shown, order.sort, order.direction, (one, column) =>
      // Sort on the meaning: "kind" is what the row *is*, not the words in the
      // cell — transfers together, ordinary payees together, names within each.
      column === "kind" ? [one.transfer_account_id ? 1 : 0, one.name] : one.name,
    );
  }, [payees.data, search, order.sort, order.direction]);

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h1>Payees</h1>
        <button className="primary" onClick={() => setAdding(true)}>
          Add payee
        </button>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        Everyone {household.name} has paid or been paid by. Most arrive on their own, from a
        transaction or an import; two spellings of the same shop are merged here.
      </p>

      <SameName household={household} onMerged={(kept) => toasts.say(`Folded into ${kept}`)} />

      <div className="card">
        <Problem error={payees.error ?? create.error ?? merge.error} />

        <Field label="Find a payee">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="part of a name"
          />
        </Field>

        {payees.isLoading ? (
          <p className="muted">Loading…</p>
        ) : rows.length === 0 ? (
          <Empty>
            {payees.data?.length
              ? "No payee matches that."
              : "No payees yet. One appears the first time you name somebody on a transaction."}
          </Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label="Name"
                    column="name"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label="Kind"
                    column="kind"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  {/* Buttons, not a fact about the row: no sort. */}
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {rows.map((one) => (
                  <tr key={one.id}>
                    <td data-primary="true">{one.name}</td>
                    <td className="small muted" data-label="Kind" data-detail-first="true">
                      {one.transfer_account_id
                        ? `Transfer to ${accountName(one.transfer_account_id)}`
                        : "Payee"}
                    </td>
                    <td>
                      {/* A transfer payee is the other side of a transfer, kept
                          in step with an account. Merging one into a shop would
                          quietly re-point every transfer that used it. */}
                      {one.transfer_account_id ? null : (
                        <button onClick={() => setMerging(one)}>Merge…</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted small" style={{ marginBottom: 0 }}>
          {rows.length} of {payees.data?.length ?? 0} shown.
        </p>
      </div>

      {adding && (
        <AddPayee
          onClose={() => setAdding(false)}
          onAdd={(name) => create.mutate(name)}
          pending={create.isPending}
          error={create.error}
        />
      )}

      {merging && (
        <MergePayee
          payee={merging}
          others={(payees.data ?? []).filter(
            (one) => one.id !== merging.id && !one.transfer_account_id,
          )}
          onClose={() => setMerging(null)}
          onMerge={(into) => merge.mutate({ from: merging, into })}
          pending={merge.isPending}
          error={merge.error}
        />
      )}

      <Toasts items={toasts.items} onDone={toasts.dismiss} />
    </>
  );
}

/**
 * Payees that are now spellings of one name (#268).
 *
 * Accents, dash style and invisible spaces stopped telling payees apart, so
 * `Café Sol` and `CAFE SOL` are one name from now on. The ones made before
 * that are still two payees, and which one survives decides where their
 * transactions go — so they are listed here and merged only when somebody
 * says so, through the same merge as the button in the list below.
 *
 * Nothing is drawn while there is nothing to decide.
 */
function SameName({
  household,
  onMerged,
}: {
  household: Household;
  onMerged: (kept: string) => void;
}) {
  const client = useQueryClient();
  // The group's key, not the group: what the dialog shows is always the group
  // as the server last described it, so after a merge that failed half-way it
  // offers the spellings still there rather than one already folded away.
  const [reviewingKey, setReviewingKey] = useState<string | null>(null);
  const order = useSort<CollisionSort>("spellings");

  const groups = useQuery({
    queryKey: ["payee-collisions", household.id],
    queryFn: () => api.get<PayeeCollision[]>(`/households/${household.id}/payee-collisions`),
  });

  const merge = useMutation({
    // One existing merge per spelling folded away, in turn: each is its own act
    // in History, exactly as if it had been chosen from the list below.
    mutationFn: async ({ group, keep }: { group: PayeeCollision; keep: string }) => {
      let kept: Payee | null = null;
      for (const one of group.payees) {
        if (one.id === keep) continue;
        kept = await api.post<Payee>(`/payees/${one.id}/merge`, { into_payee_id: keep });
      }
      return kept;
    },
    onSuccess: (kept) => {
      setReviewingKey(null);
      client.invalidateQueries();
      if (kept) onMerged(kept.name);
    },
    // The merges before the one that failed did happen. Refetch, so the
    // group, the payee list and the register say so, and keep the error on
    // screen; a retry then only posts for the spellings that are left.
    onError: () => {
      client.invalidateQueries();
    },
  });

  const rows = useMemo(
    () =>
      sortRows(groups.data ?? [], order.sort, order.direction, (group, column) =>
        // Sort on the meaning: the shared name, not whichever spelling happens
        // to be drawn first; and how much the merge would move.
        column === "transactions" ? total(group) : group.key,
      ),
    [groups.data, order.sort, order.direction],
  );

  const reviewing = rows.find((group) => group.key === reviewingKey) ?? null;

  if (!rows.length && !merge.error) return null;

  return (
    <section className="card" aria-label="Spellings of one name">
      <h2 style={{ marginTop: 0 }}>Spellings of one name</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        These payees differ only in accents, dashes or spaces you cannot see, which no longer tell
        two names apart. Nothing has been merged: review each and choose the spelling to keep.
      </p>
      <Problem error={groups.error ?? (reviewing ? null : merge.error)} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <SortHeading
                label="Spellings"
                column="spellings"
                sort={order.sort}
                direction={order.direction}
                onSort={order.onSort}
              />
              <SortHeading
                label="Transactions"
                column="transactions"
                sort={order.sort}
                direction={order.direction}
                onSort={order.onSort}
                align="right"
              />
              {/* Buttons, not a fact about the row: no sort. */}
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((group) => (
              <tr key={group.key}>
                <td data-primary="true">{group.payees.map((one) => one.name).join(" · ")}</td>
                <td className="amount" data-label="Transactions">
                  {total(group)}
                </td>
                <td>
                  <button
                    onClick={() => {
                      merge.reset();
                      setReviewingKey(group.key);
                    }}
                  >
                    Review…
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {reviewing && (
        <MergeSpellings
          // A new set of spellings is a new choice: start it afresh.
          key={reviewing.payees.map((one) => one.id).join(" ")}
          group={reviewing}
          onClose={() => setReviewingKey(null)}
          onMerge={(keep) => merge.mutate({ group: reviewing, keep })}
          pending={merge.isPending}
          error={merge.error}
        />
      )}
    </section>
  );
}

function total(group: PayeeCollision): number {
  return group.payees.reduce((sum, one) => sum + one.transaction_count, 0);
}

/**
 * Choose which spelling stays. The one with the most transactions is offered
 * first, because it is the one fewest rows move away from — but it is only
 * offered: the merge waits for the button.
 */
function MergeSpellings({
  group,
  onClose,
  onMerge,
  pending,
  error,
}: {
  group: PayeeCollision;
  onClose: () => void;
  onMerge: (keep: string) => void;
  pending: boolean;
  error: unknown;
}) {
  const busiest = [...group.payees].sort(
    (a, b) => b.transaction_count - a.transaction_count || a.name.localeCompare(b.name),
  )[0];
  const [keep, setKeep] = useState(busiest?.id ?? "");
  const kept = group.payees.find((one) => one.id === keep);
  const others = group.payees.filter((one) => one.id !== keep);

  return (
    <Dialog title="Merge spellings of one name" onClose={onClose}>
      <Problem error={error} />
      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="small">Keep</legend>
        {group.payees.map((one) => (
          <label key={one.id} className="row" style={{ gap: 8 }}>
            <input
              type="radio"
              name="keep"
              value={one.id}
              checked={keep === one.id}
              onChange={() => setKeep(one.id)}
            />
            <span>
              {one.name}{" "}
              <span className="muted small">
                {one.transaction_count} transaction{one.transaction_count === 1 ? "" : "s"}
                {one.rule_count ? `, ${one.rule_count} rule${one.rule_count === 1 ? "" : "s"}` : ""}
              </span>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="muted small">
        {kept ? (
          <>
            Every transaction and every rule that names{" "}
            <strong>{others.map((one) => one.name).join(", ")}</strong> will name{" "}
            <strong>{kept.name}</strong> instead, and{" "}
            {others.length === 1 ? "that payee" : "those payees"} will be gone. Each merge is one
            act in History, so it can be undone there.
          </>
        ) : (
          <>The one you keep is the one you choose here.</>
        )}
      </p>
      <button className="primary" disabled={!kept || pending} onClick={() => onMerge(keep)}>
        Merge into {kept?.name ?? "…"}
      </button>
    </Dialog>
  );
}

function AddPayee({
  onClose,
  onAdd,
  pending,
  error,
}: {
  onClose: () => void;
  onAdd: (name: string) => void;
  pending: boolean;
  error: unknown;
}) {
  const [name, setName] = useState("");
  return (
    <Dialog title="Add a payee" onClose={onClose}>
      <Problem error={error} />
      <p className="muted small">
        Only worth doing ahead of time for one you are about to write a rule for. Naming somebody
        on a transaction creates them anyway.
      </p>
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={200} />
      </Field>
      <p />
      <button className="primary" disabled={!name.trim() || pending} onClick={() => onAdd(name.trim())}>
        Add
      </button>
    </Dialog>
  );
}

/**
 * Fold one payee into another.
 *
 * Says what it will do to the ledger before it does it, because the thing it
 * moves is every transaction that ever named the payee being folded away —
 * and afterwards that payee is gone.
 */
function MergePayee({
  payee,
  others,
  onClose,
  onMerge,
  pending,
  error,
}: {
  payee: Payee;
  others: Payee[];
  onClose: () => void;
  onMerge: (into: string) => void;
  pending: boolean;
  error: unknown;
}) {
  const [into, setInto] = useState("");
  const target = others.find((one) => one.id === into);

  return (
    <Dialog title={`Merge ${payee.name}`} onClose={onClose}>
      <Problem error={error} />
      <Field label="Into">
        <select value={into} onChange={(e) => setInto(e.target.value)} autoFocus>
          <option value="">Choose a payee…</option>
          {others.map((one) => (
            <option key={one.id} value={one.id}>
              {one.name}
            </option>
          ))}
        </select>
      </Field>
      <p className="muted small">
        {target ? (
          <>
            Every transaction and every rule that names <strong>{payee.name}</strong> will name{" "}
            <strong>{target.name}</strong> instead, and {payee.name} will be gone. It is one act in
            History, so it can be undone there.
          </>
        ) : (
          <>The one you keep is the one you choose here.</>
        )}
      </p>
      <button className="primary" disabled={!into || pending} onClick={() => onMerge(into)}>
        Merge
      </button>
    </Dialog>
  );
}
