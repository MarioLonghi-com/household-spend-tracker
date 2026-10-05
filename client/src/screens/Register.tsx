/**
 * The register.
 *
 * Quick entry is keyboard-first: date, payee, amount, Enter, and the cursor
 * goes back to the date with the date kept -- entering a statement by hand is a
 * run of rows on nearby days, so keeping it is right more often than clearing
 * it.
 */

import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { equalParts, retype } from "../lib/splitting";
import { amountLookup, format, parse, toInput } from "../lib/money";
import { Actor, Dialog, Empty, Field, Hint, Panel, Problem, SortHeading } from "../components/bits";
import type { SortDirection, SortKey } from "../components/bits";
import { ALL_DATES, DateRange } from "../components/DateRange";
import type { Range } from "../components/DateRange";
import { GroupedPicker } from "../components/GroupedPicker";
import type { PickerGroup, Selection } from "../components/GroupedPicker";
import { Combobox } from "../components/Combobox";
import {
  Lightbox,
  MoreInfo,
  ReceiptDrop,
  ReceiptFrame,
  ReceiptMark,
  receiptsOf,
  sizeText,
} from "../components/Receipts";
import { useWindowed } from "../lib/useWindowed";
import { useSticky } from "../lib/sticky";
import { useDebounced } from "../lib/useDebounced";
import { MIN_WIDTH, tableWidth, useColumnWidths } from "../lib/columnWidths";
import { formatInstant, localToday } from "../lib/time";
import {
  REIMBURSEMENT_LABELS,
  WORK_PILLS,
  ageInDays,
  groupByPayment,
  workState,
} from "../lib/reimbursement";
import { RowPicker } from "../components/RowPicker";
import { SplitBar } from "../components/SplitBar";
import { Transfer } from "./Transfer";
import type {
  Account,
  BulkEditResult,
  CategoryGroup,
  Change,
  Household,
  Payee,
  Receipt,
  RegisterPage,
  ReimbursementState,
  ReimbursementView,
  Transaction,
  TransactionOrigin,
} from "../lib/types";

/**
 * A register row, with the currency its amount is in.
 *
 * Declared here rather than in `lib/types.ts` because the field is the
 * register's own: `TransactionOut.currency` is what the server sends, and the
 * screen that draws one IN/OUT pair per currency is the one that needs it.
 * Optional, so a row from any other endpoint still types as a Transaction.
 */
type Row = Transaction & { currency?: string | null };
type RegisterRows = Omit<RegisterPage, "transactions"> & { transactions: Row[] };

/**
 * What each cleared state is called on screen, as one letter.
 *
 * Three words in a column nobody reads word by word: "not yet", "cleared",
 * "locked" cost as much width as the payee and are scanned as shape rather
 * than as text. One letter is the same fact, the column narrows to it, and the
 * full sentence is on hover and in the accessible name -- the same bargain the
 * source column already makes one cell to the right.
 *
 * The third one is stored as `reconciled` and is not a reconciliation: there is
 * no closing balance to check against, no running difference and no
 * reconciled-through date, so nothing here has been proved against a statement.
 * What it actually does is lock the row -- against edits, against deletion,
 * against a transfer's other leg, and against being absorbed by an import -- so
 * that is what it is called, and the label describes the act rather than
 * claiming a reconciliation happened. Its letter is L for the same reason.
 *
 * The stored value keeps its name. Renaming it would mean rewriting the
 * before-images in the audit log, and those are the record of what the app did
 * at the time.
 */
const CLEARED_PILLS: Record<Transaction["cleared"], { letter: string; word: string }> = {
  uncleared: { letter: "U", word: "Uncleared — the bank has not seen this yet" },
  cleared: { letter: "C", word: "Cleared — the bank has it" },
  reconciled: {
    letter: "L",
    word: "Locked — checked against the bank, and every change to it is refused",
  },
};

function Cleared({ state }: { state: Transaction["cleared"] }) {
  const { letter, word } = CLEARED_PILLS[state];
  return (
    <span className={`tag cleared-${letter}`} title={word} aria-label={word}>
      {letter}
    </span>
  );
}

/** How an account type reads in a filter heading. */
const TYPE_NAMES: Record<string, string> = {
  checking: "Checking",
  savings: "Savings",
  cash: "Cash",
  credit_card: "Credit cards",
  other_asset: "Other assets",
  other_liability: "Other liabilities",
};

/**
 * How the accounts filter gathers its options.
 *
 * Country first, then type, exactly as the income-and-expense report does it:
 * "what did we spend in Spain" is asked more often than "what did the savings
 * accounts do", and a filter that groups one way on one screen and another way
 * on the next is two things to learn.
 *
 * A second copy of the report's own `accountGroups`, which is not exported and
 * lives in a file this session does not own. It should be hoisted next to
 * `GroupedPicker` so the two cannot drift -- noted rather than done, because
 * moving it means editing that screen.
 */
type AccountGrouping = "country" | "type";

function accountGroups(accounts: Account[], grouping: AccountGrouping): PickerGroup[] {
  const buckets = new Map<string, { label: string; items: Account[] }>();
  for (const account of accounts) {
    const key = grouping === "country" ? (account.country ?? "—") : account.type;
    const label =
      grouping === "country"
        ? account.country
          ? `${account.flag} ${account.country}`
          : "No country set"
        : (TYPE_NAMES[account.type] ?? account.type);
    const bucket = buckets.get(key) ?? { label, items: [] };
    bucket.items.push(account);
    buckets.set(key, bucket);
  }
  return [...buckets.entries()]
    .sort((a, b) => a[1].label.localeCompare(b[1].label))
    .map(([key, bucket]) => ({
      key,
      label: bucket.label,
      items: bucket.items.map((account) => ({
        id: account.id,
        label: account.name,
        hint: account.currency,
      })),
    }));
}

/**
 * What a set of selected rows comes to, **per currency**.
 *
 * One line per currency and no grand total, because there is no rate anywhere
 * in this schema and a figure adding EUR to GBP would be a number with nothing
 * behind it. That is not a limitation of this function to be fixed later by
 * summing harder: `app/services/insights.py` says the same thing about every
 * response, and the income-and-expense report says it about every screen.
 *
 * Both halves stay **signed**: the ins positive, the outs negative. So the line
 * reads as the arithmetic it actually is -- `1,200.00 + -450.00 = 750.00` --
 * rather than as two magnitudes and a total that does not follow from them.
 *
 * Exported for its test. It is the one piece of this screen that is arithmetic
 * rather than layout, and arithmetic over money is worth pinning down.
 */
export type CurrencySum = {
  currency: string;
  /** Sum of the inflows among the selected rows. Zero or positive. */
  ins: number;
  /** Sum of the outflows. Zero or negative, so `ins + outs` is the net. */
  outs: number;
  net: number;
  count: number;
};

export function selectionSums<T extends { id: string; amount: number }>(
  rows: T[],
  selected: Set<string>,
  currencyOf: (row: T) => string,
  order: string[] = [],
): CurrencySum[] {
  const byCurrency = new Map<string, CurrencySum>();
  for (const row of rows) {
    if (!selected.has(row.id)) continue;
    const currency = currencyOf(row);
    const sum =
      byCurrency.get(currency) ??
      { currency, ins: 0, outs: 0, net: 0, count: 0 };
    if (row.amount > 0) sum.ins += row.amount;
    else sum.outs += row.amount;
    sum.net += row.amount;
    sum.count += 1;
    byCurrency.set(currency, sum);
  }
  // The currencies in the order the toggle shows them, so the sum lines and
  // the columns they are about run the same way down the screen.
  return [...byCurrency.values()].sort((a, b) => {
    const at = order.indexOf(a.currency);
    const bt = order.indexOf(b.currency);
    if (at !== bt) return (at < 0 ? order.length : at) - (bt < 0 ? order.length : bt);
    return a.currency.localeCompare(b.currency);
  });
}

/**
 * How a row got here, as one letter.
 *
 * It was two words spelled out inside the payee cell, which is the cell you
 * actually read -- "SAINSBURYS transfer imported" is three things competing for
 * one line. As its own column it is scannable down the page and sortable, and
 * an initial is enough once the column has a heading. The full word is on
 * hover and in the accessible name, so nothing is lost to someone who needs it.
 */
/**
 * The Source filter's choices, in the column's own order and words (#123).
 *
 * The values are the server's `RegisterSource`, which ranks a row exactly as
 * the letter does: a linked pair of imported rows is a transfer, not an import.
 */
const SOURCE_CHOICES: { value: string; label: string }[] = [
  { value: "transfer", label: "T — Transfers" },
  { value: "split", label: "S — Split parts" },
  { value: "imported", label: "I — Imported" },
  { value: "manual", label: "M — Entered by hand" },
];

function Source({ txn }: { txn: Transaction }) {
  const [initial, word] =
    txn.transfer_account_id !== null
      ? ["T", "Transfer between your own accounts"]
      : txn.split_id
        ? ["S", "One part of a split"]
        : txn.import_id
          ? // A one-time import's rows are imports too (#183): same letter, same
            // filter, and the side panel says which door they came in by.
            ["I", "Imported from a statement or a one-time import"]
          : ["M", "Entered by hand"];
  return (
    <span className={`tag source-${initial}`} title={word} aria-label={word}>
      {initial}
    </span>
  );
}

/**
 * One side of a transfer, as a mark in a column of its own (#143).
 *
 * The Source column says T for the same rows, and keeps saying it -- that
 * letter is one of four answers to "how did this row get here", and the
 * Source filter is built on it. This is the other question, "is this money
 * moving between my own accounts", asked at the left edge where the eye
 * starts a row, so a run of transfers reads as one down the page before the
 * payee has been read. Either link column counts, as in `isTransferLeg`.
 *
 * Nothing on any other row: an empty cell, not a dash. The column is there
 * for the layout's sake, not to say "not a transfer" three hundred times.
 */
function TransferMark({ txn, otherAccount }: { txn: Row; otherAccount?: string }) {
  if (!isTransferLeg(txn)) return null;
  const word = otherAccount
    ? `Transfer — the other side is in ${otherAccount}`
    : "Transfer between your own accounts";
  return (
    <span className="transfer-mark" title={word} aria-label={word}>
      ⇄
    </span>
  );
}

/**
 * The Work expenses filter's choices, in the order a person reaches for them
 * (#reimbursements). `work` and `paid` bring each expense's payment along,
 * and the register groups the two together when either is on.
 */
const WORK_CHOICES: { value: ReimbursementView; label: string }[] = [
  { value: "work", label: "Work items and their repayments" },
  { value: "owed", label: "Not reimbursed yet" },
  { value: "paid", label: "Reimbursed, with the payment" },
  { value: "off", label: "Written off" },
];

/**
 * A work expense, as one letter at the right-hand end of the memo.
 *
 * No column of its own and no sort: a fact about the row that needs seeing
 * but not a heading. It sat beside the source letter until #143, and moved
 * to the memo because the memo is where a work expense is explained -- "taxi
 * to the client", "hotel, Lisbon" -- so the W and the words that justify it
 * are read together. The memo truncates to make room for it. The letter is
 * the same in all three states; the colour and, for anyone who cannot see
 * it, the words differ.
 */
export function WorkMark({ txn }: { txn: Pick<Transaction, "reimbursement" | "reimbursed_by_id"> }) {
  const state = workState(txn);
  if (state === "none") return null;
  const { className, word } = WORK_PILLS[state];
  return (
    <span className={className} title={word} aria-label={word}>
      W
    </span>
  );
}

/**
 * Where the register opens when another screen sends somebody to it.
 *
 * The Reimbursements report's "Show in Transactions", and a click on one of
 * its outstanding rows, land here with the Work expenses filter set and --
 * for the row -- that transaction's panel open. Read once, when the register
 * mounts; the shell forgets it as soon as anybody navigates from the menu, so
 * a filter somebody was sent to never comes back uninvited.
 *
 * Since the register remembers its filters (#143) that promise needs a second
 * half, which `useFilter` keeps: a preset is shown over the remembered
 * filters and is not written over them, so the menu brings back what the
 * person set rather than what the report did.
 */
export type RegisterPreset = {
  reimbursement?: ReimbursementView;
  /** Only these accounts, so the row in `open` is among the rows that arrive. */
  accounts?: string[];
  /** A transaction to open in the side panel once the rows arrive. */
  open?: string;
};

/**
 * Whether a row is one side of a transfer (#124).
 *
 * Either link column counts, the same test the server makes: a transfer is not
 * spending, so it has no category, and every place this screen offers one --
 * the cell, the panel, the bulk picker -- asks this first.
 */
export function isTransferLeg(txn: Pick<Transaction, "transfer_account_id" | "transfer_transaction_id">): boolean {
  return Boolean(txn.transfer_account_id || txn.transfer_transaction_id);
}


/**
 * What to say after a bulk work-expense change, or nothing.
 *
 * Same reasoning as `skippedNote` below: a selection of forty rows with a
 * salary and a transfer in it is flagged except for those two, and the
 * person who selected forty should not believe forty are now owed.
 */
export function reimbursementSkippedNote(skipped: number, sent: number): string | null {
  if (skipped <= 0) return null;
  const done = sent - skipped;
  return (
    `Changed ${done} ${done === 1 ? "row" : "rows"} and left ${skipped} alone — only money ` +
    "out that is not a transfer can be a work expense."
  );
}

/**
 * The most expenses one "Link as reimbursement" sends. The server takes up
 * to this many in one batch; past it the button says why rather than failing.
 */
export const LINK_MAX_EXPENSES = 200;

/**
 * What to say after a bulk category change, or nothing.
 *
 * Only when something was skipped: a selection that included transfer legs
 * was categorised except for them, and a person who shift-clicked forty rows
 * would otherwise believe all forty now carry the category.
 */
export function skippedNote(skipped: number, sent: number): string | null {
  if (skipped <= 0) return null;
  const done = sent - skipped;
  const legs = skipped === 1 ? "1 transfer leg" : `${skipped} transfer legs`;
  return (
    `Set the category on ${done} ${done === 1 ? "row" : "rows"} and skipped ${legs} — ` +
    "a transfer has no category."
  );
}

/**
 * The table's text sizes, smallest first, and the padding each gets (#143).
 *
 * The (−) and (+) beside Add transaction step through these. The point is
 * more rows on screen, so the padding shrinks with the text -- a smaller
 * font in a row padded for 14px buys a few pixels a row, and a row whose
 * padding follows it buys a third of the screen back. Five steps with the
 * register's long-standing 14px fourth: three to fit more in, one to read
 * more easily, and a floor and a ceiling rather than a slider, because a
 * size between two of these is not a different way of reading the list.
 */
export const TEXT_STEPS: readonly { px: number; pad: number }[] = [
  { px: 11, pad: 2 },
  { px: 12, pad: 3 },
  { px: 13, pad: 5 },
  { px: 14, pad: 7 },
  { px: 16, pad: 9 },
];
export const DEFAULT_TEXT_STEP = 3;
const isTextStep = (raw: unknown): raw is number =>
  Number.isInteger(raw) && (raw as number) >= 0 && (raw as number) < TEXT_STEPS.length;

/**
 * What a remembered setting has to look like to be used (#143).
 *
 * `useSticky` checks a plain string, number or flag by its shape. Everything
 * here has members -- an enum, a list of ids, a range -- and a value from an
 * older version, or typed into the devtools, reads as the default rather
 * than as a filter the server has never heard of.
 */
const oneOf =
  <T extends string>(...allowed: readonly T[]) =>
  (raw: unknown): raw is T =>
    typeof raw === "string" && (allowed as readonly string[]).includes(raw);
const isIdsOrEverything = (raw: unknown): raw is string[] | null =>
  raw === null || (Array.isArray(raw) && raw.every((one) => typeof one === "string"));
const isBoolean = (raw: unknown): raw is boolean => typeof raw === "boolean";
const isDay = (raw: unknown): raw is string | null =>
  raw === null || (typeof raw === "string" && /^\d{4}-\d{2}-\d{2}$/.test(raw));
const isRange = (raw: unknown): raw is Range =>
  typeof raw === "object" &&
  raw !== null &&
  isDay((raw as Range).since) &&
  isDay((raw as Range).until);
const isGrouping = oneOf<AccountGrouping>("country", "type");
const isCleared = oneOf<"" | Transaction["cleared"]>("", "uncleared", "cleared", "reconciled");
const isSource = oneOf("", ...SOURCE_CHOICES.map((one) => one.value));
const isWork = oneOf<"" | ReimbursementView>("", ...WORK_CHOICES.map((one) => one.value));
const isSortKey = oneOf<SortKey>(
  "date", "account", "payee", "category", "memo", "amount", "cleared", "source",
);
const isDirection = oneOf<SortDirection>("asc", "desc");

