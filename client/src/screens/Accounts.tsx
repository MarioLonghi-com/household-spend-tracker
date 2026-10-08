/** Accounts, and the config panel for setting one up. */

import { useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { format, parse, toInput } from "../lib/money";
import { localToday } from "../lib/time";
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
import type { RegisterPreset } from "./Register";
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
import { ACCOUNT_TYPE_LABELS } from "../lib/labels";
import { compareNames, countryName as localCountryName, formatCount } from "../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

export const TYPES: { value: AccountType; label: string; owed: boolean; blurb: string }[] = [
  {
    value: "checking",
    owed: false,
    get label() {
      return ACCOUNT_TYPE_LABELS.checking.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.checking.blurb;
    },
  },
  {
    value: "savings",
    owed: false,
    get label() {
      return ACCOUNT_TYPE_LABELS.savings.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.savings.blurb;
    },
  },
  {
    value: "cash",
    owed: false,
    get label() {
      return ACCOUNT_TYPE_LABELS.cash.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.cash.blurb;
    },
  },
  {
    value: "credit_card",
    owed: true,
    get label() {
      return ACCOUNT_TYPE_LABELS.credit_card.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.credit_card.blurb;
    },
  },
  {
    value: "other_asset",
    owed: false,
    get label() {
      return ACCOUNT_TYPE_LABELS.other_asset.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.other_asset.blurb;
    },
  },
  {
    value: "other_liability",
    owed: true,
    get label() {
      return ACCOUNT_TYPE_LABELS.other_liability.label;
    },
    get blurb() {
      return ACCOUNT_TYPE_LABELS.other_liability.blurb;
    },
  },
];

/** What the six types mean, as one bubble. */
export function TypeHelp() {
  return (
    <Hint label={t({ message: "account types", comment: "Screen-reader name of a help button on the Accounts screen" })}>
      <p>
        <Trans>
          The type says what kind of thing the account is. It sorts the list and marks which
          balances are money you <em>owe</em> rather than money you have.
        </Trans>
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
        <Trans>
          <span className="neg">▾</span> marks the two whose balance is normally negative — you
          owe it. Nothing else changes: every account holds transactions the same way, and no
          arithmetic depends on the type. Pick the one that describes it, because it can't be
          changed afterwards.
        </Trans>
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
  if (!code) return t`no country set`;
  const english = list?.find((one) => one.code === code)?.name;
  return english ? localCountryName(code, english) : code;
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
        aria-label={t({ message: "Country", comment: "Screen-reader name on the Accounts screen: noun. See GLOSSARY.md" })}
        autoComplete="off"
        spellCheck={false}
        placeholder={t`type to search — leave empty for none`}
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

export function Accounts({
  household,
  onOpenRegister,
}: {
  household: Household;
  /** Opens the register at a row, as a report does. Absent, the link is not drawn. */
  onOpenRegister?: (preset: RegisterPreset) => void;
}) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<Account | null>(null);
  // Bumped on every save, so a panel that stays open is mounted afresh from
  // the saved account and shows what was stored -- trimmed -- not what was
  // typed (#20).
  const [saves, setSaves] = useState(0);
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
        (a, b) => (sort === "order" ? 0 : compareNames(a.name, b.name)),
      ),
    [accounts.data, sort, direction, countries.data, household.base_currency],
  );

  const refresh = () => client.invalidateQueries({ queryKey: ["accounts", household.id] });

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 16 }}>
        <h1><Trans comment="Screen title on the Accounts screen: noun, bank or cash accounts. See GLOSSARY.md">Accounts</Trans></h1>
        <label className="small muted" style={{ flex: "0 0 auto" }}>
          <input
            type="checkbox"
            checked={showClosed}
            onChange={(e) => setShowClosed(e.target.checked)}
            style={{ width: "auto", marginRight: 6 }}
          />
          <Trans comment="Label of a choice on the Accounts screen">Show closed</Trans>
        </label>
        <div className="row" style={{ flex: "0 0 auto" }}>
          <button onClick={() => setImporting(true)}>
            <Trans>Import from a file</Trans>
          </button>
          <button className="primary" onClick={() => setAdding(true)}>
            <Trans>
              Add an account
            </Trans>
          </button>
        </div>
      </div>

      <Problem error={accounts.error} />

      <div className="card">
        {accounts.data?.length === 0 ? (
          <Empty><Trans>No accounts yet. Add the one you use most and import a statement into it.</Trans></Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {/* Every column sorts. The one exception is the actions cell,
                      which holds buttons rather than a fact about the row. */}
                  <SortHeading
                    label={t({ message: "Account", comment: "Column heading on the Accounts screen: noun, a bank or cash account. See GLOSSARY.md" })}
                    column="name"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "Type", comment: "Column heading on the Accounts screen: noun, account type" })}
                    column="type"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "Bank", comment: "Column heading on the Accounts screen: noun, the institution that holds an account. See GLOSSARY.md" })}
                    column="institution"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "Country", comment: "Column heading on the Accounts screen: noun. See GLOSSARY.md" })}
                    column="country"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="flag-col"
                  />
                  <SortHeading
                    label={t({ message: "Currency", comment: "Column heading on the Accounts screen: noun. See GLOSSARY.md" })}
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
                    label={t({ message: "Oldest", comment: "Column heading on the Accounts screen" })}
                    column="oldest"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="span-col"
                  />
                  <SortHeading
                    label={t({ message: "Newest", comment: "Column heading on the Accounts screen" })}
                    column="newest"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="span-col"
                  />
                  <SortHeading
                    label={t({ message: "Rows", comment: "Column heading on the Accounts screen: noun, lines of a file or table" })}
                    column="rows"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                  <SortHeading
                    label={t({ message: "Cleared", comment: "Column heading on the Accounts screen: state, the bank has the row. See GLOSSARY.md" })}
                    column="cleared"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                  <SortHeading
                    label={t({ message: "Balance", comment: "Column heading on the Accounts screen: noun, the amount an account holds. See GLOSSARY.md" })}
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
                        <span className="tag closed" title={t`this account is closed`}>
                          <Trans comment="Tag beside a name on the Accounts screen: adjective, an account no longer in use. See GLOSSARY.md">
                            Closed
                          </Trans>
                        </span>
                      ) : null}
                    </td>
                    <td className="small muted" data-label={t({ message: "Type", comment: "Column name shown beside a value on phones on the Accounts screen: noun, account type" })} data-detail-first="true">
                      {TYPES.find((t) => t.value === account.type)?.label ?? account.type}
                      {account.is_liability ? (
                        <span className="neg" title={t`a negative balance here is money you owe`}>
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
                      data-label={t({ message: "Bank", comment: "Column name shown beside a value on phones on the Accounts screen: noun, the institution that holds an account. See G…" })}
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
                      data-label={t({ message: "Country", comment: "Column name shown beside a value on phones on the Accounts screen: noun. See GLOSSARY.md" })}
                      data-empty={account.country ? undefined : "true"}
                    >
                      <span className="flag" title={countryName(countries.data, account.country)}>
                        {account.flag}
                      </span>
                    </td>
                    {/* Its own column: the ledger never converts, so which
                        currency a figure is in is a fact about the row, not a
                        footnote under the name. */}
                    <td className="small muted mono" data-label={t({ message: "Currency", comment: "Column name shown beside a value on phones on the Accounts screen: noun. See GLOSSARY.md" })}>
                      {account.currency}
                    </td>
                    {/* An em dash, not an empty cell: a blank here reads as a
                        column that failed to render, and "this account has no
                        transactions" is a fact worth stating. */}
                    <td
                      className="small muted mono span-col"
                      data-label={t({ message: "Oldest", comment: "Column name shown beside a value on phones on the Accounts screen" })}
                      data-empty={account.oldest_transaction ? undefined : "true"}
                    >
                      {account.oldest_transaction ?? "—"}
                    </td>
                    <td
                      className="small muted mono span-col"
                      data-label={t({ message: "Newest", comment: "Column name shown beside a value on phones on the Accounts screen" })}
                      data-empty={account.newest_transaction ? undefined : "true"}
                    >
                      {account.newest_transaction ?? "—"}
                    </td>
                    <td className="amount muted" data-label={t({ message: "Rows", comment: "Column name shown beside a value on phones on the Accounts screen: noun, lines of a file or table" })}>
                      {formatCount(account.transaction_count)}
                    </td>
                    <td className="amount muted" data-label={t({ message: "Cleared", comment: "Column name shown beside a value on phones on the Accounts screen: state, the bank has the row. See GLOSSARY.md" })}>
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
                        <Trans comment="Button on the Accounts screen: verb, check an account against a bank statement. See GLOSSARY.md">
                          Reconcile
                        </Trans>
                      </button>{" "}
                      <button className="link" onClick={() => setEditing(account)}>
                        <Trans comment="Button on the Accounts screen">
                          Settings
                        </Trans>
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
          key={`${editing.id}:${saves}`}
          household={household}
          account={editing}
          onOpenRegister={onOpenRegister}
          onClose={() => setEditing(null)}
          onSaved={(saved, sentOpening) => {
            // A save that came back with a warning stays open to show it:
            // closing the panel would be the one way to make sure nobody
            // reads it. But a warning is a standing fact about the account,
            // so only a save that could have caused it -- one that sent the
            // opening figure or date, or that changed what the warnings say
            // -- keeps the panel open; renaming such an account still closes
            // it. The panel is handed the saved account so it compares the
            // next edit with what is stored now.
            const changed = saved.warnings.join("\n") !== editing.warnings.join("\n");
            setEditing(saved.warnings.length && (sentOpening || changed) ? saved : null);
            setSaves((n) => n + 1);
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
  const [openingDate, setOpeningDate] = useState(localToday);

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
    <Panel title={t({ message: "New account", comment: "Title of a panel on the Accounts screen" })} onClose={onClose} config>
      <Problem error={save.error} />
      <Field label={t({ message: "Name", comment: "Label of a form field on the Accounts screen: noun" })}>
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label={t({ message: "Type", comment: "Label of a form field on the Accounts screen: noun, account type" })} hint={<TypeHelp />}>
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
      <Field label={t({ message: "Currency", comment: "Label of a form field on the Accounts screen: noun. See GLOSSARY.md" })}>
        <input
          value={currency}
          onChange={(e) => setCurrency(e.target.value.toUpperCase())}
          maxLength={3}
        />
      </Field>
      <p className="muted small">
        <Trans>
          Each account keeps its own currency. Nothing is ever converted in the ledger.
        </Trans>
      </p>

      <Field label={t({ message: "Country", comment: "Label of a form field on the Accounts screen: noun. See GLOSSARY.md" })}>
        <CountryPicker value={country} onChange={setCountry} />
      </Field>
      <p className="muted small">
        <Trans>
          Where the account is held. Optional, and separate from the currency — a euro account can
          sit in any number of countries.
        </Trans>
      </p>
      <Field label={t`Bank or institution`}>
        <input
          value={institution}
          onChange={(e) => setInstitution(e.target.value)}
          maxLength={INSTITUTION_MAX}
        />
      </Field>
      <p />
      <Field label={t({ message: "Note", comment: "Label of a form field on the Accounts screen. See GLOSSARY.md" })}>
        <textarea
          value={note}
          rows={3}
          onChange={(e) => setNote(e.target.value)}
          maxLength={NOTE_MAX}
        />
      </Field>
      <p />

      <Field
        label={t({ message: "Opening balance", comment: "Label of a form field on the Accounts screen: noun, what the account held on the day it starts. See GLOSSARY.md" })}
        hint={
          <Hint label={t({ message: "opening balance", comment: "Label of a form field on the Accounts screen: noun, what the account held on the day it starts. See GLOSSARY.md" })}>
            <p>
              <Trans>
                What was in the account on the day you started tracking it — usually a date in the
                past, the one your first statement opens with.
              </Trans>
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              <Trans>
                It is recorded as a real transaction on that date, not a hidden number, so it shows in
                the register and can be corrected like anything else. For a card or a loan, type what
                you owe as a negative.
              </Trans>
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
        <p className="small neg">
          <Trans>That isn't an amount in {currency}.</Trans>
        </p>
      ) : (
        <p className="muted small">
          {openingMinor === 0
            ? t`Leave it blank for an account that starts empty.`
            : t`Recorded as ${format(openingMinor, currency)} on the date below.`}
        </p>
      )}

      <Field label={t({ message: "Opening date", comment: "Label of a form field on the Accounts screen" })}>
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
        <Trans comment="Button on the Accounts screen: verb">Create</Trans>
      </button>
    </Panel>
  );
}

/**
 * What the opening-balance box starts with: the stored figure, signed, or
 * nothing for an account opened empty -- blank reads as zero, as it does on
 * the New account panel, and an empty box says "none" better than a 0.00.
 */
export function openingText(minor: number, currency: string): string {
  if (minor === 0) return "";
  return `${minor < 0 ? "-" : ""}${toInput(minor, currency)}`;
}

function AccountSettings({
  household,
  account,
  onOpenRegister,
  onClose,
  onSaved,
}: {
  household: Household;
  account: Account;
  onOpenRegister?: (preset: RegisterPreset) => void;
  onClose: () => void;
  /** `sentOpening`: whether the save sent the opening figure or date. */
  onSaved: (saved: Account, sentOpening: boolean) => void;
}) {
  const [name, setName] = useState(account.name);
  const [institution, setInstitution] = useState(account.institution ?? "");
  const [country, setCountry] = useState(account.country ?? "");
  const [note, setNote] = useState(account.note ?? "");
  const [closed, setClosed] = useState(account.closed);
  const [product, setProduct] = useState(account.statement_product ?? "");
  const [opening, setOpening] = useState(() =>
    openingText(account.opening_balance, account.currency),
  );
  const [openingDate, setOpeningDate] = useState(account.opening_date ?? "");

  // The same reading as the New account panel: blank is zero, and anything
  // else has to parse in this account's currency or nothing is sent.
  const openingMinor = opening.trim() === "" ? 0 : parse(opening, account.currency);
  const openingBad = openingMinor === null;
  const today = localToday();
  const openingFuture = openingDate > today;

  const body = () => ({
    name,
    closed,
    // Null means "leave it alone" on a PATCH, so clearing a country that
    // was set needs to say so explicitly rather than send nothing.
    country: country || null,
    clear_country: country === "" && account.country !== null,
    // Trimmed, as the New account panel sends them, and an emptied one
    // cleared the same way as the country -- never stored as "" (#20).
    institution: institution.trim() || null,
    clear_institution: institution.trim() === "" && account.institution !== null,
    note: note.trim() || null,
    clear_note: note.trim() === "" && account.note !== null,
    statement_product: product.trim() || null,
    clear_statement_product: product.trim() === "" && account.statement_product !== null,
    // Only what changed, null being "leave it alone". Both are written to
    // the opening-balance row, so sending them unchanged would still be
    // an edit to a reconciled transaction on every save of a name.
    opening_balance: openingMinor !== account.opening_balance ? openingMinor : null,
    opening_date: openingDate && openingDate !== account.opening_date ? openingDate : null,
  });
  const save = useMutation({
    mutationFn: (sending: ReturnType<typeof body>) =>
      api.patch<Account>(`/accounts/${account.id}`, sending),
    onSuccess: (saved, sending) =>
      onSaved(saved, sending.opening_balance !== null || sending.opening_date !== null),
  });

  return (
    <Panel title={account.name} onClose={onClose} config>
      <Problem error={save.error} />
      <Field label={t({ message: "Name", comment: "Label of a form field on the Accounts screen: noun" })}>
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <p />
      <Field label={t({ message: "Country", comment: "Label of a form field on the Accounts screen: noun. See GLOSSARY.md" })}>
        <CountryPicker value={country} onChange={setCountry} />
      </Field>
      <p />
      <Field label={t`Bank or institution`}>
        <input
          value={institution}
          onChange={(e) => setInstitution(e.target.value)}
          maxLength={INSTITUTION_MAX}
        />
      </Field>
      <p />
      <Field label={t({ message: "Note", comment: "Label of a form field on the Accounts screen. See GLOSSARY.md" })}>
        <textarea
          value={note}
          rows={3}
          onChange={(e) => setNote(e.target.value)}
          maxLength={NOTE_MAX}
        />
      </Field>
      <p />
      <Field label={t({ message: "Statement product", comment: "Label of a form field on the Accounts screen" })}>
        <input
          value={product}
          // Revolut's own Product values, which are English in every file it writes.
          placeholder={account.type === "checking" ? "Current" : t({ message: `e.g. ${"Savings"}`, comment: "Text on the Accounts screen" })}
          onChange={(e) => setProduct(e.target.value)}
        />
      </Field>
      <p className="small muted" style={{ marginTop: 4 }}>
        <Trans>
          Only for a bank whose statement holds several accounts in one file, told apart by a
          Product column (Revolut's account statement). This account takes the rows with this
          value; the others are skipped. A current account takes the Current rows unless you
          say otherwise.
        </Trans>
      </p>
      <Field label={t({ message: "Opening balance", comment: "Label of a form field on the Accounts screen: noun, what the account held on the day it starts. See GLOSSARY.md" })}>
        <input
          value={opening}
          onChange={(e) => setOpening(e.target.value)}
          inputMode="decimal"
          placeholder={toInput(0, account.currency)}
        />
      </Field>
      {openingBad && (
        <p className="small neg">
          <Trans>That isn't an amount in {account.currency}.</Trans>
        </p>
      )}
      <Field label={t({ message: "Opening date", comment: "Label of a form field on the Accounts screen" })}>
        <input
          type="date"
          value={openingDate}
          max={today}
          onChange={(e) => setOpeningDate(e.target.value)}
        />
      </Field>
      {openingFuture && (
        <p className="small neg"><Trans>An account cannot have been opened in the future.</Trans></p>
      )}
      <p className="muted small" style={{ marginTop: 4 }}>
        {account.opening_transaction_id ? (
          onOpenRegister ? (
            <Trans>
              Both are a real transaction in the register, reconciled, dated the day tracking
              started. Changing them here edits that row and keeps it reconciled
              {" — "}
              <button
                type="button"
                className="link"
                onClick={() =>
                  onOpenRegister({
                    accounts: [account.id],
                    open: account.opening_transaction_id ?? undefined,
                  })
                }
              >
                show it in the register
              </button>
              . Zero removes it.
            </Trans>
          ) : (
            <Trans>
              Both are a real transaction in the register, reconciled, dated the day tracking
              started. Changing them here edits that row and keeps it reconciled. Zero removes it.
            </Trans>
          )
        ) : (
          <Trans>
            This account started empty, so there is no opening balance row. Type a figure to add
            one; without a date it is dated at the account's oldest transaction.
          </Trans>
        )}
      </p>
      {account.warnings.length > 0 && (
        <div className="banner warn" role="status">
          {account.warnings.map((one) => (
            <p key={one} style={{ margin: 0 }}>
              {one.charAt(0).toUpperCase() + one.slice(1)}.
            </p>
          ))}
        </div>
      )}
      <label className="small">
        <input
          type="checkbox"
          checked={closed}
          onChange={(e) => setClosed(e.target.checked)}
          style={{ width: "auto", marginRight: 8 }}
        />
        <Trans>Closed — hide it from the accounts list</Trans>
      </label>
      <p className="muted small">
        <Trans>
          The type and currency are fixed once an account exists, because every transaction on it is
          recorded in that currency.
        </Trans>
      </p>
      <button
        className="primary"
        disabled={save.isPending || openingBad || openingFuture}
        onClick={() => save.mutate(body())}
      >
        <Trans comment="Button on the Accounts screen: verb">Save</Trans>
      </button>
      <hr />
      <Identifiers household={household} account={account} />
    </Panel>
  );
}

/** Getters, so each is read in the language active when it is shown. */
const IDENTIFIER_KINDS: { value: IdentifierKind; label: string; example: string }[] = [
  { value: "iban", label: "IBAN", example: "GB82 WEST 1234 5698 7654 32" },
  {
    value: "number",
    get label() {
      return t({ message: "Account number", comment: "Label on the Accounts screen" });
    },
    get example() {
      return t`the number other statements quote`;
    },
  },
  {
    value: "card",
    get label() {
      return t({ message: "Card number", comment: "Label on the Accounts screen" });
    },
    get example() {
      return t`the last four, or the part the bank prints`;
    },
  },
  {
    value: "alias",
    get label() {
      return t`Name the bank uses`;
    },
    get example() {
      return t`a pocket's or a card product's name`;
    },
  },
  {
    value: "file_tag",
    get label() {
      return t`Tag in the download's file name`;
    },
    get example() {
      return t`e.g. the code after _en_`;
    },
  },
];

function kindLabel(kind: IdentifierKind): string {
  if (kind === "holder") return t({ message: "Holder's name", comment: "Label on the Accounts screen" });
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
    <section aria-label={account ? t`What banks call it` : t`How banks write our names`}>
      <h3 style={{ marginBottom: 4 }}>
        {account ? t`What banks call it` : t`How banks write our names`}
      </h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        {account
          ? t`Numbers and names that statements use for this account. A file carrying one is matched to this account on the Import screen, and a transfer naming one is matched to it.`
          : t`Each spelling a bank uses for someone in this household. A payment naming one is your own money moving, not income or spending.`}
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
                aria-label={t({ message: `Remove ${row.value}`, comment: "Screen-reader name of a button on the Accounts screen" })}
              >
                <Trans comment="Button on the Accounts screen: verb">Remove</Trans>
              </button>
              {!account && <HolderSamples household={household} identifierId={row.id} />}
            </li>
          ))}
        </ul>
      )}
      {suggested && (
        <p className="small">
          {suggested.kind === "iban" ? t`The name looks like its IBAN.` : t`The name looks like its number.`}{" "}
          <button className="link" onClick={() => add.mutate(suggested)}>
            <Trans comment="Button on the Accounts screen">
              Add <span className="mono">{suggested.value}</span>
            </Trans>
          </button>
        </p>
      )}
      <div className="row">
        {account && (
          <Field label={t({ message: "Kind", comment: "Label of a form field on the Accounts screen: noun, what sort of thing" })}>
            <select value={kind} onChange={(e) => setKind(e.target.value as IdentifierKind)}>
              {IDENTIFIER_KINDS.map((one) => (
                <option key={one.value} value={one.value}>
                  {one.label}
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field label={account ? t({ message: "Value", comment: "Text on the Accounts screen" }) : t`Name as the bank writes it`}>
          <input
            value={value}
            placeholder={
              account
                ? IDENTIFIER_KINDS.find((one) => one.value === kind)?.example
                : t({ message: "DOE JANE", comment: "Text on the Accounts screen" })
            }
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
      </div>
      <button
        disabled={!value.trim() || add.isPending}
        onClick={() => add.mutate({ kind: account ? kind : "holder", value })}
      >
        <Trans comment="Button on the Accounts screen: verb">Add</Trans>
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
    return (
      <div className="muted">
        <Trans>Matches nothing in the register yet.</Trans>
      </div>
    );
  }
  return (
    <div className="muted">
      {plural(mine.rows, {
        one: `Matches ${mine.rows} row, such as:`,
        other: `Matches ${mine.rows} rows, such as:`,
      })}
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

/**
 * "12 rows", "1,234 files": the count in the reader's own grouping, as
 * `toLocaleString()` wrote it. English said "rows" even for one, and still
 * does; a translation gets its plural forms.
 */
function mentionsText(count: number, unit: string): string {
  const shown = formatCount(count);
  return unit === "files"
    ? plural(count, { other: `${shown} files` })
    : plural(count, { other: `${shown} rows` });
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
    <section aria-label={t({ message: "Suggested identifiers", comment: "Screen-reader name on the Accounts screen" })}>
      <h3 style={{ marginBottom: 4 }}><Trans comment="Heading on the Accounts screen">Suggested identifiers</Trans></h3>
      <p className="small muted" style={{ marginTop: 0 }}>
        <Trans>
          Numbers and names your statements already use for your accounts, read off the register.
          An identifier lets transfers link without asking, so a wrong one makes wrong links:
          nothing here is added until you say so. <em>Would link</em> is how many transfer pairs
          adding it would link now.
        </Trans>
      </p>
      <Problem error={listed.error ?? counted.error ?? add.error ?? ignore.error} />
      {found.data && rows.length === 0 ? (
        <Empty><Trans>Nothing to suggest.</Trans></Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label={t({ message: "Value", comment: "Column heading on the Accounts screen" })} column="value" {...heading} />
                <SortHeading label={t({ message: "Kind", comment: "Column heading on the Accounts screen: noun, what sort of thing" })} column="kind" {...heading} />
                <SortHeading label={t({ message: "For", comment: "Column heading on the Accounts screen" })} column="account" {...heading} />
                <SortHeading label={t({ message: "Mentions", comment: "Column heading on the Accounts screen" })} column="mentions" align="right" {...heading} />
                <SortHeading label={t({ message: "Would link", comment: "Column heading on the Accounts screen" })} column="would_link" align="right" {...heading} />
                <SortHeading label={t({ message: "Why", comment: "Column heading on the Accounts screen: noun, the reason" })} column="why" {...heading} />
                <th aria-label={t({ message: "Actions", comment: "Screen-reader name on the Accounts screen" })} />
              </tr>
            </thead>
            <tbody>
              {rows.map((one) => {
                const key = keyOf(one);
                return (
                  <tr key={key}>
                    <td data-primary="true">
                      <span className="mono">{one.value}</span>
                      {one.sample && (
                        <div className="small muted">
                          <Trans comment="Text on the Accounts screen">e.g. {one.sample}</Trans>
                        </div>
                      )}
                    </td>
                    <td className="small muted" data-label={t({ message: "Kind", comment: "Column name shown beside a value on phones on the Accounts screen: noun, what sort of thing" })}>
                      {kindLabel(one.kind)}
                    </td>
                    <td className="small" data-label={t({ message: "For", comment: "Column name shown beside a value on phones on the Accounts screen" })}>
                      {one.account_name ?? (
                        <select
                          aria-label={t({ message: `Account for ${one.value}`, comment: "Screen-reader name on the Accounts screen" })}
                          value={chosen[key] ?? ""}
                          onChange={(e) => setChosen({ ...chosen, [key]: e.target.value })}
                        >
                          <option value=""><Trans comment="Option in a dropdown on the Accounts screen">Choose…</Trans></option>
                          {accounts.map((account) => (
                            <option key={account.id} value={account.id}>
                              {account.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="amount muted" data-label={t({ message: "Mentions", comment: "Column name shown beside a value on phones on the Accounts screen" })}>
                      {mentionsText(one.mentions, one.unit)}
                    </td>
                    <td className="amount" data-label={t({ message: "Would link", comment: "Column name shown beside a value on phones on the Accounts screen" })}>
                      {one.would_link === null ? (
                        <span className="muted" title={t({ message: "not counted", comment: "Tooltip on the Accounts screen" })}>
                          —
                        </span>
                      ) : (
                        plural(one.would_link, {
                          one: `${one.would_link} pair`,
                          other: `${one.would_link} pairs`,
                        })
                      )}
                    </td>
                    <td className="small muted" data-label={t({ message: "Why", comment: "Column name shown beside a value on phones on the Accounts screen: noun, the reason" })}>
                      {one.why}
                    </td>
                    <td className="amount row-actions">
                      <button
                        className="link"
                        disabled={busy || !(one.account_id ?? chosen[key])}
                        onClick={() => add.mutate(one)}
                        aria-label={t({ message: `Add ${one.value}`, comment: "Screen-reader name of a button on the Accounts screen" })}
                      >
                        <Trans comment="Button on the Accounts screen: verb">Add</Trans>
                      </button>{" "}
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() => ignore.mutate(one)}
                        aria-label={t({ message: `Ignore ${one.value}`, comment: "Screen-reader name of a button on the Accounts screen" })}
                      >
                        <Trans comment="Button on the Accounts screen">Ignore</Trans>
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
