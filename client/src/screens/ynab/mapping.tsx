/**
 * Steps 5 and 6 of the YNAB wizard: where each YNAB account and category goes.
 *
 * Accounts are one to one -- two YNAB accounts landing in one Spend Tracker
 * account would merge two registers nobody could pull apart again -- so an
 * account already chosen on one row is disabled on every other. Categories are
 * many to one: several YNAB categories may share a target.
 *
 * The suggestions arrive from the server already worked out; this screen only
 * pre-selects them and says which choice was one.
 */

import { useMemo } from "react";
import { format } from "../../lib/money";
import { SortHeading, sortRows, useSort } from "../../components/bits";
import { TYPES, typeLabel } from "../Accounts";
import type { AccountType } from "../../lib/types";
import type {
  AccountChoice,
  AccountSuggestion,
  CategoryChoice,
  CategorySuggestion,
  TargetAccount,
  TargetCategory,
  YnabAccount,
  YnabCategory,
} from "./types";
import { compareNames } from "../../lib/locale";
import { YNAB_ACCOUNT_WORDS, YNAB_CATEGORY_WORDS } from "../../lib/labels";

/**
 * An amount, or the bare figure when the code is not one `Intl` knows.
 *
 * `format` is that fallback now, with the currency's own decimals: the
 * `(minor / 100).toFixed(2)` that stood here wrote 1,234 JPY as "12.34" and
 * could not be reached, because `format` does not throw.
 */
export function money(minor: number, currency: string): string {
  return format(minor, currency);
}

