"""What crosses the HTTP boundary.

Amounts are integer minor units in both directions, never rounded and never a
float. Anything meant for a human to read carries a separate ``*_display``
field, so the number and its presentation never get confused for each other.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .auth.email_canonical import MAX_LENGTH as MAX_EMAIL_LENGTH
from .models.enums import (
    AccountType,
    AgentScope,
    BatchKind,
    BatchStatus,
    Categorisation,
    ChangeOp,
    ClearedState,
    IdentifierKind,
    ImportOutcome,
    MatchType,
    ReimbursementState,
    Role,
    RuleAction,
)
from .money import MAX_MINOR, MIN_MINOR


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #


class UserOut(ORMModel):
    id: str
    email: str
    display_name: str
    role: Role
    disabled_at: datetime | None = None


class PresenceOut(BaseModel):
    """Who else is here right now."""

    online: list[UserOut]


# --------------------------------------------------------------------------- #
# Setup and sign-in
# --------------------------------------------------------------------------- #


#: Amounts are bounded by the column, not by taste: `transactions.amount` is a
#: 64-bit integer, and an out-of-range value used to escape as an OverflowError
#: out of the SQLite driver -- a 500, and for an import, one that took every
#: other line of the file with it.
Minor = Annotated[int, Field(ge=MIN_MINOR, le=MAX_MINOR)]
#: Free text the user types. Matched to the column width: SQLite ignores
#: VARCHAR(n), so an over-long value is stored happily and the schema quietly
#: stops describing the data -- and the same request is a hard error on
#: Postgres. Capping here keeps the two honest.
Memo = Annotated[str, Field(max_length=500)]
Note = Annotated[str, Field(max_length=2000)]
Institution = Annotated[str, Field(max_length=120)]
DateFormat = Annotated[str, Field(max_length=32)]
#: A single request should not be able to name an unbounded number of rows.
IdList = Annotated[list[str], Field(max_length=1000)]


class SetupBegin(BaseModel):
    token: str
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1)


class SetupEnrol(BaseModel):
    blob: str
    code: str = Field(min_length=6, max_length=10)


class SetupComplete(BaseModel):
    blob: str
    #: The owner confirms they have stored the recovery codes somewhere that is
    #: not this browser.
    codes_saved: bool = False


class SetupStarted(BaseModel):
    blob: str
    otpauth_uri: str
    secret: str
    recovery_codes: list[str]


class SignIn(BaseModel):
    #: Bounded because a refused sign-in is still *recorded*: the address lands
    #: in `login_attempts` on its own committed transaction before the password
    #: is judged, and SQLite does not enforce the column's `String(254)`. An
    #: unbounded field let a stranger park a megabyte per request there.
    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    #: argon2 pre-hashes the password, so length is not a cost problem, but
    #: nothing this long is a password and it should not reach the hasher.
    password: str = Field(max_length=1024)


class SubmitCode(BaseModel):
    code: str = Field(min_length=6, max_length=10)
    trust_this_browser: bool = True


class SubmitRecoveryCode(BaseModel):
    code: str = Field(min_length=6, max_length=64)


class SignInState(BaseModel):
    """Where the sign-in has got to."""

    authenticated: bool
    needs_code: bool = False
    user: UserOut | None = None
    #: Only after a recovery code: how many live agent keys it revoked, so the
    #: screen can say so rather than leave them to be found not working.
    keys_revoked: int = 0
    #: Only after a recovery code, and only in recovery mode -- the server's
    #: key cannot open this member's authenticator (#287): leave to set up a
    #: new one without a second recovery code. Handed back as `grant` to
    #: `POST /me/authenticator/confirm`; see `profile.grant_key_recovery`.
    reenrolment_grant: str | None = None
    #: Only after the right password, in recovery mode (#287): no code from
    #: this member's authenticator can be checked, so the screen goes straight
    #: to a recovery code. With `detail`, the sentence that says why -- the
    #: same pair the code step's refusal carries, so the screen reads both one
    #: way. The password is proven by then, so neither tells anybody anything
    #: the code step would not.
    key_replaced: bool = False
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Households
# --------------------------------------------------------------------------- #


class HouseholdCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="EUR", min_length=3, max_length=3)
    date_format: DateFormat = "YYYY-MM-DD"


class HouseholdOut(ORMModel):
    id: str
    name: str
    base_currency: str
    date_format: str
    note: str | None = None
    theme: str
    #: The accent as it was picked. What each scheme actually shows is in
    #: `colours` -- one stored colour cannot sit on both a white page and a
    #: near-black one, so the two are not the same value.
    accent: str | None = None
    #: The finished article: palette plus accent, resolved, as plain hex. Sent
    #: so the browser applies colours rather than computing them; the contrast
    #: rules live in one language and are tested there.
    colours: HouseholdColours | None = None
    #: Keep the uploaded bytes of a receipt as well as the derivatives.
    receipts_keep_original: bool = False
    #: True when the *operator* has turned it on for the whole instance, in
    #: which case the household toggle cannot turn it off. Sent so the screen
    #: can say that rather than showing a control that does nothing. Issue #62.
    receipts_keep_original_forced: bool = False
    #: How many receipts in this household already have their original stored.
    #: The number the warning needs: switching the toggle off keeps every one
    #: of these and changes only what happens to the next upload.
    receipts_with_original: int = 0


class HouseholdUpdate(BaseModel):
    """Everything about a household that can be changed after it exists.

    `None` for `accent` means "go back to the palette's own colour", which is
    why it is optional-with-a-default rather than just optional: leaving the
    field out and sending null are different requests.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    date_format: DateFormat | None = None
    note: str | None = None
    clear_note: bool = False
    theme: str | None = None
    accent: str | None = None
    clear_accent: bool = False
    receipts_keep_original: bool | None = None


class SchemeOut(BaseModel):
    """One scheme's colours, named as the stylesheet names them."""

    ink: str
    muted: str
    paper: str
    surface: str
    surface_2: str
    line: str
    accent: str
    accent_ink: str
    danger: str
    warn: str
    positive: str


class PaletteOut(BaseModel):
    key: str
    label: str
    light: SchemeOut
    dark: SchemeOut


class HouseholdColours(BaseModel):
    light: SchemeOut
    dark: SchemeOut


class MemberOut(BaseModel):
    user_id: str
    display_name: str
    email: str
    role: Role
    added_at: datetime
    #: Transactions in this household's register that this person entered,
    #: counted from the audit log rather than from a column on the row. See
    #: `services/households.transactions_logged` for the three things that
    #: query decides -- in particular that it counts rows that are still there.
    transactions_logged: int = 0
    #: The part of `transactions_logged` that one of their agent keys did. A
    #: key borrows its owner's authority, so the work is counted under their
    #: name and this says how much of it was not typed by hand.
    transactions_by_agent: int = 0


class AddMember(BaseModel):
    user_id: str


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: AccountType
    #: Defaults to the household's currency.
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    note: Note | None = None
    institution: Institution | None = None
    #: ISO 3166-1 alpha-2. Optional: most accounts will never say.
    country: str | None = Field(default=None, min_length=2, max_length=2)
    #: What was in it when you started tracking, in minor units. Recorded as a
    #: real transaction dated `opening_date`, which is usually in the past.
    opening_balance: Minor = 0
    opening_date: Date | None = None


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    #: Both trimmed when stored, and a blank one stored as null (#20). The
    #: length limit is on the value as sent -- the characters the panel's
    #: `maxLength` counts -- so trimming can only bring it further under.
    note: Note | None = None
    institution: Institution | None = None
    #: Separate from `note` / `institution` being null, which means "leave it
    #: alone" -- the shape `clear_country` has.
    clear_note: bool = False
    clear_institution: bool = False
    country: str | None = Field(default=None, min_length=2, max_length=2)
    #: Separate from `country` being null, which means "leave it alone".
    clear_country: bool = False
    closed: bool | None = None
    #: Where it sits in the list. The accounts query has always ordered by this
    #: column and nothing could write it, so the order was silently by name.
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    #: Which product of a multi-account statement this account takes (#68).
    statement_product: str | None = Field(default=None, min_length=1, max_length=40)
    clear_statement_product: bool = False
    #: Written to the account's opening-balance row, not to the account (#10).
    #: Zero removes that row; a figure on an account opened empty writes one.
    #: See `accounts.set_opening` for what each combination does.
    opening_balance: Minor | None = None
    opening_date: Date | None = None

    @model_validator(mode="after")
    def _set_or_clear(self) -> AccountUpdate:
        # A value and its clear flag are two answers to one question, so both
        # at once is a malformed request rather than one to guess the meaning of.
        for field in ("note", "institution"):
            if getattr(self, field) is not None and getattr(self, f"clear_{field}"):
                raise ValueError(f"send {field} or clear_{field}, not both")
        return self


class IdentifierCreate(BaseModel):
    kind: IdentifierKind
    value: str = Field(min_length=1, max_length=120)
    #: Null only for `holder`, which names a person rather than an account.
    account_id: str | None = None


class IdentifierOut(ORMModel):
    id: str
    kind: IdentifierKind
    value: str
    account_id: str | None = None


class HolderSamplesOut(BaseModel):
    """What one holder's name matches in the ledger (issue #132)."""

    identifier_id: str
    #: Rows whose bank text names this holder.
    rows: int
    #: A few of those texts, most recent first.
    samples: list[str]


class RecognisedOut(BaseModel):
    """Which account a statement file is for, when the file says so."""

    account_id: str | None = None
    account_name: str | None = None
    #: In words, for the Import screen. Null when nothing was recognised.
    how: str | None = None
    #: When nothing was: the token in the file's name the Import screen offers
    #: as its tag, once a person picks the account (#130). Kind `file_tag`, or
    #: `iban` when the token is one.
    tag_kind: IdentifierKind | None = None
    tag: str | None = None


class IdentifierSuggestionOut(BaseModel):
    """One identifier the ledger suggests (#130). Nothing is added until a
    person says so."""

    kind: IdentifierKind
    value: str
    #: Null when it matches none of the household's accounts.
    account_id: str | None = None
    account_name: str | None = None
    #: `links`, `pattern`, `account_name` or `file_name`.
    source: str
    why: str
    #: Rows that mention it -- statement files, for a file tag (`unit`).
    mentions: int
    unit: str
    sample: str | None = None
    #: Transfer pairs adding it would link. Null when not counted.
    would_link: int | None = None


class IdentifierSuggestionsOut(BaseModel):
    items: list[IdentifierSuggestionOut]
    #: Distinct pairs the counted suggestions would link between them. Null
    #: when the counts were not asked for (`count_pairs`).
    would_link: int | None


class IdentifierSuggestionIgnore(BaseModel):
    kind: IdentifierKind
    value: str = Field(min_length=1, max_length=120)


class IgnoredSuggestionOut(ORMModel):
    id: str
    kind: IdentifierKind
    value: str


class TransferLegOut(BaseModel):
    id: str
    account_id: str
    account_name: str
    date: Date
    amount: Minor
    currency: str
    #: What the bank said, else the payee: what a person recognises a row by.
    description: str | None = None


