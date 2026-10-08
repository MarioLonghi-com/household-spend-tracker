/**
 * The household's own page: what it holds, how it is set up, who is in it.
 *
 * This replaces the "Household settings" panel the nav used to open. A panel
 * was the wrong shape for it twice over: it is the one screen that wants to
 * show a lot of numbers at once, and a panel slides over the screen you were
 * reading rather than being somewhere you can go back to. There is no panel
 * version left — one place edits these fields, so two cannot disagree.
 *
 * The numbers come from `GET /households/{id}/stats`, counted in SQL. **None of
 * them is an amount of money.** The honest household-level figure would be a
 * net of assets against liabilities in each currency, and the Accounts screen
 * already gives a balance per account, which is the level where it means
 * something. `app/services/insights.py` has the long version of why nothing
 * here adds two currencies together.
 */

import { useId, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Field, Hint, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import type { Household, Member, Palette, User } from "../lib/types";
import { OneTimeImport } from "./OneTimeImport";
import { roleLabel } from "../lib/labels";

/** One currency this household keeps accounts in. Counts, never a figure. */
interface CurrencyRows {
  currency: string;
  accounts: number;
  transactions: number;
}

/** `HouseholdStatsOut` in `app/api/routers/households.py`. */
interface HouseholdStats {
  members: number;
  accounts: number;
  accounts_closed: number;
  transactions: number;
  transactions_uncleared: number;
  first_transaction: string | null;
  last_transaction: string | null;
  receipts: number;
  receipts_unattached: number;
  payees: number;
  payee_rules: number;
  payee_rules_enabled: number;
  categories: number;
  categories_archived: number;
  category_groups: number;
  reconciliations: number;
  currencies: CurrencyRows[];
  countries: { code: string; name: string; flag: string }[];
}

const count = (n: number) => n.toLocaleString();

export function HouseholdPage({
  household,
  user,
  onChanged,
}: {
  household: Household;
  user: User;
  /** The name and the colour are in the shell, so it has to hear about a save. */
  onChanged: () => void;
}) {
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h1>{household.name}</h1>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        One ledger: its accounts, its register, its receipts and the people who can reach them.
      </p>

      <Stats household={household} />
      <Settings household={household} onSaved={onChanged} />
      <People household={household} user={user} />
      <OneTimeImport household={household} />
    </>
  );
}

// --------------------------------------------------------------------------- //
// What it holds
// --------------------------------------------------------------------------- //

