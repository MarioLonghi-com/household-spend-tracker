"""Every enum in the schema.

The rule this build follows: an enum value with no designed behaviour is a bug
with a menu item. The previous build shipped twelve account types that drove
three behaviours, five of them loan types it never modelled -- and opening a
mortgage booked the principal as income.
"""

from __future__ import annotations

import enum


class AccountType(enum.StrEnum):
    checking = "checking"
    savings = "savings"
    cash = "cash"
    credit_card = "credit_card"
    other_asset = "other_asset"
    other_liability = "other_liability"

    @property
    def is_liability(self) -> bool:
        return self in {AccountType.credit_card, AccountType.other_liability}


class IdentifierKind(enum.StrEnum):
    """What a bank calls an account, when it is not calling it by our name.

    Each kind is matched differently, and that difference is the reason there
    is more than one (issue #66):

    - `iban`, `number`, `card`: digits and letters only, spacing ignored. Found
      in a file's name or content (which account a statement is for) and in a
      descriptor (which account a transfer went to).
    - `alias`: words -- a savings pocket's name, a card product's name. Matched
      as whole words in a descriptor, and as a pocket named in a statement.
    - `file_tag`: a token the bank puts in its download's file name, and only
      there. Revolut's statements carry one per account.
    - `holder`: how a bank writes a household member's name. Belongs to no
      account -- one person holds several -- so it says "this is our own money
      moving", never which account received it.
    """

    iban = "iban"
    number = "number"
    card = "card"
    alias = "alias"
    file_tag = "file_tag"
    holder = "holder"


class RegisterSort(enum.StrEnum):
    """Which column the register is sorted by. One per sortable heading."""

    date = "date"
    account = "account"
    payee = "payee"
    category = "category"
    memo = "memo"
    amount = "amount"
    cleared = "cleared"
    #: How the row got here: a transfer, an import, or somebody typing.
    source = "source"


class RegisterSource(enum.StrEnum):
    """How a row got into the register, as the Source column letters it (#123).

    The same four answers and the same precedence as the column: a transfer
    leg is a transfer even when it came from a statement, and a split part is
    a split even when the row it replaced was imported. A filter that ranked
    them differently from the letter on screen would hide rows showing the
    letter being asked for.
    """

    transfer = "transfer"
    split = "split"
    imported = "imported"
    manual = "manual"


class SortDirection(enum.StrEnum):
    asc = "asc"
    desc = "desc"


class ClearedState(enum.StrEnum):
    uncleared = "uncleared"
    cleared = "cleared"
    reconciled = "reconciled"


class ReimbursementState(enum.StrEnum):
    """Whether somebody else -- an employer, a client -- is to pay this row back.

    Two values, and **NULL is the third state**: the ordinary transaction,
    which is nearly every row in a ledger. Making "not a work expense" the
    absence of a value keeps this from being a column every row has to be
    given an opinion about.

    There is deliberately no ``settled`` member. Settled is
    ``reimbursed_by_id IS NOT NULL``; storing it too would be two facts about
    one concept, free to disagree. There is no ``submitted`` either: it was
    decided against for this build (2026-09-25), and the age of an outstanding
    row is what says it needs chasing.
    """

    #: Flagged by hand. Outstanding until a payment is linked to it.
    expected = "expected"
    #: Refused by work, or given up on. Stops being owed and becomes a cost the
    #: household absorbed -- a different figure from "never claimed", which is
    #: the only reason this value exists.
    written_off = "written_off"


class ReimbursementView(enum.StrEnum):
    """Which slice of work expenses the register is showing.

    Derived, not stored: four ``WHERE`` clauses over the two columns. Two of
    them bring the **payments** along with the expenses they repaid, because a
    claim is read as a unit -- the money in and what it covered.
    """

    #: Every flagged row, whatever became of it, and every payment linked to one.
    work = "work"
    #: Flagged and not yet paid back. The one people look for.
    owed = "owed"
    #: Paid back, together with the payments that did it.
    paid = "paid"
    #: Written off.
    off = "off"


