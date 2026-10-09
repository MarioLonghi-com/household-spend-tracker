/** Shapes the server sends. Kept in one file so a change is one diff. */

import type { Coded } from "./noticeMessages";

export type Role = "owner" | "member";
export type ClearedState = "uncleared" | "cleared" | "reconciled";
/**
 * Whether work is expected to pay a row back. Null on the row is "not a work
 * expense", which is nearly every row. There is no "settled" value: a row is
 * repaid when `reimbursed_by_id` points at the payment, and a stored copy of
 * that fact would be a second answer that could disagree with the first.
 */
export type ReimbursementState = "expected" | "written_off";
/**
 * The register's Work expenses filter, as the server spells it.
 *
 * `work` and `paid` also return the *payments* -- the rows something points
 * at -- which is why the register groups each payment with what it repaid.
 */
export type ReimbursementView = "work" | "owed" | "paid" | "off";
export type AccountType =
  | "checking"
  | "savings"
  | "cash"
  | "credit_card"
  | "other_asset"
  | "other_liability";

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  disabled_at: string | null;
}

/** One scheme's colours, named as `styles.css` names its custom properties. */
export interface Scheme {
  ink: string;
  muted: string;
  paper: string;
  surface: string;
  surface_2: string;
  line: string;
  accent: string;
  accent_ink: string;
  danger: string;
  warn: string;
  positive: string;
}

export interface Palette {
  key: string;
  label: string;
  light: Scheme;
  dark: Scheme;
}

export interface Household {
  id: string;
  name: string;
  base_currency: string;
  date_format: string;
  note: string | null;
  /** Which palette. The colours themselves arrive resolved, in `colours`. */
  theme: string;
  /** The accent as picked, if this household overrides the palette's. */
  accent: string | null;
  /**
   * Palette and accent already worked out, as plain hex. The browser applies
   * these; it never computes them. The contrast rules that keep a colour
   * usable live in `app/theming.py` and are tested there, and a second copy
   * here would be a copy that eventually disagrees.
   */
  colours: { light: Scheme; dark: Scheme } | null;
  /** Keep the file a camera or scanner produced, as well as the two copies
   *  this app makes of it. Off by default: the pair of copies is ~33 KB
   *  against the original's ~2 MB. Issue #62. */
  receipts_keep_original: boolean;
  /** True when whoever runs the server has turned it on for every household,
   *  in which case the switch above cannot turn it off. */
  receipts_keep_original_forced: boolean;
  /** How many receipts here already have their original stored. Turning the
   *  switch off keeps every one of them. */
  receipts_with_original: number;
}

export type Categorisation = "history" | "fixed" | "none";

export interface Category {
  id: string;
  group_id: string;
  name: string;
  /** "Quality of Life: Subscriptions" — how it reads outside the picker. */
  full_name: string;
  sort_order: number;
  archived: boolean;
  /** Transactions carrying it. What makes archive and delete different acts. */
  used_by: number;
}

export interface CategoryGroup {
  id: string;
  name: string;
  sort_order: number;
  categories: Category[];
}

/**
 * How a payee picks a category for a new transaction.
 *
 * Not the same as `PayeeRule`, which decides *which payee* a statement line
 * belongs to. This one runs after that and decides what the money was for.
 */
export interface PayeeCategorisation {
  payee_id: string;
  payee_name: string;
  categorisation: Categorisation;
  default_category_id: string | null;
  /** What it would choose right now — derived for history, so sent not guessed. */
  current_default_id: string | null;
  current_default_name: string | null;
  history_window: number;
  history_available: number;
}

export interface Account {
  id: string;
  name: string;
  type: AccountType;
  currency: string;
  closed: boolean;
  note: string | null;
  institution: string | null;
  /** ISO 3166-1 alpha-2, or null when the account never said. */
  country: string | null;
  /** The flag for `country`, or the UN's when there is none. Built server-side. */
  flag: string;
  /** Sent by the server so both sides cannot disagree about which types are debts. */
  is_liability: boolean;
  sort_order: number;
  /** In a statement holding several accounts, the product this one takes (#68). */
  statement_product: string | null;
  balance: number;
  cleared: number;
  uncleared: number;
  /** How many rows of the register sit in this account. */
  transaction_count: number;
  /** The span those rows cover, ISO dates. Both null when there are none. */
  oldest_transaction: string | null;
  newest_transaction: string | null;
  /**
   * Read off the opening-balance row, which is where they live (#10): 0 and
   * nulls for an account opened empty, which has no such row.
   */
  opening_balance: number;
  opening_date: string | null;
  opening_transaction_id: string | null;
  /** Worth saying, never a refusal: today, an opening date after older rows. */
  warnings: string[];
}