class TransferPairOut(BaseModel):
    out_leg: TransferLegOut
    in_leg: TransferLegOut
    strength: str
    why: str
    #: The description words both rows carry (#127). A suggestion with any
    #: goes first in "Worth a look".
    words: list[str] = []


class AwaitingLegOut(BaseModel):
    leg: TransferLegOut
    why: str


class LinkedPairOut(BaseModel):
    """A link already made, which nothing but account history vouches for (#131)."""

    out_leg: TransferLegOut
    in_leg: TransferLegOut
    #: How it was linked: `history`, `import` (before links said how),
    #: `named` when the name it was linked on has since gone, or `agent` --
    #: a program linked it and no person has confirmed it (#134).
    link_source: str | None
    why: str


class TransferFindingsOut(BaseModel):
    """Likely transfers among rows nobody has linked yet (issue #70)."""

    strong: list[TransferPairOut]
    suggested: list[TransferPairOut]
    awaiting: list[AwaitingLegOut]
    #: Links with no name evidence that no person made: "Linked by history
    #: only", for review (#131).
    unproven: list[LinkedPairOut] = []


class TransferLinkPair(BaseModel):
    first_id: str
    second_id: str


class TransferLinkRequest(BaseModel):
    pairs: list[TransferLinkPair] = Field(min_length=1, max_length=2000)
    #: Who vouches for each pair (#131). `person`, the default: somebody chose
    #: these pairs -- one Link, the ticked ones, the register's "Link as
    #: transfer". `evidence`: "Link all", which is not a person looking at
    #: each; each link is recorded as what its evidence is, so one linked on
    #: history alone does not become history for the next.
    by: Literal["person", "evidence"] = "person"


class TransferRejectRequest(BaseModel):
    """Pairs a person says are not transfers, never to be offered again (#131)."""

    pairs: list[TransferLinkPair] = Field(min_length=1, max_length=2000)


class TransferRejectResult(BaseModel):
    rejected: int


class TransferLinkResult(BaseModel):
    linked: int


class AccountOut(ORMModel):
    id: str
    name: str
    type: AccountType
    currency: str
    closed: bool
    note: str | None = None
    institution: str | None = None
    country: str | None = None
    #: The flag, worked out from the code, or the UN's when there is not one.
    #: Sent rather than derived on the client so both halves of the fallback --
    #: "no country" and "a code we do not know" -- are decided in one place.
    flag: str = ""
    #: Whether a negative balance here means "you owe this" rather than "you
    #: are overdrawn". Derived from `type`, and sent rather than re-derived on
    #: the client, so the two cannot come to disagree about which types are
    #: debts. This is what `AccountType.is_liability` is for -- it had no caller
    #: at all until now, which by this project's own rule made six account types
    #: six menu items with no behaviour behind them.
    is_liability: bool = False
    statement_product: str | None = None
    #: Minor units, in this account's own currency.
    sort_order: int = 0
    balance: int = 0
    cleared: int = 0
    uncleared: int = 0
    #: How much of the register sits in this account, and over what span. Dates
    #: rather than amounts, so none of the three has a currency and none of them
    #: can be added across accounts by mistake. Both dates are null for an
    #: account with nothing in it, which is a real state -- an account opened
    #: with a zero balance has no opening-balance row either.
    #:
    #: `transaction_count`, not `transactions`: this model is validated from
    #: the ORM object, and `Account.transactions` is the relationship. A field
    #: of that name silently reads the list instead of the count, which is a
    #: 500 on every account route -- found by the suite the first time it ran.
    transaction_count: int = 0
    oldest_transaction: Date | None = None
    newest_transaction: Date | None = None
    #: Read off the opening-balance row, which is where they live: there is
    #: no column for either (#10). An account opened empty has no row, so its
    #: balance is 0 and its date and row id are null. The id is so the screen
    #: can open the row in the register.
    opening_balance: int = 0
    opening_date: Date | None = None
    opening_transaction_id: str | None = None
    #: Things worth saying that are not refusals -- today only that the
    #: opening date is after the account's earliest row. Worked out from the
    #: two dates above, so the list and the PATCH say the same thing.
    warnings: list[str] = []


class AccountImportRow(BaseModel):
    """One line of an accounts file, as the importer read it (#146).

    What the line *means*, not what it said: `currency` is the household's
    when the cell was blank, `country` is tidied to its code. A cell that
    could not be read comes back null rather than half-read, and `problems`
    says why in the words to show. A row is importable when `problems` is
    empty; the client decides nothing about it on its own.
    """

    #: The file's own line number, the header being line 1, so a person can
    #: find it in the spreadsheet they typed it into.
    line: int
    name: str
    #: The type's value when it is one, otherwise the text as typed.
    type: str
    currency: str
    country: str | None = None
    flag: str = ""
    #: Minor units of `currency`. Null when the cell was blank or unreadable.
    opening_balance: int | None = None
    #: Null when blank (the service then dates the balance today) or unreadable.
    opening_date: Date | None = None
    iban: str | None = None
    problems: list[str] = Field(default_factory=list)


class AccountImportOut(BaseModel):
    dry_run: bool
    #: Accounts written by this request: always 0 on a dry run.
    created: int
    rows: list[AccountImportRow]


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


class TransactionCreate(BaseModel):
    account_id: str
    date: Date
    amount: Minor
    payee_id: str | None = None
    payee_name: str | None = None
    #: Left out means "work it out from the payee". Sending null means
    #: "deliberately uncategorised", which is not the same request.
    category_id: str | None = None
    uncategorised: bool = False
    memo: Memo | None = None
    cleared: ClearedState = ClearedState.uncleared


class TransactionUpdate(BaseModel):
    category_id: str | None = None
    clear_category: bool = False
    date: Date | None = None
    amount: Minor | None = None
    payee_id: str | None = None
    payee_name: str | None = None
    memo: Memo | None = None
    cleared: ClearedState | None = None
    #: Explicit rather than a sentinel, so "leave it alone" and "empty it" are
    #: different requests rather than different spellings of null.
    clear_payee: bool = False
    clear_memo: bool = False


class TransferCreate(BaseModel):
    from_account_id: str
    to_account_id: str
    date: Date
    #: Positive magnitude leaving the source account, in its own minor units.
    amount: int = Field(gt=0, le=MAX_MINOR)
    #: Required when the two accounts differ in currency; we never invent a rate.
    to_amount: int | None = Field(default=None, gt=0, le=MAX_MINOR)
    memo: Memo | None = None
    cleared: ClearedState = ClearedState.uncleared


class BulkEdit(BaseModel):
    """One act over many rows, so undo reverses the whole selection."""

    transaction_ids: IdList = Field(min_length=1)
    payee_id: str | None = None
    payee_name: str | None = None
    cleared: ClearedState | None = None
    memo: Memo | None = None
    category_id: str | None = None
    #: Empty the category on every selected row. Separate from `category_id`
    #: being null, which means "leave it alone".
    clear_category: bool = False
    #: Flag every selected row as a work expense, or as written off. Money
    #: coming in and transfer legs are skipped and counted, not refused. There
    #: is deliberately no payment here: linking one is its own route, where
    #: every expense was ticked beside the one payment it names.
    reimbursement: ReimbursementState | None = None
    #: Take the work-expense flag (and any payment link) off every selected row.
    clear_reimbursement: bool = False


class ReimbursementUpdate(BaseModel):
    """Both fields tri-state, in the house style: absent means leave it alone,
    and the `clear_*` boolean means empty it."""

    state: ReimbursementState | None = None
    clear_state: bool = False
    #: The payment -- money coming in, in the same household -- that repaid
    #: this. Setting one on a row nobody had flagged flags it `expected`.
    settled_by_id: str | None = None
    clear_settlement: bool = False


class ReimbursementLink(BaseModel):
    """Many expenses, one payment: the claim, recorded in one act."""

    expense_ids: list[str] = Field(min_length=1, max_length=200)
    settlement_id: str


class SplitPartIn(BaseModel):
    amount: Minor
    category_id: str | None = None
    memo: Memo | None = None


class SplitRequest(BaseModel):
    """Divide one transaction into 2-5 parts that add up to it."""

    parts: list[SplitPartIn] = Field(min_length=2, max_length=5)


class DuplicateRequest(BaseModel):
    date: Date | None = None


class TransactionOut(ORMModel):
    id: str
    account_id: str
    date: Date
    amount: int
    payee_id: str | None = None
    payee_name: str | None = None
    category_id: str | None = None
    #: "Quality of Life: Subscriptions". Sent alongside the id so the register
    #: can show it without holding the whole category tree in memory.
    category_name: str | None = None
    #: Set on every part of a split, shared between them. A grouping, not a
    #: parent: the row they replaced is in the audit log, not in the register.
    split_id: str | None = None
    memo: str | None = None
    cleared: ClearedState
    transfer_account_id: str | None = None
    transfer_transaction_id: str | None = None
    import_id: str | None = None
    #: Only set when the register is one account in date order; meaningless
    #: otherwise, so it is absent rather than wrong.
    running_balance: int | None = None
    #: Whether anything is attached. Computed per request from one indexed
    #: read, never stored on the row -- see services/receipts.py::receipted_ids.
    has_receipt: bool = False
    #: The currency of the account this row is in. `amount` has always been in
    #: it and never said so, which left the register deducing it from the
    #: accounts list -- and that list omits closed accounts, so a row on a
    #: closed account fell back to the household's base currency and could be
    #: drawn under the wrong currency's column. Sent with the row instead, so
    #: the figure and its currency arrive together.
    currency: str | None = None
    #: A work expense: `expected` (work should pay it back) or `written_off`
    #: (work will not). Null on an ordinary row.
    reimbursement: ReimbursementState | None = None
    #: The payment that repaid it. Repaid is this being set; nothing else says so.
    reimbursed_by_id: str | None = None


class BulkEditResult(BaseModel):
    """What a bulk edit did, including what it declined to do (#124).

    A list of rows used to be the whole answer. Setting a category over a
    selection now leaves its transfer legs alone -- a transfer has no category
    -- and a caller told nothing would believe it categorised every row it
    sent. So the count comes back beside the rows, and the register prints it.
    """

    transactions: list[TransactionOut]
    #: Transfer legs in the selection whose category was not set. Their other
    #: fields (cleared, memo, payee) were still applied.
    skipped_transfer_legs: int = 0
    #: Rows in the selection that could not take the work-expense flag --
    #: money coming in, or a transfer leg. Their other fields were applied.
    skipped_reimbursement: int = 0


class RegisterPage(BaseModel):
    transactions: list[TransactionOut]
    total: int
    #: True when a running balance was computable for this view.
    has_running_balance: bool = False
    #: True when `total` is larger than what came back. The register returns
    #: everything it matched, so this is only set for a household past the
    #: ceiling -- and when it is set the screen says so, because returning
    #: fewer rows without saying which is how somebody reads a fifth of their
    #: ledger and believes it is all of it.
    capped: bool = False


# --------------------------------------------------------------------------- #
# Payees and rules
# --------------------------------------------------------------------------- #


class PayeeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PayeeOut(ORMModel):
    id: str
    name: str
    transfer_account_id: str | None = None
    #: How many transactions carry it, and how many rules point at it. Issue
    #: #59's cheap half: a payee with one transaction and a name ending in
    #: twelve digits is visible the moment the list can be ordered by this,
    #: and invisible in an alphabetical list of several hundred.
    transaction_count: int = 0
    rule_count: int = 0


class PayeeCollision(BaseModel):
    """Payees whose names now fold to one key, offered for merging. Issue #268."""

    #: The folded form they share -- what made them one name.
    key: str
    payees: list[PayeeOut]


class PayeeRuleTrial(BaseModel):
    """A pattern to try against what is already in the ledger. Issue #61."""

    match_type: MatchType
    action: RuleAction = RuleAction.map
    pattern: str = Field(min_length=1, max_length=300)
    replacement: str | None = Field(default=None, max_length=300)


class PayeeRuleTrialOut(BaseModel):
    """What that pattern would claim."""

    matches: int
    considered: int
    #: For a map rule, the strings it claims. For a rewrite rule, what it turns
    #: them into -- `PAGO MOVIL BAR MARISOL -> Bar Marisol` -- because "it matches
    #: 217 rows" is not the question anybody has about a rewrite. Issue #58.
    examples: list[str]
    #: True when a regex pattern blew its 0.1s deadline. Worth finding out on
    #: the screen that wrote it rather than through an import going quiet.
    timed_out: bool = False


class PayeeSuggestion(BaseModel):
    """A set of bank strings that look like one shop. Issue #59."""

    pattern: str
    match_type: str
    strings: int
    transactions: int
    payees: int
    examples: list[str]


class ReapplyScope(BaseModel):
    """Which rows a re-apply should look at. Issue #60."""

    account_id: str | None = None
    since: Date | None = None
    until: Date | None = None
    #: Only rows that still carry the payee the bank gave them. The default,
    #: and the important half: re-running rules over a row somebody corrected
    #: by hand would undo their work, silently, in a bulk operation.
    only_untouched: bool = True


class ReapplyMove(BaseModel):
    transaction_id: str
    raw: str
    from_name: str | None = None
    #: None when the payee does not exist yet and applying the plan would
    #: create it -- which is what a rail rule does to every row behind it.
    to_payee_id: str | None = None
    to_name: str


class ReapplyPlanOut(BaseModel):
    """What re-applying the rules would do, before anything is written."""

    considered: int
    changing: int
    moves: list[ReapplyMove]
    #: Payees that would be left with nothing pointing at them. "This turns 28
    #: payees into 1" is the sentence that makes the work worth doing.
    orphaned: list[str]


class ReapplyResult(BaseModel):
    """What it did. One batch, so History has one entry and undo is one act."""

    batch_id: str
    moved: int
    #: Named only when `delete_orphans` was asked for.
    deleted_payees: list[str] = []


class PayeeMerge(BaseModel):
    into_payee_id: str


class PayeeRuleCreate(BaseModel):
    match_type: MatchType
    #: `map` unless said otherwise, so a client written before rewrite rules
    #: existed goes on sending exactly what it sent and means what it meant.
    action: RuleAction = RuleAction.map
    pattern: str = Field(min_length=1, max_length=300)
    payee_id: str | None = None
    payee_name: str | None = None
    #: What a rewrite puts in place of what it matched. Empty means "nothing",
    #: which is the strip case. A template for a `regex` rule, a literal
    #: otherwise. Ignored on a `map` rule, which is refused rather than
    #: silently accepted -- see `payees.create_rule`.
    replacement: str | None = Field(default=None, max_length=300)
    priority: int = Field(default=100, ge=0, le=100_000)


class PayeeRuleUpdate(BaseModel):
    """Turning a rule off without losing it.

    `payee_rules.enabled` was read by the loader and written by nothing: there
    was no route and no schema field that could set it false, so the column and
    the query that filtered on it were both decoration.
    """

    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100_000)


class PayeeRuleOut(ORMModel):
    id: str
    match_type: MatchType
    action: RuleAction
    pattern: str
    #: None on a rewrite rule, which has no payee to point at. Issue #58.
    payee_id: str | None = None
    replacement: str | None = None
    priority: int
    enabled: bool



# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


class ImportLineOut(ORMModel):
    id: str
    line_no: int
    #: The input row, re-serialised. Null on the agent path unless asked for
    #: with `include_raw=true`: the caller sent it, and handing 40 KB back
    #: double-escaped was the single most expensive thing in that API. The
    #: Import screen always gets it -- it is what the "as it arrived" row
    #: shows, and it came from a file the browser no longer has.
    raw: str | None = None
    parsed: dict | None = None
    outcome: ImportOutcome
    transaction_id: str | None = None
    reason: str | None = None
    #: Where this line will land. The payee's rule decides it unless somebody
    #: has said otherwise on the preview screen.
    category_id: str | None = None
    category_name: str | None = None
    #: True when a person picked it, false when it is the rule's guess. The
    #: screen shows which, because "likely" and "decided" are different
    #: promises and only one of them is worth checking.
    category_chosen: bool = False
    #: True when a person chose "no category" for this line (issue #9): it
    #: commits uncategorised, whatever the payee's rule or the bank's wording
    #: would have said. `category_chosen` is true with it, and `category_id`
    #: null -- which without this would read as a rule with nothing to go on.
    category_uncategorised: bool = False
    #: How many other lines in this import have the same payee and no category
    #: of their own. Sent after a change so the screen can offer to do the same
    #: to them rather than making somebody type it eleven more times.
    similar_lines: int = 0


class SetLineCategory(BaseModel):
    category_id: str | None = None
    #: Hand it back to the payee's rule.
    clear_category: bool = False
    #: No category, and do not ask the rule or the bank's wording either. The
    #: third answer, beside a category and "back to the rule" (issue #9) --
    #: the same distinction `clear_memo` and an empty `memo` make.
    uncategorised: bool = False

    @model_validator(mode="after")
    def _one_answer(self) -> SetLineCategory:
        # Each of the three is a different answer to the same question, so two
        # at once is a malformed request rather than one to guess the meaning of.
        if self.uncategorised and (self.category_id or self.clear_category):
            raise ValueError(
                "uncategorised means no category at all, so send it without "
                "category_id and without clear_category"
            )
        return self


class SetLineMemo(BaseModel):
    """A memo typed on the import preview, before anything is written."""

    memo: Memo | None = None
    #: Hand it back to whatever the bank sent, the same shape as `clear_memo`
    #: on a transaction. Sending an empty `memo` is the other request: keep the
    #: override and let this row have no memo at all.
    clear_memo: bool = False


class StagedImportOut(BaseModel):
    """One import staged and not yet committed, as the Import queue lists it."""

    batch_id: str
    filename: str | None = None
    account_id: str = ""
    account_name: str | None = None
    #: Who staged it, by name. `actor_name`, not `actor`, for the reason
    #: `BatchOut` gives.
    actor_name: str | None = None
    staged_at: datetime
    #: How many lines are waiting in it.
    row_count: int = 0
    sha256: str = ""


class ImportDecision(BaseModel):
    """The answer, without the working. Issue #40.

    A 193-row import came back larger than the request that produced it, and
    its main content was that request handed back double-escaped. What a
    caller actually needs is *how many landed* and *which ones need
    attention*; it already has the input.

    Only the agent path fills this in. The Import screen has the lines on
    screen and needs none of it.
    """

    #: Nothing was refused and nothing needs review. Not "this is correct" --
    #: read `warnings` for that, which is where a sign-convention or totals
    #: mismatch lands without setting this to false.
    safe_to_commit: bool
    created: int
    duplicates: int
    needs_review: int
    rejected: int
    #: Rows that would land with no category. The backlog this import adds.
    uncategorised: int
    #: Per currency, as a decimal STRING, because this ledger never converts
    #: and a grand total across currencies does not exist.
    totals: dict[str, str]


class ImportPreview(BaseModel):
    """What the file turned out to be, and what committing it would do."""

    batch_id: str
    filename: str | None = None
    account_id: str
    sha256: str
    detected: dict
    #: Anything the sniffer could not settle, in words -- and, on the agent
    #: path, everything `agent_warnings` noticed about the staged rows.
    warnings: list[str] = []
    counts: dict[str, int]
    #: The compact answer. Null on the file path, which has the lines already.
    decision: ImportDecision | None = None
    lines: list[ImportLineOut]


class ImportCommit(BaseModel):
    #: Lines the user unticked.
    skip_line_ids: IdList = []
    #: Proposed absorptions the user rejected; each becomes a new transaction.
    reject_match_line_ids: IdList = []


class ImportResult(BaseModel):
    batch_id: str
    created: int
    absorbed: int
    skipped: int
    lines: int
    #: Rows of this import linked as one leg of a transfer, to a leg already in
    #: another account (issue #70).
    transfers_linked: int = 0
    #: `external_id -> transaction_id` for the rows this commit created, when
    #: asked for with `include_ids=true`. Off by default because for a 193-row
    #: import it is 12 KB, and on by request because it is the cheapest answer
    #: to "now attach something to what I just imported" -- see `AgentLookup`
    #: for the standalone version. Issue #41.
    created_ids: dict[str, str] | None = None


class BatchOut(ORMModel):
    id: str
    kind: BatchKind
    status: BatchStatus
    actor_id: str
    started_at: datetime
    finished_at: datetime | None = None
    source: dict | None = None
    summary: dict | None = None
    undone_by_id: str | None = None
    #: Which key acted, when a program did. Null for everything a person did
    #: directly, which is most of History.
    agent_key_id: str | None = None
    #: The program's name, for the sentence. Survives the key being swept,
    #: because it is read from `source` rather than from the foreign key.
    via: str | None = None
    #: What kind of act it was, in words.
    headline: str = ""
    #: What it actually did. Computed from the change rows on every read, never
    #: stored -- a stored copy would be free to drift from the log it describes.
    detail: str = ""
    #: Who did it, by name rather than by id. Named `actor_name` and not
    #: `actor`: `Batch.actor` is the ORM relationship to the User, and a
    #: field of that name on a `from_attributes` model picks the object up
    #: and fails to coerce it to a string.
    actor_name: str | None = None
    #: How many rows it touched, so the undo confirmation can say.
    change_count: int = 0


class FieldChangeOut(BaseModel):
    field: str
    was: str
    now: str


class ChangeDetailOut(BaseModel):
    """One changed row, as far down as the log goes."""

    seq: int
    table: str
    row_id: str
    op: str
    summary: str
    #: What each column went from and to. Empty on an insert or a delete.
    fields: list[FieldChangeOut] = []
    #: The whole row, for an insert or a delete, where there is no "from".
    snapshot: list[FieldChangeOut] = []
    #: Columns deliberately kept out of the log. Named rather than starred
    #: over, because they are *absent* -- a "***" written back by an undo would
    #: be a password hash of three asterisks.
    redacted: list[str] = []