class Categorisation(enum.StrEnum):
    """How a payee decides the category for a new transaction.

    Three behaviours, and each one does something different -- which is the bar
    an enum has to clear here. `history` is the default because it is right
    without anyone configuring it and gets better as the ledger grows.
    """

    #: Look at how this payee's recent transactions were categorised.
    history = "history"
    #: Always this category, whatever the history says.
    fixed = "fixed"
    #: Leave the category empty and let a person choose.
    none = "none"


class Role(enum.StrEnum):
    owner = "owner"
    member = "member"


class InstanceState(enum.StrEnum):
    fresh = "fresh"
    configured = "configured"


class BatchKind(enum.StrEnum):
    setup = "setup"
    manual = "manual"
    bulk_update = "bulk_update"
    imported = "import"
    #: Proving an account against a statement, which locks the rows it covered.
    reconciled = "reconcile"
    #: Dividing one transaction into several, which is one act and one undo.
    split = "split"
    undo = "undo"
    seed = "seed"
    admin = "admin"


class BatchStatus(enum.StrEnum):
    #: Open. A batch that never leaves this state died with its process.
    running = "running"
    #: An import parsed and staged but not yet committed by the user.
    preview = "preview"
    applied = "applied"
    undone = "undone"
    failed = "failed"


class ChangeOp(enum.StrEnum):
    insert = "insert"
    update = "update"
    delete = "delete"


class ImportOutcome(enum.StrEnum):
    created = "created"
    matched_existing = "matched_existing"
    duplicate_skipped = "duplicate_skipped"
    needs_review = "needs_review"
    rejected = "rejected"
    #: Read fine, and deliberately not imported: the row belongs to another
    #: account in a file that holds several (Revolut's `Product` column), or it
    #: moves no money at all. Kept apart from `rejected`, which means the line
    #: could not be read -- a preview with 394 "rejected" lines reads as a
    #: broken file when nothing about it is broken.
    skipped = "skipped"


class MatchType(enum.StrEnum):
    """How a payee rule decides a bank string is its own."""

    contains = "contains"
    equals = "equals"
    prefix = "prefix"
    regex = "regex"


class RuleAction(enum.StrEnum):
    """What a rule does once its pattern has matched.

    Orthogonal to :class:`MatchType`, which is how it matches: every match type
    is usable with either action, and the two answer different questions. This
    is a separate column rather than two more `MatchType` values on purpose --
    overloading the match type is how an enum grows behaviours it does not
    have, which `CLAUDE.md` has a standing rule about.
    """

    #: Pattern -> one fixed payee. What every rule written before this column
    #: existed does, and the default, so nothing had to be migrated.
    map = "map"
    #: Pattern -> a rewritten string, which the mapping rules then run against.
    #:
    #: The shape a payment *rail* needs. `SQ *`, `PAGO MOVIL` and `COMPRA
    #: INTERNET` each hide one distinct merchant per line, so mapping them to a
    #: fixed payee is wrong by construction -- there is no one payee to map to.
    #: A rewrite rule takes the rail off and lets whatever is left be the shop.
    rewrite = "rewrite"


class BlobRole(enum.StrEnum):
    """Which derivative of one upload a blob row holds.

    Three values, and each one is fetched by a different part of the app --
    which is the bar an enum has to clear here.
    """

    #: What was uploaded, byte for byte. Kept for PDFs, whose later pages a
    #: page-1 raster throws away, and for everything when
    #: SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL is on.
    original = "original"
    #: AVIF, 2000px long edge. What the lightbox shows.
    display = "display"
    #: AVIF, 320px. What the panel frame and the inbox grid show.
    thumb = "thumb"