/**
 * One line of an accounts file, as the server read it (#146).
 *
 * What the line means rather than what it said: `currency` is the household's
 * when the cell was blank. A row is importable when `problems` is empty -- the
 * server's verdict, shown as it is sent, never worked out again here.
 */
export interface AccountImportRow {
  /** The file's own line number, the header being line 1. */
  line: number;
  name: string;
  /** The type's value when it is one, otherwise the text as typed. */
  type: string;
  currency: string;
  country: string | null;
  flag: string;
  /** Minor units of `currency`. Null when blank or unreadable. */
  opening_balance: number | null;
  /** ISO date. Null when blank (the server dates it today) or unreadable. */
  opening_date: string | null;
  iban: string | null;
  problems: string[];
  /** Each problem as a code and raw params, or null (#267). */
  problem_codes?: (Coded | null)[];
}

export interface AccountImportOut {
  dry_run: boolean;
  /** Accounts written by this request: always 0 on a dry run. */
  created: number;
  rows: AccountImportRow[];
}

export type IdentifierKind = "iban" | "number" | "card" | "alias" | "file_tag" | "holder";

/** What a bank calls an account, or how it spells a household member (issue #66). */
export interface AccountIdentifier {
  id: string;
  kind: IdentifierKind;
  value: string;
  /** Null for `holder`, which names a person rather than an account. */
  account_id: string | null;
}

/** Which account a statement file is for, when the file says so. */
export interface Recognised {
  account_id: string | null;
  account_name: string | null;
  how: string | null;
  /**
   * When nothing was recognised: the token in the file's name to offer as its
   * tag once a person picks the account (#130). `file_tag`, or `iban`.
   */
  tag_kind?: IdentifierKind | null;
  tag?: string | null;
}

/** An identifier the ledger suggests, for a person to add or ignore (#130). */
export interface IdentifierSuggestion {
  kind: IdentifierKind;
  value: string;
  /** Null when it matches none of the household's accounts. */
  account_id: string | null;
  account_name: string | null;
  source: "links" | "pattern" | "account_name" | "file_name";
  why: string;
  /** Rows that mention it -- statement files, for a file tag (`unit`). */
  mentions: number;
  unit: "rows" | "files";
  sample: string | null;
  /** Transfer pairs adding it would link. Null when not counted. */
  would_link: number | null;
}

export interface IdentifierSuggestions {
  items: IdentifierSuggestion[];
  /** Distinct pairs the counted suggestions would link between them. Null
   * unless the counts were asked for (`?count_pairs=1`). */
  would_link: number | null;
}

/** One entry in the country picker. The server sends the list it will accept. */
export interface Country {
  code: string;
  name: string;
  flag: string;
}

export interface Transaction {
  id: string;
  account_id: string;
  date: string;
  amount: number;
  payee_id: string | null;
  payee_name: string | null;
  category_id: string | null;
  category_name: string | null;
  memo: string | null;
  cleared: ClearedState;
  transfer_account_id: string | null;
  transfer_transaction_id: string | null;
  import_id: string | null;
  /** Shared by every part of one split. A grouping, not a parent. */
  split_id: string | null;
  running_balance: number | null;
  /** Whether anything is attached. Computed per request from one indexed read
   *  on the server; there is no column on the row and there must not be. */
  has_receipt: boolean;
  /** Null on an ordinary row. Only money out that is not a transfer leg can
   *  carry one; the server refuses it anywhere else. */
  reimbursement: ReimbursementState | null;
  /** The money-in row that paid this back. Only ever set with `expected`. */
  reimbursed_by_id: string | null;
}