class BatchDetail(BatchOut):
    """One batch, spelled out. What the undo confirmation is built from."""

    #: One line per changed row, in the order they happened.
    lines: list[str] = []
    #: The same rows with their columns, for the panel that shows everything.
    #:
    #: Named `changed_rows`, not `changes`: `Batch.changes` is the ORM
    #: relationship, and a field of that name on a `from_attributes` model
    #: picks the `Change` objects up and fails to coerce them. Same trap as
    #: `actor` on `BatchOut`.
    changed_rows: list[ChangeDetailOut] = []


class TransactionOrigin(BaseModel):
    """Where an imported transaction came from, kept for later.

    A transaction outlives the statement it arrived on. Once the file is gone,
    this is the only account of what the bank actually wrote -- which column
    said what, what it called the payee before a rule renamed it, and the
    bank's own reference for finding it again at their end.
    """

    #: Which door it came in by. ``statement``: a statement file (or rows an
    #: agent staged), and it has a line. ``one_time_import``: another app's
    #: history brought in once (#183), which has no statement line to show --
    #: the panel names the workflow and the file or plan instead.
    kind: Literal["statement", "one_time_import"] = "statement"
    imported_at: datetime
    #: The statement file, as uploaded; for a one-time import, its export file.
    filename: str | None = None
    #: Null for a one-time import, which keeps no lines.
    line_no: int | None = None
    #: The line exactly as it appeared in the file. Null for a one-time import.
    raw: str | None = None
    #: The bank's own fields, under the bank's own names.
    bank: dict | None = None
    #: What this app made of it.
    payee_original: str | None = None
    import_id: str | None = None
    batch_id: str
    #: The program that staged it, when one did. An agent import has no file,
    #: so without this the panel says "Imported from a statement" about rows
    #: that never came from one. Read from `Batch.source["agent"]`, the copy
    #: that survives the key being swept.
    via: str | None = None
    #: One-time import only: the app it came from, as a person reads it
    #: ("YNAB"), how it was read (``csv`` or ``api``), and the plan's name
    #: when it came through the API rather than a file.
    workflow: str | None = None
    workflow_via: str | None = None
    plan_name: str | None = None


class ChangeOut(ORMModel):
    """One recorded change to one row.

    `Audit Log Decision.md` sells this as the headline: *"why is this EUR 45
    here?" -- walk `changes` for that `row_id`*. `Change.household_id` was
    denormalised for exactly this query, with a comment naming it, and
    `ix_changes_row_history` was built for it -- and nothing read either until
    now.
    """

    seq: int
    batch_id: str
    table_name: str
    row_id: str
    op: ChangeOp
    before: dict | None = None
    after: dict | None = None
    redacted: list | None = None
    at: datetime
    #: The batch this change belonged to, so one read answers "what happened,
    #: when, and as part of what".
    batch: BatchOut | None = None
    #: The same sentence the History screen shows, from the same engine. The
    #: raw images above are the record; this is the record read aloud.
    summary: str = ""
    #: Who did it, by name rather than by id.
    actor_name: str | None = None
    #: The program that did it on their behalf, when one did. Beside
    #: `actor_name`, never instead of it -- a key borrows a person's authority
    #: and does not replace them. Same field, same source and same reasoning as
    #: `BatchOut.via`; this is the per-row reader of it.
    via: str | None = None


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


class AdminUserOut(ORMModel):
    """A person, as the owner sees them on the admin screen."""

    id: str
    email: str
    display_name: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None = None
    #: How many ways back in they still have, if they lose their phone.
    recovery_codes_left: int = 0
    #: Which households they can reach.
    households: list[str] = []


class AdminHouseholdOut(ORMModel):
    id: str
    name: str
    base_currency: str
    date_format: DateFormat
    created_at: datetime
    member_ids: IdList = []


class AdminHouseholdCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="EUR", min_length=3, max_length=3)
    date_format: DateFormat = "YYYY-MM-DD"
    #: Who else goes in it. The creator is always a member.
    member_ids: IdList = []


class SetDisabled(BaseModel):
    disabled: bool


class SetRole(BaseModel):
    role: Role
    #: From `POST /me/step-up`. Required to make somebody an owner: an owner
    #: account outlives this session and every reset the promoting owner can
    #: perform on their own credentials (#205). Spent whether or not the change
    #: that follows succeeds.
    step_up_token: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- #
# Application management
# --------------------------------------------------------------------------- #
#
# The owner-only page that describes the instance rather than a ledger. One
# model per block on the screen, and every one of them a plain read -- nothing
# here validates an input except `SetLoggingStyle`.


class PlaceOut(BaseModel):
    what: str
    path: str
    exists: bool
    bytes: int | None = None
    note: str
    #: Whether being absent is a normal state for this path rather than a
    #: fault. The screen only flags the ones that ought to be there.
    optional: bool = False


class TableRowsOut(BaseModel):
    name: str
    rows: int
    bytes: int | None = None


class HouseholdDataOut(BaseModel):
    id: str
    name: str
    transactions: int
    receipts: int


class DatabaseEngineOut(BaseModel):
    """What the database is, beside how big it is.

    `path` comes from `platform.database_path()` and never from
    `settings.database_url`, which can carry a password. For anything that is
    not SQLite only the scheme is reported, for the same reason.
    """

    name: str
    version: str | None = None
    journal_mode: str | None = None
    path: str | None = None


class SizeOut(BaseModel):
    total_bytes: int
    main_bytes: int
    wal_bytes: int
    page_size: int | None = None
    page_count: int | None = None
    free_pages: int | None = None


class PackageOut(BaseModel):
    name: str
    version: str


class BackupOut(BaseModel):
    name: str
    path: str
    bytes: int
    made_at: datetime


class DownloadBackup(BaseModel):
    """`POST .../download`: the one way to a zip with `secret.key` in it."""

    include_key: bool = False
    #: From `POST /me/step-up`. Required when `include_key` is set: the zip then
    #: carries the key that opens every member's authenticator secret, which
    #: outlives this session by a long way. Spent whether or not the download
    #: that follows succeeds.
    step_up_token: str | None = Field(default=None, max_length=200)


class LogFileOut(BaseModel):
    name: str
    bytes: int
    modified: datetime


class LogTextOut(BaseModel):
    name: str
    bytes: int
    text: str


class LoggingStyleOut(BaseModel):
    key: str
    label: str
    blurb: str
    #: Whether this style writes the ledger's own values into the log. The
    #: screen warns on it rather than hiding it; see `app/logging_setup.py`.
    echo_sql: bool


class LogStreamOut(BaseModel):
    """One of the files the instance writes, and what is in it."""

    key: str
    label: str
    filename: str
    blurb: str
    #: True for `sql.log`, which at the `sql` style holds every statement and
    #: every row it returned -- transactions, payees and amounts in plain text.
    #: Sent so the screen can mark it rather than leaving somebody to find out
    #: after they have emailed it to someone.
    holds_ledger_values: bool = False


class LoggingOut(BaseModel):
    current: str
    styles: list[LoggingStyleOut]
    files: list[LogFileOut]
    directory: str
    #: The separated streams. Files rotate to `app-20260923-131545.log`, so the
    #: listing above holds more names than there are streams.
    streams: list[LogStreamOut] = []


class SetLoggingStyle(BaseModel):
    style: str = Field(min_length=1, max_length=20)


class UpstreamOut(BaseModel):
    checked_at: datetime
    running: str
    latest: str | None = None
    newer: bool = False
    problem: str | None = None


class BuildOut(BaseModel):
    """Which commit the process is running. `app/build.py` says where from."""

    commit: str | None = None
    branch: str | None = None
    committed_at: datetime | None = None
    dirty: bool | None = None
    source: str


class RecoveryModeOut(BaseModel):
    """Whether this server's key opens its members' authenticators (#287).

    Computed on every ask, never stored. Counts, not names: the banner says
    how many, and the admin screen already says who is who.
    """

    #: Members who can sign in -- not disabled -- with an authenticator enrolled.
    enrolled: int
    #: Of those, the ones the key cannot open: each will be asked for a
    #: recovery code. Recovery mode is on while this is above nought.
    locked: int
    #: The key came from `SPENDTRACKER_SECRET_KEY`, so the banner names the
    #: variable rather than `secret.key`, which is then never read.
    key_from_environment: bool


class InstanceOut(BaseModel):
    """Everything the Application management page opens with."""

    app_name: str
    version: str
    build: BuildOut
    environment: str
    python: str
    platform: str
    schema_revision: str | None = None
    started_at: datetime | None = None
    #: Which process answered. Owner-only, like everything else here.
    process_id: int
    database_url_scheme: str
    engine: DatabaseEngineOut
    size: SizeOut
    households: list[HouseholdDataOut]
    places: list[PlaceOut]
    packages: list[PackageOut]
    addresses: list[str]
    latest_backup: BackupOut | None = None
    logging_style: str
    logs: list[LogFileOut]
    repository: str
    author: str


class InviteCreate(BaseModel):
    role: Role = Role.member
    email: str | None = None
    #: Households they join the moment they accept.
    household_ids: IdList = []
    #: From `POST /me/step-up`. Required when `role` is owner: the account the
    #: link mints outlives this session and every reset the inviting owner can
    #: perform on their own credentials (#205). A member-role link does not
    #: need one -- a member is contained by household scoping and can neither
    #: mint keys nor invite.
    step_up_token: str | None = Field(default=None, max_length=200)


class InviteOut(ORMModel):
    id: str
    email: str | None = None
    role: Role
    invited_by_id: str
    household_ids: list | None = None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None


class InviteCreated(BaseModel):
    """The link is shown once and never again -- the server keeps only a hash.

    The raw token used to be sent beside the link as well. Nothing read it, and
    a one-time credential duplicated in a response body is one more copy to leak.
    """

    invitation: InviteOut
    link: str


class InviteState(BaseModel):
    """What the person clicking the link should be told before they commit."""

    role: Role
    email: str | None = None
    invited_by: str
    households: list[str] = []


class ResetState(BaseModel):
    """What the person following a reset link is told before they act (#284)."""

    email: str
    display_name: str
    #: Whether the link sets a new password.
    password: bool
    #: Whether the link enrols a new authenticator and issues recovery codes.
    authenticator: bool
    #: The owner who reset the account, by name. None: from the server.
    reset_by: str | None = None
    created_at: datetime


class ResetAuthenticator(BaseModel):
    """A new authenticator on offer. Nothing is stored until a code comes back."""

    #: Sealed: the secret, bound to this link, for fifteen minutes.
    blob: str
    otpauth_uri: str
    secret: str


class ResetComplete(BaseModel):
    #: Bounded like every other password field; the rule itself is
    #: `passwords.complaints`.
    password: str | None = Field(default=None, max_length=1024)
    #: `ResetAuthenticator.blob`, handed back.
    blob: str | None = None
    code: str | None = Field(default=None, max_length=10)


class ResetDone(BaseModel):
    """Shown once. Empty when only the password was reset."""

    recovery_codes: list[str]