/**
 * One of the register's filters: as the person last left it on this device,
 * unless another screen sent them here (#143).
 *
 * A preset -- the Reimbursements report's "Show in Transactions" -- is shown
 * *instead of* what was remembered, and is not itself remembered. The report
 * is promising a view of its rows, which a remembered date range or search
 * could quietly hide; and a filter somebody was sent to is not one they set,
 * so it must not greet them the next time they open the register from the
 * menu. The moment they change one, that change is theirs and is kept.
 *
 * `sent` is read once, when the register mounts -- the same as the preset
 * always was.
 */
function useFilter<T>(
  household: string,
  name: string,
  fallback: T,
  accept: ((raw: unknown) => raw is T) | undefined,
  sent: { value: T } | undefined,
): [T, (next: T) => void] {
  const [kept, keep] = useSticky("register", household, name, fallback, accept);
  const [shown, setShown] = useState(sent);
  const set = useCallback(
    (next: T) => {
      setShown(undefined);
      keep(next);
    },
    [keep],
  );
  return [shown ? shown.value : kept, set];
}

export function Register({
  household,
  onGo,
  preset,
}: {
  household: Household;
  /** Where to open, when another screen sent somebody here. */
  preset?: RegisterPreset;
  /**
   * Switch the app to another screen. `App.tsx` owns navigation and does not
   * pass this yet; until it does, the Import button says where Import is
   * rather than pretending it can go there.
   */
  onGo?: (screen: string) => void;
}) {
  const client = useQueryClient();
  /*
   * Every filter, the sort and its direction are remembered per browser and
   * per household (#143): leaving for Accounts and coming back used to put
   * the register back to everything, newest first, and the filters had to be
   * set again. `useSticky` holds the why of where; `useFilter` is how a
   * preset from another screen still wins. The selection, and which panel
   * is open, are not remembered -- a selection is the middle of an act, and
   * coming back to one half-made is coming back to a surprise.
   *
   * Under a preset every filter starts from its default except the one the
   * preset sets. Sort and direction are how the rows are laid out, not which
   * rows, so a preset leaves them as they were.
   */
  const h = household.id;
  const sent = <T,>(value: T) => (preset ? { value } : undefined);
  const [accountIds, setAccountIds] = useFilter<Selection>(
    h, "accounts", null, isIdsOrEverything, sent(preset?.accounts ?? null),
  );
  //: How the picker gathers accounts. Not a filter -- it changes the headings
  //: you tick, not the rows -- so a preset leaves it alone too.
  const [grouping, setGrouping] = useSticky<AccountGrouping>(
    "register", h, "grouping", "country", isGrouping,
  );
  const [pickedCurrencies, setPickedCurrencies] = useFilter<string[] | null>(
    h, "currencies", null, isIdsOrEverything, sent(null),
  );
  const [search, setSearch] = useFilter(h, "search", "", undefined, sent(""));
  //: The amount lookup (#123): one box for every money column, Out and In in
  //: every currency, rather than a box per column nobody could choose between.
  const [amountText, setAmountText] = useFilter(h, "amount", "", undefined, sent(""));
  //: The Cleared and Source columns as filters (#123). Empty is "any".
  const [clearedFilter, setClearedFilter] = useFilter<"" | Transaction["cleared"]>(
    h, "cleared", "", isCleared, sent(""),
  );
  const [sourceFilter, setSourceFilter] = useFilter(h, "source", "", isSource, sent(""));
  //: Work expenses: flagged rows, and in two of the views their payments.
  const [workFilter, setWorkFilter] = useFilter<"" | ReimbursementView>(
    h, "work", "", isWork, sent(preset?.reimbursement ?? ""),
  );
  const [range, setRange] = useFilter<Range>(h, "range", ALL_DATES, isRange, sent(ALL_DATES));
  const [sort, setSort] = useSticky<SortKey>("register", h, "sort", "date", isSortKey);
  const [direction, setDirection] = useSticky<SortDirection>(
    "register", h, "direction", "desc", isDirection,
  );
  //: The table's text size, as an index into TEXT_STEPS. A preset leaves it
  //: alone for the same reason it leaves the sort: it is how, not which.
  const [textStep, setTextStep] = useSticky<number>(
    "register", h, "text", DEFAULT_TEXT_STEP, isTextStep,
  );
  //: The category picker (#188), in two halves the way the report's is: the
  //: ticked categories, and the backlog -- "Needs a category" -- which is a
  //: slice, not an id. Untouched is every category *and* the backlog ticked,
  //: which is no filter at all. Remembered under new names: the checkbox it
  //: replaced stored "only the backlog" as `uncategorised`, which read back
  //: here would mean the opposite of what it says.
  const [categoryIds, setCategoryIds] = useFilter<Selection>(
    h, "categories", null, isIdsOrEverything, sent(null),
  );
  const [backlog, setBacklogTick] = useFilter(h, "backlog", true, isBoolean, sent(true));
  //: Categorising is the one recurring chore here, and it is a worklist:
  //: what still needs doing, oldest first, worked forward. The register's own
  //: default stays newest-first -- right for "what happened lately", wrong for
  //: clearing a backlog, because you start at the end and the list reshuffles
  //: under you as you work. So the moment the view *becomes* the backlog alone
  //: -- ticked on its own, or everything else unticked around it -- it turns
  //: oldest-first. Ordinary categories leave the order alone, and so does
  //: leaving the backlog: by then the order may be one the person chose.
  const backlogOnly = categoryIds !== null && categoryIds.length === 0 && backlog;
  const pickCategories = (ids: Selection, tick: boolean) => {
    if (!backlogOnly && ids !== null && ids.length === 0 && tick) {
      setSort("date");
      setDirection("asc");
    }
    setCategoryIds(ids);
    setBacklogTick(tick);
  };
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [opened, setOpened] = useState<Row | null>(null);
  const [transferring, setTransferring] = useState(false);
  const [adding, setAdding] = useState(false);
  //: Phone only -- see `.filters-toggle`. Open on a desktop is irrelevant
  //: because the stylesheet never hides them there.
  const [filtersOpen, setFiltersOpen] = useState(false);

  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts`),
  });
  const payees = useQuery({
    queryKey: ["payees", household.id],
    queryFn: () => api.get<Payee[]>(`/households/${household.id}/payees`),
  });
  const categories = useQuery({
    queryKey: ["categories", household.id, false],
    queryFn: () => api.get<CategoryGroup[]>(`/households/${household.id}/categories`),
  });
  /**
   * Which currencies this household holds, busiest first.
   *
   * The same question the report's currency toggle asks, asked through the
   * same endpoint and cached under the same key -- read from the accounts
   * rather than from `base_currency`, which is a default for new accounts and
   * not a statement about what exists. Two screens deciding separately what
   * currencies a household has is two screens that can disagree.
   */
  const currencies = useQuery({
    queryKey: ["report-currencies", household.id],
    queryFn: () =>
      api.get<{ currencies: string[] }>(`/households/${household.id}/reports/currencies`),
  });
  //: An empty tick-list is a real answer -- "none of them" -- and not one the
  //: server can be asked, because a repeated query parameter has no spelling
  //: for it. So nothing is requested and the screen says so.
  const noAccounts = accountIds !== null && accountIds.length === 0;
  //: The same answer for categories: nothing ticked, not even the backlog.
  const noCategories = categoryIds !== null && categoryIds.length === 0 && !backlog;
  //: What the query asks for, which lags the box by a pause in the typing. The
  //: box itself stays on `search` so it never drops a character.
  const searched = useDebounced(search).trim();
  //: Debounced like the search, and normalised: "45,20" and "-45.20" both ask
  //: for 45.20. Text that is not an amount asks for nothing rather than for a
  //: filter the server would answer with no rows -- the box says so instead.
  const typedAmount = useDebounced(amountText).trim();
  const amountAsked = typedAmount ? amountLookup(typedAmount) : null;
  const amountUnreadable = amountText.trim() !== "" && amountLookup(amountText) === null;

  const query = new URLSearchParams();
  for (const id of accountIds ?? []) query.append("account_id", id);
  if (searched) query.set("search", searched);
  if (amountAsked) query.set("amount", amountAsked);
  if (clearedFilter) query.set("cleared", clearedFilter);
  if (sourceFilter) query.set("source", sourceFilter);
  if (workFilter) query.set("reimbursement", workFilter);
  if (range.since) query.set("since", range.since);
  if (range.until) query.set("until", range.until);
  //: The picker's ticks add up (#188): each one brings its rows in. Every
  //: category with the backlog unticked is `categorised`, not every id.
  const categoryFiltered = categoryIds !== null || !backlog;
  if (categoryFiltered) {
    if (categoryIds === null) query.set("categorised", "true");
    for (const id of categoryIds ?? []) query.append("category_id", id);
    if (backlog) query.set("uncategorised", "true");
  }
  query.set("sort", sort);
  query.set("direction", direction);

  const register = useQuery({
    queryKey: [
      "register",
      household.id,
      (accountIds ?? []).join(","),
      searched,
      amountAsked,
      clearedFilter,
      sourceFilter,
      workFilter,
      range.since,
      range.until,
      categoryFiltered ? (categoryIds ?? ["*"]).join(",") : null,
      categoryFiltered && backlog,
      sort,
      direction,
    ],
    // The signal lets a superseded search be aborted in flight rather than
    // downloaded and parsed for nobody.
    queryFn: ({ signal }) =>
      api.get<RegisterRows>(`/households/${household.id}/transactions?${query.toString()}`, {
        signal,
      }),
    enabled: !noAccounts && !noCategories,
  });

  /**
   * How many rows need a category, for the badge beside that tick (#188).
   *
   * Asked of the register itself -- every other filter, the backlog alone --
   * so the number is how many of *these* rows need one, and it is there
   * whether or not the tick is on: it is what tells you whether ticking it is
   * worth doing. One row, because only the total is wanted.
   */
  const backlogQuery = new URLSearchParams(query);
  for (const key of ["category_id", "categorised", "uncategorised", "sort", "direction"]) {
    backlogQuery.delete(key);
  }
  backlogQuery.set("uncategorised", "true");
  backlogQuery.set("limit", "1");
  const backlogCount = useQuery({
    queryKey: ["register", household.id, "backlog-count", backlogQuery.toString()],
    queryFn: ({ signal }) =>
      api.get<RegisterRows>(
        `/households/${household.id}/transactions?${backlogQuery.toString()}`,
        { signal },
      ),
    enabled: !noAccounts,
  });

  const onSort = (column: SortKey, next: SortDirection) => {
    setSort(column);
    setDirection(next);
  };

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["register", household.id] });
    // Any edit can move a figure on the Reimbursements report -- an amount, a
    // date into another month, a deleted payment -- not only a flag.
    client.invalidateQueries({ queryKey: ["reimbursements", household.id] });
    client.invalidateQueries({ queryKey: ["accounts", household.id] });
    client.invalidateQueries({ queryKey: ["payees", household.id] });
    client.invalidateQueries({ queryKey: ["categories", household.id] });
  };

  //: Said after the selection is gone, so it outlives the banner it came from.
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const bulk = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.post<BulkEditResult>(`/households/${household.id}/transactions/bulk`, {
        transaction_ids: [...selected],
        ...body,
      }),
    onSuccess: (answer, body) => {
      const sent = answer?.transactions.length ?? 0;
      setBulkNote(
        "category_id" in body
          ? skippedNote(answer?.skipped_transfer_legs ?? 0, sent)
          : "reimbursement" in body || "clear_reimbursement" in body
            ? reimbursementSkippedNote(answer?.skipped_reimbursement ?? 0, sent)
            : null,
      );
      setSelected(new Set());
      refresh();
    },
  });

  const rows = useMemo(() => register.data?.transactions ?? [], [register.data]);

  //: Two existing rows made the two legs of one transfer (issues #70, #71):
  //: one from each bank's statement, in the same currency or not.
  const linking = useMutation({
    mutationFn: (body: { link: [string, string] } | { unlink: string }) =>
      "link" in body
        ? api.post(`/households/${household.id}/transfers/link`, {
            pairs: [{ first_id: body.link[0], second_id: body.link[1] }],
          })
        : api.post(`/transactions/${body.unlink}/unlink`),
    onSuccess: () => {
      setSelected(new Set());
      refresh();
    },
  });
  const picked = rows.filter((row) => selected.has(row.id));
  const linkable =
    picked.length === 2 &&
    picked.every((row) => !row.transfer_account_id && !row.split_id) &&
    picked[0].account_id !== picked[1].account_id &&
    Math.sign(picked[0].amount) === -Math.sign(picked[1].amount);
  const unlinkable = picked.length === 1 && picked[0].transfer_account_id !== null;

  //: Some money out and exactly one payment in, every one of them ticked by a
  //: person and on screen: the one shape "Link as reimbursement" means. A
  //: selection reaching past the filter is refused rather than guessed at,
  //: because which of the hidden rows is the payment is not something this
  //: screen can see. The banner's sums show whether the two sides
  //: agree before anybody clicks.
  const repaidBy = picked.filter((one) => one.amount > 0);
  const repaying = picked.filter((one) => one.amount < 0);
  const reimbursable =
    picked.length === selected.size &&
    repaidBy.length === 1 &&
    repaying.length >= 1 &&
    repaying.length + repaidBy.length === picked.length &&
    picked.every((one) => !isTransferLeg(one));
  const reimbursing = useMutation({
    mutationFn: () =>
      api.post<Transaction[]>(
        `/households/${household.id}/transactions/reimbursements/link`,
        {
          expense_ids: repaying.map((one) => one.id),
          settlement_id: repaidBy[0].id,
        },
      ),
    onSuccess: (linked) => {
      const count = linked?.length ?? repaying.length;
      setBulkNote(
        `Linked ${count} ${count === 1 ? "expense" : "expenses"} to the payment of ` +
          `${format(repaidBy[0].amount, currencyOf(repaidBy[0]))} on ${repaidBy[0].date}.`,
      );
      setSelected(new Set());
      refresh();
    },
  });
  //: Whatever the last bulk act was refused with. Shown in the selection
  //: dock beside the banner the act was taken from, not above the table.
  const bulkProblem = bulk.error ?? linking.error ?? reimbursing.error;

  const accountsById = useMemo(
    () => new Map((accounts.data ?? []).map((a) => [a.id, a])),
    [accounts.data],
  );
  //: A remembered tick on an account that has since gone is dropped once the
  //: accounts arrive, so the picker's "2 of 5" counts ticks that exist. If
  //: none of them do, the filter goes back to everything rather than to
  //: "none ticked", which is a register with nothing on it and no act of the
  //: person's behind it.
  useEffect(() => {
    if (!accounts.data || accountIds === null || accountIds.length === 0) return;
    const known = accountIds.filter((id) => accountsById.has(id));
    if (known.length === accountIds.length) return;
    setAccountIds(known.length === 0 ? null : known);
  }, [accounts.data, accountsById, accountIds, setAccountIds]);
  //: Every account, in both groupings -- unlike the report, which offers only
  //: the ones in the currency on screen. The register shows every currency it
  //: has a column for, so an account is never a tick that changes nothing.
  //: Categories grouped as they are on the Categories screen, so a group
  //: heading is one tick for the lot (#188). A remembered tick on a category
  //: that has since gone is dropped, the same as an account's.
  const categoryOptions: PickerGroup[] = useMemo(
    () =>
      (categories.data ?? []).map((group) => ({
        key: group.id,
        label: group.name,
        items: group.categories.map((one) => ({ id: one.id, label: one.name })),
      })),
    [categories.data],
  );
  useEffect(() => {
    if (!categories.data || categoryIds === null || categoryIds.length === 0) return;
    const known = new Set(categoryOptions.flatMap((group) => group.items.map((one) => one.id)));
    const kept = categoryIds.filter((id) => known.has(id));
    if (kept.length === categoryIds.length) return;
    setCategoryIds(kept.length === 0 ? null : kept);
  }, [categories.data, categoryOptions, categoryIds, setCategoryIds]);
  const accountOptions = useMemo(
    () => accountGroups(accounts.data ?? [], grouping),
    [accounts.data, grouping],
  );
  /**
   * Which currency a row's amount is in.
   *
   * The row says so now. The accounts list is kept as a second answer only for
   * a row that arrived without one -- it is the weaker of the two, because it
   * omits closed accounts and a closed account's rows are still in its own
   * currency.
   */
  const currencyOf = useMemo(
    () => (txn: Row) =>
      txn.currency ?? accountsById.get(txn.account_id)?.currency ?? household.base_currency,
    [accountsById, household.base_currency],
  );

  /**
   * Which currencies there are columns to be had about.
   *
   * The household's own list while it is in hand. Until it arrives, the rows
   * themselves -- so the first paint draws a pair of columns per currency it
   * is actually about to show rather than one pair for the base currency,
   * which would hide half the register for as long as the request took. An
   * empty register still needs one pair, and the household's currency is the
   * honest guess for it.
   */
  const available = useMemo(() => {
    const listed = currencies.data?.currencies ?? [];
    if (listed.length > 0) return listed;
    const fromRows = [...new Set(rows.map(currencyOf))].sort();
    return fromRows.length > 0 ? fromRows : [household.base_currency];
  }, [currencies.data, rows, currencyOf, household.base_currency]);

  /**
   * Which of them the table is showing.
   *
   * `null` is "every currency", and it is not the same value as a frozen list
   * of today's: a household that opens a dollar account tomorrow is still
   * showing everything. Same reasoning as `GroupedPicker`'s own Selection.
   *
   * Never empty. A table with no money column at all is a register showing
   * everything except the figures.
   */
  const columns = useMemo(() => {
    const kept = (pickedCurrencies ?? available).filter((code) => available.includes(code));
    return kept.length > 0 ? kept : available;
  }, [pickedCurrencies, available]);

  //: Whether anything is narrowing the list. Collapsed filters must not hide
  //: the fact that they are set: a register showing a third of its rows and
  //: no visible reason why is a bug report waiting to be written. The currency
  //: toggle counts -- it hides rows as surely as a date does.
  const filtered =
    accountIds !== null ||
    search.trim() !== "" ||
    amountText.trim() !== "" ||
    clearedFilter !== "" ||
    sourceFilter !== "" ||
    workFilter !== "" ||
    range.since !== null ||
    range.until !== null ||
    categoryFiltered ||
    columns.length < available.length;

  /**
   * The rows the currency toggle leaves on screen.
   *
   * A row whose currency has no column would show a payee, a date and no
   * figure -- a transaction rendered as though it were for nothing. So the
   * toggle governs the rows as well as the columns, and how many it left out
   * is printed under the table rather than left to be noticed.
   */
  const visible = useMemo(
    () => rows.filter((row) => columns.includes(currencyOf(row))),
    [rows, columns, currencyOf],
  );
  const hiddenByCurrency = rows.length - visible.length;

  /**
   * The rows in the order they are drawn.
   *
   * The server's order, except under the two work views that bring payments
   * along: there each payment is followed by the expenses it repaid, so a
   * claim reads as one block rather than as a payment in October and its
   * expenses scattered through September. The grouping is in hand rather
   * than a server sort because it is an arrangement of rows already here.
   */
  const groupedByPayment = workFilter === "work" || workFilter === "paid";
  const { ordered, repaidBelow } = useMemo(() => {
    if (!groupedByPayment) return { ordered: visible, repaidBelow: new Set<string>() };
    const grouped = groupByPayment(visible);
    return {
      ordered: grouped.map((one) => one.row),
      repaidBelow: new Set(grouped.filter((one) => one.child).map((one) => one.row.id)),
    };
  }, [visible, groupedByPayment]);

  // Everything the filter matched is already here; this decides how much of it
  // is in the DOM. See `useWindowed` for the measurements behind the split.
  const page = useWindowed(ordered);

  /**
   * Every column of the table, left to right, by what it holds (#74).
   *
   * The order has to be the order the headings are drawn in: the first drag
   * measures the heading row cell by cell and files each width under the key
   * at the same place here.
   */
  const hasBalance = register.data?.has_running_balance ?? false;
  const columnKeys = useMemo(
    () => [
      "select",
      "transfer",
      "date",
      "account",
      "payee",
      "category",
      "memo",
      ...columns.flatMap((code) => [`out-${code}`, `in-${code}`]),
      ...(hasBalance ? ["balance"] : []),
      "cleared",
      "source",
    ],
    [columns, hasBalance],
  );
  const widths = useColumnWidths(columnKeys, fallbackWidth);

  /**
   * What the selection comes to, one line per currency.
   *
   * Counted over every row the filter matched rather than over the rows on
   * screen, so a selection the currency toggle has since hidden is still in
   * the total that claims to describe it.
   */
  const sums = useMemo(
    () => selectionSums(rows, selected, currencyOf, available),
    [rows, selected, currencyOf, available],
  );
  const counted = sums.reduce((all, one) => all + one.count, 0);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  /**
   * The heading's tick box: every row the filter shows, or none of them
   * (#143).
   *
   * "Shows" is `ordered` -- everything the filter and the currency toggle
   * leave, including the rows further down that the windowing has not put in
   * the DOM yet. The register fetches the whole filtered set, so there is no
   * page beyond this one for "all" to be ambiguous about.
   *
   * Ticked when every one of them is, indeterminate when some are, and a
   * click on a full box empties it. Rows selected earlier under another
   * filter are neither counted nor cleared by it: the box is about the rows
   * in front of you, and the banner already says how many are selected out
   * of sight and has Clear selection for all of them.
   */
  const tickedShown = ordered.reduce((n, one) => n + (selected.has(one.id) ? 1 : 0), 0);
  const allShownTicked = ordered.length > 0 && tickedShown === ordered.length;
  function tickAllShown() {
    const next = new Set(selected);
    for (const one of ordered) {
      if (allShownTicked) next.delete(one.id);
      else next.add(one.id);
    }
    setSelected(next);
  }

  /**
   * Where the last plain click landed, so Shift has something to reach from.
   *
   * An index into the rows as currently shown, so it is dropped whenever that
   * order changes -- a Shift-click after re-sorting would otherwise select a
   * run of rows nobody pointed at.
   */
  const anchor = useRef<number | null>(null);
  useEffect(() => {
    anchor.current = null;
  }, [ordered, sort, direction]);

  //: A row another screen asked to see. Opened once, as soon as the rows it
  //: is among have arrived, and then forgotten -- the panel is not reopened
  //: every time the register refetches behind it.
  const openOnArrival = useRef(preset?.open ?? null);
  useEffect(() => {
    if (!openOnArrival.current || !register.data) return;
    const wanted = register.data.transactions.find((one) => one.id === openOnArrival.current);
    openOnArrival.current = null;
    if (wanted) setOpened(wanted);
  }, [register.data]);

  function pickRow(index: number, event: React.MouseEvent) {
    // The row is a selection target *and* holds the controls that open, edit
    // and sort it. Anything that is its own control keeps its own click.
    const target = event.target as HTMLElement;
    if (target.closest("button, a, input, select, textarea, label, td.editing")) return;

    const shown = page.visible;
    if (event.shiftKey && anchor.current !== null && anchor.current < shown.length) {
      // Shift-click clears the browser's own text selection first: dragging a
      // highlight across half the register is not what the gesture meant.
      window.getSelection()?.removeAllRanges();
      const [from, to] = [anchor.current, index].sort((a, b) => a - b);
      const next = new Set(selected);
      for (let at = from; at <= to; at++) next.add(shown[at].id);
      setSelected(next);
      return;
    }

    toggle(shown[index].id);
    anchor.current = index;
  }

  return (
    /* The screen fills the window and does not scroll; the rows do. Everything
       else -- the heading, the filters, the column headings, the entry box --
       stays where you left it. */
    <div className="screen-fill">
      {/* Reaching a transaction from the keyboard meant walking two entry
          buttons, the filters toggle, a date range of up to six controls, the
          account picker, the search box, Clear filters and nine sortable
          column headings -- about twenty stops, every one of them a control
          you set once a session, on the screen the app opens on.

          The conventional fix, and the cheapest: hidden until focused, and it
          lands on the first row rather than on the table, because the table is
          another thing to arrow past. The within-row order -- tick, date,
          payee, category, memo -- is already right and is not touched. */}
      <a className="skip-link" href="#register-rows">
        Skip to transactions
      </a>
      {/* The three ways a transaction gets into this ledger, together at the
          top right. They are the same kind of act -- one of them by hand, one
          in a pair, one by the hundred -- so they are one group rather than
          two beside the heading and a third in the nav. Since #143 the first
          two share the Add button's menu. */}
      <div className="row register-title">
        {/* "Transactions", the word the menu item uses (#143). The menu's
            section is still called Register -- it holds Import and Receipts
            too -- but the page you land on should be called what you clicked
            to get there. The code keeps calling it the register. */}
        <h1>Transactions</h1>
        {/* Beside the heading rather than in the filter line (#143). It
            decides which columns the table *has*, which is a question about
            the table's shape before it is one about which rows -- and on the
            filter line it cost the accounts picker and the search box the
            width they needed. */}
        <CurrencyColumns available={available} columns={columns} onChange={setPickedCurrencies} />
        {/* Both entry routes open as panels. The add form used to sit in a
            card *below* the register, which cost the rows a third of the
            screen permanently to serve the minority of visits that add
            something by hand. */}
        <div className="row register-actions">
          {/* One button, two answers (#143). A row by hand and a transfer are
              both "adding", and two primary-looking buttons side by side
              made the header read as a toolbar. The transfer still gets its
              own form -- money between two of your own accounts is one act
              writing two rows -- it is just reached through the one door. */}
          {/* Left of Add, where they sit beside the table's top edge rather
              than among the filters: they change how the rows are drawn, not
              which rows there are. Hidden on a phone, where each row is a card
              with its own sizes and these would change nothing. */}
          <div className="text-steps" role="group" aria-label="Table text size">
            <button
              type="button"
              className="small-button"
              aria-label="Smaller text in the table"
              title="Smaller text, more rows on screen"
              disabled={textStep <= 0}
              onClick={() => setTextStep((at) => Math.max(0, at - 1))}
            >
              <span aria-hidden="true">−</span>
            </button>
            <button
              type="button"
              className="small-button"
              aria-label="Larger text in the table"
              title="Larger text"
              disabled={textStep >= TEXT_STEPS.length - 1}
              onClick={() => setTextStep((at) => Math.min(TEXT_STEPS.length - 1, at + 1))}
            >
              <span aria-hidden="true">+</span>
            </button>
          </div>
          <AddMenu onSingle={() => setAdding(true)} onTransfer={() => setTransferring(true)} />
          {/* Import is a screen, not a panel, and this screen does not own
              navigation. With `onGo` it goes there; without it, it says where
              to find it rather than doing nothing when clicked. */}
          <button
            className="small-button"
            disabled={!onGo}
            title={
              onGo
                ? "Read a statement file into this register"
                : "Import is in the menu on the left — this screen was given no way to navigate"
            }
            onClick={() => onGo?.("import")}
          >
            <span aria-hidden="true">📥</span> Import
          </button>
        </div>
      </div>

      <div className="card register-card">
        {/* Above the scroller, so it simply stays where it is -- no sticky,
            nothing to measure, and nothing to get wrong when the filters wrap. */}
        <div className="register-head">
        {/* Shown only on a phone, where the filters cost 225px before the
            first transaction -- a quarter of the screen spent on controls
            that are set once and then scrolled past. On a desktop this button
            is not rendered at all by the stylesheet and the filters are
            simply there. */}
        <button
          type="button"
          className="filters-toggle"
          aria-expanded={filtersOpen}
          onClick={() => setFiltersOpen(!filtersOpen)}
        >
          {filtersOpen ? "Hide filters" : "Filters"}
          {filtered ? <span className="nav-badge" aria-label="filters are set">•</span> : null}
        </button>
        <div className={filtersOpen ? "filters" : "filters collapsed"}>
          {/* One line: the presets at the left edge, the exact months at the
              right. Two lines of filter above a table is two lines of
              transactions not shown. */}
          {/* "Latest 3 months" is not offered here (#123): between "This
              month" and "Latest 6 months" it was the chip nobody reached for,
              and the register's filter bar is short of room. */}
          <DateRange value={range} onChange={setRange} omit={["3-months"]} />
          <div className="row filter-line">
            {/* Grouped, the way the income-and-expense report groups it:
                "the three Spanish accounts" is one tick, not three.

                Labelled like the boxes beside it (#143), and the label is
                where the grouping is chosen: "Country" and "Type" in it are
                the two answers, pressed or not. It was a separate select
                beside the picker, which made the picker the one control on
                the line with no label above it and so sit lower than Search
                and Amount. The grouping is not a filter -- it changes the
                headings you tick, not the accounts that exist -- which is why
                it can live in a label at all. */}
            <div className="field picker-with-mode">
              <div className="field-head" role="group" aria-label="Group accounts by">
                <span>
                  Accounts (
                  <button
                    type="button"
                    className="grouping-toggle"
                    aria-pressed={grouping === "country"}
                    title="Gather the accounts by country"
                    onClick={() => setGrouping("country")}
                  >
                    Country
                  </button>
                  /
                  <button
                    type="button"
                    className="grouping-toggle"
                    aria-pressed={grouping === "type"}
                    title="Gather the accounts by type: checking, savings, cards…"
                    onClick={() => setGrouping("type")}
                  >
                    Type
                  </button>
                  )
                </span>
              </div>
              <GroupedPicker
                label="All accounts"
                groups={accountOptions}
                value={accountIds}
                onChange={setAccountIds}
              />
            </div>
            <Field label="Search">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="payee, memo, or the bank's own words"
              />
            </Field>
            {/* Every money column at once: Out or In, in whichever currency
                the row is in. A whole number is the whole unit -- "45" finds
                45.20 -- because an amount is usually remembered to the euro,
                not to the cent. */}
            <label className="field amount-lookup">
              <span>Amount</span>
              <input
                value={amountText}
                onChange={(e) => setAmountText(e.target.value)}
                placeholder="in or out, any currency"
                inputMode="decimal"
                aria-invalid={amountUnreadable || undefined}
                title={amountUnreadable ? "That isn't an amount" : undefined}
              />
            </label>
            <label className="field narrow-filter">
              <span>Cleared</span>
              <select
                className="small"
                value={clearedFilter}
                onChange={(e) => setClearedFilter(e.target.value as typeof clearedFilter)}
              >
                <option value="">Any</option>
                <option value="uncleared">U — Uncleared</option>
                <option value="cleared">C — Cleared</option>
                <option value="reconciled">L — Locked</option>
              </select>
            </label>
            <label className="field narrow-filter">
              <span>Source</span>
              <select
                className="small"
                value={sourceFilter}
                onChange={(e) => setSourceFilter(e.target.value)}
              >
                <option value="">Any</option>
                {SOURCE_CHOICES.map((one) => (
                  <option key={one.value} value={one.value}>
                    {one.label}
                  </option>
                ))}
              </select>
            </label>
            {/* Its own select rather than a fifth Source: where a row came
                from and whether work owes you for it are different
                questions, and a row can be imported *and* a work expense. */}
            <label className="field narrow-filter">
              <span>Work expenses</span>
              <select
                className="small"
                value={workFilter}
                onChange={(e) => setWorkFilter(e.target.value as typeof workFilter)}
              >
                <option value="">All transactions</option>
                {WORK_CHOICES.map((one) => (
                  <option key={one.value} value={one.value}>
                    {one.label}
                  </option>
                ))}
              </select>
            </label>
            {/* Where the backlog checkbox was (#188). The backlog is the
                first tick rather than a category with a sentinel id -- "no
                category" is not a category id -- and it still leaves out
                transfers, which have no category by design. Ticking a group
                heading ticks every category in it. */}
            <div className="field category-filter">
              <div className="field-head">
                <span>Categories</span>
              </div>
              <GroupedPicker
                label="All categories"
                groups={categoryOptions}
                value={categoryIds}
                onChange={(ids) => pickCategories(ids, backlog)}
                extra={{
                  label: "Needs a category",
                  count: backlogCount.data?.total,
                  checked: backlog,
                  onChange: (tick) => pickCategories(categoryIds, tick),
                }}
              />
            </div>
            {filtered && (
              <button
                onClick={() => {
                  setAccountIds(null);
                  setPickedCurrencies(null);
                  setSearch("");
                  setAmountText("");
                  setClearedFilter("");
                  setSourceFilter("");
                  setWorkFilter("");
                  setRange(ALL_DATES);
                  setCategoryIds(null);
                  setBacklogTick(true);
                }}
              >
                Clear filters
              </button>
            )}
          </div>
        </div>

        {/* Only the register's own load failing is said up here. What a bulk
            act answers -- its note or its refusal -- belongs to the act, so it
            is said in the dock at the bottom, where the act was taken (#144). */}
        <Problem error={register.error} />

        {/* How many rows the filter shows, right above them -- the number the
            heading's tick box selects. Only while there are rows; the empty
            states below say their own thing. */}
        {ordered.length > 0 ? (
          <p className="small muted register-count">
            {ordered.length.toLocaleString()} {ordered.length === 1 ? "transaction" : "transactions"}
            {filtered ? " match the filters" : ""}
            {selected.size > 0 ? ` · ${selected.size.toLocaleString()} selected` : ""}
          </p>
        ) : null}
        </div>

        {noCategories && !noAccounts ? (
          <Empty>
            No categories are ticked, not even Needs a category, so there is nothing to show.
            Tick one in the categories filter, or Select all.
          </Empty>
        ) : noAccounts ? (
          <Empty>
            No accounts are ticked, so there is nothing to show. Tick one in the accounts
            filter — or Select all, which is not the same as ticking every one of them.
          </Empty>
        ) : register.data?.transactions.length === 0 ? (
          <Empty>
            Nothing here yet. Add a row above, or import a statement from the Import screen.
          </Empty>
        ) : visible.length === 0 && hiddenByCurrency > 0 ? (
          /* Only when the toggle is what emptied it. While the request is
             still out there are no rows for a different reason, and an empty
             table waiting for them is what this screen has always shown. */
          <Empty>
            Every row here is in a currency the toggle is hiding. Turn one back on to see
            them — {hiddenByCurrency} {hiddenByCurrency === 1 ? "row is" : "rows are"} waiting.
          </Empty>
        ) : (
          <div className="table-scroll">
            {/* `sized` once anybody has dragged an edge: fixed layout, one
                `<col>` per column, and the table exactly as wide as they add
                up to. Until then the browser sizes it, as it always has. The
                width is a custom property rather than `style.width` so the
                phone's card layout, which has its own idea of a table's
                width, never sees it. */}
            <table
              className={widths.sized ? "sized" : undefined}
              style={
                {
                  // The text size and the padding that goes with it (#143),
                  // as properties the stylesheet reads above the phone
                  // breakpoint only -- a card has its own sizes.
                  "--register-text": `${TEXT_STEPS[textStep].px}px`,
                  "--register-pad": `${TEXT_STEPS[textStep].pad}px`,
                  ...(widths.sized
                    ? {
                        "--table-width": `${tableWidth(columnKeys, widths.widths, fallbackWidth)}px`,
                      }
                    : {}),
                } as CSSProperties
              }
            >
              {widths.sized ? (
                <colgroup>
                  {columnKeys.map((key) => (
                    <col key={key} data-col={key} style={{ width: widths.widthOf(key) }} />
                  ))}
                </colgroup>
              ) : null}
              <thead>
                <tr>
                  <th style={{ width: 28 }} data-select="true">
                    <input
                      type="checkbox"
                      aria-label="Select every row shown"
                      title={
                        allShownTicked
                          ? "Untick every row shown"
                          : `Tick all ${ordered.length.toLocaleString()} rows the filter shows`
                      }
                      checked={allShownTicked}
                      ref={(box) => {
                        // The third state, only reachable from script: some
                        // of the rows shown, not all.
                        if (box) box.indeterminate = tickedShown > 0 && !allShownTicked;
                      }}
                      onChange={tickAllShown}
                      style={{ width: "auto" }}
                    />
                  </th>
                  {/* The transfer mark's column (#143): always there, whether
                      or not the filter shows a transfer, so ticking a filter
                      never shifts every column one mark to the side. It has no
                      sort of its own -- the Source heading already sorts
                      transfers together, and a second arrow for the same
                      order would be two ways to say one thing. The word is
                      for a screen reader; sighted, the glyph is the heading. */}
                  <th className="transfer-col">
                    <span className="sr-only">Transfer</span>
                  </th>
                  {[
                    { label: "Date", column: "date" as SortKey },
                    { label: "Account", column: "account" as SortKey },
                    { label: "Payee", column: "payee" as SortKey },
                    { label: "Category", column: "category" as SortKey },
                    { label: "Memo", column: "memo" as SortKey },
                  ].map((one) => (
                    <SortHeading
                      key={one.column}
                      label={one.label}
                      column={one.column}
                      sort={sort}
                      direction={direction}
                      onSort={onSort}
                    >
                      <ColumnEdge column={one.column} label={one.label} widths={widths} />
                    </SortHeading>
                  ))}
                  {/* One Out and one In **per currency**, never a column that
                      holds two. Out and In are two views of one stored number,
                      so they sort the same column -- and that sort is by
                      currency first and then the figure, which is what keeps
                      four money columns from reading as one ranking.

                      The code is in the heading only when there is more than
                      one currency in the household: a single-currency ledger
                      should not have to read "Out (EUR)" on every screen to be
                      told what it already knows. */}
                  {columns.map((code, at) => (
                    <Fragment key={code}>
                      <SortHeading
                        label={available.length > 1 ? `Out ${code}` : "Out"}
                        column="amount"
                        sort={sort}
                        direction={direction}
                        onSort={onSort}
                        align="right"
                        className="money-open"
                      >
                        <ColumnEdge column={`out-${code}`} label={`Out ${code}`} widths={widths} />
                      </SortHeading>
                      <SortHeading
                        label={available.length > 1 ? `In ${code}` : "In"}
                        column="amount"
                        sort={sort}
                        direction={direction}
                        onSort={onSort}
                        align="right"
                        className={at === columns.length - 1 ? "money-close" : undefined}
                      >
                        <ColumnEdge column={`in-${code}`} label={`In ${code}`} widths={widths} />
                      </SortHeading>
                    </Fragment>
                  ))}
                  {/* The one column that does not sort. A running balance only
                      exists under a date sort on one account -- it *is* that
                      order, accumulated -- so sorting by it would either be a
                      no-op or produce a column of numbers that no longer add
                      up to anything. */}
                  {hasBalance ? (
                    <th className="amount">
                      Balance
                      <ColumnEdge column="balance" label="Balance" widths={widths} />
                    </th>
                  ) : null}
                  {/* Both hold one letter and both were sized by their
                      heading rather than by their value -- Source was six
                      characters wide to draw one. The letter is drawn, the
                      word is the accessible name, and `flag-col` takes the
                      column down to its content. */}
                  <SortHeading
                    label="Cleared"
                    short="C"
                    column="cleared"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="flag-col"
                  >
                    <ColumnEdge column="cleared" label="Cleared" widths={widths} />
                  </SortHeading>
                  <SortHeading
                    label="Source"
                    short="S"
                    column="source"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    className="flag-col"
                  >
                    <ColumnEdge column="source" label="Source" widths={widths} />
                  </SortHeading>
                </tr>
              </thead>
              <tbody id="register-rows" tabIndex={-1}>
                {page.visible.map((txn, index) => (
                  <tr
                    key={txn.id}
                    className={selected.has(txn.id) ? "row-pick selected" : "row-pick"}
                    onClick={(event) => pickRow(index, event)}
                  >
                    <td data-select="true">
                      <input
                        type="checkbox"
                        aria-label={`Select ${txn.date}`}
                        checked={selected.has(txn.id)}
                        onChange={() => toggle(txn.id)}
                        style={{ width: "auto" }}
                      />
                    </td>
                    <td className="transfer-col" data-label="Transfer">
                      <TransferMark
                        txn={txn}
                        otherAccount={
                          txn.transfer_account_id
                            ? accountsById.get(txn.transfer_account_id)?.name
                            : undefined
                        }
                      />
                    </td>
                    <td data-label="Date" data-detail-first="true">
                      {repaidBelow.has(txn.id) ? (
                        <span
                          className="work-child"
                          title="Repaid by the payment above"
                          aria-label="Repaid by the payment above"
                        >
                          ↳
                        </span>
                      ) : null}
                      <button className="link" onClick={() => setOpened(txn)}>
                        {txn.date}
                      </button>
                    </td>
                    <td
                      className="small muted"
                      data-label="Account"
                      title={accountsById.get(txn.account_id)?.name}
                    >
                      {accountsById.get(txn.account_id)?.name ?? "—"}
                    </td>
                    <EditableCell
                      txn={txn}
                      field="payee"
                      value={txn.payee_name}
                      payees={payees.data ?? []}
                      onSaved={refresh}
                    />
                    <CategoryCell
                      txn={txn}
                      groups={categories.data ?? []}
                      onSaved={refresh}
                    />
                    <EditableCell
                      txn={txn}
                      field="memo"
                      value={txn.memo}
                      payees={payees.data ?? []}
                      onSaved={refresh}
                      mark={workState(txn) === "none" ? null : <WorkMark txn={txn} />}
                    />
                    {/* Money out and money in are different questions, so they
                        get different columns. One signed column makes you read
                        a minus sign to answer either of them.

                        A row puts its figure in its **own** currency's pair and
                        leaves every other pair empty -- which is what makes a
                        column of figures a column of one currency, and what
                        stops a reader adding down a column that could not be
                        added. On a phone the empty cells disappear entirely
                        (`td:empty`), so a row is still one line of money. */}
                    {columns.map((code, at) => {
                      const own = currencyOf(txn) === code;
                      const suffix = available.length > 1 ? ` ${code}` : "";
                      const last = at === columns.length - 1;
                      return (
                        <Fragment key={code}>
                          <td
                            className="amount neg money-open"
                            data-label={`Out${suffix}`}
                            data-figure="true"
                          >
                            {own && txn.amount < 0 ? format(-txn.amount, code) : ""}
                          </td>
                          <td
                            className={last ? "amount pos money-close" : "amount pos"}
                            data-label={`In${suffix}`}
                            data-figure="true"
                          >
                            {own && txn.amount > 0 ? format(txn.amount, code) : ""}
                          </td>
                        </Fragment>
                      );
                    })}
                    {register.data?.has_running_balance ? (
                      <td className="amount muted" data-label="Balance">
                        {txn.running_balance === null
                          ? ""
                          : format(txn.running_balance, currencyOf(txn))}
                      </td>
                    ) : null}
                    <td className="small muted cleared-cell flag-col" data-label="Cleared">
                      <Cleared state={txn.cleared} />
                    </td>
                    <td className="flag-col" data-label="Source">
                      <Source txn={txn} />
                      {txn.has_receipt ? <ReceiptMark /> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* The sentinel. Crossing it hands the next few hundred rows to the
                browser; there is no request behind it. */}
            {!page.allShown && (
              <button
                type="button"
                className="more-rows"
                ref={page.sentinelRef}
                onClick={page.extend}
              >
                Showing {page.shown.toLocaleString()} of {page.total.toLocaleString()} — show more
              </button>
            )}
          </div>
        )}

        {/* Said out loud, for the same reason the report prints what it left
            out: a register showing fewer rows than it matched, with nothing on
            screen to say why, is how somebody reads a slice of their ledger
            and believes it is all of it. */}
        {hiddenByCurrency > 0 && visible.length > 0 ? (
          <p className="small muted" style={{ marginTop: 10 }}>
            {hiddenByCurrency.toLocaleString()}{" "}
            {hiddenByCurrency === 1 ? "row is" : "rows are"} not shown: the currency toggle is
            showing {columns.join(" and ")}, and{" "}
            {hiddenByCurrency === 1 ? "that row is" : "those rows are"} in another.
          </p>
        ) : null}

        {groupedByPayment && repaidBelow.size > 0 ? (
          <p className="small muted" style={{ marginTop: 10 }}>
            Each payment from work is followed by the expenses it repaid, marked ↳. The rest are
            in the order the headings say.
          </p>
        ) : null}

        {register.data?.capped ? (
          <div className="banner warn" style={{ marginTop: 10 }}>
            This household has {register.data.total.toLocaleString()} transactions and the register
            hands over {register.data.transactions.length.toLocaleString()} at a time. Narrow it
            with the dates or an account to see the rest.
          </div>
        ) : null}

        {register.data && !register.data.has_running_balance && register.data.total > 0 ? (
          <p className="small muted" style={{ marginTop: 10 }}>
            A running balance needs one account, newest-first by date, and no filters — it is a
            sum down the page, so in any other order or with rows hidden it would not mean
            anything.
          </p>
        ) : null}

        {/* The selection dock (#144): the banner with the sums and the bulk
            acts, pinned to the bottom of the screen while anything is
            selected, with the note and the error a bulk act leaves behind.

            #143 put the banner above the rows, under the count line. That
            was a misreading of "at the bottom of the list", and it was also
            the wrong place in practice: tick a row two hundred lines down and
            the sums and the acts were two hundred lines up. Straight after
            the table in the flow would be as far the other way. Pinned, it is
            in reach from wherever the ticking happened.

            How it pins is the stylesheet's business, and it is one rule for
            both layouts because the card is the last thing in either: on a
            desktop the card is a flex column the height of the window, so the
            dock is simply its last item and the scroller above it gives up
            the room -- nothing is covered. On a phone or a short window the
            page scrolls instead, and `position: sticky; bottom: 0` holds it
            to the bottom edge; because it is still in the flow, scrolling to
            the end brings the last rows out from under it rather than
            leaving them behind it.

            The note outlives the selection it came from (the act clears the
            selection), so the dock is here for either one. The count line
            stays above the table: it says what the filter shows, which is
            about the rows, not about the selection. */}
        {selected.size > 0 || bulkNote || bulkProblem ? (
          <div className="selection-dock">
            {bulkNote ? (
              <div className="banner info" role="status">
                {bulkNote}{" "}
                <button className="link" onClick={() => setBulkNote(null)}>
                  OK
                </button>
              </div>
            ) : null}

            <Problem error={bulkProblem} />

            {selected.size > 0 && (
              <div className="banner info selection-banner">
                {/* What the selection comes to, before what can be done to it:
                    adding up a run of rows is why most selections are made at
                    all, and it used to mean a calculator beside the screen. */}
                <div className="selection-sums">
                  {sums.map((sum) => (
                    <p className="sum-line" key={sum.currency}>
                      <span className="sum-label">Sum</span>
                      <span className="mono">{sum.currency}</span>
                      <span className="amount pos">{format(sum.ins, sum.currency)}</span>
                      <span aria-hidden="true">+</span>
                      <span className="amount neg">{format(sum.outs, sum.currency)}</span>
                      <span aria-hidden="true">=</span>
                      <strong className="amount">{format(sum.net, sum.currency)}</strong>
                      <span className="small muted">
                        ({sum.count} {sum.count === 1 ? "line" : "lines"})
                      </span>
                    </p>
                  ))}
                  {sums.length > 1 && (
                    /* A div rather than a paragraph: the Hint's bubble is made of
                       paragraphs, and a <p> inside a <p> is closed by the parser
                       before it starts. */
                    <div className="small muted">
                      One line per currency, and no total across them.{" "}
                      <Hint label="why the currencies are not added">
                        <p>
                          This ledger never converts. Currency lives on the account and there is no
                          exchange rate stored anywhere, so a figure adding{" "}
                          {sums.map((one) => one.currency).join(" and ")} would be a number with
                          nothing behind it.
                        </p>
                        <p className="muted small" style={{ marginBottom: 0 }}>
                          Each currency is totalled on its own instead. Nothing on this screen ever
                          adds two together.
                        </p>
                      </Hint>
                    </div>
                  )}
                  {counted < selected.size && (
                    <p className="small muted" style={{ margin: 0 }}>
                      {selected.size - counted} of the selected rows are outside the current filter
                      and are not in the figures above — they are still selected, and a change made
                      here still reaches them.
                    </p>
                  )}
                </div>
                <div>
                  <strong>{selected.size} selected.</strong> Changing them is one act, so undo puts
                  all of them back together.
                </div>
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  {/* A picker rather than the typeahead the cells use: this is one
                      choice applied to many rows, so it is worth seeing the whole
                      list before committing to it. */}
                  <select
                    aria-label="Set the category on the selected rows"
                    value=""
                    onChange={(e) => {
                      if (!e.target.value) return;
                      bulk.mutate(
                        e.target.value === "__none"
                          ? { clear_category: true }
                          : { category_id: e.target.value },
                      );
                    }}
                    style={{ width: "auto", minWidth: "14em" }}
                  >
                    <option value="">Set the category…</option>
                    <option value="__none">Uncategorised</option>
                    {(categories.data ?? []).map((group) => (
                      <optgroup key={group.id} label={group.name}>
                        {group.categories.map((one) => (
                          <option key={one.id} value={one.id}>
                            {one.name}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  {/* Flag, write off or clear, in one act over the selection.
                      Money in and transfer legs are left alone and counted,
                      and the note after says how many. */}
                  <select
                    aria-label="Set work expense on the selected rows"
                    value=""
                    onChange={(e) => {
                      const value = e.target.value;
                      if (!value) return;
                      bulk.mutate(
                        value === "__none" ? { clear_reimbursement: true } : { reimbursement: value },
                      );
                    }}
                    style={{ width: "auto", minWidth: "12em" }}
                  >
                    <option value="">Work expense…</option>
                    <option value="expected">Work should pay these back</option>
                    <option value="written_off">Written off — work will not pay</option>
                    <option value="__none">Not a work expense</option>
                  </select>
                  {reimbursable && (
                    <button
                      className="link"
                      disabled={reimbursing.isPending || repaying.length > LINK_MAX_EXPENSES}
                      onClick={() => reimbursing.mutate()}
                      title={
                        repaying.length > LINK_MAX_EXPENSES
                          ? `At most ${LINK_MAX_EXPENSES} expenses at a time`
                          : "The money out, repaid by the one payment in"
                      }
                    >
                      Link as reimbursement
                    </button>
                  )}
                  {linkable && (
                    <button
                      className="link"
                      disabled={linking.isPending}
                      onClick={() => linking.mutate({ link: [picked[0].id, picked[1].id] })}
                      title="One row from each account's statement, made the two sides of one transfer"
                    >
                      Link as transfer
                    </button>
                  )}
                  {unlinkable && (
                    <button
                      className="link"
                      disabled={linking.isPending}
                      onClick={() => linking.mutate({ unlink: picked[0].id })}
                    >
                      Unlink transfer
                    </button>
                  )}
                  <button className="link" onClick={() => bulk.mutate({ cleared: "cleared" })}>
                    Mark cleared
                  </button>
                  <button className="link" onClick={() => bulk.mutate({ cleared: "uncleared" })}>
                    Mark uncleared
                  </button>
                  <button className="link" onClick={() => setSelected(new Set())}>
                    Clear selection
                  </button>
                </div>
              </div>
            )}
          </div>
        ) : null}
      </div>

      {adding && (
        <Panel title="Add a transaction" onClose={() => setAdding(false)}>
          <QuickEntry
            household={household}
            accounts={accounts.data ?? []}
            payees={payees.data ?? []}
            groups={categories.data ?? []}
            /* The account the filter is on, when it is on exactly one --
               entering rows by hand almost always continues the account you
               were just reading. Several ticked is not an answer to "which
               account", so the form opens on its own first choice. */
            defaultAccountId={accountIds?.length === 1 ? accountIds[0] : ""}
            /* The panel deliberately stays open: entering a handful of rows
               off one receipt is the reason this form is used by hand at all,
               and closing after each would make that five round trips. The
               register behind it updates as they land. */
            onAdded={refresh}
          />
        </Panel>
      )}

      {transferring && (
        <Transfer
          household={household}
          accounts={accounts.data ?? []}
          onClose={() => setTransferring(false)}
          onDone={() => {
            setTransferring(false);
            refresh();
          }}
        />
      )}

      {opened && (
        <TransactionPanel
          txn={opened}
          household={household}
          accountNameOf={(id) => accountsById.get(id)?.name ?? ""}
          householdId={household.id}
          currency={currencyOf(opened)}
          accountName={accountsById.get(opened.account_id)?.name ?? ""}
          payees={payees.data ?? []}
          groups={categories.data ?? []}
          onClose={() => setOpened(null)}
          onChanged={() => {
            setOpened(null);
            refresh();
          }}
          onSaved={refresh}
        />
      )}
    </div>
  );
}

/**
 * "Add transaction", as a menu of the two ways in (#143).
 *
 * The menu-button pattern, because that is what a screen reader expects of a
 * button that opens a short list of acts: `aria-haspopup="menu"`, the first
 * item focused on opening, arrows to move, Escape or a click anywhere else
 * to close. Escape and choosing hand focus back to where it was, so the
 * keyboard does not land on the page's first control after a cancel.
 */
function AddMenu({ onSingle, onTransfer }: { onSingle: () => void; onTransfer: () => void }) {
  const [open, setOpen] = useState(false);
  const holder = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();

  // Closed by Escape or a click anywhere else, the way the currency "+" is.
  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) items.current[0]?.focus();
  }, [open]);

  const choose = (act: () => void) => {
    setOpen(false);
    act();
  };

  const onItemKey = (event: React.KeyboardEvent, at: number) => {
    const count = items.current.length;
    const to =
      event.key === "ArrowDown"
        ? (at + 1) % count
        : event.key === "ArrowUp"
          ? (at - 1 + count) % count
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? count - 1
              : null;
    if (to !== null) {
      event.preventDefault();
      items.current[to]?.focus();
    } else if (event.key === "Tab") {
      // Tab leaves the menu the way it leaves anything else, and shuts it.
      setOpen(false);
    }
  };

  const choices = [
    { label: "Single transaction", hint: "One row, typed in by hand", act: onSingle },
    { label: "Transfer", hint: "Between two of your own accounts — two rows, one act", act: onTransfer },
  ];

  return (
    <div className="add-menu" ref={holder}>
      <button
        ref={button}
        type="button"
        className="primary small-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((was) => !was)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" && !open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span aria-hidden="true">➕</span> Add transaction <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="add-menu-list" role="menu" id={menuId} aria-label="Add">
          {choices.map((one, at) => (
            <button
              key={one.label}
              ref={(node) => {
                items.current[at] = node;
              }}
              type="button"
              role="menuitem"
              title={one.hint}
              onClick={() => choose(one.act)}
              onKeyDown={(event) => onItemKey(event, at)}
            >
              {one.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Which currencies the table has columns for.
 *
 * The report's currency toggle is a radio group -- exactly one, never two,
 * because a column adding EUR to GBP would be a number with nothing behind it.
 * The register is a list rather than a matrix, so it can afford the other
 * answer to the same rule: **a pair of columns each**, and a row only ever
 * fills its own pair. Nothing is added across them here either.
 *
 * So these are tick boxes in spirit where the report's are radio buttons, and
 * the vocabulary is deliberately the report's: the same chips, the same
 * heading, the same explanation of why there is no total.
 *
 * The last one on cannot be turned off. A register with no money column is a
 * list of payees.
 */
/**
 * How many currency chips sit beside the heading before the rest go behind a
 * "+" (#123). Four was what fitted beside the accounts picker and the search
 * box when the chips were on the filter line; beside the heading (#143) it is
 * what fits between "Transactions" and the buttons at a laptop width. A
 * household with more than that is rare, and the rest are one click away
 * rather than gone.
 */
export const CURRENCY_CHIPS = 4;

function CurrencyColumns({
  available,
  columns,
  onChange,
}: {
  available: string[];
  columns: string[];
  onChange: (next: string[] | null) => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const moreHolder = useRef<HTMLDivElement>(null);

  // Closed by Escape or a click anywhere else, the way the accounts picker is.
  useEffect(() => {
    if (!moreOpen) return;
    const away = (event: MouseEvent) => {
      if (!moreHolder.current?.contains(event.target as Node)) setMoreOpen(false);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setMoreOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key, true);
    };
  }, [moreOpen]);

  if (available.length < 2) return null;

  const flip = (code: string) => {
    const next = columns.includes(code)
      ? columns.filter((one) => one !== code)
      : available.filter((one) => columns.includes(one) || one === code);
    // Back to `null` when everything is on, so "I turned one off and on again"
    // leaves the filter as it started rather than freezing today's list.
    onChange(next.length === available.length ? null : next);
  };

  const chip = (code: string) => {
    const on = columns.includes(code);
    const last = on && columns.length === 1;
    return (
      <button
        key={code}
        type="button"
        aria-pressed={on}
        disabled={last}
        title={
          last
            ? "The table needs at least one currency column"
            : on
              ? `Hide the ${code} columns`
              : `Show the ${code} columns`
        }
        className={on ? "chip active" : "chip"}
        onClick={() => flip(code)}
      >
        {code}
      </button>
    );
  };

  // `available` is busiest first, so the four on the line are the four most
  // used and the "+" holds the long tail.
  const first = available.slice(0, CURRENCY_CHIPS);
  const rest = available.slice(CURRENCY_CHIPS);
  const restOn = rest.filter((code) => columns.includes(code));

  return (
    <div className="currency-toggle">
      <span className="daterange-label">Currency</span>
      <div className="daterange-presets" role="group" aria-label="Currencies shown">
        {first.map(chip)}
        {rest.length > 0 ? (
          <div className="currency-more" ref={moreHolder}>
            <button
              type="button"
              className={restOn.length > 0 ? "chip active" : "chip"}
              aria-expanded={moreOpen}
              aria-label={`${rest.length} more currencies: ${rest.join(", ")}. ${restOn.length} shown.`}
              title={
                restOn.length > 0
                  ? `Also showing ${restOn.join(", ")}`
                  : `${rest.join(", ")} — all hidden`
              }
              onClick={() => setMoreOpen(!moreOpen)}
            >
              +{rest.length}
            </button>
            {/* Right after its button in the page, so Tab walks from the "+"
                straight into the currencies it opened. */}
            {moreOpen ? (
              <div className="currency-more-list" role="group" aria-label="More currencies">
                {rest.map(chip)}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
      <Hint label="why each currency has its own columns">
        <p>
          This ledger never converts. Currency lives on the account and there is no exchange
          rate stored anywhere, so one Out column holding {available.join(" and ")} would be a
          column nobody could add up.
        </p>
        <p className="muted small" style={{ marginBottom: 0 }}>
          Each currency gets its own Out and In instead, and a row only ever fills its own.
          Turning one off hides its rows as well as its columns — the table says how many.
        </p>
      </Hint>
    </div>
  );
}

/**
 * A payee or memo you can change without leaving the row.
 *
 * Correcting an imported payee is the single most repeated act on this screen,
 * and it cost four clicks: open the panel, change one field, save, close. Here
 * the cell is a button; clicking it swaps in an input on the spot. Enter or
 * moving away saves, Escape puts it back.
 *
 * It still writes through the ordinary PATCH, so it opens a batch of one and
 * the History screen can undo it exactly like an edit made in the panel. An
 * inline edit is not a lesser kind of change.
 *
 * A locked row stays plain text: the service refuses to edit one, and
 * offering an input that always fails is worse than not offering it.
 */
/**
 * What a column is drawn at before anybody has measured it.
 *
 * Only reached for a column that appears *after* the first drag -- a
 * currency turned back on, a balance that exists only under a date sort on
 * one account. Every column on screen at the first drag is measured instead.
 */
function fallbackWidth(key: string): number {
  if (key === "select") return 28;
  if (key === "transfer") return 20;
  if (key === "cleared" || key === "source") return 44;
  if (key.startsWith("out-") || key.startsWith("in-") || key === "balance") return 110;
  return 140;
}

/**
 * The right-hand edge of a heading, to drag (#74).
 *
 * A `separator` with a value is how ARIA spells a splitter, so a keyboard
 * gets it too: Tab to it, arrows move it, Enter puts every column back.
 * Double-click does the same for a pointer. Not on a phone -- there are no
 * headings there to have edges; each row is a card.
 */
function ColumnEdge({
  column,
  label,
  widths,
}: {
  column: string;
  label: string;
  widths: ReturnType<typeof useColumnWidths>;
}) {
  return (
    <span
      className="col-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label={`Width of the ${label} column`}
      aria-valuenow={widths.widths[column]}
      aria-valuemin={MIN_WIDTH}
      tabIndex={0}
      title="Drag to resize · double-click to reset every column"
      onPointerDown={(event) => widths.startDrag(column, event)}
      onClick={(event) => event.stopPropagation()}
      onDoubleClick={(event) => {
        event.stopPropagation();
        widths.reset();
      }}
      onKeyDown={(event) => widths.onKey(column, event)}
    />
  );
}

function EditableCell({
  txn,
  field,
  value,
  payees,
  onSaved,
  mark,
}: {
  txn: Transaction;
  field: "payee" | "memo";
  value: string | null;
  payees: Payee[];
  onSaved: () => void;
  /**
   * A pill at the cell's right-hand edge, outside the edit button -- the
   * memo's W (#143). The text beside it truncates to make room. Outside the
   * button so a click on it is a click on the row, which is what it was in
   * the Source cell: it selects the row rather than opening the editor.
   */
  mark?: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");

  const save = useMutation({
    mutationFn: (next: string) =>
      api.patch<Transaction>(`/transactions/${txn.id}`, {
        ...(field === "payee"
          ? { payee_name: next.trim() || null, clear_payee: !next.trim() }
          : { memo: next.trim() || null, clear_memo: !next.trim() }),
      }),
    onSuccess: () => {
      setEditing(false);
      onSaved();
    },
  });

  const locked = txn.cleared === "reconciled";
  const shown = value || "";

  function commit(next: string = draft) {
    // Nothing typed, nothing to record: a no-op PATCH would still open a batch
    // and leave a change with no change in it on the History screen.
    if (next.trim() === (value ?? "").trim()) {
      setEditing(false);
      return;
    }
    save.mutate(next);
  }

  if (!editing) {
    // What the cell says. Wrapped with its pill for the memo; bare for the
    // payee, whose cell has nothing beside it.
    const text = locked ? (
      // A locked row has no button, and the padding lives on the button --
      // so without this the text sat flush against the cell edge and every
      // locked row was nine pixels out of line with the rest of its column.
      <span className="cell-static" title={shown || undefined}>
        {shown || <span className="muted">—</span>}
      </span>
    ) : (
      <button
        type="button"
        className="cell-edit"
        /* The full text first when there is any: a truncated memo's whole
           point is that the rest of it is one hover away. What the button
           does is obvious from the shape of the cell; what it says is not,
           once it ends in an ellipsis. */
        title={shown ? `${shown}\n\nClick to edit` : `Edit the ${field}`}
        aria-label={shown ? `${shown} — edit the ${field}` : `Edit the ${field}`}
        onClick={() => {
          setDraft(value ?? "");
          setEditing(true);
        }}
      >
        {shown || <span className="muted">—</span>}
      </button>
    );
    return (
      <td
        /* `memo-cell` is what keeps a memo on one line. A memo is free text
           and the panel's box takes newlines, so one row holding three lines
           made every other row in the table taller than it needed to be and
           the register scanned as a ragged list. The cell truncates instead;
           the whole text is on hover, in the accessible name, and in the panel
           the date opens. Nothing is lost, and the row is a row. */
        className={field === "memo" ? "small muted editable memo-cell" : "editable"}
        data-label={field === "memo" ? "Memo" : "Payee"}
        /* Marks a cell whose only content is the em dash standing in for
           nothing. On a phone the row is a card, and a line reading "MEMO —"
           is a line spent saying there is nothing to say -- the stylesheet
           drops it there and keeps it in the table, where the column has to
           stay aligned. */
        data-empty={shown || mark ? undefined : "true"}
        /* The headline of the card on a phone. See `data-primary` in the
           stylesheet -- the same two markers do this on every screen. */
        data-primary={field === "payee" ? "true" : undefined}
      >
        {field === "memo" ? (
          <div className="memo-line">
            {text}
            {mark}
          </div>
        ) : (
          text
        )}
      </td>
    );
  }

  return (
    <td className="editing">
      {field === "payee" ? (
        <Combobox
          value={draft}
          onChange={setDraft}
          options={payees.map((one) => one.name)}
          aria-label="Payee"
          autoFocus
          onCommit={commit}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <input
          value={draft}
          autoFocus
          aria-label="Memo"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              setEditing(false);
            }
          }}
        />
      )}
      {save.isPending ? <span className="small muted"> saving…</span> : null}
      {save.error ? <div className="small neg">{(save.error as Error).message}</div> : null}
    </td>
  );
}

/**
 * Which category a piece of typed text means.
 *
 * Module level because three places ask the question now -- the cell in the
 * list, the transaction panel and the entry form -- and three copies of this
 * would eventually disagree about what "groc" resolves to.
 *
 * Exact wins over partial so that typing a name in full is never ambiguous
 * with a longer one containing it: "Travel" must not be undecidable because
 * "Business Travel" also exists. Only a single partial match commits, because
 * two matches is not a decision the app should make on somebody's behalf.
 */
function resolveCategory(
  all: { id: string; name: string; full_name: string }[],
  text: string,
): { id: string | null } | "ambiguous" {
  const needle = fold(text);
  if (!needle) return { id: null };

  for (const one of all) {
    if (fold(one.full_name) === needle || fold(one.name) === needle) return { id: one.id };
  }
  const hits = all.filter(
    (one) => fold(one.full_name).includes(needle) || fold(one.name).includes(needle),
  );
  if (hits.length === 1) return { id: hits[0].id };
  return "ambiguous";
}

/**
 * The category field, for a form rather than a cell.
 *
 * Same typing and the same resolution as `CategoryCell`, because a picker that
 * behaves one way in the register and another in the panel beside it is two
 * things to learn. What differs is only when it commits: a cell saves the row
 * on leaving the field, a form holds the choice until the form is saved.
 */
function CategoryChooser({
  groups,
  value,
  onPick,
}: {
  groups: CategoryGroup[];
  value: string | null;
  onPick: (id: string | null) => void;
}) {
  const all = useMemo(() => groups.flatMap((group) => group.categories), [groups]);
  const labels = useMemo(() => all.map((one) => one.full_name), [all]);

  const [typed, setTyped] = useState(
    () => all.find((one) => one.id === value)?.full_name ?? "",
  );
  const [unmatched, setUnmatched] = useState(false);

  // The row can change under the panel -- a save that came back with a
  // category a payee rule decided, or another tab. Follow it rather than
  // showing a value the transaction no longer has.
  useEffect(() => {
    setTyped(all.find((one) => one.id === value)?.full_name ?? "");
  }, [value, all]);

  function commit(next: string = typed) {
    const answer = resolveCategory(all, next);
    if (answer === "ambiguous") {
      setUnmatched(true);
      return;
    }
    setUnmatched(false);
    onPick(answer.id);
  }

  if (groups.length === 0) {
    return (
      <p className="small muted" style={{ margin: "6px 0 0" }}>
        No categories yet — add some on the Categories screen.
      </p>
    );
  }

  return (
    <>
      <Combobox
        value={typed}
        onChange={(next) => {
          setTyped(next);
          setUnmatched(false);
        }}
        options={labels}
        browse
        limit={Infinity}
        placeholder="type any part"
        aria-label="Category"
        onCommit={commit}
        onCancel={() => setUnmatched(false)}
      />
      {unmatched ? (
        <div className="small neg">No single category matches that. Keep typing, or empty it.</div>
      ) : null}
    </>
  );
}

/**
 * The category, typed rather than picked.
 *
 * A select was tried first and is wrong for this list: thirty categories under
 * five headings means scrolling to find one you already know the name of. Here
 * you type any part of it -- "mortgage" finds "Bills: Rent / Mortgage",
 * "quality" finds everything under that heading -- and Tab commits it.
 *
 * Tab, specifically. Correcting categories is done in runs, down a column, and
 * the hand is already on Tab to get to the next row. So leaving the field
 * saves, rather than needing Enter and then Tab.
 */
function CategoryCell({
  txn,
  groups,
  onSaved,
}: {
  txn: Transaction;
  groups: CategoryGroup[];
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState("");
  const [unmatched, setUnmatched] = useState(false);

  const all = useMemo(() => groups.flatMap((group) => group.categories), [groups]);
  const labels = useMemo(() => all.map((one) => one.full_name), [all]);

  const save = useMutation({
    mutationFn: (categoryId: string | null) =>
      api.patch<Transaction>(`/transactions/${txn.id}`, {
        category_id: categoryId,
        clear_category: categoryId === null,
      }),
    onSuccess: () => {
      setEditing(false);
      setUnmatched(false);
      onSaved();
    },
  });

  const locked = txn.cleared === "reconciled";
  const transfer = isTransferLeg(txn);

  function commit(next: string = typed) {
    const answer = resolveCategory(all, next);
    if (answer === "ambiguous") {
      // Left open rather than guessed at, and rather than silently discarded.
      setUnmatched(true);
      return;
    }
    if (answer.id === (txn.category_id ?? null)) {
      setEditing(false);
      setUnmatched(false);
      return;
    }
    save.mutate(answer.id);
  }

  function open() {
    setTyped(txn.category_name ?? "");
    setUnmatched(false);
    setEditing(true);
  }

  if (transfer) {
    // Not a button: there is nothing to choose. A transfer is not spending, so
    // the server refuses a category on it (#124), and a cell offering one
    // would be a control that always fails.
    return (
      <td className="small editable" data-label="Category">
        <span
          className="cell-static muted"
          title="A transfer moves money between your own accounts, so it is not spending and has no category"
        >
          No category needed
        </span>
      </td>
    );
  }

  if (!editing) {
    return (
      <td className="small editable" data-label="Category">
        {locked ? (
          <span className="cell-static">
            {txn.category_name ?? <span className="muted">—</span>}
          </span>
        ) : (
          <button
            type="button"
            className="cell-edit"
            /* The full name first, for the same reason the memo cell does it:
               "Quality of Life: Subscriptions" is wider than the column. */
            title={
              txn.category_name
                ? `${txn.category_name}\n\nClick to change`
                : "Set the category"
            }
            onClick={open}
          >
            {txn.category_name ?? <span className="muted">uncategorised</span>}
          </button>
        )}
      </td>
    );
  }

  return (
    <td className="editing" data-label="Category">
      <Combobox
        value={typed}
        onChange={(next) => {
          setTyped(next);
          setUnmatched(false);
        }}
        options={labels}
        browse
        limit={Infinity}
        placeholder="type any part"
        aria-label="Category"
        autoFocus
        onCommit={commit}
        onCancel={() => {
          setEditing(false);
          setUnmatched(false);
        }}
      />
      {unmatched ? (
        <div className="small neg">No single category matches that. Keep typing, or empty it.</div>
      ) : null}
      {save.error ? <div className="small neg">{(save.error as Error).message}</div> : null}
    </td>
  );
}

/** Same folding the Combobox ranks with, so what matches is what was offered. */
function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toLowerCase();
}

function QuickEntry({
  household,
  accounts,
  payees,
  groups,
  defaultAccountId,
  onAdded,
}: {
  household: Household;
  accounts: Account[];
  payees: Payee[];
  groups: CategoryGroup[];
  defaultAccountId: string;
  onAdded: () => void;
}) {
  const [accountId, setAccountId] = useState(defaultAccountId || accounts[0]?.id || "");
  const [date, setDate] = useState(localToday());
  const [payee, setPayee] = useState("");
  const [outflow, setOutflow] = useState("");
  const [inflow, setInflow] = useState("");
  const [memo, setMemo] = useState("");
  const [categoryId, setCategoryId] = useState<string | null>(null);
  const dateRef = useRef<HTMLInputElement>(null);

  const account = accounts.find((a) => a.id === accountId) ?? accounts[0];
  const currency = account?.currency ?? household.base_currency;
  const payeeNames = useMemo(() => payees.map((one) => one.name), [payees]);

  const add = useMutation({
    mutationFn: () =>
      api.post<Transaction>(`/households/${household.id}/transactions`, {
        account_id: account?.id,
        date,
        amount: signedAmount ?? 0,
        payee_name: payee.trim() || null,
        category_id: categoryId,
        memo: memo.trim() || null,
      }),
    onSuccess: () => {
      // Keep the date: entering by hand is a run of rows on nearby days.
      setPayee("");
      setOutflow("");
      setInflow("");
      setMemo("");
      // The category is not kept. A payee rule may decide one, and holding the
      // last choice would silently override it on the next row.
      setCategoryId(null);
      dateRef.current?.focus();
      onAdded();
    },
  });

  // Whichever box has a number decides the sign, so nobody types a minus.
  const out = parse(outflow, currency);
  const inn = parse(inflow, currency);
  const signedAmount = out !== null ? -Math.abs(out) : inn !== null ? Math.abs(inn) : null;
  const ready = Boolean(account) && signedAmount !== null;

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Enter" && ready && !add.isPending) {
      event.preventDefault();
      add.mutate();
    }
  }

  if (accounts.length === 0) {
    return <p className="muted small">Add an account before entering transactions.</p>;
  }

  return (
    <div onKeyDown={onKeyDown}>
      <Problem error={add.error} />
      <div className="row">
        <Field label="Account">
          <select value={account?.id} onChange={(e) => setAccountId(e.target.value)}>
            {accounts.map((one) => (
              <option key={one.id} value={one.id}>
                {one.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Date">
          <input ref={dateRef} type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
        <Field label="Payee">
          <Combobox
            value={payee}
            onChange={setPayee}
            options={payeeNames}
            placeholder="Who was paid"
            aria-label="Payee"
          />
        </Field>
        {/* Beside the payee, because the two are chosen together and a rule may
            decide this one from that one. Same typing and the same resolution
            as the register's own cell -- a picker that behaves differently in
            the panel from the list is two things to learn. */}
        <Field label="Category">
          <CategoryChooser groups={groups} value={categoryId} onPick={setCategoryId} />
        </Field>
        <Field label={`Out (${currency})`}>
          <input
            value={outflow}
            onChange={(e) => {
              setOutflow(e.target.value);
              if (e.target.value) setInflow("");
            }}
            placeholder="12,34"
            inputMode="decimal"
          />
        </Field>
        <Field label={`In (${currency})`}>
          <input
            value={inflow}
            onChange={(e) => {
              setInflow(e.target.value);
              if (e.target.value) setOutflow("");
            }}
            placeholder="12,34"
            inputMode="decimal"
          />
        </Field>
        <Field label="Memo">
          <input value={memo} onChange={(e) => setMemo(e.target.value)} />
        </Field>
        <button className="primary" disabled={!ready || add.isPending} onClick={() => add.mutate()}>
          Add
        </button>
      </div>
      <p className="small muted" style={{ marginTop: 8 }}>
        Put the number in Out or In — no minus signs. Press Enter to add and go straight to
        the next row.
      </p>
    </div>
  );
}

/**
 * Where an imported row came from, in the bank's own words.
 *
 * A transaction outlives the statement it arrived on. Once the file is gone
 * this is the only account of what the bank actually wrote -- which is the
 * difference between "why is this EUR 45 here" having an answer and not.
 */
export function Origin({ transactionId }: { transactionId: string }) {
  const [open, setOpen] = useState(false);
  const origin = useQuery({
    queryKey: ["origin", transactionId],
    queryFn: () => api.get<TransactionOrigin>(`/transactions/${transactionId}/origin`),
    enabled: open,
    retry: false,
  });

  if (!open) {
    return (
      <p className="small">
        <button className="link" onClick={() => setOpen(true)}>
          Where did this come from?
        </button>
      </p>
    );
  }

  if (origin.isLoading) return <p className="small muted">Looking…</p>;
  if (origin.error) return <p className="small muted">This one was entered by hand.</p>;

  const found = origin.data!;
  if (found.kind === "one_time_import") return <OneTimeOrigin found={found} />;
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <p className="small muted" style={{ marginBottom: 6 }}>
        Imported from <strong>{found.filename ?? "a statement"}</strong>, line {found.line_no}, on{" "}
        {formatInstant(found.imported_at)}.
      </p>
      {/* An agent import reaches here like any other -- it goes through the
          same staging path, which is the point of the agent API being a second
          front door rather than a second write path. The line above names the
          agent's own description of what it read, which is the closest thing
          to a file there is; this says who was holding the pen. Without it a
          row a program created and a row somebody uploaded read identically.
          Issue #57. */}
      {found.via ? (
        <p className="small muted" style={{ marginBottom: 6 }}>
          Staged through the agent API by <strong>{found.via}</strong>.
        </p>
      ) : null}
      {found.payee_original ? (
        <p className="small muted" style={{ marginBottom: 6 }}>
          The bank called it <span className="mono">{found.payee_original}</span>.
        </p>
      ) : null}
      {found.bank ? (
        <dl className="origin">
          {Object.entries(found.bank).map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>{key}</dt>
              <dd className="mono">{String(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <details>
        <summary className="small muted">The line exactly as it arrived</summary>
        <p className="mono small" style={{ wordBreak: "break-all", marginTop: 6 }}>
          {found.raw}
        </p>
      </details>
    </div>
  );
}

/**
 * A row another app's history brought in (#183).
 *
 * A one-time import keeps no statement lines -- it reads a whole export once
 * -- so there is no "line 12" and no raw text to show. What there is: which
 * app, how it was read, the file or the plan, and when. Without this the panel
 * said such a row was entered by hand, which is the one thing it was not.
 * The bank's own text is shown when the other app kept it (YNAB's API does,
 * its CSV does not, #265).
 */
function OneTimeOrigin({ found }: { found: TransactionOrigin }) {
  const app = found.workflow ?? "another app";
  const how =
    found.workflow_via === "api"
      ? `${app}, via the ${app} API`
      : found.workflow_via === "csv"
        ? `${app}, via its CSV export`
        : app;
  const what = found.filename ? (
    <>
      the file <strong>{found.filename}</strong>
    </>
  ) : found.plan_name ? (
    <>
      the plan <strong>{found.plan_name}</strong>
    </>
  ) : null;
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <p className="small muted" style={{ marginBottom: 6 }}>
        Brought in by the <strong>One-time Import</strong> from {how}
        {what ? <>, reading {what}</> : null}, on {formatInstant(found.imported_at)}.
      </p>
      {found.payee_original ? (
        <p className="small muted" style={{ marginBottom: 6 }}>
          The bank called it <span className="mono">{found.payee_original}</span>, as {app} kept it.
        </p>
      ) : null}
      <p className="small muted" style={{ margin: 0 }}>
        The whole import is one entry in History, and undoing it there takes every row it brought
        in back out.
      </p>
    </div>
  );
}

/**
 * Everything that has ever happened to this one row.
 *
 * `Audit Log Decision.md` sold this as the headline capability -- *"why is this
 * EUR 45 here?"*, answered by walking `changes` for one `row_id` -- and
 * `Change.household_id` was denormalised with an index built for exactly this
 * query. It had an endpoint and no screen. This is the screen.
 *
 * The sentences come from the same engine as the History list, deliberately:
 * "what happened to this transaction" and "what happened to this household" are
 * the same question at two scopes, and two ways of putting a change into words
 * would eventually disagree about one.
 */
function TransactionHistory({
  householdId,
  transactionId,
}: {
  householdId: string;
  transactionId: string;
}) {
  const history = useQuery({
    queryKey: ["row-history", transactionId],
    queryFn: () =>
      api.get<Change[]>(
        `/households/${householdId}/changes?table=transactions&row_id=${transactionId}`,
      ),
  });

  const entries = history.data ?? [];

  return (
    <section>
      <h3 className="section-title">What has happened to this</h3>
      <Problem error={history.error} />
      {history.isLoading ? (
        <p className="muted small">Reading the log…</p>
      ) : entries.length === 0 ? (
        <p className="muted small" style={{ margin: 0 }}>
          Nothing recorded. Rows created before the audit log existed have no history.
        </p>
      ) : (
        <ol className="row-history">
          {entries.map((entry) => (
            <li key={entry.seq}>
              <div className="small muted">
                {formatInstant(entry.at)}
                {entry.actor_name || entry.via ? (
                  <>
                    {" · "}
                    <Actor name={entry.actor_name} via={entry.via} />
                  </>
                ) : null}
              </div>
              <div className="small">{entry.summary}</div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/**
 * Dividing one transaction into the parts it was really made of.
 *
 * Built around the same number the reconciliation screen is: what is left to
 * account for. A split that does not add up is not a split -- it is an edit and
 * a new transaction sharing a name, and it would move the account's balance
 * while looking like a reclassification. So the button stays off until the
 * remainder is zero, and the service refuses it as well.
 *
 * The original is replaced rather than kept as a parent. Every part is an
 * ordinary transaction, so nothing in the register, no balance and no future
 * report has to know a split happened -- which is exactly what the previous
 * build got wrong by keeping a parent row that was itself a transaction.
 */
export function SplitPanel({
  txn,
  currency,
  groups,
  onClose,
  onDone,
}: {
  txn: Transaction;
  currency: string;
  groups: CategoryGroup[];
  onClose: () => void;
  onDone: () => void;
}) {
  type Part = { amount: string; categoryId: string; memo: string };
  const blank = (): Part => ({ amount: "", categoryId: "", memo: "" });

  // Opens split evenly, because that is the common case and it means the
  // panel arrives already adding up. It used to open with the whole amount on
  // part one and part two empty, so the first thing it said was "one part is
  // still empty" -- the screen telling you off for its own starting state.
  const spread = (count: number, keep: Part[] = []): Part[] => {
    const figures = equalParts(txn.amount, count);
    return figures.map((figure, index) => ({
      amount: toInput(figure, currency),
      // The original's category rides on the first part; anything already
      // chosen on a part that still exists stays where it was put.
      categoryId: keep[index]?.categoryId ?? (index === 0 ? (txn.category_id ?? "") : ""),
      memo: keep[index]?.memo ?? "",
    }));
  };

  const [parts, setParts] = useState<Part[]>(() => spread(2));
  // What the panel opened with. Anything else is work that closing would throw
  // away, so the backdrop stops closing it and Escape, the cross and "Not now"
  // ask first (#28).
  const [opened] = useState(() => JSON.stringify(parts));
  const dirty = JSON.stringify(parts) !== opened;
  const [asking, setAsking] = useState(false);
  const leave = () => (dirty ? setAsking(true) : onClose());

  const save = useMutation({
    mutationFn: () =>
      api.post<Transaction[]>(`/transactions/${txn.id}/split`, {
        parts: parts.map((part, index) => ({
          amount: amounts[index],
          category_id: part.categoryId || null,
          memo: part.memo.trim() || null,
        })),
      }),
    onSuccess: onDone,
  });

  // Magnitudes, with the direction taken from the row being split. The rest of
  // the app never asks anyone to type a minus sign -- quick entry has separate
  // Out and In boxes for exactly that reason -- and `toInput` returns an
  // absolute value to match, so reading these as signed turned a -22.48 into a
  // +22.48 and told the user they were 44.96 out.
  const sign = txn.amount < 0 ? -1 : 1;

  // An empty part is not a typo, it is a part you have not filled in yet --
  // the panel opens with one, and greeting somebody with "that isn't an amount"
  // before they have typed anything is the screen telling them off for its own
  // starting state.
  const amounts = parts.map((part) => {
    if (part.amount.trim() === "") return null;
    const magnitude = parse(part.amount, currency);
    return magnitude === null ? null : sign * Math.abs(magnitude);
  });
  const anyUnreadable = parts.some(
    (part, index) => part.amount.trim() !== "" && amounts[index] === null,
  );
  const anyBlank = parts.some((part) => part.amount.trim() === "");
  const assigned = amounts.reduce((sum: number, one) => sum + (one ?? 0), 0);
  const left = txn.amount - assigned;
  const balanced =
    !anyUnreadable && !anyBlank && left === 0 && amounts.every((one) => one !== 0);

  function change(index: number, patch: Partial<Part>) {
    setParts(parts.map((part, at) => (at === index ? { ...part, ...patch } : part)));
  }

  // Two or three parts get the bar. Past that the segments are too thin to
  // grab, and the typed amounts work the way they always have.
  const barred = parts.length <= 3;
  const magnitudes = amounts.map((one) => (one === null ? null : Math.abs(one)));
  const drawable = balanced ? magnitudes.map((one) => one ?? 0) : null;

  // With the bar, typing one part moves its neighbour by the same amount, so
  // the parts keep adding up while the figure is still being typed. When the
  // neighbour cannot cover it, what was typed stays and the remainder shows.
  function typeAmount(index: number, text: string) {
    const typed = text.trim() === "" ? null : parse(text, currency);
    const moved =
      barred && typed !== null
        ? retype(magnitudes, Math.abs(txn.amount), index, Math.abs(typed))
        : null;
    setParts(
      parts.map((part, at) =>
        at === index
          ? { ...part, amount: text }
          : moved && moved[at] !== magnitudes[at]
            ? { ...part, amount: toInput(moved[at], currency) }
            : part,
      ),
    );
  }

  function drag(sizes: number[]) {
    setParts(parts.map((part, at) => ({ ...part, amount: toInput(sizes[at], currency) })));
  }

  return (
    <Panel title="Split this transaction" onClose={leave} dirty={dirty} wide>
      <Problem error={save.error} />
      <p className="muted small" style={{ marginTop: 0 }}>
        {format(txn.amount, currency)}
        {txn.payee_name ? ` · ${txn.payee_name}` : null} on {txn.date}. The parts replace it, so
        they have to come to the same amount. No minus signs — every part goes the same way the
        original did.
      </p>

      <div className={balanced ? "difference agreed" : "difference apart"}>
        <div>
          <strong>
            {anyUnreadable
              ? `That isn't an amount in ${currency}`
              : left === 0 && anyBlank
                ? "One part is still empty"
                : left === 0
                  ? "It adds up"
                  : `${format(Math.abs(left), currency)} left to account for`}
          </strong>
          <span className="small">
            {balanced
              ? "Every part of the original is accounted for."
              : "Change a part, or add another."}
          </span>
        </div>
      </div>

      {barred ? (
        drawable ? (
          <SplitBar magnitudes={drawable} currency={currency} onChange={drag} />
        ) : (
          <p className="small muted split-bar-help">
            The bar comes back once the parts add up.
          </p>
        )
      ) : null}

      <div className="split-parts">
        {parts.map((part, index) => (
          <div className="split-part" key={index}>
            <Field
              label={
                <>
                  {barred ? (
                    <span className={`split-swatch split-seg-${index + 1}`} aria-hidden="true" />
                  ) : null}
                  {`Part ${index + 1} (${currency})`}
                </>
              }
            >
              <input
                value={part.amount}
                inputMode="decimal"
                autoFocus={index === 1}
                onChange={(e) => typeAmount(index, e.target.value)}
              />
            </Field>
            <Field label="Category">
              {/* A select whose only option is "Uncategorised" looks like a
                  broken picker rather than an empty ledger, and that is how
                  this screen was read. Say which it is. */}
              {groups.length === 0 ? (
                <p className="small muted" style={{ margin: "6px 0 0" }}>
                  No categories yet — add some on the Categories screen.
                </p>
              ) : (
                <select
                  value={part.categoryId}
                  onChange={(e) => change(index, { categoryId: e.target.value })}
                >
                  <option value="">Uncategorised</option>
                  {groups.map((group) => (
                    <optgroup key={group.id} label={group.name}>
                      {group.categories.map((one) => (
                        <option key={one.id} value={one.id}>
                          {one.name}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Memo">
              <input
                value={part.memo}
                placeholder={txn.memo ?? ""}
                onChange={(e) => change(index, { memo: e.target.value })}
              />
            </Field>
            <button
              className="link"
              disabled={parts.length <= 2}
              title={parts.length <= 2 ? "A split needs at least two parts" : "Remove this part"}
              onClick={() => {
                const kept = parts.filter((_, at) => at !== index);
                setParts(spread(kept.length, kept));
              }}
            >
              Remove
            </button>
          </div>
        ))}
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <button
          disabled={parts.length >= 5}
          title={parts.length >= 5 ? "Five parts is the most" : ""}
          onClick={() => setParts(spread(parts.length + 1, parts))}
        >
          Add a part
        </button>
        {/* Adding a part re-divides evenly, so the usual case needs no
            arithmetic from anybody. This puts it back after hand-editing,
            which is the only way to undo a typo without reopening the panel. */}
        <button onClick={() => setParts(spread(parts.length, parts))}>Split equally</button>
        {left !== 0 && !anyUnreadable && parts.length < 5 ? (
          <button
            onClick={() =>
              setParts([...parts, { ...blank(), amount: toInput(left, currency) }])
            }
          >
            Add a part for the {format(left, currency)}
          </button>
        ) : null}
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <button className="primary" disabled={!balanced || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Splitting…" : `Split into ${parts.length}`}
        </button>
        <button onClick={leave}>Not now</button>
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>
        This is one act: the original goes, the parts arrive, and History undoes the whole thing
        in one click. The parts stay marked as belonging together.
      </p>

      {asking ? (
        <Dialog title="Discard this split?" onClose={() => setAsking(false)}>
          <p className="small">
            The parts you have set up have not been saved. Discarding leaves the transaction as it
            was.
          </p>
          <div className="dialog-choices">
            <button className="primary" autoFocus onClick={() => setAsking(false)}>
              Keep editing
            </button>
            <button className="danger" onClick={onClose}>
              Discard
            </button>
          </div>
        </Dialog>
      ) : null}
    </Panel>
  );
}

/** Everything there is to know about one transaction, and everything editable. */
/**
 * A field that will not be edited by accident.
 *
 * The date and the amount are the two facts on a row that every other number
 * depends on: change an amount and the account balance moves, change a date
 * and it moves in a different month and out of whatever was reconciled. In a
 * panel that saves on blur, brushing either of them with a stray keystroke is
 * a silent correction to the ledger.
 *
 * So they are shown rather than presented for editing, and it takes a
 * deliberate act to open one: a double click, or Enter twice. Once open it is
 * an ordinary field and leaving it saves like any other.
 *
 * They are also last in the DOM, which is what puts them last in the tab
 * order. Tabbing through a row lands on the things that are usually being
 * corrected -- payee, category, memo -- and never reaches these without being
 * aimed at them.
 */
function Guarded({
  label,
  display,
  disabled,
  onDone,
  children,
}: {
  label: string;
  display: string;
  disabled?: boolean;
  onDone: () => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [armed, setArmed] = useState(false);

  if (!open) {
    return (
      <Field label={label}>
        <button
          type="button"
          className="guarded"
          disabled={disabled}
          aria-label={`${label} — ${display}. Double click, or press Enter twice, to edit.`}
          title="Double click, or press Enter twice, to edit"
          onDoubleClick={() => setOpen(true)}
          onBlur={() => setArmed(false)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            // First Enter arms, second opens. The label says so in between, so
            // the second press is something you chose rather than discovered.
            if (armed) {
              setArmed(false);
              setOpen(true);
            } else {
              setArmed(true);
            }
          }}
        >
          <span>{display}</span>
          <span className="small muted">
            {armed ? "Enter again to edit" : "double click to edit"}
          </span>
        </button>
      </Field>
    );
  }

  return (
    <Field label={label}>
      <div
        onBlur={(event) => {
          // Only when focus has actually left this field, not while moving
          // between the two boxes an amount is made of.
          if (event.currentTarget.contains(event.relatedTarget as Node)) return;
          setOpen(false);
          onDone();
        }}
      >
        {children}
      </div>
    </Field>
  );
}

function TransactionPanel({
  txn,
  household,
  accountNameOf,
  householdId,
  currency,
  accountName,
  payees,
  groups,
  onClose,
  onChanged,
  onSaved,
}: {
  txn: Transaction;
  household: Household;
  //: For the payment a work expense was repaid by, which is in another
  //: account more often than not.
  accountNameOf: (accountId: string) => string;
  //: The row's own history is a household-scoped read -- that is what stops a
  //: member of one household walking another's audit log -- and a transaction
  //: does not carry its household over the wire.
  householdId: string;
  currency: string;
  accountName: string;
  payees: Payee[];
  groups: CategoryGroup[];
  onClose: () => void;
  //: Closes the panel. For the acts that end it: delete, duplicate.
  onChanged: () => void;
  //: Refreshes the register behind the panel and leaves it open, which is what
  //: an autosave wants -- closing the panel on every field would make editing
  //: two things a matter of opening it twice.
  onSaved: () => void;
}) {
  const [date, setDate] = useState(txn.date);
  const [payee, setPayee] = useState(txn.payee_name ?? "");
  const [outflow, setOutflow] = useState(txn.amount < 0 ? toInput(txn.amount, currency) : "");
  const [inflow, setInflow] = useState(txn.amount > 0 ? toInput(txn.amount, currency) : "");
  const [memo, setMemo] = useState(txn.memo ?? "");
  const [cleared, setCleared] = useState(txn.cleared);
  const [categoryId, setCategoryId] = useState<string | null>(txn.category_id ?? null);
  const [splitting, setSplitting] = useState(false);

  //: The row as the server last confirmed it. Autosaving keeps the panel open
  //: across a save, so "what has changed" has to be measured against the last
  //: answer rather than against the row that was clicked minutes ago.
  const [row, setRow] = useState(txn);
  useEffect(() => {
    setRow(txn);
  }, [txn]);

  const locked = row.cleared === "reconciled";

  //: One side of a transfer: no category, and the amount moves both legs.
  const isTransfer = isTransferLeg(txn);

  const save = useMutation({
    mutationFn: () =>
      api.patch<Transaction>(`/transactions/${txn.id}`, {
        date,
        amount: panelAmount,
        payee_name: payee.trim() || null,
        clear_payee: !payee.trim(),
        // Nothing about the category on a transfer leg: the server refuses
        // one, and the field below is not offered, so there is nothing to say.
        ...(isTransfer
          ? {}
          : { category_id: categoryId, clear_category: categoryId === null }),
        memo: memo.trim() || null,
        clear_memo: !memo.trim(),
        cleared,
      }),
    onSuccess: (updated) => {
      setRow(updated);
      onSaved();
    },
  });


  const remove = useMutation({
    mutationFn: () => api.del(`/transactions/${txn.id}`),
    onSuccess: onChanged,
  });
  //: Delete asks first, naming the row (#199). The panel closes on a delete,
  //: and Undo in History is four steps away from a stray click.
  const [confirming, setConfirming] = useState(false);

  const copy = useMutation({
    mutationFn: () => api.post(`/transactions/${txn.id}/duplicate`, { date: localToday() }),
    onSuccess: onChanged,
  });

  const panelOut = parse(outflow, currency);
  const panelIn = parse(inflow, currency);
  const panelAmount =
    panelOut !== null ? -Math.abs(panelOut) : panelIn !== null ? Math.abs(panelIn) : txn.amount;

  const dirty =
    date !== row.date ||
    (payee.trim() || null) !== (row.payee_name ?? null) ||
    panelAmount !== row.amount ||
    (memo.trim() || null) !== (row.memo ?? null) ||
    cleared !== row.cleared ||
    (!isTransfer && categoryId !== (row.category_id ?? null));

  /**
   * Save on the way out of a field.
   *
   * The panel is a set of facts about one row rather than a form you submit:
   * you open it to change one of them, and an explicit Save was one more thing
   * to remember on the way past. Leaving the field is the moment the change is
   * finished -- which is the same moment `CategoryCell` in the list has always
   * saved on, so the two now agree.
   *
   * Guarded by `dirty` rather than fired on every blur: tabbing through the
   * panel without touching anything must not write a batch to the audit log
   * and leave an entry in History saying nothing happened.
   *
   * The split panel is deliberately not like this. A half-entered split does
   * not add up, and saving one on the way past is the one thing the service
   * refuses outright.
   */
  function autoSave() {
    if (dirty && !save.isPending && !locked) save.mutate();
  }


  if (splitting) {
    return (
      <SplitPanel
        txn={txn}
        currency={currency}
        groups={groups}
        onClose={() => setSplitting(false)}
        onDone={onChanged}
      />
    );
  }

  return (
    <Panel title={`${txn.date} · ${accountName}`} onClose={onClose}>
      <Problem error={save.error ?? (confirming ? null : remove.error) ?? copy.error} />
      {txn.split_id ? (
        <div className="banner info">
          One part of a split. The others are in the register on the same date, and the
          transaction they replaced is in History.
        </div>
      ) : null}

      {isTransfer && (
        <div className="banner warn">
          This is one leg of a transfer. Changing the amount changes the other side too, so the two
          can never disagree.
        </div>
      )}
      {txn.cleared === "reconciled" && (
        <div className="banner warn">
          This row is locked, so nothing here can be changed and an import will not
          match against it. Set it back to cleared to edit it.
        </div>
      )}

      {/* The order is the tab order, and it is deliberate: the things usually
          being corrected come first, and the two that move money come last,
          behind a guard. */}
      <div onBlur={autoSave}>
        <Field label="Payee">
          <Combobox
            value={payee}
            onChange={setPayee}
            options={payees.map((one) => one.name)}
            aria-label="Payee"
          />
        </Field>
      </div>
      <p />
      <Field label="Category">
        {isTransfer ? (
          /* Disabled and greyed rather than missing, so the panel keeps its
             shape and says why there is nothing to choose (#124). Unlinking
             the transfer makes the row ordinary and the chooser comes back. */
          <input
            className="no-category"
            disabled
            value="No category needed"
            aria-label="Category: no category needed, this is one leg of a transfer"
            title="A transfer moves money between your own accounts, so it is not spending and has no category"
            readOnly
          />
        ) : (
          <CategoryChooser groups={groups} value={categoryId} onPick={setCategoryId} />
        )}
      </Field>
      <p />
      <div onBlur={autoSave}>
        <Field label="Memo">
          <textarea rows={2} value={memo} onChange={(e) => setMemo(e.target.value)} />
        </Field>
      </div>
      <p />
      {txn.import_id ? <Origin transactionId={txn.id} /> : null}

      <ReceiptSection householdId={householdId} txn={txn} />

      <ReimbursementSection
        household={household}
        txn={txn}
        currency={currency}
        accountNameOf={accountNameOf}
        onSaved={onSaved}
      />

      <div onBlur={autoSave}>
        <Field label="Cleared">
          <select
            value={cleared}
            onChange={(e) => setCleared(e.target.value as Transaction["cleared"])}
          >
            <option value="uncleared">Not seen by the bank yet</option>
            <option value="cleared">Cleared — the bank has it</option>
            <option value="reconciled">
              Locked — checked against the bank, refuse every change
            </option>
          </select>
        </Field>
      </div>

      {/* Last, and guarded. Everything above is a description of the row;
          these two are the row's effect on a balance. */}
      <hr className="rule" />
      <Guarded label="Date" display={date} disabled={locked} onDone={autoSave}>
        <input
          type="date"
          value={date}
          autoFocus
          onChange={(e) => setDate(e.target.value)}
        />
      </Guarded>
      <p />
      <Guarded
        label={`Amount (${currency})`}
        display={format(panelAmount, currency)}
        disabled={locked || isTransfer}
        onDone={autoSave}
      >
        <div className="row">
          <Field label={`Out (${currency})`}>
            <input
              value={outflow}
              autoFocus
              onChange={(e) => {
                setOutflow(e.target.value);
                if (e.target.value) setInflow("");
              }}
              inputMode="decimal"
            />
          </Field>
          <Field label={`In (${currency})`}>
            <input
              value={inflow}
              onChange={(e) => {
                setInflow(e.target.value);
                if (e.target.value) setOutflow("");
              }}
              inputMode="decimal"
            />
          </Field>
        </div>
      </Guarded>

      {txn.import_id && (
        <p className="small muted" style={{ marginTop: 12 }}>
          Came from a statement. Its line key is <span className="mono">{txn.import_id}</span>,
          which is what stops the same line being imported twice.
        </p>
      )}

      <div className="row" style={{ marginTop: 16 }}>
        {/* Not "Save" any more -- the panel saves on the way out of each
            field, so a Save button would be a promise that the thing you just
            typed had *not* been saved. It flushes anything still pending and
            closes. */}
        <button
          className="primary"
          disabled={save.isPending}
          onClick={() => {
            autoSave();
            onClose();
          }}
        >
          Done
        </button>
        {save.isPending ? <span className="small muted">saving…</span> : null}
        {!isTransfer && (
          <button disabled={copy.isPending} onClick={() => copy.mutate()}>
            Duplicate
          </button>
        )}
        {/* Not on a transfer leg: one movement of money recorded twice, and
            splitting one side would desynchronise the pair. */}
        {!isTransfer && txn.cleared !== "reconciled" && (
          <button onClick={() => setSplitting(true)}>Split</button>
        )}
        <button
          className="danger"
          disabled={remove.isPending}
          onClick={() => {
            remove.reset();
            setConfirming(true);
          }}
        >
          Delete
        </button>
      </div>
      {confirming && (
        <Dialog title="Delete this transaction?" onClose={() => setConfirming(false)}>
          <p style={{ marginTop: 0 }}>
            {row.date} · {row.payee_name ?? "No payee"} · {format(row.amount, currency)}
            {txn.split_id ? " — one part of a split; the other parts stay." : null}
          </p>
          <p className="small muted">Undo in History brings it back.</p>
          <div className="dialog-choices">
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {remove.isPending ? "Deleting…" : "Yes, delete it"}
            </button>
            <button disabled={remove.isPending} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </div>
          <Problem error={remove.error} />
        </Dialog>
      )}
      <hr className="rule" />
      <TransactionHistory householdId={householdId} transactionId={txn.id} />

      <p className="small muted" style={{ marginTop: 10 }}>
        Anything you change here is recorded, and can be put back from the History screen.
      </p>
    </Panel>
  );
}


/** How far either side of an expense "Find the payment…" looks. Wider than
 *  a receipt's ten days: an employer's expense run is monthly at best, and an
 *  advance arrives before the spending it covers. */
export const PAYMENT_PICKER_DAYS = 45;

/** What the panel's select sends to move a row from one state to another. */
export function reimbursementChange(
  from: Pick<Transaction, "reimbursement" | "reimbursed_by_id">,
  to: "" | ReimbursementState,
): Record<string, unknown> | null {
  // Landing on the value it already had sends nothing: an empty batch would
  // reach the audit log and leave History saying something happened.
  if ((from.reimbursement ?? "") === to) return null;
  if (to === "") return { clear_state: true };
  // Written off and linked is a combination the server refuses, so moving a
  // repaid row to written off says in the same request that the link goes.
  if (to === "written_off" && from.reimbursed_by_id)
    return { state: "written_off", clear_settlement: true };
  return { state: to };
}

/**
 * Whether work owes you for this row, and what paid it back.
 *
 * Between the receipt and Cleared, because a claim is read together with its
 * receipt -- the receipt is very often why the panel was opened, and whether
 * work pays for it is the next question about the same piece of paper.
 *
 * Three shapes, by what the row is:
 *
 * - **Money out, not a transfer:** the select, and under it either the
 *   payment that repaid it or how long it has been waiting.
 * - **A payment something points at:** what it repaid, and whether the
 *   figures agree -- or, across two currencies, that they are not compared.
 * - **Anything else:** nothing. A transfer leg can never be a work expense,
 *   and an ordinary payment in has nothing to say here.
 *
 * Its own save, straight to the reimbursement endpoint, rather than a field in
 * the panel's autosave: flagging works on a locked row (whether work pays for
 * something is not a fact about the bank), and the panel's own PATCH refuses
 * every change to one of those.
 */
function ReimbursementSection({
  household,
  txn,
  currency,
  accountNameOf,
  onSaved,
}: {
  household: Household;
  txn: Transaction;
  currency: string;
  accountNameOf: (accountId: string) => string;
  onSaved: () => void;
}) {
  const [current, setCurrent] = useState<Row>(txn);
  useEffect(() => {
    setCurrent(txn);
  }, [txn]);
  const [picking, setPicking] = useState(false);

  const state = workState(current);
  const expense = current.amount < 0 && !isTransferLeg(current);
  const maybePayment = current.amount > 0 && !isTransferLeg(current) && !current.reimbursement;

  // The repaid expenses and their payments, whole-household. The register's
  // own `paid` view, keyed under the register so every refresh reaches it,
  // and filtered here by who points at whom: there is no endpoint for one
  // payment's expenses, and one is not needed for a list this short.
  const links = useQuery({
    queryKey: ["register", household.id, "reimbursement-links"],
    queryFn: () =>
      api.get<RegisterRows>(`/households/${household.id}/transactions?reimbursement=paid`),
    enabled: state === "paid" || maybePayment,
  });

  const change = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patch<Row>(`/transactions/${current.id}/reimbursement`, body),
    onSuccess: (updated) => {
      setCurrent(updated);
      setPicking(false);
      onSaved();
    },
  });

  const linked = links.data?.transactions ?? [];

  if (maybePayment) {
    const repays = linked
      .filter((one) => one.reimbursed_by_id === current.id)
      .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    if (repays.length === 0) return null;
    const currencies = new Set([currency, ...repays.map((one) => one.currency ?? currency)]);
    const mixed = currencies.size > 1;
    const covered = repays.reduce((all, one) => all - one.amount, 0);
    const difference = current.amount - covered;
    return (
      <div className="reimbursement-section">
        <h3 className="section-title">
          Repays {repays.length} {repays.length === 1 ? "expense" : "expenses"}
        </h3>
        <table className="work-repays">
          <tbody>
            {repays.map((one) => (
              <tr key={one.id}>
                <td className="mono">{one.date}</td>
                <td>{one.payee_name ?? <span className="muted">—</span>}</td>
                <td className="small muted">{accountNameOf(one.account_id)}</td>
                <td className="amount">{format(-one.amount, one.currency ?? currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className="difference-sum work-sum">
          <dt>Received</dt>
          <dd className="amount">{format(current.amount, currency)}</dd>
          <dt>Covered</dt>
          <dd className="amount">
            {mixed
              ? repays.map((one) => format(-one.amount, one.currency ?? currency)).join(" + ")
              : format(covered, currency)}
          </dd>
          <dt>Difference</dt>
          <dd className="amount">
            {mixed ? (
              <span className="work-waiting">mixed currencies, not compared</span>
            ) : difference === 0 ? (
              format(0, currency)
            ) : (
              <span className="work-waiting">{format(difference, currency)}</span>
            )}
          </dd>
        </dl>
        {!mixed && difference > 0 ? (
          <p className="small muted">
            {format(difference, currency)} of this payment is not matched to an expense yet. For
            an advance, that is the part still unspent.
          </p>
        ) : null}
        {!mixed && difference < 0 ? (
          <p className="small muted">
            Work paid {format(-difference, currency)} less than these expenses. If it will not
            pay the rest, split that part off and write it off.
          </p>
        ) : null}
      </div>
    );
  }

  if (!expense) return null;

  const payment = linked.find((one) => one.id === current.reimbursed_by_id);
  const waited = Math.max(0, ageInDays(current.date, localToday()));

  return (
    <div className="reimbursement-section">
      <Problem error={change.error} />
      <Field label="Reimbursement">
        <select
          value={current.reimbursement ?? ""}
          disabled={change.isPending}
          onChange={(e) => {
            const body = reimbursementChange(current, e.target.value as "" | ReimbursementState);
            if (body) change.mutate(body);
          }}
        >
          {(Object.keys(REIMBURSEMENT_LABELS) as ("" | ReimbursementState)[]).map((value) => (
            <option key={value} value={value}>
              {REIMBURSEMENT_LABELS[value]}
            </option>
          ))}
        </select>
      </Field>

      {state === "paid" ? (
        <>
          <p className="small" style={{ marginBottom: 4 }}>
            <span className="muted">Reimbursed by </span>
            {payment ? (
              <>
                {payment.date} · {accountNameOf(payment.account_id)} ·{" "}
                <span className="amount pos">
                  {format(payment.amount, payment.currency ?? currency)}
                </span>
                {payment.payee_name ? ` · ${payment.payee_name}` : ""}
              </>
            ) : links.isLoading ? (
              <span className="muted">…</span>
            ) : (
              <span className="muted">a payment outside what the register can show</span>
            )}
          </p>
          <div className="row">
            <button disabled={change.isPending} onClick={() => setPicking(true)}>
              Change
            </button>
            <button
              disabled={change.isPending}
              onClick={() => change.mutate({ clear_settlement: true })}
            >
              Not reimbursed after all
            </button>
          </div>
        </>
      ) : null}

      {state === "owed" ? (
        <>
          <p className="small work-waiting" style={{ marginBottom: 4 }}>
            Not reimbursed yet · {waited} {waited === 1 ? "day" : "days"}
          </p>
          <button disabled={change.isPending} onClick={() => setPicking(true)}>
            Find the payment…
          </button>
        </>
      ) : null}

      {state === "off" ? (
        <p className="small muted">Work will not pay this. It counts as your spending.</p>
      ) : null}

      {picking ? (
        <RowPicker
          household={household}
          title="Which payment repaid this?"
          anchor={current.date}
          days={PAYMENT_PICKER_DAYS}
          intro={
            `Money in within ${PAYMENT_PICKER_DAYS} days either side of ${current.date}. ` +
            "Either side, because an advance arrives before the spending it covers."
          }
          accept={(one) =>
            one.amount > 0 && !isTransferLeg(one) && !one.reimbursement && one.id !== current.id
          }
          action="Link"
          busy={change.isPending}
          error={change.error}
          empty="No money in between those dates. Widen them above — a repayment can take weeks."
          onPick={(one) => change.mutate({ settled_by_id: one.id })}
          onClose={() => setPicking(false)}
        />
      ) : null}
    </div>
  );
}

/**
 * The receipts on one transaction.
 *
 * Between Memo and Cleared, because checking the receipt is very often the
 * reason the panel was opened at all — putting it under the delete button
 * would mean scrolling past every control to see a picture.
 */
function ReceiptSection({ householdId, txn }: { householdId: string; txn: Transaction }) {
  const client = useQueryClient();
  const [shown, setShown] = useState(0);
  const [enlarged, setEnlarged] = useState<Receipt | null>(null);
  const [replacing, setReplacing] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);

  const query = useQuery(receiptsOf(householdId, txn.id));
  const list = query.data ?? [];
  const current = list[Math.min(shown, Math.max(list.length - 1, 0))] ?? null;

  function refresh(message: string | null) {
    setWarning(message);
    setReplacing(false);
    void client.invalidateQueries({ queryKey: ["receipts", householdId, txn.id] });
    void client.invalidateQueries({ queryKey: ["register"] });
  }

  //: Asked first (#199), naming the file.
  const [detaching, setDetaching] = useState<Receipt | null>(null);
  const detach = useMutation({
    mutationFn: (id: string) => api.patch(`/receipts/${id}`, { detach: true }),
    onSuccess: () => {
      setDetaching(null);
      refresh(null);
    },
  });

  return (
    <div className="receipt-section">
      <h3 className="section-title">
        Receipt{list.length > 1 ? `s (${list.length})` : ""}
      </h3>

      {warning ? <div className="banner warn">{warning}</div> : null}

      <ReceiptFrame
        receipt={current}
        onEnlarge={current ? () => setEnlarged(current) : undefined}
      />

      {list.length > 1 ? (
        <div className="thumb-strip">
          {list.map((one, index) => (
            <button
              key={one.id}
              type="button"
              className={index === shown ? "thumb chosen" : "thumb"}
              aria-pressed={index === shown}
              onClick={() => setShown(index)}
            >
              <img src={`/api/receipts/${one.id}/thumb`} alt="" loading="lazy" />
            </button>
          ))}
        </div>
      ) : null}

      {current ? (
        <>
          <p className="small muted" style={{ marginTop: 8 }}>
            <span className="mono">{current.download_name.split("/").pop()}</span> ·{" "}
            {sizeText(current.download_bytes)}
            {" · "}
            <a href={`/api/receipts/${current.id}/${current.has_original ? "original" : "display"}`}>
              Download
            </a>
          </p>
          {/* Detaching one part of a split does not detach the others — they
              are independent attachments now. Saying so beats pretending. */}
          {current.also_on > 0 ? (
            <p className="small muted">
              Also on {current.also_on} other{" "}
              {current.also_on === 1 ? "transaction" : "transactions"} — the same file,
              stored once.
            </p>
          ) : null}
          <MoreInfo receipt={current} />
        </>
      ) : null}

      <div className="row" style={{ marginTop: 10 }}>
        <ReceiptDrop
          target={{ householdId, transactionId: txn.id }}
          label={list.length ? "Add more" : "Attach receipts"}
          primary={list.length === 0}
          listenForPaste
          onDone={(result) => refresh(result.warning)}
        />
        {/* Replace detaches the current one to the inbox rather than deleting
            it: one undo puts both back, and a destructive default on a button
            in a panel people open all day is how the refund funded the wrong
            card. */}
        {current && !replacing ? (
          <button onClick={() => setReplacing(true)}>Replace</button>
        ) : null}
        {current ? (
          <button
            disabled={detach.isPending}
            onClick={() => {
              detach.reset();
              setDetaching(current);
            }}
            title="Send it to the inbox. It stays there until you delete it."
          >
            Detach
          </button>
        ) : null}
      </div>

      {replacing && current ? (
        <div className="banner info">
          The one on screen goes to the inbox, and the new file takes its place. One
          undo puts both back.
          <ReceiptDrop
            target={{ householdId, transactionId: txn.id, replacesId: current.id }}
            label="Choose the replacement"
            primary
            onDone={(result) => refresh(result.warning)}
          />
          <button onClick={() => setReplacing(false)}>Cancel</button>
        </div>
      ) : null}

      {detaching ? (
        <Dialog title="Detach this receipt?" onClose={() => setDetaching(null)}>
          <p style={{ marginTop: 0 }}>
            <span className="mono">{detaching.download_name.split("/").pop()}</span> goes to the
            receipts inbox and stays there until it is attached again or deleted.
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={detach.isPending}
              onClick={() => detach.mutate(detaching.id)}
            >
              {detach.isPending ? "Detaching…" : "Yes, detach it"}
            </button>
            <button disabled={detach.isPending} onClick={() => setDetaching(null)}>
              Keep it here
            </button>
          </div>
          <Problem error={detach.error} />
        </Dialog>
      ) : null}

      {enlarged ? <Lightbox receipt={enlarged} onClose={() => setEnlarged(null)} /> : null}
    </div>
  );
}