/** What a bulk edit did, including what it declined to do. */
export interface BulkEditResult {
  transactions: Transaction[];
  /** Transfer legs whose category was not set (#124). */
  skipped_transfer_legs: number;
  /** Rows that cannot be work expenses -- money in, transfer legs -- left
   *  unflagged. Their other fields were still applied. */
  skipped_reimbursement: number;
}

/** Which copy of a receipt a URL asks for. */
export type BlobRole = "original" | "display" | "thumb";

export interface Receipt {
  id: string;
  household_id: string;
  /** Null is the inbox: a real state, not a missing one. */
  transaction_id: string | null;
  content_sha256: string;
  original_filename: string | null;
  /** The sniffed type. Never what the browser called it. */
  media_type: string;
  byte_size: number;
  width: number | null;
  height: number | null;
  /** Pages, for a PDF. The frame shows page 1 and says so. */
  page_count: number | null;
  captured_at: string | null;
  /** True when the camera gave a time and no offset, so the time above is
   *  local wall-clock. The panel says "no zone recorded" rather than drawing
   *  a moment it cannot justify. */
  captured_at_is_local: boolean;
  gps_lat: number | null;
  gps_lon: number | null;
  /** Metres. A 4 m fix and a 2 km cell-tower fix must not be drawn alike. */
  gps_accuracy_m: number | null;
  gps_bearing: number | null;
  camera: string | null;
  /** Every remaining scalar EXIF tag. Binary fields were dropped at ingest. */
  exif: Record<string, unknown> | null;
  client_encoded: boolean;
  note: string | null;
  uploaded_by_id: string | null;
  uploaded_by_name: string | null;
  created_at: string;
  /** Derived server-side from live state, never stored. */
  download_name: string;
  has_original: boolean;
  /** The size of the copy the download link serves — not `byte_size`, which
   *  is the file that was uploaded. A 3.2 MB photo downloads as ~300 KB. */
  download_bytes: number;
  /** How many other rows in this household hold the same bytes. */
  also_on: number;
}

export interface ReceiptUpload {
  receipt: Receipt;
  /** Set when these exact bytes are attached elsewhere in this household.
   *  Not a refusal — a bill can cover two rows. */
  warning: string | null;
}

export interface RegisterPage {
  transactions: Transaction[];
  total: number;
  has_running_balance: boolean;
  /** True when the household is past the register's ceiling and `total` is
   *  larger than what came back. Never silent: the screen says so. */
  capped: boolean;
  /** Rows with no category under every other filter of the request, the
   *  category picker set aside: the count beside "Needs a category". */
  needs_category: number;
}

export interface Payee {
  id: string;
  name: string;
  transfer_account_id: string | null;
  /** How many transactions carry it. A payee with one, and a name ending in
   *  twelve digits, is bank noise — and invisible in an alphabetical list. */
  transaction_count: number;
  rule_count: number;
}

/** Payees whose names now fold to one key (#268): offered for merging, never
 *  merged on their own. `key` is the folded form they share. */
export interface PayeeCollision {
  key: string;
  payees: Payee[];
}

/** What a pattern would claim, tried before it is saved. */
export interface RuleTrial {
  matches: number;
  considered: number;
  examples: string[];
  /** A regex that blew its 0.1s deadline. An import would set it aside. */
  timed_out: boolean;
}

/** A set of bank strings that look like one shop, and the rule that fixes it. */
export interface PayeeSuggestion {
  pattern: string;
  match_type: PayeeRule["match_type"];
  strings: number;
  transactions: number;
  /** How many payees those rows are spread across now. Four becoming one is
   *  the number that makes the click worth it. */
  payees: number;
  examples: string[];
}

export interface ReapplyMove {
  transaction_id: string;
  raw: string;
  from_name: string | null;
  /** Null when the payee does not exist yet and applying would create it. */
  to_payee_id: string | null;
  to_name: string;
}

/** What re-applying the rules would do, before anything is written. */
export interface ReapplyPlan {
  considered: number;
  changing: number;
  moves: ReapplyMove[];
  orphaned: string[];
}

export interface ReapplyResult {
  batch_id: string;
  moved: number;
  deleted_payees: string[];
}

