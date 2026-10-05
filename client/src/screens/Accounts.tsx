/** Accounts, and the config panel for setting one up. */

import { useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { format, parse } from "../lib/money";
import {
  Empty,
  Field,
  Hint,
  Money,
  moneyKey,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
} from "../components/bits";
import { AccountImport } from "./AccountImport";
import { Reconcile } from "./Reconcile";
import type {
  Account,
  AccountIdentifier,
  AccountType,
  Country,
  Household,
  IdentifierKind,
  IdentifierSuggestion,
  IdentifierSuggestions,
} from "../lib/types";

export const TYPES: { value: AccountType; label: string; owed: boolean; blurb: string }[] = [
  {
    value: "checking",
    label: "Current account",
    owed: false,
    blurb: "Day-to-day money at a bank. Salary in, card and direct debits out.",
  },
  {
    value: "savings",
    label: "Savings",
    owed: false,
    blurb: "Money set aside at a bank. Same as a current account, kept apart so the register reads clearly.",
  },
  {
    value: "cash",
    label: "Cash",
    owed: false,
    blurb: "Notes and coins in a wallet or a tin. Nothing imports into it — you enter what you spend.",
  },
  {
    value: "credit_card",
    label: "Credit card",
    owed: true,
    blurb: "Money you owe the card issuer. Spending makes the balance more negative; paying the bill is a transfer from the account that pays it.",
  },
  {
    value: "other_asset",
    label: "Other asset",
    owed: false,
    blurb: "Something you own that holds value and that you want in the totals — a deposit held by a landlord, an investment you track by hand.",
  },
  {
    value: "other_liability",
    label: "Other debt",
    owed: true,
    blurb: "Money you owe that isn't a card — a mortgage, a car loan, money owed to a person. Repayments are transfers into it.",
  },
];

/** What the six types mean, as one bubble. */
export function TypeHelp() {
  return (
    <Hint label="account types">
      <p>
        The type says what kind of thing the account is. It sorts the list and marks which balances
        are money you <em>owe</em> rather than money you have.
      </p>
      <dl>
        {TYPES.map((one) => (
          <div key={one.value} style={{ display: "contents" }}>
            <dt>
              {one.label}
              {one.owed ? <span className="neg"> ▾</span> : null}
            </dt>
            <dd>{one.blurb}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small" style={{ marginTop: 10, marginBottom: 0 }}>
        <span className="neg">▾</span> marks the two whose balance is normally negative — you owe
        it. Nothing else changes: every account holds transactions the same way, and no arithmetic
        depends on the type. Pick the one that describes it, because it can't be changed afterwards.
      </p>
    </Hint>
  );
}

/** The country list, fetched once and then kept.
 *
 * It comes from the server rather than the bundle so the list the picker
 * offers and the list the server will accept cannot drift apart. It is 249
 * rows of two strings and it never changes, so it is fetched once per session
 * and never refetched.
 */
export function useCountries() {
  return useQuery({
    queryKey: ["countries"],
    queryFn: () => api.get<Country[]>("/countries"),
    staleTime: Infinity,
    gcTime: Infinity,
  });
}

/** What to call a code in a tooltip, before the list has arrived or when unset. */
export function countryName(list: Country[] | undefined, code: string | null): string {
  if (!code) return "no country set";
  return list?.find((one) => one.code === code)?.name ?? code;
}

/** Accent- and case-insensitive, so "curacao" finds Curaçao. */
function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toLowerCase();
}

const MAX_SUGGESTIONS = 8;

/** Prefix matches first, then anything containing it. The code counts as a name. */
function search(list: Country[], typed: string): Country[] {
  const needle = fold(typed);
  if (!needle) return list.slice(0, MAX_SUGGESTIONS);

  const starts: Country[] = [];
  const contains: Country[] = [];
  for (const one of list) {
    const name = fold(one.name);
    if (name.startsWith(needle) || fold(one.code) === needle) starts.push(one);
    else if (name.includes(needle)) contains.push(one);
  }
  return [...starts, ...contains].slice(0, MAX_SUGGESTIONS);
}

/**
 * A country, typed for, never invented.
 *
 * A `<select>` of 249 rows is a scroll, and the native type-ahead on one only
 * matches from the first letter — so finding Spain meant typing "S" nine
 * times. This filters on any part of the name as you type, but it is a *closed*
 * list: the text box is a search field, not a value. Whatever is in it when
 * focus leaves is resolved back to a real country or thrown away, so no
 * hand-typed string can reach the server. The shared `Combobox` is deliberately
 * not reused — it exists to let you type a payee that does not exist yet, which
 * is the exact opposite of what a country needs.
 *
 * Empty is a real answer: most accounts will never say which country they are
 * in, and "not set" has to stay reachable once one has been set by mistake.
 */
function CountryPicker({ value, onChange }: { value: string; onChange: (code: string) => void }) {
  const countries = useCountries();
  const list = useMemo(() => countries.data ?? [], [countries.data]);
  const chosen = useMemo(() => list.find((one) => one.code === value) ?? null, [list, value]);
  const label = chosen ? `${chosen.flag} ${chosen.name}` : "";

  const [typed, setTyped] = useState<string | null>(null);
  const [active, setActive] = useState(-1);
  const holder = useRef<HTMLSpanElement>(null);
  const listId = useId();

  // Null means "not searching" — the field is showing the chosen country.
  const searching = typed !== null;
  const matches = useMemo(() => (searching ? search(list, typed) : []), [searching, list, typed]);
  const showing = searching && matches.length > 0;

  const take = (country: Country | null) => {
    onChange(country?.code ?? "");
    setTyped(null);
    setActive(-1);
  };

  /** What the text in the box means, once focus leaves it. */
  function settle() {
    if (typed === null) return;
    const needle = fold(typed);
    if (!needle) {
      take(null); // emptied on purpose
      return;
    }
    const exact = list.find((one) => fold(one.name) === needle || fold(one.code) === needle);
    if (exact) {
      take(exact);
      return;
    }
    if (matches.length === 1) {
      take(matches[0]);
      return;
    }
    // Nothing, or too many. Fall back to what was already there rather than
    // keeping half-typed text that stands for no country at all.
    setTyped(null);
    setActive(-1);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!searching) setTyped("");
      else if (matches.length) setActive((at) => (at + 1) % matches.length);
      return;
    }
    if (event.key === "ArrowUp") {
      if (!showing) return;
      event.preventDefault();
      setActive((at) => (at <= 0 ? matches.length - 1 : at - 1));
      return;
    }
    if (event.key === "Escape" && searching) {
      event.preventDefault();
      event.stopPropagation(); // the panel closes on Escape too; one key, one thing
      setTyped(null);
      setActive(-1);
      return;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      if (showing && active >= 0) {
        if (event.key === "Enter") event.preventDefault();
        event.stopPropagation();
        take(matches[active]);
        return;
      }
      if (event.key === "Enter" && searching) {
        event.preventDefault();
        event.stopPropagation();
        settle();
      }
    }
  }

  return (
    <span className="combo" ref={holder}>
      <input
        type="text"
        role="combobox"
        aria-expanded={showing}
        aria-controls={showing ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={showing && active >= 0 ? `${listId}-${active}` : undefined}
        aria-label="Country"
        autoComplete="off"
        spellCheck={false}
        placeholder="type to search — leave empty for none"
        value={searching ? typed : label}
        onChange={(event) => {
          setTyped(event.target.value);
          setActive(-1);
        }}
        // Typing replaces the chosen country rather than appending to its name,
        // which is what makes "click, type spa, Enter" work.
        onFocus={(event) => event.target.select()}
        onBlur={(event) => {
          if (holder.current?.contains(event.relatedTarget as Node | null)) return;
          settle();
        }}
        onKeyDown={onKeyDown}
      />
      {showing && (
        <ul className="combo-list" id={listId} role="listbox">
          {matches.map((one, index) => (
            <li
              key={one.code}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "combo-option active" : "combo-option"}
              // mousedown, not click: click lands after blur has shut the list.
              onMouseDown={(event) => {
                event.preventDefault();
                take(one);
              }}
              onMouseEnter={() => setActive(index)}
            >
              <span className="flag">{one.flag}</span> {one.name}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

/**
 * What the accounts list can be ordered by.
 *
 * `order` is not a column: it is the household's own arrangement, which is what
 * the server sends and what the list opens on. Sorting by a heading takes over
 * from it; nothing lights up until one does.
 */
type AccountSort =
  | "order"
  | "name"
  | "type"
  | "institution"
  | "country"
  | "currency"
  | "oldest"
  | "newest"
  | "rows"
  | "cleared"
  | "balance";

export function typeLabel(type: AccountType): string {
  return TYPES.find((one) => one.value === type)?.label ?? type;
}

export function Accounts({ household }: { household: Household }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<Account | null>(null);
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [reconciling, setReconciling] = useState<Account | null>(null);
  const [showClosed, setShowClosed] = useState(false);
  const { sort, direction, onSort } = useSort<AccountSort>("order");
  const countries = useCountries();

  // Settings are only reachable from this list, so without the toggle closing
  // an account is a one-way door: it disappears and nothing can reopen it.
  const accounts = useQuery({
    queryKey: ["accounts", household.id, showClosed],
    queryFn: () =>
      api.get<Account[]>(
        `/households/${household.id}/accounts${showClosed ? "?include_closed=true" : ""}`,
      ),
  });

  const rows = useMemo(
    () =>
      sortRows(
        accounts.data ?? [],
        sort,
        direction,
        (account, column) => {
          switch (column) {
            case "name":
              return account.name;
            case "type":
              return typeLabel(account.type);
            case "institution":
              return account.institution;
            // By name, not by flag or code: a pair of regional indicators has
            // no order anyone reads, and the code files Spain under E.
            case "country":
              return account.country ? countryName(countries.data, account.country) : null;
            case "currency":
              return account.currency;
            // ISO dates, so lexical order is chronological order and no Date is
            // constructed to compare two of them. An account with nothing in it
            // sorts last either way -- `sortRows` puts blanks at the bottom in
            // both directions, which is what an account with no span deserves.
            case "oldest":
              return account.oldest_transaction;
            case "newest":
              return account.newest_transaction;
            case "rows":
              return account.transaction_count;
            // Currency first, then the figure. Minor units of EUR and GBP are
            // not comparable quantities, and interleaving them would present
            // them as if they were -- the mistake three of the old build's
            // reports made by adding them. Within a currency they rank properly;
            // the groups stay base currency first whichever way it points (#129).
            case "cleared":
              return moneyKey(account.currency, account.cleared, household.base_currency);
            case "balance":
              return moneyKey(account.currency, account.balance, household.base_currency);
            // "order" is the household's own arrangement: nothing to compare,
            // so every row ties and the tiebreak leaves them as they came.
            default:
              return null;
          }
        },
        // Equal cells keep a fixed order rather than shuffling between
        // renders -- except under "order", where every row ties and the
        // household's own arrangement is what must survive untouched.
        (a, b) => (sort === "order" ? 0 : a.name.localeCompare(b.name)),
      ),
    [accounts.data, sort, direction, countries.data, household.base_currency],
  );

  const refresh = () => client.invalidateQueries({ queryKey: ["accounts", household.id] });

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 16 }}>
        <h1>Accounts</h1>
        <label className="small muted" style={{ flex: "0 0 auto" }}>
          <input
            type="checkbox"
            checked={showClosed}
            onChange={(e) => setShowClosed(e.target.checked)}
            style={{ width: "auto", marginRight: 6 }}
          />
          Show closed
        </label>
        <div className="row" style={{ flex: "0 0 auto" }}>
          <button onClick={() => setImporting(true)}>Import from a file</button>
          <button className="primary" onClick={() => setAdding(true)}>
            Add an account
          </button>
        </div>
      </div>

      <Problem error={accounts.error} />

      <div className="card">
        {accounts.data?.length === 0 ? (
          <Empty>No accounts yet. Add the one you use most and import a statement into it.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {/* Every column sorts. The one exception is the actions cell,
                      which holds buttons rather than a fact about the row. */}
                  <SortHeading
                    label="Account"
                    column="name"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label="Type"
                    column="type"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label="Bank"
                    column="institution"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label="Country"
                    column="country"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="flag-col"
                  />
                  <SortHeading
                    label="Currency"
                    column="currency"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  {/* What the account has actually seen. Three facts about
                      the rows rather than about the money in them, which is
                      why they sit between the account's description and its
                      figures: an account you are about to reconcile is one you
                      want the span of, and an account with nothing in it since
                      March is the one nobody has imported. */}
                  <SortHeading
                    label="Oldest"
                    column="oldest"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="span-col"
                  />
                  <SortHeading
                    label="Newest"
                    column="newest"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="span-col"
                  />
                  <SortHeading
                    label="Rows"
                    column="rows"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                  <SortHeading
                    label="Cleared"
                    column="cleared"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                  <SortHeading
                    label="Balance"
                    column="balance"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                  <th className="amount row-actions" />
                </tr>
              </thead>
              <tbody>
                {rows.map((account) => (
                  <tr key={account.id}>
                    {/* `data-primary` / `data-figure` make the first line of
                        the card on a phone; `data-label` names the rest. The
                        stylesheet does the shape -- see "Tables on a phone". */}
                    <td data-primary="true">
                      {account.name}
                      {/* A closed account is still listed, still carries a
                          balance, and reads exactly like a live one at a
                          glance. The tag has to be loud enough that nobody
                          reconciles against an account that is gone. */}
                      {account.closed ? (
                        <span className="tag closed" title="this account is closed">
                          Closed
                        </span>
                      ) : null}
                    </td>
                    <td className="small muted" data-label="Type" data-detail-first="true">
                      {TYPES.find((t) => t.value === account.type)?.label ?? account.type}
                      {account.is_liability ? (
                        <span className="neg" title="a negative balance here is money you owe">
                          {" "}
                          ▾
                        </span>
                      ) : null}
                    </td>
                    {/* Who holds it. It was settable and shown nowhere, so
                        two accounts called "Current" gave no way to tell which
                        bank each was at — which is exactly when you need it. */}
                    <td
                      className="small muted"
                      data-label="Bank"
                      data-empty={account.institution ? undefined : "true"}
                    >
                      {account.institution || <span className="muted">—</span>}
                    </td>
                    {/* The flag alone: at this width a name would push the
                        figures off, and the flag is legible at a glance in a
                        way a two-letter code is not. An account that never
                        said carries the UN's, so the column never has a hole
                        in it that reads as a rendering fault. */}
                    <td
                      className="flag-col"
                      data-label="Country"
                      data-empty={account.country ? undefined : "true"}
                    >
                      <span className="flag" title={countryName(countries.data, account.country)}>
                        {account.flag}
                      </span>
                    </td>
                    {/* Its own column: the ledger never converts, so which
                        currency a figure is in is a fact about the row, not a
                        footnote under the name. */}
                    <td className="small muted mono" data-label="Currency">
                      {account.currency}
                    </td>
                    {/* An em dash, not an empty cell: a blank here reads as a
                        column that failed to render, and "this account has no
                        transactions" is a fact worth stating. */}
                    <td
                      className="small muted mono span-col"
                      data-label="Oldest"
                      data-empty={account.oldest_transaction ? undefined : "true"}
                    >
                      {account.oldest_transaction ?? "—"}
                    </td>
                    <td
                      className="small muted mono span-col"
                      data-label="Newest"
                      data-empty={account.newest_transaction ? undefined : "true"}
                    >
                      {account.newest_transaction ?? "—"}
                    </td>
                    <td className="amount muted" data-label="Rows">
                      {account.transaction_count.toLocaleString()}
                    </td>
                    <td className="amount muted" data-label="Cleared">
                      {format(account.cleared, account.currency)}
                    </td>
                    <td className="amount" data-figure="true">
                      <Money minor={account.balance} currency={account.currency} />
                    </td>
                    <td className="amount row-actions">
                      {/* Reconciling is a thing you do *to* an account, so it
                          starts from the account, not from a screen of its own
                          that would then have to ask which one you meant. */}
                      <button className="link" onClick={() => setReconciling(account)}>
                        Reconcile
                      </button>{" "}
                      <button className="link" onClick={() => setEditing(account)}>
                        Settings
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <SuggestedIdentifiers household={household} accounts={accounts.data ?? []} />
      </div>

      <div className="card">
        <Identifiers household={household} account={null} />
      </div>

      {adding && (
        <AccountForm
          household={household}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            refresh();
          }}
        />
      )}
      {importing && (
        <AccountImport
          household={household}
          onClose={() => setImporting(false)}
          onImported={() => {
            setImporting(false);
            refresh();
          }}
        />
      )}
      {reconciling && (
        <Reconcile
          account={reconciling}
          onClose={() => setReconciling(null)}
          onDone={() => {
            setReconciling(null);
            refresh();
          }}
        />
      )}
      {editing && (
        <AccountSettings
          household={household}
          account={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
    </>
  );
}

// The API's own limits (`Institution` and `Note` in app/schemas.py), so a long
// paste stops at the field rather than coming back as a 422. Both panels ask
// for these in the same order: country, then bank, then note.
const INSTITUTION_MAX = 120;
const NOTE_MAX = 2000;

function AccountForm({
  household,
  onClose,
  onSaved,
}: {
  household: Household;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<AccountType>("checking");
  const [currency, setCurrency] = useState(household.base_currency);
  const [country, setCountry] = useState("");
  const [institution, setInstitution] = useState("");
  const [note, setNote] = useState("");
  const [opening, setOpening] = useState("");
  const [openingDate, setOpeningDate] = useState(() => new Date().toISOString().slice(0, 10));

  // Blank means zero; anything else has to parse, or the account would be
  // created with a balance the typist did not intend.
  const openingMinor = opening.trim() === "" ? 0 : parse(opening, currency);
  const openingBad = openingMinor === null;

  const save = useMutation({
    mutationFn: () =>
      api.post<Account>(`/households/${household.id}/accounts`, {
        name,
        type,
        currency,
        country: country || null,
        institution: institution.trim() || null,
        note: note.trim() || null,
        opening_balance: openingMinor ?? 0,
        opening_date: openingDate,
      }),
    onSuccess: onSaved,
  });

  return (
    <Panel title="New account" onClose={onClose} config>
      <Problem error={save.error} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label="Type" hint={<TypeHelp />}>
        <select value={type} onChange={(e) => setType(e.target.value as AccountType)}>
          {TYPES.map((one) => (
            <option key={one.value} value={one.value}>
              {one.label}
            </option>
          ))}
        </select>
      </Field>
      <p className="muted small" style={{ marginTop: 4 }}>
        {TYPES.find((one) => one.value === type)?.blurb}
      </p>
      <Field label="Currency">
        <input
          value={currency}
          onChange={(e) => setCurrency(e.target.value.toUpperCase())}
          maxLength={3}
        />
      </Field>
      <p className="muted small">
        Each account keeps its own currency. Nothing is ever converted in the ledger.
      </p>

      <Field label="Country">
        <CountryPicker value={country} onChange={setCountry} />
      </Field>
      <p className="muted small">
        Where the account is held. Optional, and separate from the currency — a euro account can
        sit in any number of countries.
      </p>
      <Field label="Bank or institution">
        <input
          value={institution}
          onChange={(e) => setInstitution(e.target.value)}
          maxLength={INSTITUTION_MAX}
        />
      </Field>
      <p />
      <Field label="Note">
        <textarea
          value={note}
          rows={3}
          onChange={(e) => setNote(e.target.value)}
          maxLength={NOTE_MAX}
        />
      </Field>
      <p />

      <Field
        label="Opening balance"
        hint={
          <Hint label="opening balance">
            <p>
              What was in the account on the day you started tracking it — usually a date in the
              past, the one your first statement opens with.
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              It is recorded as a real transaction on that date, not a hidden number, so it shows in
              the register and can be corrected like anything else. For a card or a loan, type what
              you owe as a negative.
            </p>
          </Hint>
        }
      >
        <input
          value={opening}
          onChange={(e) => setOpening(e.target.value)}
          inputMode="decimal"
          placeholder="0.00"
        />
      </Field>
      {openingBad ? (
        <p className="small neg">That isn't an amount in {currency}.</p>
      ) : (
        <p className="muted small">
          {openingMinor === 0
            ? "Leave it blank for an account that starts empty."
            : `Recorded as ${format(openingMinor, currency)} on the date below.`}
        </p>
      )}

      <Field label="Opening date">
        <input
          type="date"
          value={openingDate}
          onChange={(e) => setOpeningDate(e.target.value)}
        />
      </Field>
      <p />

      <button
        className="primary"
        disabled={!name.trim() || openingBad || !openingDate || save.isPending}
        onClick={() => save.mutate()}
      >
        Create
      </button>
    </Panel>
  );
}

function AccountSettings({
  household,
  account,
  onClose,
  onSaved,
}: {
  household: Household;
  account: Account;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(account.name);
  const [institution, setInstitution] = useState(account.institution ?? "");
  const [country, setCountry] = useState(account.country ?? "");
  const [note, setNote] = useState(account.note ?? "");
  const [closed, setClosed] = useState(account.closed);
  const [product, setProduct] = useState(account.statement_product ?? "");

  const save = useMutation({
    mutationFn: () =>
      api.patch<Account>(`/accounts/${account.id}`, {
        name,
        institution,
        note,
        closed,
        // Null means "leave it alone" on a PATCH, so clearing a country that
        // was set needs to say so explicitly rather than send nothing.
        country: country || null,
        clear_country: country === "" && account.country !== null,
        statement_product: product.trim() || null,
        clear_statement_product: product.trim() === "" && account.statement_product !== null,
      }),
    onSuccess: onSaved,
  });

  return (
    <Panel title={account.name} onClose={onClose} config>
      <Problem error={save.error} />
      <Field label="Name">
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label="Country">
        <CountryPicker value={country} onChange={setCountry} />
      </Field>
      <p />
      <Field label="Bank or institution">
        <input
          value={institution}
          onChange={(e) => setInstitution(e.target.value)}
          maxLength={INSTITUTION_MAX}
        />
      </Field>
      <p />
      <Field label="Note">
        <textarea
          value={note}
          rows={3}
          onChange={(e) => setNote(e.target.value)}
          maxLength={NOTE_MAX}
        />
      </Field>
      <p />
      <Field label="Statement product">
        <input
          value={product}
          placeholder={account.type === "checking" ? "Current" : "e.g. Savings"}
          onChange={(e) => setProduct(e.target.value)}
        />
      </Field>
      <p className="small muted" style={{ marginTop: 4 }}>
        Only for a bank whose statement holds several accounts in one file, told apart by a
        Product column (Revolut's account statement). This account takes the rows with this
        value; the others are skipped. A current account takes the Current rows unless you
        say otherwise.
      </p>
      <label className="small">
        <input
          type="checkbox"
          checked={closed}
          onChange={(e) => setClosed(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        Closed — hide it from the accounts list
      </label>
      <p className="muted small">
        The type and currency are fixed once an account exists, because every transaction on it is
        recorded in that currency.
      </p>
      <button className="primary" disabled={save.isPending} onClick={() => save.mutate()}>
        Save
      </button>
      <hr />
      <Identifiers household={household} account={account} />
    </Panel>
  );
}

const IDENTIFIER_KINDS: { value: IdentifierKind; label: string; example: string }[] = [
  { value: "iban", label: "IBAN", example: "GB82 WEST 1234 5698 7654 32" },
  { value: "number", label: "Account number", example: "the number other statements quote" },
  { value: "card", label: "Card number", example: "the last four, or the part the bank prints" },
  { value: "alias", label: "Name the bank uses", example: "a pocket's or a card product's name" },
  { value: "file_tag", label: "Tag in the download's file name", example: "e.g. the code after _en_" },
];

function kindLabel(kind: IdentifierKind): string {
  if (kind === "holder") return "Holder's name";
  return IDENTIFIER_KINDS.find((one) => one.value === kind)?.label ?? kind;
}

/** An IBAN or a long number written into the account's own name. */
function suggestedFromName(name: string): { kind: IdentifierKind; value: string } | null {
  const compact = name.replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  const iban = compact.match(/[A-Z]{2}\d{2}[A-Z0-9]{11,30}/);
  if (iban) return { kind: "iban", value: iban[0] };
  const digits = name.match(/\d{6,}/);
  if (digits) return { kind: "number", value: digits[0] };
  return null;
}

/**
 * What banks call this account -- or, with no account, how they spell the
 * household's members. Issue #66: these are what let a statement find its own
 * account on the Import screen and a transfer find the account on its other side.
 */
export function Identifiers({
  household,
  account,
}: {
  household: Household;
  account: Account | null;
}) {
  const client = useQueryClient();
  const [kind, setKind] = useState<IdentifierKind>(account ? "number" : "holder");
  const [value, setValue] = useState("");
  const key = ["identifiers", household.id];
  const all = useQuery({
    queryKey: key,
    queryFn: () => api.get<AccountIdentifier[]>(`/households/${household.id}/identifiers`),
  });
  const mine = (all.data ?? []).filter((row) =>
    account ? row.account_id === account.id : row.kind === "holder",
  );
  const add = useMutation({
    mutationFn: (body: { kind: IdentifierKind; value: string }) =>
      api.post<AccountIdentifier>(`/households/${household.id}/identifiers`, {
        ...body,
        account_id: account?.id ?? null,
      }),
    onSuccess: () => {
      setValue("");
      client.invalidateQueries({ queryKey: key });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/households/${household.id}/identifiers/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  });
  const suggestion = account ? suggestedFromName(account.name) : null;
  const suggested =
    suggestion &&
    !(all.data ?? []).some(
      (row) => row.value.replace(/[^0-9A-Za-z]/g, "").toUpperCase() === suggestion.value,
    )
      ? suggestion
      : null;

  return (
    <section aria-label={account ? "What banks call it" : "How banks write our names"}>
      <h3 style={{ marginBottom: 4 }}>
        {account ? "What banks call it" : "How banks write our names"}
      </h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        {account
          ? "Numbers and names that statements use for this account. A file carrying one is matched to this account on the Import screen, and a transfer naming one is matched to it."
          : "Each spelling a bank uses for someone in this household. A payment naming one is your own money moving, not income or spending."}
      </p>
      <Problem error={add.error ?? remove.error} />
      {mine.length > 0 && (
        <ul className="plain-list small">
          {mine.map((row) => (
            <li key={row.id}>
              <span className="muted">{kindLabel(row.kind)}:</span>{" "}
              <span className="mono">{row.value}</span>{" "}
              <button
                className="link"
                disabled={remove.isPending}
                onClick={() => remove.mutate(row.id)}
                aria-label={`Remove ${row.value}`}
              >
                Remove
              </button>
              {!account && <HolderSamples household={household} identifierId={row.id} />}
            </li>
          ))}
        </ul>
      )}
      {suggested && (
        <p className="small">
          The name looks like its {suggested.kind === "iban" ? "IBAN" : "number"}.{" "}
          <button className="link" onClick={() => add.mutate(suggested)}>
            Add <span className="mono">{suggested.value}</span>
          </button>
        </p>
      )}
      <div className="row">
        {account && (
          <Field label="Kind">
            <select value={kind} onChange={(e) => setKind(e.target.value as IdentifierKind)}>
              {IDENTIFIER_KINDS.map((one) => (
                <option key={one.value} value={one.value}>
                  {one.label}
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field label={account ? "Value" : "Name as the bank writes it"}>
          <input
            value={value}
            placeholder={
              account
                ? IDENTIFIER_KINDS.find((one) => one.value === kind)?.example
                : "DOE JANE"
            }
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
      </div>
      <button
        disabled={!value.trim() || add.isPending}
        onClick={() => add.mutate({ kind: account ? kind : "holder", value })}
      >
        Add
      </button>
    </section>
  );
}

/** What one holder's name matches in the ledger. Issue #132. */
interface HolderSample {
  identifier_id: string;
  rows: number;
  samples: string[];
}

/**
 * The bank text a holder's name catches, under the name. Names match in any
 * order, with middle names dropped, as initials and cut off at the end -- so a
 * person has to be able to see what that catches.
 */
function HolderSamples({
  household,
  identifierId,
}: {
  household: Household;
  identifierId: string;
}) {
  // One request for every holder: react-query shares it between the rows, and
  // keying it under "identifiers" means adding or removing a name refreshes it.
  const samples = useQuery({
    queryKey: ["identifiers", household.id, "holder-samples"],
    queryFn: () =>
      api.get<HolderSample[]>(`/households/${household.id}/identifiers/holder-samples`),
  });
  const mine = samples.data?.find((one) => one.identifier_id === identifierId);
  if (!mine) return null;
  if (mine.rows === 0) {
    return <div className="muted">Matches nothing in the register yet.</div>;
  }
  return (
    <div className="muted">
      Matches {mine.rows} {mine.rows === 1 ? "row" : "rows"}, such as:
      <ul className="plain-list" style={{ marginLeft: 12 }}>
        {mine.samples.map((text) => (
          <li key={text} className="mono">
            {text}
          </li>
        ))}
      </ul>
    </div>
  );
}

type SuggestionSort = "value" | "kind" | "account" | "mentions" | "would_link" | "why";

/**
 * The suggestions query, shared with the Transfers screen's summary line.
 *
 * `counted` asks for the transfer pairs each would link as well. That runs the
 * matcher once per suggestion, so the list is fetched without it and the counts
 * follow in their own request (#232).
 */
export function useIdentifierSuggestions(household: Household, counted = false) {
  return useQuery({
    // Under "identifiers", so adding or removing one refreshes what is offered.
    queryKey: ["identifiers", household.id, "suggestions", counted ? "counted" : "listed"],
    queryFn: () =>
      api.get<IdentifierSuggestions>(
        `/households/${household.id}/identifiers/suggestions${counted ? "?count_pairs=1" : ""}`,
      ),
  });
}

/**
 * Identifiers the ledger already names and nobody has added (#130): a pocket's
 * quoted name, an A/C number, a card number, a statement file's tag. Each is
 * evidence for linking transfers without asking, so none is ever added by
 * itself -- a person adds or ignores each one here.
 */
export function SuggestedIdentifiers({
  household,
  accounts,
}: {
  household: Household;
  accounts: Account[];
}) {
  const client = useQueryClient();
  // The list as soon as it is read; the same list with its pair counts once
  // those are, which is what the "Would link" column shows.
  const listed = useIdentifierSuggestions(household);
  const counted = useIdentifierSuggestions(household, true);
  const found = counted.data ? counted : listed;
  const { sort, direction, onSort } = useSort<SuggestionSort>("would_link", "desc");
  //: The account picked for a suggestion that matched none, by its key.
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const keyOf = (one: IdentifierSuggestion) => `${one.kind}:${one.value}`;
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["identifiers", household.id] });
    client.invalidateQueries({ queryKey: ["transfer-findings", household.id] });
  };
  const add = useMutation({
    mutationFn: (one: IdentifierSuggestion) =>
      api.post(`/households/${household.id}/identifiers/suggestions/add`, {
        kind: one.kind,
        value: one.value,
        account_id: one.account_id ?? chosen[keyOf(one)],
      }),
    onSuccess: refresh,
  });
  const ignore = useMutation({
    mutationFn: (one: IdentifierSuggestion) =>
      api.post(`/households/${household.id}/identifiers/suggestions/ignore`, {
        kind: one.kind,
        value: one.value,
      }),
    onSuccess: refresh,
  });
  const rows = useMemo(
    () =>
      sortRows(found.data?.items ?? [], sort, direction, (one, column) => {
        switch (column) {
          case "value":
            return one.value.toLowerCase();
          case "kind":
            return kindLabel(one.kind);
          case "account":
            return one.account_name;
          // Rows and files are different counts: files apart, then the figure.
          case "mentions":
            return [one.unit, one.mentions];
          case "would_link":
            return one.would_link;
          case "why":
            return one.why;
        }
      }),
    [found.data, sort, direction],
  );
  const heading = { sort, direction, onSort };
  const busy = add.isPending || ignore.isPending;

  return (
    <section aria-label="Suggested identifiers">
      <h3 style={{ marginBottom: 4 }}>Suggested identifiers</h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        Numbers and names your statements already use for your accounts, read off the register.
        An identifier lets transfers link without asking, so a wrong one makes wrong links:
        nothing here is added until you say so. <em>Would link</em> is how many transfer pairs
        adding it would link now.
      </p>
      <Problem error={listed.error ?? counted.error ?? add.error ?? ignore.error} />
      {found.data && rows.length === 0 ? (
        <Empty>Nothing to suggest.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label="Value" column="value" {...heading} />
                <SortHeading label="Kind" column="kind" {...heading} />
                <SortHeading label="For" column="account" {...heading} />
                <SortHeading label="Mentions" column="mentions" align="right" {...heading} />
                <SortHeading label="Would link" column="would_link" align="right" {...heading} />
                <SortHeading label="Why" column="why" {...heading} />
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((one) => {
                const key = keyOf(one);
                return (
                  <tr key={key}>
                    <td data-primary="true">
                      <span className="mono">{one.value}</span>
                      {one.sample && <div className="small muted">e.g. {one.sample}</div>}
                    </td>
                    <td className="small muted" data-label="Kind">
                      {kindLabel(one.kind)}
                    </td>
                    <td className="small" data-label="For">
                      {one.account_name ?? (
                        <select
                          aria-label={`Account for ${one.value}`}
                          value={chosen[key] ?? ""}
                          onChange={(e) => setChosen({ ...chosen, [key]: e.target.value })}
                        >
                          <option value="">Choose…</option>
                          {accounts.map((account) => (
                            <option key={account.id} value={account.id}>
                              {account.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="amount muted" data-label="Mentions">
                      {one.mentions.toLocaleString()} {one.unit === "files" ? "files" : "rows"}
                    </td>
                    <td className="amount" data-label="Would link">
                      {one.would_link === null ? (
                        <span className="muted" title="not counted">
                          —
                        </span>
                      ) : (
                        `${one.would_link} ${one.would_link === 1 ? "pair" : "pairs"}`
                      )}
                    </td>
                    <td className="small muted" data-label="Why">
                      {one.why}
                    </td>
                    <td className="amount row-actions">
                      <button
                        className="link"
                        disabled={busy || !(one.account_id ?? chosen[key])}
                        onClick={() => add.mutate(one)}
                        aria-label={`Add ${one.value}`}
                      >
                        Add
                      </button>{" "}
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() => ignore.mutate(one)}
                        aria-label={`Ignore ${one.value}`}
                      >
                        Ignore
                      </button>
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