class ResetIssue(BaseModel):
    """What an owner asks to reset (#286). At least one; the service says so."""

    password: bool = False
    authenticator: bool = False


class ResetIssued(BaseModel):
    """The link, shown once -- the server keeps only a hash, as for an invitation.

    The switches come back because they may be more than were asked for: a
    link that replaces a pending one keeps what that one reset, and an account
    with no authenticator always gets one enrolled.
    """

    link: str
    expires_at: datetime
    password: bool
    authenticator: bool


class PendingResetOut(BaseModel):
    """A reset link not yet followed, withdrawn or swept."""

    id: str
    user_id: str
    display_name: str
    email: str
    password: bool
    authenticator: bool
    #: The owner who issued it, by name. None: from the server.
    issued_by: str | None = None
    created_at: datetime
    expires_at: datetime
    #: Lapsed, and kept only so the screen can say so. The account it was for
    #: is still shut; the way back in is a new link.
    expired: bool


class SignInChangeOut(BaseModel):
    """One reset, new owner, or server act on an account's way in, read from
    the audit log. See `services/sign_in_changes.py`."""

    #: The change's position in the log. Newer is larger. One change can make
    #: two items (promoted and re-enabled in one write); they share it.
    id: int
    #: Unique per item.
    key: str
    what: Literal["reset", "owner_added", "promoted", "reenabled", "authenticator_replaced"]
    user_id: str
    user_name: str
    password: bool = False
    authenticator: bool = False
    #: None when it came from the server.
    by_id: str | None = None
    by_name: str | None = None
    from_server: bool
    at: datetime


class InviteBegin(BaseModel):
    token: str
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class PasswordChanged(BaseModel):
    #: How many other browsers were signed out, so the screen can say so.
    other_sessions_ended: int
    #: Trusted browsers that will ask for a code again -- this one included.
    devices_revoked: int = 0
    #: Agent keys that still work. A password change does not revoke them, so
    #: the screen says how many are out there rather than implying "everywhere".
    keys_still_live: int = 0


class AuthenticatorStatus(BaseModel):
    """The caller's own second factor, as the profile needs it (#287).

    Only ever the caller's: there is no id in the route, and nothing here a
    member does not already know about themselves.
    """

    #: An authenticator is set up. False after a reset cleared it (#284).
    enrolled: bool
    #: Enrolled, and this server's key cannot open it: recovery mode. The
    #: profile then asks for a recovery code in place of a code from it -- or
    #: takes the grant the recovery sign-in left in this browser.
    locked_by_key: bool


class StartReenrolment(BaseModel):
    current_password: str


class ReenrolmentOffer(BaseModel):
    """A new authenticator, offered and not yet stored."""

    token: str
    secret: str
    uri: str


class ConfirmReenrolment(BaseModel):
    token: str
    #: From the NEW authenticator: proves it pairs.
    code: str
    #: From the CURRENT authenticator, or an unused recovery code: proves the
    #: person replacing the second factor holds it. The password does not.
    current_code: str | None = None
    #: In place of `current_code`, in recovery mode only: the
    #: `reenrolment_grant` the recovery sign-in returned (#287).
    grant: str | None = None

    @model_validator(mode="after")
    def _one_proof(self) -> ConfirmReenrolment:
        # One or the other, and saying which is missing is a malformed
        # request rather than a refused proof.
        if (self.current_code is None) == (self.grant is None):
            raise ValueError("give either current_code or grant, not both and not neither")
        return self


class StepUpRequest(BaseModel):
    """Both factors, for an act the session alone does not buy."""

    password: str
    code: str


class StepUpGranted(BaseModel):
    """One act, and the moment it stops being worth anything.

    ``expires_at`` is returned so the screen can say how long is left rather
    than discovering the answer by being refused. It does not slide.
    """

    token: str
    expires_at: datetime


class RegenerateRecoveryCodes(BaseModel):
    """Both factors. A recovery code is not accepted for ``code`` (#289)."""

    password: str = Field(max_length=1024)
    code: str = Field(max_length=64)


class RecoveryCodesIssued(BaseModel):
    """The new codes, shown once. Only their hashes are stored."""

    codes: list[str]


class RecoveryCodesLeft(BaseModel):
    #: Unused recovery codes, so the profile screen can say how many are left.
    unused: int


class ReenrolmentDone(BaseModel):
    #: Trusted browsers revoked, so the screen can say they will all ask again.
    devices_revoked: int
    #: Other browsers signed out. This one is kept.
    other_sessions_ended: int = 0


# `HouseholdOut` names `HouseholdColours` before it is defined -- fine under
# postponed annotations, but pydantic needs telling once the name exists.
HouseholdOut.model_rebuild()


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


class ReconcileCandidate(BaseModel):
    """A row that has not been proved yet."""

    id: str
    date: Date
    payee: str | None = None
    memo: str | None = None
    amount: int
    cleared: ClearedState


class ReconcileWorksheet(BaseModel):
    account_id: str
    currency: str
    #: The sum of rows already locked. A new statement's closing balance is
    #: measured from here, so nothing already proved is proved twice.
    locked_balance: int
    last_statement_date: Date | None = None
    last_statement_balance: int | None = None
    candidates: list[ReconcileCandidate]


class ReconcileRequest(BaseModel):
    statement_date: Date
    #: What the bank says the account held on that date, in minor units.
    statement_balance: Minor
    transaction_ids: IdList


class ReconciliationOut(ORMModel):
    id: str
    account_id: str
    statement_date: Date
    statement_balance: int
    batch_id: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #


class CategoryOut(ORMModel):
    id: str
    group_id: str
    name: str
    #: "Quality of Life: Subscriptions" — how it reads outside the picker.
    full_name: str
    sort_order: int
    archived: bool
    #: How many transactions carry it. What makes "delete" and "archive"
    #: different choices rather than two words for the same button.
    used_by: int = 0


class CategoryGroupOut(ORMModel):
    id: str
    name: str
    sort_order: int
    categories: list[CategoryOut] = []


class CategoryGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CategoryGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CategoryCreate(BaseModel):
    group_id: str
    name: str = Field(min_length=1, max_length=120)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    group_id: str | None = None
    archived: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class SeedCategories(BaseModel):
    """Add the starter tree to a household that has none."""

    confirm: bool = True


class PayeeCategorisationOut(BaseModel):
    """How a payee decides the category for a new transaction.

    Not to be confused with `PayeeRuleOut` above: that is the text-matching rule
    that decides *which payee* a statement line belongs to. This one runs after
    it and decides what the transaction was for.
    """

    payee_id: str
    payee_name: str
    categorisation: Categorisation
    default_category_id: str | None = None
    #: What the payee would choose right now. For `history` this is derived, and
    #: it is sent so the screen can show it rather than re-deriving it.
    current_default_id: str | None = None
    current_default_name: str | None = None
    #: How many recent transactions the history rule looks at. A property of
    #: the rule, not of this payee, so the sentence describing it reads the same
    #: for a payee with no history as for one with years of it -- "the last 0
    #: transactions" is not a rule anybody can follow.
    history_window: int = 3
    #: How many this payee actually has to go on. Zero is why the screen says
    #: "nothing to go on yet" instead of naming a category.
    history_available: int = 0


class PayeeRuleFromLine(BaseModel):
    """What setting a rule from an import line did, in words for a toast."""

    payee_id: str
    payee_name: str
    category_name: str
    #: True when the payee did not exist yet and was made for this. Worth
    #: saying: it is a row in the ledger that was not there a moment ago.
    payee_created: bool = False


class SetPayeeCategorisation(BaseModel):
    categorisation: Categorisation
    category_id: str | None = None


class CountryOut(BaseModel):
    """One country, as the picker offers it."""

    code: str
    name: str
    flag: str


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #


class ReceiptOut(ORMModel):
    """One receipt, with everything the panel needs to render in one round trip."""

    id: str
    household_id: str
    transaction_id: str | None = None
    #: SHA-256 of the original upload. Shown in *More info* so a receipt can be
    #: checked against a copy somebody else holds. This is the dedupe key; the
    #: pointer into the blob store is a separate column the client never sees
    #: and cannot choose.
    content_sha256: str
    original_filename: str | None = None
    media_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    #: Pages, for a PDF. The frame shows page 1; this is what lets it say so
    #: rather than presenting one page as the whole document.
    page_count: int | None = None
    captured_at: datetime | None = None
    #: True when the camera gave a time and no offset, so `captured_at` is
    #: local wall-clock rather than UTC. The screen says "no zone recorded"
    #: rather than drawing a time it cannot justify.
    captured_at_is_local: bool = False
    gps_lat: float | None = None
    gps_lon: float | None = None
    #: Metres. A 4 m fix and a 2 km cell-tower fix are both "GPS" and the UI
    #: must not draw them the same way.
    gps_accuracy_m: float | None = None
    gps_bearing: float | None = None
    camera: str | None = None
    exif: dict | None = None
    client_encoded: bool = False
    note: str | None = None
    uploaded_by_id: str | None = None
    uploaded_by_name: str | None = None
    created_at: datetime

    #: Derived, never stored. A receipt that moves from the inbox onto a
    #: transaction renames itself with nothing rewritten anywhere.
    download_name: str = ""
    #: True when the uploaded bytes were kept -- always for a PDF, otherwise
    #: only under SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL.
    has_original: bool = False
    #: The size of the copy `download_name` actually serves, which is not
    #: `byte_size`: a 3.2 MB phone photo downloads as a 300 KB AVIF. Naming the
    #: file `.avif` and sizing it as the JPEG is the same quiet lie as storing
    #: the browser's Content-Type as fact.
    download_bytes: int = 0
    #: How many other rows in this household hold the same bytes. A split
    #: copies its receipt onto every part, and the panel says so rather than
    #: pretending detaching one detaches them all.
    also_on: int = 0


class ReceiptUpload(BaseModel):
    """The 201 from an upload, and the one thing worth saying about it."""

    receipt: ReceiptOut
    #: Set when these exact bytes are already attached somewhere else in this
    #: household. Not a refusal -- a bill can cover two rows -- but the other
    #: reading is that it went onto the wrong row a minute ago.
    warning: str | None = None


class ReceiptUpdate(BaseModel):
    """Attach, detach, re-attach, or write a note. One batch either way."""

    transaction_id: str | None = None
    #: Explicit, because `transaction_id: None` cannot mean both "leave it" and
    #: "send it to the inbox" -- the same shape as `clear_memo` on a transaction.
    detach: bool = False
    note: Note | None = None
    clear_note: bool = False


class ReceiptReplace(BaseModel):
    """Which receipt a newly uploaded one is replacing.

    "Replace" detaches the old one **to the inbox** and attaches the new one in
    one batch, so one undo puts both back. It deliberately does not delete:
    destructive-by-default on a one-click button in a panel people open all day
    is how the refund that funded the wrong card happened.
    """

    replaces_id: str


class ReceiptBulkDelete(BaseModel):
    """A selection of receipts deleted as one act, so one undo takes it back.

    Same shape and same reason as `BulkEdit`: the alternative is the client
    calling `DELETE /receipts/{id}` in a loop, which is one batch each and one
    undo each, in the right order, for a single gesture.
    """

    receipt_ids: IdList = Field(min_length=1)