/** What a rule does once its pattern has matched — see issue #58.
 *
 *  `map` is the original and the default: this pattern means that payee.
 *  `rewrite` is for a payment rail — `SQ *`, `PAGO MOVIL`, `COMPRA INTERNET` —
 *  where the prefix hides a different shop on every line, so there is no one
 *  payee to map to. It produces a string instead, and the `map` rules run
 *  against that. */
export type RuleAction = "map" | "rewrite";

export interface PayeeRule {
  id: string;
  match_type: "contains" | "equals" | "prefix" | "regex";
  action: RuleAction;
  pattern: string;
  /** Null on a rewrite rule, which has no payee to point at. */
  payee_id: string | null;
  /** What a rewrite puts back. Null means "nothing", the strip case. */
  replacement: string | null;
  priority: number;
  enabled: boolean;
}

export type ImportOutcome =
  | "created"
  | "matched_existing"
  | "duplicate_skipped"
  | "needs_review"
  | "rejected"
  | "skipped";

export interface ImportLine {
  id: string;
  line_no: number;
  /** Null on the agent path unless asked for; the browser always gets it. */
  raw: string | null;
  parsed: Record<string, unknown> | null;
  outcome: ImportOutcome;
  transaction_id: string | null;
  reason: string | null;
  /** `reason` as a code and raw params, for a screen not in English (#267). */
  reason_code?: string | null;
  reason_params?: Record<string, unknown> | null;
  /** Where this line will land — the payee's rule, unless somebody said otherwise. */
  category_id: string | null;
  category_name: string | null;
  /** True when a person picked it, false when it is the rule's guess. */
  category_chosen: boolean;
  /**
   * True when a person chose "no category" (#9): it commits uncategorised
   * whatever the payee's rule or the bank's wording would say. `category_chosen`
   * is true with it and `category_id` null.
   */
  category_uncategorised: boolean;
  /** Other lines in this import with the same payee and no choice of their own. */
  similar_lines: number;
}

export interface ImportPreview {
  batch_id: string;
  filename: string | null;
  account_id: string;
  sha256: string;
  detected: Record<string, unknown>;
  warnings: string[];
  /** Each warning as a code and raw params, or null (#267). */
  warning_codes?: (Coded | null)[];
  counts: Record<string, number>;
  lines: ImportLine[];
}

/**
 * One of History's sentences as structure (#266): a key from the server's
 * `SENTENCES` and its params. `lib/historyWords.ts` words it.
 */
export interface HistoryPhrase {
  key: string;
  params: Record<string, HistoryNode>;
}

/** A raw value in a History phrase; the shapes are `app/services/describing.py`'s. */
export type HistoryValue =
  | { type: "money"; amount: number; currency: string }
  | { type: "date"; value: string; style?: "medium" }
  | { type: "name" | "text"; value: string }
  | { type: "number"; value: number }
  | { type: "category"; group: string; name: string }
  | { type: "enum"; column: string; value: string }
  | { type: "word"; set: string; key: string; lower?: boolean }
  | { type: "count"; table: string; count: number }
  | { type: "list"; items: HistoryNode[]; sep: string };

export type HistoryNode = HistoryPhrase | HistoryValue | number | string;

export interface Batch {
  id: string;
  kind: string;
  status: string;
  actor_id: string;
  started_at: string;
  finished_at: string | null;
  source: Record<string, unknown> | null;
  summary: Record<string, number> | null;
  undone_by_id: string | null;
  /** What kind of act it was, in words. */
  headline: string;
  /** The same as a key, for a screen not in English (#57). */
  headline_key?: string;
  /** What it actually did — computed from the change rows, never stored. */
  detail: string;
  /** The same as structure, for a screen not in English (#266). */
  detail_phrase?: HistoryPhrase | null;
  /** Who did it, by name. */
  actor_name: string | null;
  /**
   * The program that did it on their behalf, when one did.
   *
   * Beside the person, never instead of them: a key borrows a human's
   * authority and does not replace them, so `actor_name` stays whoever the
   * key belongs to. Read from the batch's denormalised copy, so it still
   * reads correctly once the key has been revoked and swept.
   */
  via: string | null;
  change_count: number;
}

export interface FieldChange {
  field: string;
  was: string;
  now: string;
  /** The column `field` names, for a screen not in English (#57). */
  column?: string;
  /** `was` and `now` as raw values, for a screen not in English (#266). */
  was_value?: HistoryValue | HistoryPhrase | null;
  now_value?: HistoryValue | HistoryPhrase | null;
}