class AgentScope(enum.StrEnum):
    """What a key may do. Two values, because there are two behaviours.

    The spec drafted this as a JSON list -- ``["read"]`` or ``["read",
    "write"]``. Stored as a list it has more shapes than it has meanings:
    ``write`` implies ``read``, so ``["write"]`` alone, ``[]``, ``["read",
    "read"]`` and any order of the two are all states the column can hold and
    the application would have to refuse one by one. One column of two values
    refuses them by construction: *a state the schema refuses to hold* is one
    no code path has to remember to refuse.

    It is also what the standing rule asks for: **store the deliberate act,
    compute the consequence.** The deliberate act is "this key may write". That
    ``write`` implies ``read`` is a consequence, and the manifest computes it --
    see :attr:`granted`, which is the list an agent is shown.

    §1.3 of the spec argues against a third scope ever arriving ("a receipt is
    a write like any other and splitting it buys a menu item and no
    behaviour"), so the flexibility a list would buy is flexibility the design
    has already declined.
    """

    read = "read"
    write = "write"

    @property
    def may_write(self) -> bool:
        return self is AgentScope.write

    @property
    def granted(self) -> list[str]:
        """Every scope this one carries, for the manifest and the key list.

        ``write`` implies ``read``, and that implication is the app's to apply
        rather than the agent's to remember.
        """
        return ["read"] if self is AgentScope.read else ["read", "write"]


class LinkSource(enum.StrEnum):
    """How two rows came to be linked as one transfer (#131).

    Stored on both legs because the matcher's "linked before" lanes read it:
    a link only vouches for the next one between the same two accounts when
    something other than that history said it was a transfer. Before this,
    one wrong link made on account history alone was the history that made
    the next wrong link strong. Every value is read by the matcher or by the
    Transfers screen's "Linked by history only" list.
    """

    #: A row named the other account, and the link was made without asking --
    #: at import, or by "Link all" on the Transfers screen. Counts as history.
    named = "named"
    #: A person chose this pair: a transfer typed in the register, "Link as
    #: transfer", or one pair linked on the Transfers screen. Counts as history.
    person = "person"
    #: Linked without asking because the two accounts had been linked before,
    #: and nothing named the other side. Does **not** count as history, and is
    #: listed for review.
    history = "history"
    #: Linked before links said how -- at an import or on the Transfers screen,
    #: which the ledger cannot now tell apart. Counts as history only while
    #: one of its rows still names the other account; otherwise it is listed
    #: for review like `history`.
    imported = "import"
    #: A program chose this pair, through an agent key (#134). Does **not**
    #: count as history -- a key borrows a person's authority, it does not
    #: stand in for their judgement -- and is always listed for review, even
    #: when a row names the other account, until a person keeps it (which
    #: makes it `person`) or unlinks it. Five letters, inside the column's
    #: eight, so no migration: the column is a plain VARCHAR with no CHECK.
    agent = "agent"


class SystemPayee(enum.StrEnum):
    """What a payee *is*, when the app made it rather than a person.

    Two values, and each one changes what a report does with the rows behind
    it -- which is the bar an enum has to clear here.

    The reason this column exists at all is that neither fact was in the schema.
    `accounts._write_opening_balance` writes a real transaction whose only mark
    is the payee *name* "Opening balance", and the only way a report could tell
    it from a salary payment was to string-match a name a person is free to
    rename, merge or translate. Left alone, every account's opening balance
    lands in month one as income: on a freshly imported ledger the "money in"
    column for the first month is the account balance, and the report opens on
    a lie.

    `transfer` is the same argument one table over. `Payee.transfer_account_id`
    already says *which* account a transfer names; this says *what kind* of
    payee it is, and the two are different questions -- a household is free to
    have a real payee called "Transfer" that moves no money at all.

    Neither is flow. Money entering and leaving the household is what a flow
    report measures; a transfer moves money *within* it, and an opening balance
    describes where it started. Both are counted in full by every balance
    query, where they are exactly right.
    """

    #: What the account held when tracking started. Not income.
    opening_balance = "opening_balance"
    #: The auto-managed "Transfer : <Account>" payees. Not spend, not income.
    transfer = "transfer"