# --------------------------------------------------------------------------- #
# The agent API
# --------------------------------------------------------------------------- #


class ManifestHousehold(BaseModel):
    id: str
    name: str
    #: Reporting roll-ups only. Real currency lives on each account, and no
    #: endpoint here ever converts between them.
    base_currency: str
    date_format: str


class ManifestKey(BaseModel):
    label: str
    agent_name: str | None = None
    #: A list, although one value is stored: `write` implies `read`, and that
    #: implication is ours to apply rather than the agent's to remember.
    scopes: list[str]
    may_commit: bool
    expires_at: datetime


class ManifestAccount(BaseModel):
    id: str
    name: str
    type: str
    #: Which way money is expected to move here, in words. Absent until #46:
    #: the manifest said an account was `checking` and left the caller to work
    #: out what that implied for a sign, which is how EUR 23.4k of credit-card
    #: repayments landed in one as income.
    sign_hint: str
    currency: str
    #: How many decimal places this currency has, so an agent never has to
    #: guess an exponent for a currency it has not met. EUR 2, JPY 0.
    minor_exponent: int
    closed: bool
    #: Where it is held and with whom, so a combined position can be read by
    #: country (#134). ISO 3166-1 alpha-2, or null when nobody said.
    country: str | None = None
    institution: str | None = None
    #: Money owed rather than held -- a card, a loan. Its balance is what the
    #: household owes, which is not an asset of the same size with its sign lost.
    is_liability: bool = False
    #: What a person wrote about the account when its name does not say --
    #: "joint, for the rent". In full, up to `Note`'s 2000 characters, and
    #: text a person wrote: data to read, never an instruction (#21).
    note: str | None = None
    #: Read off the opening-balance row, as `AccountOut`'s are (#10), with
    #: one difference: an account opened empty has no row, and both are null
    #: here rather than 0 and null, so "nobody said" is not read as "it
    #: started at zero". Minor units of `currency`.
    opening_balance: int | None = None
    opening_date: Date | None = None


class ManifestCategory(BaseModel):
    id: str
    #: "Food: Groceries" -- the form a model should both read and write.
    full_name: str
    archived: bool


class ManifestAuth(BaseModel):
    """How to make any call at all.

    Absent until issue #37: the manifest described the household, the key's own
    scopes, every account, all the categories, the conventions and the rate
    limit -- and not the one thing required before any of it. The `key` block
    made auth read as already settled, which is exactly what delays finding the
    real answer.
    """

    header: str
    scheme: str
    #: A whole header line, because that is what the caller has to produce.
    example: str


class ManifestEndpoint(BaseModel):
    method: str
    path: str
    #: One sentence. What it is for, not how it works.
    says: str
    #: The top-level shape of a success, e.g. `{server_seq, has_more, changed[]}`.
    #: Issue #42: the manifest described the *protocol* for reading transactions
    #: and not the *payload*, so a caller that guessed `transactions` or `rows`
    #: got nothing back and read it as "no data" rather than "wrong key" -- a
    #: successful-looking empty query, which is the expensive kind of wrong.
    #:
    #: `test_agent_manifest.py` asserts these against each route's declared
    #: `response_model`, so this cannot drift from what is actually returned.
    returns: str
    #: Which scope it needs, so an agent can tell before it is refused.
    scope: str


class Manifest(BaseModel):
    """Everything an agent needs before its first useful request.

    One call in place of a dozen exploratory ones -- and the place the three
    rules a model most reliably breaks are stated where it will actually read
    them: float money, cross-currency sums, and the sign convention.
    """

    api_version: str
    app_version: str
    #: How to authenticate. First, because it is the first thing needed and an
    #: agent reading top to bottom should not reach the accounts before it.
    auth: ManifestAuth
    #: Where the full schema is, for a caller that wants more than one sentence
    #: per endpoint. Key-gated, which a caller reading this already satisfies.
    openapi: str
    #: The anonymous discovery document, so the manifest can be handed on.
    discovery: str
    household: ManifestHousehold
    key: ManifestKey
    accounts: list[ManifestAccount]
    categories: list[ManifestCategory]
    conventions: dict
    endpoints: list[ManifestEndpoint]


class GroupOut(BaseModel):
    """One line of an answer. Minor units and a string, always both."""

    key: str | None = None
    name: str
    sum_minor: int
    #: The same figure as text, for the sentence the agent writes. Without it a
    #: model formats the integer itself, in a locale it guessed.
    sum: str
    count: int


class CurrencyTotalsOut(BaseModel):
    total_minor: int
    total: str
    count: int
    groups: list[GroupOut] = []


class SummaryOut(BaseModel):
    """Totals, partitioned by currency, with no grand total anywhere.

    The absence is the design. This ledger never converts, so a figure spanning
    two currencies does not exist -- and a null or a zero would be a field an
    agent could mistake for one.
    """

    since: Date | None = None
    until: Date | None = None
    group_by: str
    by_currency: dict[str, CurrencyTotalsOut]


class BalanceOut(BaseModel):
    account_id: str
    name: str
    currency: str
    balance_minor: int
    balance: str
    #: The same three facts the manifest's accounts carry (#134).
    country: str | None = None
    institution: str | None = None
    is_liability: bool = False
    #: The manifest's note again, in full, so this stays one complete line
    #: per account (#21). The opening balance is not repeated: it is a fact
    #: about where the account started, not about what it holds, and the
    #: manifest carries it.
    note: str | None = None


class BalancesOut(BaseModel):
    as_of: Date | None = None
    #: Per account. There is no household total, for the same reason there is
    #: no grand total above.
    accounts: list[BalanceOut]


class DeltaOut(BaseModel):
    """What moved since a given point in the log.

    `server_seq` is `changes.seq`, which is already this app's authoritative
    commit order -- the same thing YNAB calls `server_knowledge`. Send it back
    next time and receive only what changed.

    It is the high-water mark of what this response actually carried, never the
    household's global maximum: a page cut short by `limit` reports the last
    row it handed over, so following the cursor cannot skip the remainder.
    Keep reading while `has_more` is true.
    """

    server_seq: int
    #: Whether the window held more than `limit` allowed back. `server_seq` is
    #: the last row actually returned when this is true, so paging until it is
    #: false converges -- and a client that ignores it still loses nothing,
    #: because the cursor never steps over a row it was not handed.
    has_more: bool = False
    changed: list[TransactionOut] = []
    #: Hard deletes make this simpler, not harder: `changes` with `op = delete`
    #: IS the tombstone stream, so there is no flag anybody can forget to filter
    #: on.
    deleted: list[str] = []


class AgentKeyOut(ORMModel):
    """A key as its owner sees it. Never the token -- that is shown once, at
    issue, and is not stored in a form anything could show again."""

    id: str
    label: str
    agent_name: str | None = None
    household_id: str
    #: The list, computed from the one stored value. `write` implies `read`.
    scopes: list[str]
    may_commit: bool
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    #: Computed, not stored: "is this key any good right now" is a question
    #: about three columns and a clock, and every screen would otherwise
    #: reimplement it.
    live: bool


class IssueAgentKey(BaseModel):
    """What the person chose, plus the grant that proves it is still them."""

    #: From `POST /me/step-up`. Single-use, and spent whether or not this
    #: succeeds.
    step_up_token: str
    label: str
    agent_name: str | None = None
    household_id: str
    scope: AgentScope = AgentScope.read
    may_commit: bool = False
    days: int = 90


class AgentKeyIssued(BaseModel):
    """The one and only time the token exists outside the holder's hands."""

    key: AgentKeyOut
    #: Shown once, in full. There is no route that will ever return it again --
    #: only its SHA-256 is stored, so there is nothing to return.
    token: str


class AgentImportRow(BaseModel):
    """One transaction an agent is asking to stage.

    ``extra="forbid"`` on purpose: a model that invents a plausible field name
    should be told, not silently ignored. A dropped `amount_cents` is a row
    with no money in it.
    """

    model_config = ConfigDict(extra="forbid")

    date: Date
    #: Exactly one of these two. Both are STRICT, which is the whole point --
    #: `StrictInt` refuses `12.5` *and* `12.0`, and `StrictStr` refuses a bare
    #: `12.50`. A JSON float never reaches `money.to_minor` from here.
    #:
    #: `money.to_minor` itself accepts floats, deliberately, because a CSV cell
    #: has no type and refusing there would reject real statements. This edge
    #: is different: the caller chose the encoding, so the rule can be strict,
    #: and a model that writes `12.50` unquoted is told why rather than being
    #: silently rounded.
    amount_minor: StrictInt | None = None
    #: A decimal string, converted with the ACCOUNT's currency -- so an agent
    #: that naturally writes "12.50" is right, and never has to guess an
    #: exponent for a currency it has not met.
    amount: StrictStr | None = None
    #: The currency the source says this amount is in, as an ISO code. Optional:
    #: left out, the row is taken to be in the account's currency, as it always
    #: was. Given, it is held to the account's exactly as a statement file's
    #: currency column is (#86) -- a row in another currency is refused, never
    #: recorded as the same figure in the wrong money.
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    payee: str | None = Field(default=None, max_length=200)
    memo: Memo | None = None
    #: The source system's own id. Preferred over the derived key for dedupe,
    #: exactly as a bank's FITID is, and for the same reason: whoever issued it
    #: promises it is stable.
    external_id: str | None = Field(default=None, max_length=200)
    #: Whatever else the source said. Lands in `import_lines.parsed["bank"]`.
    details: dict | None = None
    #: What this is for. Issue #45: the manifest hands an agent all twenty
    #: categories with their ids and `ImportLineOut` carries a category back,
    #: so the server plainly had a notion of categorisation the caller could
    #: not participate in -- and 396 transactions landed uncategorised with no
    #: bulk endpoint to fix them.
    #:
    #: Treated exactly as a category chosen on the preview screen is: it wins
    #: over the payee rule, because a rule is a guess and this is a caller
    #: having looked. Refused if it is not a real category for this household.
    category_id: str | None = None
    #: The row has no category, and the payee rule and the bank's wording are
    #: not to be asked either. Leaving `category_id` out is a different
    #: request -- it hands the row to the rule, which may well categorise it.
    #: Issue #9. Not together with `category_id`.
    uncategorised: bool = False
    #: How sure you are, 0 to 1, and why. Neither is used to decide anything --
    #: they are kept so a person reviewing the preview can see which rows were
    #: a confident match and which were the agent's best guess.
    category_confidence: float | None = Field(default=None, ge=0, le=1)
    category_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def exactly_one_amount(self) -> AgentImportRow:
        given = [v for v in (self.amount_minor, self.amount) if v is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of amount_minor (integer minor units, e.g. -1250) "
                "or amount (a decimal STRING, e.g. \"-12.50\"). A JSON float is not "
                "accepted for money."
            )
        return self

    @model_validator(mode="after")
    def category_or_uncategorised(self) -> AgentImportRow:
        if self.uncategorised and self.category_id:
            raise ValueError(
                "give category_id or uncategorised: true, not both. uncategorised "
                "means the row lands with no category at all."
            )
        return self