/** One changed row, as far down as the log goes. */
export interface ChangeDetail {
  seq: number;
  table: string;
  row_id: string;
  op: "insert" | "update" | "delete";
  summary: string;
  /** What each column went from and to. Empty on an insert or a delete. */
  fields: FieldChange[];
  /** The whole row, for an insert or a delete, where there is no "from". */
  snapshot: FieldChange[];
  /** Columns deliberately kept out of the log. Absent, not masked. */
  redacted: string[];
  /** The table `table` names, for a screen not in English (#57). */
  table_key?: string;
  /** `summary` as structure (#266). */
  summary_phrase?: HistoryPhrase | null;
}

/** One batch spelled out, which is what the undo confirmation is built from. */
export interface BatchDetail extends Batch {
  lines: string[];
  /** The same lines as structure (#266). */
  line_phrases?: HistoryPhrase[];
  changed_rows: ChangeDetail[];
}

export interface Member {
  user_id: string;
  display_name: string;
  email: string;
  role: Role;
  added_at: string;
  /** Transactions this person entered that are still in the register. */
  transactions_logged: number;
  /** The part of that one of their agent keys did, rather than their hands. */
  transactions_by_agent: number;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  created_at: string;
  disabled_at: string | null;
  recovery_codes_left: number;
  households: string[];
}

export interface AdminHousehold {
  id: string;
  name: string;
  base_currency: string;
  date_format: string;
  created_at: string;
  member_ids: string[];
}

export interface Invitation {
  id: string;
  email: string | null;
  role: Role;
  invited_by_id: string;
  household_ids: string[] | null;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
}

export interface InviteCreated {
  invitation: Invitation;
  link: string;
}

/** What an account reset link says before it is followed (#284). */
export interface ResetState {
  email: string;
  display_name: string;
  /** The link sets a new password. */
  password: boolean;
  /** The link enrols a new authenticator and issues new recovery codes. */
  authenticator: boolean;
  /** The owner who reset the account; null when it came from the server. */
  reset_by: string | null;
  created_at: string;
}

/** Whether this server's key opens its members' authenticators (#287). Owners only. */
export interface RecoveryMode {
  /** Members who can sign in -- not disabled -- with an authenticator enrolled. */
  enrolled: number;
  /** Of those, the ones the key cannot open: recovery mode while above nought. */
  locked: number;
  /**
   * The key came from `SPENDTRACKER_SECRET_KEY`, so `secret.key` is not read
   * and the banner names the variable instead.
   */
  key_from_environment: boolean;
}

/** Your own second factor, as the profile needs it (#287). Never anybody else's. */
/** A passkey as its owner sees it (`GET /me/passkeys`, #120). */
export interface Passkey {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
  /** May live on more than one device; false is "this device only". */
  synced: boolean;
  /** The host name it was made for ... */
  rp_id: string;
  /** ... and whether that is this instance's. False: it can only be removed. */
  usable_here: boolean;
  aaguid: string | null;
}

export interface AuthenticatorStatus {
  /** An authenticator is set up. False after a reset cleared it. */
  enrolled: boolean;
  /** Set up, and this server's key cannot open it: recovery mode, for you. */
  locked_by_key: boolean;
}

/**
 * A reset link an owner has just issued (#286). Shown once: the server keeps
 * only a hash. The switches may be more than were asked for -- a link that
 * replaces a pending one keeps what that one reset.
 */
export interface ResetIssued {
  link: string;
  expires_at: string;
  password: boolean;
  authenticator: boolean;
}

/** A reset link not yet followed or withdrawn (#286). */
export interface PendingReset {
  id: string;
  user_id: string;
  display_name: string;
  email: string;
  password: boolean;
  authenticator: boolean;
  /** The owner who issued it; null when it came from the server. */
  issued_by: string | null;
  created_at: string;
  expires_at: string;
  /** Lapsed. The account is still shut; the way in is a new link. */
  expired: boolean;
}

/**
 * One reset, new owner, or server act on an account's way in, from the last
 * fourteen days, read from the audit log (#286).
 */