export function range(first: string | null, last: string | null): string {
  if (!first && !last) return "—";
  if (first === last) return first ?? "—";
  return `${first ?? "…"} – ${last ?? "…"}`;
}

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n.toLocaleString()} ${n === 1 ? one : many}`;

// --------------------------------------------------------------------------- //
// Match state: the row's tint, its word and the legend above each table
// --------------------------------------------------------------------------- //

/**
 * Where a row stands, from the choice as it is now rather than the suggestion
 * it started from. The tint is a second cue only: every row also carries the
 * word, so nothing is said by colour alone.
 */
export type MatchState = "matched" | "create" | "unmatched" | "fixed";

const STATE_ICON: Record<MatchState, string> = {
  matched: "✓",
  create: "+",
  unmatched: "!",
  fixed: "–",
};

export function accountState(choice: AccountChoice): MatchState {
  return choice.kind === "existing" ? "matched" : choice.kind === "create" ? "create" : "unmatched";
}

export function categoryState(category: YnabCategory, choice: CategoryChoice): MatchState {
  if (category.fixed_uncategorised) return "fixed";
  return choice.kind === "existing" ? "matched" : choice.kind === "create" ? "create" : "unmatched";
}

function StateWord({ state, word }: { state: MatchState; word: string }) {
  return (
    <span className={`ynab-state ynab-state-${state}`}>
      <span aria-hidden="true">{STATE_ICON[state]}</span> {word}
    </span>
  );
}

function StateLegend({ items }: { items: [MatchState, string, string][] }) {
  return (
    <ul className="ynab-legend" aria-label="Row colours">
      {items.map(([state, word, meaning]) => (
        <li key={state} className={`ynab-legend-item ynab-row-${state}`}>
          <StateWord state={state} word={word} />
          <span className="small muted">{meaning}</span>
        </li>
      ))}
    </ul>
  );
}

const ACCOUNT_WORD = YNAB_ACCOUNT_WORDS;
const CATEGORY_WORD = YNAB_CATEGORY_WORDS;

// --------------------------------------------------------------------------- //
// Accounts
// --------------------------------------------------------------------------- //

/** Everything known about a target account, on one line. */
export function describeTarget(target: TargetAccount): string {
  return [
    target.name,
    typeLabel(target.type),
    target.institution,
    target.identifiers_masked.length ? target.identifiers_masked.join(" ") : null,
    money(target.balance_minor, target.currency),
    plural(target.txn_count, "txn"),
    target.txn_count > 0 ? range(target.first_date, target.last_date) : null,
    target.closed ? "(closed)" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function accountValue(choice: AccountChoice): string {
  return choice.kind === "existing" ? `existing:${choice.account_id}` : choice.kind;
}

export function accountFromSuggestion(
  suggestion: AccountSuggestion,
  account: YnabAccount,
): AccountChoice {
  if (suggestion.kind === "existing") return { kind: "existing", account_id: suggestion.account_id };
  return {
    kind: "create",
    name: suggestion.name || account.name,
    type: suggestion.type ?? account.type_hint ?? "checking",
  };
}

function sameAccountChoice(a: AccountChoice, b: AccountChoice): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "existing" && b.kind === "existing") return a.account_id === b.account_id;
  return true;
}

type AccountSort = "name" | "rows" | "range" | "balance";

export function MapAccounts({
  accounts,
  targets,
  currency,
  choices,
  onChoose,
}: {
  accounts: YnabAccount[];
  targets: TargetAccount[];
  currency: string;
  choices: Record<string, AccountChoice>;
  onChoose: (key: string, choice: AccountChoice) => void;
}) {
  const { sort, direction, onSort } = useSort<AccountSort>("name");
  const eligible = useMemo(() => targets.filter((target) => target.eligible), [targets]);
  const byId = useMemo(() => new Map(targets.map((target) => [target.id, target])), [targets]);

  /** Which YNAB account holds each existing target, so the others can be refused it. */
  const holder = useMemo(() => {
    const held = new Map<string, string>();
    for (const account of accounts) {
      const choice = choices[account.key];
      if (choice?.kind === "existing") held.set(choice.account_id, account.key);
    }
    return held;
  }, [accounts, choices]);
  const nameOf = useMemo(() => new Map(accounts.map((one) => [one.key, one.name])), [accounts]);

  const rows = useMemo(
    () =>
      sortRows(accounts, sort, direction, (row, column) => {
        switch (column) {
          case "name":
            return row.name;
          case "rows":
            return row.rows;
          case "range":
            return row.date_min;
          case "balance":
            return row.balance_minor;
        }
      }),
    [accounts, sort, direction],
  );
  const heading = { sort, direction, onSort };

  return (
    <>
      <p className="small">
        Each YNAB account goes to one Spend Tracker account in {currency}, to a new one, or is
        skipped. An account can take only one YNAB account, so one chosen on a row is not offered
        on the others.
      </p>
      <StateLegend
        items={[
          ["matched", ACCOUNT_WORD.matched, "an existing account"],
          ["create", ACCOUNT_WORD.create, "a new account"],
          ["unmatched", ACCOUNT_WORD.unmatched, "left out"],
        ]}
      />
      <div className="table-scroll">
        <table className="ynab-map">
          <thead>
            <tr>
              <SortHeading label="YNAB account" column="name" {...heading} />
              <SortHeading label="Rows" column="rows" align="right" {...heading} />
              <SortHeading label="Dates" column="range" {...heading} />
              <SortHeading label="Balance" column="balance" align="right" {...heading} />
              <th>Goes to</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((account) => {
              const choice = choices[account.key] ?? { kind: "skip" };
              const suggested = accountFromSuggestion(account.suggestion, account);
              const isSuggested = sameAccountChoice(choice, suggested);
              const chosen = choice.kind === "existing" ? byId.get(choice.account_id) : undefined;
              const label = `Target for ${account.name}`;
              const state = accountState(choice);
              return (
                <tr key={account.key} className={`ynab-row-${state}`} data-state={state}>
                  <td data-primary="true">
                    {account.name}
                    {account.closed ? <span className="tag closed">closed</span> : null}
                    <StateWord state={state} word={ACCOUNT_WORD[state]} />
                  </td>
                  <td className="amount small" data-label="Rows" data-detail-first="true">
                    {account.rows.toLocaleString()}
                  </td>
                  <td className="small muted mono" data-label="Dates">
                    {range(account.date_min, account.date_max)}
                  </td>
                  <td className="amount" data-figure="true">
                    <span className={account.balance_minor < 0 ? "neg" : undefined}>
                      {money(account.balance_minor, currency)}
                    </span>
                  </td>
                  <td className="ynab-target" data-label="Goes to">
                    <div className="ynab-target-body">
                      <div className="ynab-target-pick">
                        <select
                          aria-label={label}
                          value={accountValue(choice)}
                          onChange={(event) => {
                            const value = event.target.value;
                            if (value === "skip") onChoose(account.key, { kind: "skip" });
                            else if (value === "create")
                              onChoose(account.key, {
                                kind: "create",
                                name:
                                  suggested.kind === "create" ? suggested.name : account.name,
                                type:
                                  suggested.kind === "create"
                                    ? suggested.type
                                    : (account.type_hint ?? "checking"),
                              });
                            else
                              onChoose(account.key, {
                                kind: "existing",
                                account_id: value.slice("existing:".length),
                              });
                          }}
                        >
                          <optgroup label={`Existing accounts in ${currency}`}>
                            {eligible.map((target) => {
                              const heldBy = holder.get(target.id);
                              const taken = heldBy !== undefined && heldBy !== account.key;
                              const isSuggestion =
                                account.suggestion.kind === "existing" &&
                                account.suggestion.account_id === target.id;
                              return (
                                <option
                                  key={target.id}
                                  value={`existing:${target.id}`}
                                  disabled={taken}
                                >
                                  {describeTarget(target)}
                                  {isSuggestion ? " — suggested" : ""}
                                  {taken ? ` — used by ${nameOf.get(heldBy!) ?? heldBy}` : ""}
                                </option>
                              );
                            })}
                          </optgroup>
                          <option value="create">
                            Create new…{account.suggestion.kind === "create" ? " — suggested" : ""}
                          </option>
                          <option value="skip">Skip</option>
                        </select>
                        {isSuggested ? <span className="pill">suggested</span> : null}
                      </div>
                      {chosen ? (
                        <div className="small muted ynab-detail">{describeTarget(chosen)}</div>
                      ) : null}
                      {choice.kind === "create" ? (
                        <div className="ynab-create">
                          <input
                            aria-label={`New account name for ${account.name}`}
                            value={choice.name}
                            onChange={(event) =>
                              onChoose(account.key, { ...choice, name: event.target.value })
                            }
                          />
                          <select
                            aria-label={`New account type for ${account.name}`}
                            value={choice.type}
                            onChange={(event) =>
                              onChoose(account.key, {
                                ...choice,
                                type: event.target.value as AccountType,
                              })
                            }
                          >
                            {TYPES.map((one) => (
                              <option key={one.value} value={one.value}>
                                {one.label}
                              </option>
                            ))}
                          </select>
                          <div className="small muted">
                            In {currency}, opening balance 0, opened the day before its first
                            imported transaction.
                          </div>
                        </div>
                      ) : null}
                      {choice.kind === "skip" ? (
                        <div className="small muted ynab-detail">
                          Its {plural(account.rows, "row")} are left out and listed in the report.
                        </div>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// --------------------------------------------------------------------------- //
// Categories
// --------------------------------------------------------------------------- //

function categoryValue(choice: CategoryChoice): string {
  return choice.kind === "existing" ? `existing:${choice.category_id}` : choice.kind;
}

export function categoryFromSuggestion(
  category: YnabCategory,
  suggestion: CategorySuggestion = category.suggestion,
): CategoryChoice {
  if (category.fixed_uncategorised) return { kind: "uncategorised" };
  if (suggestion.kind === "existing")
    return { kind: "existing", category_id: suggestion.category_id };
  if (suggestion.kind === "create") return { kind: "create", name: suggestion.name || category.name };
  return { kind: "uncategorised" };
}

function sameCategoryChoice(a: CategoryChoice, b: CategoryChoice): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "existing" && b.kind === "existing") return a.category_id === b.category_id;
  return true;
}

type CategorySort = "name" | "groups" | "rows";

export function MapCategories({
  categories,
  targets,
  choices,
  onChoose,
}: {
  categories: YnabCategory[];
  targets: TargetCategory[];
  choices: Record<string, CategoryChoice>;
  onChoose: (key: string, choice: CategoryChoice) => void;
}) {
  const { sort, direction, onSort } = useSort<CategorySort>("name");

  /** Existing categories by group, archived ones left out unless already chosen. */
  const groups = useMemo(() => {
    const chosenIds = new Set(
      Object.values(choices)
        .filter((choice) => choice.kind === "existing")
        .map((choice) => (choice as { category_id: string }).category_id),
    );
    const grouped = new Map<string, TargetCategory[]>();
    for (const target of targets) {
      if (target.archived && !chosenIds.has(target.id)) continue;
      const list = grouped.get(target.group_name) ?? [];
      list.push(target);
      grouped.set(target.group_name, list);
    }
    return [...grouped.entries()]
      .sort(([a], [b]) => compareNames(a, b))
      .map(([name, list]) => ({
        name,
        list: [...list].sort((a, b) => compareNames(a.name, b.name)),
      }));
  }, [targets, choices]);

  const rows = useMemo(
    () =>
      sortRows(categories, sort, direction, (row, column) => {
        switch (column) {
          case "name":
            return row.name;
          case "groups":
            return row.groups.join(", ");
          case "rows":
            return row.rows;
        }
      }),
    [categories, sort, direction],
  );
  const heading = { sort, direction, onSort };

  return (
    <>
      <p className="small">
        YNAB's category groups are not kept: each YNAB category goes to one category here, and
        several may share one.
      </p>
      <StateLegend
        items={[
          ["matched", CATEGORY_WORD.matched, "an existing category"],
          ["create", CATEGORY_WORD.create, "a new category"],
          ["unmatched", CATEGORY_WORD.unmatched, "no category"],
          ["fixed", CATEGORY_WORD.fixed, "YNAB's own bucket, always uncategorised"],
        ]}
      />
      <div className="table-scroll">
        <table className="ynab-map">
          <thead>
            <tr>
              <SortHeading label="YNAB category" column="name" {...heading} />
              <SortHeading label="YNAB groups" column="groups" {...heading} />
              <SortHeading label="Rows" column="rows" align="right" {...heading} />
              <th>Goes to</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((category) => {
              const choice = category.fixed_uncategorised
                ? ({ kind: "uncategorised" } as CategoryChoice)
                : (choices[category.key] ?? { kind: "uncategorised" });
              const isSuggested =
                !category.fixed_uncategorised &&
                sameCategoryChoice(choice, categoryFromSuggestion(category));
              const suggestedId =
                category.suggestion.kind === "existing" ? category.suggestion.category_id : null;
              const state = categoryState(category, choice);
              return (
                <tr key={category.key} className={`ynab-row-${state}`} data-state={state}>
                  <td data-primary="true">
                    {category.name || <span className="muted">(none)</span>}
                    <StateWord state={state} word={CATEGORY_WORD[state]} />
                  </td>
                  <td
                    className="small muted"
                    data-label="YNAB groups"
                    data-detail-first="true"
                    data-empty={category.groups.length ? undefined : "true"}
                  >
                    {category.groups.join(", ") || "—"}
                  </td>
                  <td className="amount" data-figure="true">
                    {category.rows.toLocaleString()}
                  </td>
                  <td className="ynab-target" data-label="Goes to">
                    <div className="ynab-target-body">
                      <div className="ynab-target-pick">
                        <select
                          aria-label={`Target for ${category.name || "no category"}`}
                          value={categoryValue(choice)}
                          disabled={category.fixed_uncategorised}
                          onChange={(event) => {
                            const value = event.target.value;
                            if (value === "uncategorised")
                              onChoose(category.key, { kind: "uncategorised" });
                            else if (value === "create")
                              onChoose(category.key, {
                                kind: "create",
                                name:
                                  category.suggestion.kind === "create"
                                    ? category.suggestion.name
                                    : category.name,
                              });
                            else
                              onChoose(category.key, {
                                kind: "existing",
                                category_id: value.slice("existing:".length),
                              });
                          }}
                        >
                          {groups.map((group) => (
                            <optgroup key={group.name} label={group.name}>
                              {group.list.map((target) => (
                                <option key={target.id} value={`existing:${target.id}`}>
                                  {target.name}
                                  {target.archived ? " (archived)" : ""}
                                  {target.id === suggestedId ? " — suggested" : ""}
                                </option>
                              ))}
                            </optgroup>
                          ))}
                          <option value="create">
                            Create new…
                            {category.suggestion.kind === "create" ? " — suggested" : ""}
                          </option>
                          <option value="uncategorised">
                            Uncategorised
                            {!category.fixed_uncategorised &&
                            category.suggestion.kind === "uncategorised"
                              ? " — suggested"
                              : ""}
                          </option>
                        </select>
                        {isSuggested ? <span className="pill">suggested</span> : null}
                      </div>
                      {category.fixed_uncategorised ? (
                        <div className="small muted ynab-detail">
                          Fixed: this is YNAB's own bucket rather than a category of yours
                          (Uncategorized, or Inflow: Ready to Assign), so its rows always arrive
                          uncategorised.
                        </div>
                      ) : null}
                      {choice.kind === "create" ? (
                        <div className="ynab-create">
                          <input
                            aria-label={`New category name for ${category.name}`}
                            value={choice.name}
                            onChange={(event) =>
                              onChoose(category.key, { ...choice, name: event.target.value })
                            }
                          />
                          <div className="small muted">
                            Created in a group called "Imported from YNAB".
                          </div>
                        </div>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