class AgentStatement(BaseModel):
    """What the agent believes it read, for staging to check. Issue #50.

    When an agent parses a statement itself -- which is deliberate; see the
    provenance issue for why the parsing stays with the agent -- the app has no
    way to check its arithmetic. It receives rows and believes them. A page it
    skipped, a row it merged, a sign it flipped: none of it is detectable at
    stage time.

    So let the agent declare what it believes and have the server check it.
    Every field optional, every mismatch a warning rather than a refusal; an
    agent that declares nothing behaves exactly as it did before.

    No `account_suffix` yet, although it was asked for: accounts have nowhere
    to record one, and a declaration checked against nothing is a field that
    reads like a promise.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: Date | None = None
    period_end: Date | None = None
    row_count: int | None = Field(default=None, ge=0)
    #: Integer minor units, signed, like every other figure here. The sum the
    #: agent expects the rows it is sending to come to.
    total_minor: StrictInt | None = None


class AgentImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    #: Where these came from, in the agent's own words. Kept in `Batch.source`
    #: so History can say it a year later.
    source: str = Field(min_length=1, max_length=80)
    #: Capped at `IdList`'s ceiling. More than this is several imports, which
    #: is correct: they are several operations and should be several undos.
    rows: list[AgentImportRow] = Field(min_length=1, max_length=1000)
    #: What you believe you are sending. Checked at stage time and reported in
    #: `warnings`; nothing here is ever a refusal.
    statement: AgentStatement | None = None


class AgentCategoryAssignment(BaseModel):
    """One row, and what it is for."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    #: Null empties the category, which is a different act from leaving it
    #: alone -- there is no way to spell "leave it alone" here, because every
    #: assignment in the list is one you asked for.
    category_id: str | None = None


class AgentCategorise(BaseModel):
    """Re-categorise many rows as one act, so one undo puts them all back.

    The second half of #45. Categorising at import time covers new rows; this
    covers the ones already in the ledger, which on the run that raised it was
    396 of them with no route but the UI.

    A list of pairs rather than one category for a selection: an agent's whole
    advantage here is telling FALAFEL CORNER from Mercadona, and a bulk edit
    that applies one category to everything cannot express that.
    """

    model_config = ConfigDict(extra="forbid")

    assignments: list[AgentCategoryAssignment] = Field(min_length=1, max_length=1000)


class AgentCategorised(BaseModel):
    """What the re-categorisation did, without listing what did not change."""

    #: The batch, so History and undo can be found.
    batch_id: str
    changed: int
    #: Already carrying that category, so nothing was written for them.
    unchanged: int
    #: Ids that are not rows in this household. Named, because a silent drop is
    #: how a caller believes it categorised more than it did.
    not_found: list[str] = []
    #: Transfer legs that were asked for a category and not given one: a
    #: transfer is not spending, so it has no category (#124). Named for the
    #: same reason as `not_found`. Emptying a leg's category is still done.
    transfer_legs: list[str] = []
    #: Reconciled rows, left as they were: locked, as the register treats them.
    #: Set back to cleared by a person first (#215).
    locked: list[str] = []


class AgentMemoAssignment(BaseModel):
    """One row, and what its memo should say."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    #: Required, and null or blank empties it -- the same reasoning as
    #: `AgentCategoryAssignment.category_id`: every assignment is one you asked
    #: for, so there is no spelling of "leave it alone". The memo is replaced,
    #: not appended to; a caller that wants to keep the bank's words reads the
    #: row first and sends them back as part of the new text.
    memo: Memo | None


class AgentMemos(BaseModel):
    """Write a memo per row, as one act, so one undo puts them all back.

    Categorising was the only edit a key could make to a row already in the
    ledger, so what an agent read off a ticket or an invoice -- the flight, the
    booking code, who travelled -- could go on a receipt's note but not on the
    row a person reads in the register.
    """

    model_config = ConfigDict(extra="forbid")

    assignments: list[AgentMemoAssignment] = Field(min_length=1, max_length=1000)


class AgentMemoed(BaseModel):
    """What the memo edit did, without listing what did not change."""

    batch_id: str
    changed: int
    #: Already saying exactly that, so nothing was written for them.
    unchanged: int
    #: Ids that are not rows in this household, named for the same reason as
    #: on `AgentCategorised`.
    not_found: list[str] = []
    #: Reconciled rows, left as they were. The register refuses to edit a
    #: locked row's memo, and a key gets no wider hand than its person (#215).
    locked: list[str] = []


class AgentSplitPart(BaseModel):
    """One part of a row being divided, as an agent worked it out."""

    model_config = ConfigDict(extra="forbid")

    #: Exactly one of the two, strict for the reason `AgentImportRow` gives: a
    #: JSON float never reaches money. A decimal string is read in the ROW's
    #: account currency and refused if it has more decimals than that has.
    amount_minor: StrictInt | None = None
    amount: StrictStr | None = None
    category_id: str | None = None
    #: Null takes the original row's memo, as the register's split does.
    memo: Memo | None = None
    #: Every part of a work expense stays one (`transactions.split`). "clear"
    #: takes the flag off this part in the same act -- the personal share of a
    #: partial claim -- and is refused on a row already paid back, because
    #: that would take a repayment link apart, which is a person's.
    reimbursement: Literal["keep", "clear"] = "keep"

    @model_validator(mode="after")
    def exactly_one_amount(self) -> AgentSplitPart:
        if (self.amount_minor is None) == (self.amount is None):
            raise ValueError(
                "give exactly one of amount_minor (integer minor units, e.g. -1250) "
                "or amount (a decimal STRING, e.g. \"-12.50\"). A JSON float is not "
                "accepted for money."
            )
        return self


class AgentSplit(BaseModel):
    """One row, and the 2-5 parts that add up to it."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    parts: list[AgentSplitPart] = Field(min_length=2, max_length=5)


class AgentSplits(BaseModel):
    """Divide many rows as one act, so one undo puts every original back. #7.

    A partial work claim -- a shared booking, a share of a bill -- is recorded
    by splitting the row and keeping the work flag on one part. Until this,
    an agent that had worked the shares out could only hand a person a table
    to retype into the split dialog.
    """

    model_config = ConfigDict(extra="forbid")

    splits: list[AgentSplit] = Field(min_length=1, max_length=100)


class AgentSplitDone(BaseModel):
    #: The row that was divided. It no longer exists; its before-image is in
    #: the batch, which is what undo puts back.
    transaction_id: str
    #: The new rows, in the order the parts were sent.
    parts: list[str]


class AgentSplitRefused(BaseModel):
    transaction_id: str
    #: The sentence the register would have shown a person.
    reason: str


class AgentSplitResult(BaseModel):
    """Per-row answers: what was split, what was refused and why."""

    batch_id: str
    split: list[AgentSplitDone] = []
    refused: list[AgentSplitRefused] = []
    #: Ids that are not rows in this household, named, never dropped.
    not_found: list[str] = []


class AgentLookup(BaseModel):
    """Which transactions carry these source ids? Issue #41.

    To attach six receipts the agent needed six transaction ids, and the only
    route was the register: **520 rows read to find 6**. The `import_id` was on
    every one of them and there was no way to ask.

    Exactly one of the two. A prefix is for "everything from that import
    source"; the list is for the common case, where you know what you are
    looking for because you sent it.
    """

    model_config = ConfigDict(extra="forbid")

    external_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    external_id_prefix: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def exactly_one(self) -> AgentLookup:
        given = [v for v in (self.external_ids, self.external_id_prefix) if v is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of external_ids (a list) or external_id_prefix (a string)."
            )
        return self


class AgentLookupResult(BaseModel):
    """`external_id -> transaction_id`, and what was not there."""

    found: dict[str, str]
    #: Only for a lookup by list: the ids that matched nothing. A prefix that
    #: matches nothing simply returns an empty `found`.
    missing: list[str] = []


class AgentReceiptUpload(BaseModel):
    """A receipt an agent is posting, as JSON rather than multipart.

    Base64 is not laziness. **An MCP server over stdio cannot stream a file**,
    and refusing base64 would make the most likely integration impossible.
    """

    model_config = ConfigDict(extra="forbid")

    filename: str | None = Field(default=None, max_length=300)
    #: The bytes. Capped lower than the multipart route's 25 MB because base64
    #: costs a third again in transit and in memory, and an agent has no reason
    #: to send a 25 MB original.
    content_base64: str = Field(min_length=1)
    transaction_id: str | None = None
    note: Memo | None = None
    #: What the agent read off the image. Stored as a CLAIM: nothing in the
    #: ledger is derived from it and `/candidates` does not match on it.
    extracted: dict | None = None


class AgentReceiptOut(BaseModel):
    """A receipt as an agent sees it. Never the bytes.

    An agent uploads evidence and reads metadata; handing a key the ability to
    pull every stored image back out buys nothing any archetype needs, so the
    bytes route stays cookie-only.
    """

    id: str
    transaction_id: str | None = None
    content_sha256: str
    media_type: str
    byte_size: int
    page_count: int | None = None
    captured_at: datetime | None = None
    #: Whether the camera's own timestamp was naive local time. It decides how
    #: a date is read, and reading it wrong moves a receipt across midnight.
    captured_at_is_local: bool = False
    extracted: dict | None = None
    created_at: datetime
    #: Said plainly, because it is the one thing a caller most often wants to
    #: know next and should not have to infer from a null.
    needs_a_transaction: bool


class AgentReceiptStored(BaseModel):
    receipt: AgentReceiptOut
    #: True when these exact bytes were already on this transaction, in which
    #: case nothing new was stored and `receipt` is the one that was there.
    already_had_it: bool = False
    #: Said in words when it happened, because "nothing changed" is a result an
    #: agent should report rather than retry.
    note: str | None = None
    #: The batch it was stored in, so History and undo can be found. Null when
    #: nothing was stored because the bytes were already here.
    batch_id: str | None = None


class AgentReceiptBatch(BaseModel):
    """A trip's worth of receipts, in one call. Issue #48.

    Receipts went in one per call, so seven meant seven round trips -- and
    they arrive a trip at a time, not one at a time. One Idempotency-Key
    covers the whole array, which is the point: a retry after a timeout
    re-sends the same list and gets the same answer rather than storing some of
    them twice.
    """

    model_config = ConfigDict(extra="forbid")

    receipts: list[AgentReceiptUpload] = Field(min_length=1, max_length=25)


class AgentReceiptsStored(BaseModel):
    """What the array form did, one entry per submitted receipt, in order."""

    stored: list[AgentReceiptStored]
    #: How many were new. The rest were bytes already here, which is a result
    #: and not a failure -- each entry says which it was.
    created: int


class AgentLinkReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str
    #: A receipt already on another transaction is refused (409) unless this
    #: is true. Moving evidence off a row is a decision, not a side effect.
    move: bool = False


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