export interface SignInChange {
  /**
   * The change's position in the log: newer is larger. One change can make
   * two items (promoted and re-enabled in one write), which share it.
   */
  id: number;
  /** Unique per item. */
  key: string;
  what: "reset" | "owner_added" | "promoted" | "reenabled" | "authenticator_replaced";
  user_id: string;
  user_name: string;
  /** For a reset, what the link resets. */
  password: boolean;
  authenticator: boolean;
  /** Null when it came from the server. */
  by_id: string | null;
  by_name: string | null;
  from_server: boolean;
  at: string;
}

export interface InviteState {
  role: Role;
  email: string | null;
  invited_by: string;
  households: string[];
}

export interface TransactionOrigin {
  /** Which door: a statement (or an agent's staged rows), or a one-time import (#183). */
  kind: "statement" | "one_time_import";
  imported_at: string;
  filename: string | null;
  /** Null for a one-time import, which keeps no lines. */
  line_no: number | null;
  raw: string | null;
  bank: Record<string, string> | null;
  payee_original: string | null;
  import_id: string | null;
  batch_id: string;
  /** The program that staged it, when one did. An agent import has no file. */
  via: string | null;
  /** One-time import only: the app ("YNAB"), how it was read, and the plan's name. */
  workflow?: string | null;
  workflow_via?: string | null;
  plan_name?: string | null;
}

/** One earlier one-time import, as History has it (#183). */
export interface OneTimePriorImport {
  batch_id: string;
  at: string;
  workflow: string;
  workflow_name: string;
  via: string | null;
  filename: string | null;
  plan_name: string | null;
  /** "applied", or "undone" once History took it back. */
  status: string;
}


/** One row still to be accounted for when reconciling. */
export interface Candidate {
  id: string;
  date: string;
  payee: string | null;
  memo: string | null;
  amount: number;
  cleared: ClearedState;
}

export interface Worksheet {
  account_id: string;
  currency: string;
  /** The sum of rows already locked. A new statement is measured from here. */
  locked_balance: number;
  last_statement_date: string | null;
  last_statement_balance: number | null;
  candidates: Candidate[];
}

export interface Reconciliation {
  id: string;
  account_id: string;
  statement_date: string;
  /** What the bank said, in minor units. The one figure that is not derivable. */
  statement_balance: number;
  batch_id: string | null;
  created_at: string;
}


/** One recorded change to one row, as the transaction panel reads it. */
export interface Change {
  seq: number;
  batch_id: string;
  table_name: string;
  row_id: string;
  op: "insert" | "update" | "delete";
  at: string;
  /** The same sentence the History screen shows, from the same engine. */
  summary: string;
  /** The same as structure, for a screen not in English (#266). */
  summary_phrase?: HistoryPhrase | null;
  actor_name: string | null;
  /**
   * The program that did it on their behalf, when one did.
   *
   * Beside the person, never instead of them: a key borrows a human's
   * authority and does not replace them, so `actor_name` stays whoever the
   * key belongs to. Read from the batch's denormalised copy, so it still
   * reads correctly once the key has been revoked and swept.
   */
  via: string | null;
}

/**
 * A key somebody has given to a program acting for them.
 *
 * Never carries the token: only its SHA-256 is stored, so there is nothing for
 * any response to return. It is shown once, at issue, and that is the only
 * time it exists outside the holder's hands.
 */
export interface AgentKey {
  id: string;
  label: string;
  agent_name: string | null;
  household_id: string;
  /** Computed from one stored value — `write` implies `read`. */
  scopes: string[];
  may_commit: boolean;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  /** Whether it would open the door right now. Three columns and a clock. */
  live: boolean;
}

/**
 * One category's line across a report's window.
 *
 * `by_month` carries only the months this row has anything in; read it against
 * the report's `months` list, where an absent key is a zero. On a twelve-month
 * report over a long category list, the zeroes are most of the payload.
 */
export interface ReportRow {
  key: string | null;
  name: string;
  group_name: string | null;
  by_month: Record<string, number>;
  total_minor: number;
  total: string;
  average_minor: number;
  average: string;
  count: number;
}

export interface ReportSection {
  rows: ReportRow[];
  by_month: Record<string, number>;
  total_minor: number;
  total: string;
  average_minor: number;
  average: string;
  count: number;
}