function Stats({ household }: { household: Household }) {
  const stats = useQuery({
    queryKey: ["household-stats", household.id],
    queryFn: () => api.get<HouseholdStats>(`/households/${household.id}/stats`),
  });

  if (stats.isLoading) return <div className="card muted">Counting…</div>;
  if (stats.isError)
    return (
      <div className="card">
        <Problem error={stats.error} />
      </div>
    );
  const it = stats.data!;

  return (
    <section className="card">
      <h2 className="section-title">What is in it</h2>

      <dl className="stat-grid">
        <Stat label="Accounts" value={count(it.accounts)}
          note={it.accounts_closed > 0 ? `${count(it.accounts_closed)} closed` : undefined} />
        <Stat label="Transactions" value={count(it.transactions)}
          note={
            it.transactions_uncleared > 0
              ? `${count(it.transactions_uncleared)} not yet cleared`
              : undefined
          } />
        <Stat label="Receipts" value={count(it.receipts)}
          note={
            it.receipts_unattached > 0
              ? `${count(it.receipts_unattached)} waiting to be attached`
              : undefined
          } />
        <Stat label="Payees" value={count(it.payees)} />
        <Stat label="Payee rules" value={count(it.payee_rules)}
          note={
            it.payee_rules > it.payee_rules_enabled
              ? `${count(it.payee_rules - it.payee_rules_enabled)} turned off`
              : undefined
          } />
        <Stat label="Categories" value={count(it.categories)}
          note={`in ${count(it.category_groups)} groups${
            it.categories_archived > 0 ? `, ${count(it.categories_archived)} archived` : ""
          }`} />
        <Stat label="Currencies" value={count(it.currencies.length)}
          note={it.currencies.map((one) => one.currency).join(" · ") || undefined} />
        <Stat label="Countries" value={count(it.countries.length)}
          note={it.countries.map((one) => `${one.flag} ${one.code}`).join(" ") || undefined} />
        <Stat label="People" value={count(it.members)} />
        <Stat label="Statements reconciled" value={count(it.reconciliations)} />
        {/* The span, as two figures rather than only as the sentence below.
            It was in the prose and nowhere else, which meant the one question
            this grid is scanned for -- how far back does this ledger go --
            was the one answer you had to read a paragraph to find. The
            sentence keeps it too: it is the place that says what it means
            when there is nothing in the register at all. */}
        <Stat
          label="Oldest transaction"
          value={it.first_transaction ?? "—"}
          note={it.first_transaction ? undefined : "nothing recorded yet"}
        />
        <Stat label="Newest transaction" value={it.last_transaction ?? "—"} />
      </dl>

      <p className="muted small" style={{ marginTop: 12, marginBottom: 0 }}>
        {it.first_transaction ? (
          <>
            The register runs from {it.first_transaction} to {it.last_transaction}.
          </>
        ) : (
          <>Nothing in the register yet.</>
        )}{" "}
        These are counts, not amounts: this ledger never converts one currency into another, so
        there is no such thing as a household total. Balances live on the Accounts screen, one per
        account.
      </p>

      {it.currencies.length > 0 && (
        <>
          <h3 className="section-title" style={{ marginTop: 18 }}>
            By currency
          </h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Currency</th>
                  <th className="amount">Accounts</th>
                  <th className="amount">Transactions</th>
                </tr>
              </thead>
              <tbody>
                {it.currencies.map((one) => (
                  <tr key={one.currency}>
                    <td data-primary="true" className="mono">
                      {one.currency}
                      {one.currency === household.base_currency ? (
                        <span className="muted small"> (main)</span>
                      ) : null}
                    </td>
                    <td className="amount" data-label="Accounts">
                      {count(one.accounts)}
                    </td>
                    <td className="amount" data-label="Transactions">
                      {count(one.transactions)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>
        <span className="stat-figure">{value}</span>
        {note ? <span className="muted small">{note}</span> : null}
      </dd>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// How it is set up -- every field the old panel had
// --------------------------------------------------------------------------- //

function Settings({ household, onSaved }: { household: Household; onSaved: () => void }) {
  const palettes = useQuery({
    queryKey: ["themes"],
    queryFn: () => api.get<Palette[]>("/themes"),
  });

  const [name, setName] = useState(household.name);
  const [currency, setCurrency] = useState(household.base_currency);
  const [note, setNote] = useState(household.note ?? "");
  const [theme, setTheme] = useState(household.theme);
  const [accent, setAccent] = useState(household.accent ?? "");
  const [keepOriginal, setKeepOriginal] = useState(household.receipts_keep_original);
  const forced = household.receipts_keep_original_forced;
  const stored = household.receipts_with_original;
  // Forced on means shown on: the card the server will actually honour is the
  // one that reads as chosen, whatever this household last saved.
  const keeping = forced || keepOriginal;
  const receiptsHeading = useId();

  const save = useMutation({
    mutationFn: () =>
      api.patch<Household>(`/households/${household.id}`, {
        name,
        base_currency: currency,
        note: note.trim() || null,
        clear_note: !note.trim(),
        theme,
        accent: accent.trim() || null,
        clear_accent: !accent.trim(),
        // Never sent while the operator has forced it on: the switch is
        // disabled in that case and a PATCH from a stale tab should not be the
        // one thing that can still change it.
        receipts_keep_original: forced ? null : keepOriginal,
      }),
    onSuccess: onSaved,
  });

  const chosen = (palettes.data ?? []).find((one) => one.key === theme);

  return (
    <section className="card config-card">
      <div className="config-badge">Settings</div>
      <Problem error={save.error} />

      <h2 className="section-title">Settings</h2>
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <p />
      <Field label="Main currency">
        <input
          value={currency}
          maxLength={3}
          onChange={(e) => setCurrency(e.target.value.toUpperCase())}
        />
      </Field>
      <p className="muted small">
        Only used for totals across accounts. Each account keeps its own, and nothing in the ledger
        is ever converted.
      </p>
      <Field label="Note">
        <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>

      <hr className="rule" />

      <h3 className="section-title">
        Colour
        <Hint label="why households have colours">
          <p>
            Two households open in one browser look identical, and that is how a statement gets
            posted into the wrong one. The colour is on every screen, needs no reading, and you
            notice it changed before you notice anything else.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            You can pick a palette and one accent of your own. You cannot reach the text and
            background colours, so no household can be made unreadable — and the accent is checked
            for contrast before it is saved.
          </p>
        </Hint>
      </h3>

      <div className="swatches">
        {(palettes.data ?? []).map((one) => (
          <button
            key={one.key}
            type="button"
            className={one.key === theme ? "swatch chosen" : "swatch"}
            aria-pressed={one.key === theme}
            onClick={() => setTheme(one.key)}
            title={one.label}
          >
            <span className="swatch-chips">
              <span style={{ background: one.light.accent }} />
              <span style={{ background: one.light.surface_2 }} />
              <span style={{ background: one.dark.paper }} />
            </span>
            {one.label}
          </button>
        ))}
      </div>

      <Field label="Your own accent (optional)">
        <div className="row" style={{ gap: 8 }}>
          <input
            type="color"
            aria-label="Pick an accent colour"
            value={accent || chosen?.light.accent || "#15705c"}
            onChange={(e) => setAccent(e.target.value)}
            style={{ width: 48, padding: 2, flex: "0 0 auto" }}
          />
          <input
            value={accent}
            placeholder={chosen?.light.accent ?? ""}
            onChange={(e) => setAccent(e.target.value)}
            className="mono"
          />
          {accent ? <button onClick={() => setAccent("")}>Use the palette's</button> : null}
        </div>
      </Field>
      <p className="muted small">
        Your hue, worn at the weight each mode needs — one colour cannot sit on both a white page
        and a dark one, so the brightness is worked out for you and the result is always readable.
      </p>

      <h3 id={receiptsHeading} className="section-title" style={{ marginTop: 18 }}>
        Receipts
      </h3>
      {/* Two cards rather than one checkbox (#162). A box labelled "keep the
          original as well" hid the thing worth knowing -- what it costs --
          until after it was ticked, and then explained the other answer in a
          banner longer than the choice. Here both answers carry their size
          before anyone picks, so the choice is made with the number in view.
          Native radios inside labels, so the keyboard and a screen reader get
          a real radio group without any help from us. */}
      <fieldset
        className="storage-choices"
        role="radiogroup"
        aria-labelledby={receiptsHeading}
      >
        <label className={keeping ? "storage-choice" : "storage-choice chosen"}>
          <input
            type="radio"
            name="receipts-storage"
            checked={!keeping}
            disabled={forced}
            onChange={() => setKeepOriginal(false)}
          />
          <span>
            <strong>
              Readable copies only <span className="pill storage-recommended">Recommended</span>
            </strong>
            <span className="small muted">
              A screen copy and a thumbnail. About <strong>33&nbsp;KB</strong> per receipt.
            </span>
          </span>
        </label>
        <label className={keeping ? "storage-choice chosen" : "storage-choice"}>
          <input
            type="radio"
            name="receipts-storage"
            checked={keeping}
            disabled={forced}
            onChange={() => setKeepOriginal(true)}
          />
          <span>
            <strong>Also keep the original file</strong>
            <span className="small muted">
              The exact file your camera or scanner produced, as well. About{" "}
              <strong>2&nbsp;MB</strong> per receipt, roughly 70&times; the space.
            </span>
          </span>
        </label>
      </fieldset>
      {forced ? (
        <p className="muted small">
          Turned on for every household by whoever runs this server, so it cannot be changed
          here.
        </p>
      ) : keeping ? (
        /* The sizes are measured, not estimated -- see issue #62. One short
           warning, only for the heavy answer: the cost is already on the card,
           so this says what it means for the database and what it leaves alone. */
        <div className="banner warn" role="status">
          <p className="small" style={{ margin: 0 }}>
            Every new receipt will take about <strong>70&times; more space</strong> in the
            database. Only worth it if you need the exact file. Existing receipts are not
            changed.
          </p>
        </div>
      ) : stored > 0 ? (
        <p className="muted small">
          The {stored.toLocaleString()} receipt{stored === 1 ? "" : "s"} that already{" "}
          {stored === 1 ? "has" : "have"} an original {stored === 1 ? "keeps" : "keep"} it &mdash;
          nothing here deletes anything.
        </p>
      ) : null}

      <button
        className="primary"
        style={{ marginTop: 12 }}
        disabled={save.isPending || !name.trim()}
        onClick={() => save.mutate()}
      >
        Save
      </button>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Who is in it
// --------------------------------------------------------------------------- //

function People({ household, user }: { household: Household; user: User }) {
  const members = useQuery({
    queryKey: ["members", household.id],
    queryFn: () => api.get<Member[]>(`/households/${household.id}/members`),
  });
  const order = useSort<"name" | "role" | "logged">("name");
  const people = useMemo(
    () =>
      sortRows(members.data ?? [], order.sort, order.direction, (member, column) => {
        if (column === "role") return member.role;
        if (column === "logged") return member.transactions_logged;
        return member.display_name;
      }),
    [members.data, order.sort, order.direction],
  );

  return (
    <section className="card">
      <h2 className="section-title">Who is in it</h2>
      <Problem error={members.error} />
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
                label="Role"
                column="role"
                sort={order.sort}
                direction={order.direction}
                onSort={order.onSort}
              />
              {/* Counted from the audit log, which is the only thing that
                  knows: `transactions` has no `created_by` column and is not
                  getting one. A key's work is counted under the person whose
                  key it is, because that is exactly what a key borrows. */}
              <SortHeading
                label="Transactions"
                column="logged"
                sort={order.sort}
                direction={order.direction}
                onSort={order.onSort}
                align="right"
              />
            </tr>
          </thead>
          <tbody>
            {people.map((member) => (
              <tr key={member.user_id}>
                <td data-primary="true">
                  {member.display_name}
                  {member.user_id === user.id ? <span className="muted"> (you)</span> : null}
                  <div className="small muted">{member.email}</div>
                </td>
                <td className="small muted" data-label="Role">
                  {roleLabel(member.role)}
                </td>
                <td className="amount" data-label="Transactions">
                  {count(member.transactions_logged)}
                  {member.transactions_by_agent > 0 ? (
                    <div className="small muted">
                      {count(member.transactions_by_agent)} by an agent
                    </div>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small" style={{ marginTop: 14, marginBottom: 0 }}>
        Everyone in a household can do everything in it. Only the owner can change who is in it,
        from the Admin screen. The count is transactions still in the register that each person
        entered — by hand, by import, or by an agent key of theirs — read from the History, so a
        row somebody has since deleted is not counted against anyone.
      </p>
    </section>
  );
}