class ReportRowOut(BaseModel):
    """One category's line across the window.

    `by_month` holds only the months this row has anything in. The client
    reads it against the response's `months` list, so an absent key is a zero
    and does not travel as one -- on a twelve-month report over a long category
    list that is most of the payload.
    """

    key: str | None = None
    name: str
    group_name: str | None = None
    by_month: dict[str, int] = {}
    total_minor: int
    total: str
    average_minor: int
    average: str
    count: int


class ReportSectionOut(BaseModel):
    rows: list[ReportRowOut] = []
    by_month: dict[str, int] = {}
    total_minor: int
    total: str
    average_minor: int
    average: str
    count: int


class ExcludedOut(BaseModel):
    """What the flow predicate removed, so the screen can say so out loud.

    A report that quietly drops a fifth of the register is a report the reader
    cannot check. Counting transfers as spend overstated outflow by 41.5% on
    the real ledger, and these numbers are how anybody would have noticed.

    `reimbursements` counts work expenses flagged `expected` and the payments
    that repaid them -- both out of flow since 2026-09-25, because money that
    comes back is not spending and money owed is a receivable, not a cost.
    """

    transfers: int = 0
    opening_balances: int = 0
    reimbursements: int = 0


class CoverageOut(BaseModel):
    """How much of the window actually has data.

    Four of twelve months in the real ledger are empty, and they are months
    nobody imported rather than months of frugality. The screen prints this
    wherever it is not complete, because the alternative is a reader who
    concludes they earned nothing in January.
    """

    months_in_range: int
    months_with_activity: int


class IncomeExpenseOut(BaseModel):
    """Income against expense for **one** currency, by month.

    There is no variant of this spanning two currencies and no grand total
    across them. The ledger does not convert, so that figure does not exist --
    and a null or a zero would be a field a reader could mistake for one.
    `currency` is therefore required rather than optional: an answer that did
    not say which currency it was about would be the same bug wearing a hat.
    """

    currency: str
    since: Date
    until: Date
    #: Every `YYYY-MM` in the window, including the empty ones. The calendar
    #: names the columns, not the data.
    months: list[str]
    income: ReportSectionOut
    expense: ReportSectionOut
    net_by_month: dict[str, int]
    net_total_minor: int
    net_total: str
    excluded: ExcludedOut
    coverage: CoverageOut


class ReportCurrenciesOut(BaseModel):
    """Which currencies this household holds accounts in, busiest first."""

    currencies: list[str]


class ReportEntryOut(BaseModel):
    """One transaction behind a figure, as the bubble shows it."""

    id: str
    date: Date
    account_name: str
    payee_name: str | None = None
    category_name: str | None = None
    memo: str | None = None
    amount_minor: int
    amount: str


class ReportBehindOut(BaseModel):
    """What adds up to one figure on the report.

    `total_minor` is counted over the whole match rather than over the rows
    returned, so a capped bubble still reconciles against the cell it came
    from. `capped` says when that has happened; the alternative is a shorter
    list that silently disagrees with the number above it.
    """

    entries: list[ReportEntryOut] = []
    total_minor: int
    total: str
    count: int
    capped: bool = False


class ReimbursementOutstandingOut(BaseModel):
    """One work expense still waiting for its money. `amount` is positive."""

    id: str
    date: Date
    payee_name: str | None = None
    account_id: str
    account_name: str
    amount: int
    has_receipt: bool
    memo: str | None = None
    category_name: str | None = None


class ReimbursementSettlementOut(BaseModel):
    """The payment that came back, in its own account's currency."""

    id: str
    date: Date
    amount: int
    currency: str
    account_id: str
    account_name: str
    payee_name: str | None = None
    memo: str | None = None
    category_name: str | None = None


class ReimbursementExpenseOut(BaseModel):
    """One expense a payment repaid. `amount` is positive, in `currency`."""

    id: str
    date: Date
    payee_name: str | None = None
    account_id: str
    account_name: str
    currency: str
    amount: int
    memo: str | None = None
    category_name: str | None = None


class ReimbursementClaimOut(BaseModel):
    """A payment and the expenses pointing at it: the claim, discovered.

    `covered` and `difference` are **null, not zero**, when `currencies` holds
    more than one code. There is no rate in this ledger, and a zero would say
    a GBP hotel repaid in EUR balanced.
    """

    settlement: ReimbursementSettlementOut
    expenses: list[ReimbursementExpenseOut] = []
    covered: int | None = None
    difference: int | None = None
    currencies: list[str]


class ReimbursementMonthOut(BaseModel):
    """One expense month (`YYYY-MM`)."""

    month: str
    flagged: int
    recovered: int
    written_off: int
    outstanding: int


class ReimbursementsOut(BaseModel):
    """The Reimbursements report, for **one** currency.

    Every figure is a positive count of minor units in `currency`. `since` and
    `until` narrowed the *expense* date and come back as sent, null for open.
    `available_currencies` is what the toggle offers, busiest first, and is
    empty when nothing is flagged.
    """

    currency: str
    since: Date | None = None
    until: Date | None = None
    available_currencies: list[str]
    outstanding: int
    outstanding_count: int
    oldest_outstanding: Date | None = None
    recovered: int
    recovered_count: int
    written_off: int
    written_off_count: int
    unmatched: int
    outstanding_rows: list[ReimbursementOutstandingOut] = []
    claims: list[ReimbursementClaimOut] = []
    months: list[ReimbursementMonthOut] = []


# --------------------------------------------------------------------------- #
# One-time Import (#183)
# --------------------------------------------------------------------------- #


class YnabPlansIn(BaseModel):
    #: Held for this request only: never stored, logged or echoed. No
    #: constraint here: FastAPI's 422 would echo the value that failed it. The
    #: router's `_token()` checks the length and says so in its own words (#224).
    token: str = Field(repr=False)


class YnabPlanOut(BaseModel):
    id: str
    name: str | None = None
    currency: str | None = None
    last_modified_on: str | None = None
    first_month: str | None = None
    last_month: str | None = None


class YnabPlansOut(BaseModel):
    plans: list[YnabPlanOut]


class OneTimeAccountChoice(BaseModel):
    kind: Literal["existing", "create", "skip"]
    account_id: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=120)
    type: AccountType | None = None


class OneTimeCategoryChoice(BaseModel):
    kind: Literal["existing", "create", "uncategorised"]
    category_id: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=120)
    group_name: str | None = Field(default=None, max_length=120)


class OneTimeDuplicates(BaseModel):
    all: Literal["skip", "import"] | None = None
    #: row_refs to import although they look like a row already there.
    import_: list[str] = Field(default_factory=list, alias="import", max_length=100_000)

    model_config = ConfigDict(populate_by_name=True)


class OneTimeImportPlan(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    date_format: str = Field(max_length=32)
    accounts: dict[str, OneTimeAccountChoice]
    categories: dict[str, OneTimeCategoryChoice] = Field(default_factory=dict)
    flags: Literal["memo", "ignore"] = "memo"
    starting_balance: Literal["import", "skip"] = "import"
    date_from: Date | None = None
    date_to: Date | None = None
    acknowledge_cleared_reset: bool = False
    duplicates: OneTimeDuplicates = Field(default_factory=OneTimeDuplicates)


class OneTimeCounts(BaseModel):
    rows_in_file: int
    imported: int
    transfers_linked: int
    duplicates_skipped: int
    duplicates_imported: int
    skipped_account: int
    skipped_date_range: int
    skipped_starting_balance: int
    failed: int


class OneTimeCreatedAccount(BaseModel):
    id: str
    name: str
    opening_date: Date


class OneTimeCreatedCategory(BaseModel):
    id: str
    name: str


class OneTimeCreated(BaseModel):
    accounts: list[OneTimeCreatedAccount]
    categories: list[OneTimeCreatedCategory]
    payees: int


class OneTimeExisting(BaseModel):
    id: str
    date: Date
    payee: str | None = None
    amount_minor: int


class OneTimeDuplicate(BaseModel):
    row_ref: str
    date: Date | None = None
    account: str
    payee: str
    amount_minor: int | None = None
    memo: str
    existing: OneTimeExisting
    decision: Literal["skip", "import", "pending"]


class OneTimeRowNote(BaseModel):
    row_ref: str
    date: Date | None = None
    #: The date as the source wrote it, for a row whose date could not be read.
    date_text: str = ""
    account: str
    payee: str
    category: str
    memo: str
    amount_minor: int | None = None
    reason: str


class OneTimeBalanceDifference(BaseModel):
    """An account whose imported rows do not add up to YNAB's balance (#266)."""

    #: The YNAB account's id: two YNAB accounts may share a name.
    account_key: str
    account: str
    currency: str
    #: YNAB's ``balance``, in minor units of ``currency``.
    ynab_balance_minor: int
    #: What this import wrote, plus what it left out on purpose.
    imported_minor: int
    #: ``imported_minor - ynab_balance_minor``: negative when rows are missing.
    difference_minor: int
    sentence: str


class OneTimeBalanceUnchecked(BaseModel):
    """An account whose YNAB balance could not be compared at all (#266)."""

    account_key: str
    account: str
    reason: str
    sentence: str


class OneTimeImportReport(BaseModel):
    committed: bool
    batch_id: str | None = None
    counts: OneTimeCounts
    created: OneTimeCreated
    duplicates: list[OneTimeDuplicate]
    not_imported: list[OneTimeRowNote]
    #: Transfers whose other side was not imported, so they arrived as plain rows.
    unpaired_transfers: list[OneTimeRowNote] = []
    #: Groups of bank strings that look like one payee each -- the Rules
    #: screen's suggestions, the whole household's, counted once the import
    #: has committed (#269). None on a preview, whose rows are gone.
    rule_suggestions: int | None = None
    #: Source lines imported with the bank's own text, which rule suggestions
    #: read (#265). Counted per line in the source, not per row written: a
    #: split cut into three parts is one bank line and counts once.
    bank_text_rows: int = 0
    #: API imports only, and only accounts that do not add up; empty otherwise.
    balance_differences: list[OneTimeBalanceDifference] = []
    #: Accounts whose balance YNAB sent in a figure the currency cannot hold.
    balance_unchecked: list[OneTimeBalanceUnchecked] = []
    report_text: str


class OneTimePriorImport(BaseModel):
    """One earlier one-time import, as History has it."""

    batch_id: str
    at: datetime
    #: The key in `Batch.source["one_time_import"]`, e.g. ``ynab``.
    workflow: str
    #: What a person calls it: "YNAB".
    workflow_name: str
    via: str | None = None
    filename: str | None = None
    plan_name: str | None = None
    #: ``applied``, or ``undone`` once History took it back.
    status: str


class OneTimeImportHistory(BaseModel):
    """Every one-time import this household has had, newest first."""

    imports: list[OneTimePriorImport]


class OneTimeAnalysis(BaseModel):
    """What the wizard shows before anything is decided. Shapes as in the contract."""

    source: dict
    currency: dict
    date_format: dict
    totals: dict
    flags: list[dict]
    accounts: list[dict]
    categories: list[dict]
    targets: dict
    previous_imports: list[dict]