/**
 * Income against expense for **one** currency.
 *
 * `currency` is not optional and there is no field holding a total across
 * currencies, because the ledger never converts and that figure does not
 * exist. A null or a zero there would be a number somebody could believe.
 */
export interface IncomeExpense {
  currency: string;
  since: string;
  until: string;
  /** Every `YYYY-MM` in the window, including the empty ones. */
  months: string[];
  income: ReportSection;
  expense: ReportSection;
  net_by_month: Record<string, number>;
  net_total_minor: number;
  net_total: string;
  /**
   * What the flow predicate removed. Printed, so the reader can check.
   * `reimbursements` is work expenses and the payments that repaid them.
   */
  excluded: { transfers: number; opening_balances: number; reimbursements: number };
  coverage: { months_in_range: number; months_with_activity: number };
}

/** One transaction behind a figure on a report. */
export interface ReportEntry {
  id: string;
  date: string;
  account_name: string;
  payee_name: string | null;
  category_name: string | null;
  memo: string | null;
  amount_minor: number;
  amount: string;
}

/**
 * What adds up to one figure.
 *
 * `total_minor` is counted over the whole match rather than over `entries`,
 * so a capped bubble still reconciles against the cell it came from.
 */
export interface ReportBehind {
  entries: ReportEntry[];
  total_minor: number;
  total: string;
  count: number;
  capped: boolean;
}

/** One expense work still owes, as the Reimbursements report lists it. */
export interface ReimbursementOutstanding {
  id: string;
  date: string;
  payee_name: string | null;
  account_id: string;
  account_name: string;
  /** Positive minor units, in the report's currency. */
  amount: number;
  has_receipt: boolean;
  /** What the row says about itself: the Memo column and the detail dialog. */
  memo: string | null;
  category_name: string | null;
}

/** The money-in row a claim was paid with. */
export interface ReimbursementSettlement {
  id: string;
  date: string;
  /** Positive minor units, in `currency` -- which may not be the report's. */
  amount: number;
  currency: string;
  account_id: string;
  account_name: string;
  payee_name: string | null;
  /** What the row says about itself: the Memo column and the detail dialog. */
  memo: string | null;
  category_name: string | null;
}

/** One expense a payment repaid. */
export interface ReimbursementClaimExpense {
  id: string;
  date: string;
  payee_name: string | null;
  account_id: string;
  account_name: string;
  currency: string;
  /** Positive minor units, in `currency`. */
  amount: number;
  /** What the row says about itself: the Memo column and the detail dialog. */
  memo: string | null;
  category_name: string | null;
}

/**
 * One payment and everything that points at it. The claim is discovered, not
 * stored: there is no claim table, only expenses sharing a `reimbursed_by_id`.
 */
export interface ReimbursementClaim {
  settlement: ReimbursementSettlement;
  /** Oldest first. */
  expenses: ReimbursementClaimExpense[];
  /** Null when `currencies` has more than one entry: never netted. */
  covered: number | null;
  /** `settlement.amount - covered`. Null exactly when `covered` is. */
  difference: number | null;
  currencies: string[];
}

/** One month of flagged rows, by the month the expense happened. */
export interface ReimbursementMonth {
  /** `YYYY-MM`. */
  month: string;
  flagged: number;
  recovered: number;
  written_off: number;
  outstanding: number;
}

/**
 * What work owes, for **one** currency. Every figure is positive minor units
 * in `currency`; there is no total across currencies because there is no rate.
 */
export interface ReimbursementsReport {
  currency: string;
  since: string | null;
  until: string | null;
  /** Busiest first. Empty when nothing anywhere is flagged. */
  available_currencies: string[];
  outstanding: number;
  outstanding_count: number;
  oldest_outstanding: string | null;
  recovered: number;
  recovered_count: number;
  written_off: number;
  written_off_count: number;
  /** Positive differences over payments in this currency: money in that no
   *  expense accounts for yet -- an unspent advance, or an overpayment. */
  unmatched: number;
  outstanding_rows: ReimbursementOutstanding[];
  /** Newest payment first. */
  claims: ReimbursementClaim[];
  /** Ascending, only months with a flagged row in this currency. */
  months: ReimbursementMonth[];
}
