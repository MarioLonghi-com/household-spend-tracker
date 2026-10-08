"""Money moving between the household's own accounts, found and linked.

Issue #70. A transfer between two household accounts arrives as two rows from
two statements -- often imported days apart -- and until this module both were
counted as flow: an expense on one side, income on the other, both in the
Income v Expense report. The model already had the answer (a mirrored pair,
excluded from reports once linked); what was missing was any way to link two
rows that already exist, and anything that looked for them.

**What makes a pair a candidate**: two unlinked, uncategorised rows on different accounts of
one household, in the same currency, of exactly opposite amounts, dated at
most :data:`WINDOW_DAYS` apart. Amount and date alone are never enough to link
without asking -- on one real day four rows of one identical amount were two
transfers, and pairing by amount alone joined the wrong two.

**What makes it strong** is evidence that names the other side:

- a descriptor carrying one of the other account's identifiers (`TO A/C
  <number>`, `To <pocket name>`), from either leg; or
- the two accounts having been linked to each other before -- by a link that
  something other than this rule vouched for (#131): a row that named the
  other side, or a person. A link made on history alone is not history, or
  one wrong link would vouch for the next.

**What keeps it a suggestion anyway** (#131): money out of a credit card is a
purchase until a person says otherwise, and a row whose payee has a category
rule, or whose words name somebody who has paid this household before, is
somebody else's money -- an employer's expense refund, a card's cashback --
however exactly its amount matches. And a pair a person has said is not a
transfer, by unlinking it or with "Not a transfer", is never offered again.

A strong pair is linked automatically only when it is the *only* strong pair
either leg has -- or when, among its strong rivals, it is strictly the closest
in date, or failing that its two rows strictly share the most words (#127).
Everything else is a suggestion for a person to confirm.

**Cross-currency** pairs (issue #71) are never found here by amount: the
amounts differ by a rate nobody states. They are linked by hand, through
:func:`link` with the two rows a person chose.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from ..audit.hook import BATCH_KEY
from ..errors import Conflict, ValidationError
from ..models import (
    Account,
    AccountIdentifier,
    AccountType,
    Categorisation,
    IdentifierKind,
    LinkSource,
    Payee,
    SystemPayee,
    Transaction,
    TransferRejection,
)
from ..money import minor_factor
from . import identifiers as identifier_service
from . import payees as payee_service

#: How far apart the two legs of one transfer may be dated. Five days, decided
#: 2026-09-23: a weekend plus a bank holiday. Two real legs were two days apart.
WINDOW_DAYS = 5


#: Words every transfer descriptor uses, which say nothing about *which*
#: transfer this is (#127). Compared after upper-casing and dropping
#: punctuation, so "Ref:" is "REF" -- and "A/C" is "AC", which is here for
#: that reason: every "TO A/C <number>" and "FROM A/C <number>" says it.
TRANSFER_WORDS = frozenset({
    "TO", "FROM", "TRANSFER", "FPS", "BP", "REF", "AC",
    # The same words on a Spanish statement, and the joins around them:
    # "Transferencia Inmediata A Favor De ..., Concepto ..." says nothing about
    # who the money was for, and every Spanish transfer says it.
    "TRANSFERENCIA", "INMEDIATA", "TRASPASO", "FAVOR", "CONCEPTO",
    "DEL", "LOS", "LAS", "POR", "PARA", "CON",
})


def _text(txn: Transaction) -> tuple[str | None, ...]:
    """What the bank said about this row: its own words, then ours.

    Kept as separate fields, not one string, for `identifiers.named_in`: a
    bank cuts a name off at the end of *its* field, so a truncated surname is
    the last word of the bank's text, not of the payee and memo after it
    (#132). Joined, it only matched when nothing followed it.
    """
    payee = txn.payee.name if txn.payee is not None else None
    return (txn.import_payee_original, payee, txn.memo)


def link(
    session: Session,
    first: Transaction,
    second: Transaction,
    *,
    source: LinkSource = LinkSource.person,
    known: LinkPrefetch | None = None,
) -> tuple[Transaction, Transaction]:
    """Make two existing rows the two legs of one transfer.

    Both keep their own date, amount and bank descriptor: a transfer is often
    booked on different days by the two banks, and the ledger should agree with
    both statements. What changes is what they *are* -- each points at the
    other, carries the "Transfer : <account>" payee, and loses its category,
    because a transfer is not spending. The money does not move, so a
    reconciled row may be linked.

    Same currency: the amounts must be exactly opposite. Across currencies
    (issue #71) they must only point opposite ways, and the rate between them
    is worked out from the two amounts and stored on both legs.

    ``source`` says how the pair was chosen, and is stored on both legs
    (#131): only `named` and `person` links count as the "linked before"
    that makes a later pair strong -- never `history`, and never `agent`
    (#134), which `_lanes` reads by leaving them out. A pair someone had said was not a
    transfer stops being one they rejected once it is linked.

    ``known`` is for a caller linking many pairs in one act (#236): what the
    two per-pair reads would find, read once for every pair, and the transfer
    payees kept per account. With it the rows are left pending for the caller
    to flush once.
    """
    how = LinkSource(source)
    if first.id == second.id:
        raise ValidationError("a transaction cannot be a transfer with itself")
    if first.household_id != second.household_id:
        raise ValidationError("those transactions are in different households")
    if first.account_id == second.account_id:
        raise ValidationError("both of those are in the same account; a transfer moves between two")
    for leg in (first, second):
        if leg.transfer_transaction_id or leg.transfer_account_id:
            raise Conflict("one of those is already a transfer; unlink it first")
        if leg.split_id:
            raise ValidationError("a part of a split cannot be a transfer leg")
        if leg.amount == 0:
            raise ValidationError("a row with no money in it cannot be a transfer leg")
        # #131: the matcher took an employer's refund of a card purchase for
        # a transfer, and so would a person clicking through. A work expense
        # and the payment that repaid it are two movements of money with
        # somebody else, not one between your own accounts.
        if leg.reimbursement is not None:
            raise Conflict(
                "one of those is a work expense, which is money spent, not moved. "
                "Take the work-expense flag off it first if it really is a transfer."
            )
    paid = (
        {first.id, second.id} & known.payments
        if known is not None
        else _payments(session, [first.id, second.id])
    )
    if paid:
        raise Conflict(
            "one of those is the payment that repaid a work expense, not a transfer. "
            "Take it off the expenses it repaid first if it really is a transfer."
        )

    out_leg, in_leg = (first, second) if first.amount < 0 else (second, first)
    if not (out_leg.amount < 0 < in_leg.amount):
        raise ValidationError("a transfer takes money out of one account and into the other")

    source = session.get(Account, out_leg.account_id)
    destination = session.get(Account, in_leg.account_id)
    rate: str | None = None
    if source.currency == destination.currency:
        if in_leg.amount != -out_leg.amount:
            raise ValidationError(
                "the two sides of a same-currency transfer must be the same amount"
            )
    else:
        rate = str(
            (Decimal(in_leg.amount) / minor_factor(destination.currency))
            / (Decimal(-out_leg.amount) / minor_factor(source.currency))
        )

    for leg, other_account, other in (
        (out_leg, destination, in_leg),
        (in_leg, source, out_leg),
    ):
        leg.transfer_account_id = other_account.id
        leg.transfer_transaction_id = other.id
        leg.transfer_fx_rate = rate
        if known is None:
            payee = payee_service.transfer_payee(session, leg.household_id, other_account)
        else:
            payee = known.payees.get(other_account.id)
            if payee is None:
                payee = payee_service.transfer_payee(session, leg.household_id, other_account)
                known.payees[other_account.id] = payee
        leg.payee_id = payee.id
        leg.category_id = None
        leg.link_source = how
    rejections = (
        known.rejections.get((out_leg.id, in_leg.id), [])
        if known is not None
        else _rejections(session, out_leg, in_leg)
    )
    for rejected in rejections:
        session.delete(rejected)
    if known is None:
        session.flush()
    return out_leg, in_leg


@dataclass(slots=True)
class LinkPrefetch:
    """What `link` reads per pair, read once for many pairs (#236)."""

    #: Ids among the legs that repaid a work expense.
    payments: set[str]
    #: (out id, in id) -> the rejections recorded for that pair.
    rejections: dict[tuple[str, str], list[TransferRejection]]
    #: account id -> its "Transfer : <account>" payee, filled as it goes.
    payees: dict[str, Payee]


def prefetch(
    session: Session,
    household_id: str,
    pairs: list[tuple[Transaction, Transaction]],
    payees: dict[str, Payee] | None = None,
) -> LinkPrefetch:
    """One query for every pair's payments and one for its rejections."""
    ids = [leg.id for pair in pairs for leg in pair]
    rejections: dict[tuple[str, str], list[TransferRejection]] = defaultdict(list)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in session.execute(
            select(TransferRejection).where(
                TransferRejection.household_id == household_id,
                TransferRejection.out_transaction_id.in_(chunk),
            )
        ).scalars():
            rejections[(row.out_transaction_id, row.in_transaction_id)].append(row)
    payments: set[str] = set()
    for start in range(0, len(ids), 500):
        payments |= _payments(session, ids[start : start + 500])
    return LinkPrefetch(payments=payments, rejections=dict(rejections), payees=payees if payees is not None else {})


def unlink(session: Session, txn: Transaction) -> None:
    """Undo a link, leaving both rows where they are as ordinary rows.

    Each gets back the payee its bank named, when it came from a statement.
    The rows themselves are not deleted -- that is what deleting a transfer
    does, and it is a different act.

    The pair is recorded as rejected (#131), so the matcher does not offer it
    straight back: before this, the next sweep put every wrong link a person
    had just removed under "Sure of these" again. Undoing the unlink in
    History removes the record with the rest of the batch.
    """
    if not txn.transfer_transaction_id and not txn.transfer_account_id:
        raise Conflict("that transaction is not a transfer")
    other = session.get(Transaction, txn.transfer_transaction_id) if txn.transfer_transaction_id else None
    for leg in (txn, other):
        if leg is None:
            continue
        leg.transfer_account_id = None
        leg.transfer_transaction_id = None
        leg.transfer_fx_rate = None
        leg.link_source = None
        leg.payee_id = (
            payee_service.get_or_create(session, leg.household_id, leg.import_payee_original).id
            if leg.import_payee_original
            else None
        )
    if other is not None:
        reject(session, txn, other)
    session.flush()


def reject(session: Session, first: Transaction, second: Transaction) -> TransferRejection | None:
    """Say two rows are not one transfer, so they are never offered as one.

    "Not a transfer" on a pair the matcher found, and the second half of an
    unlink. Returns the record, or None when the pair was already rejected.
    Two rows that could never be a pair -- the same row, one household's row
    and another's, two rows going the same way -- are refused rather than
    recorded, since a record nobody can see would be a record of nothing.
    """
    if first.id == second.id:
        raise ValidationError("a transaction cannot be a transfer with itself")
    if first.household_id != second.household_id:
        raise ValidationError("those transactions are in different households")
    out_leg, in_leg = (first, second) if first.amount < 0 else (second, first)
    if not (out_leg.amount < 0 < in_leg.amount):
        raise ValidationError("a transfer takes money out of one account and into the other")
    if _rejections(session, out_leg, in_leg):
        return None
    batch_row = session.info.get(BATCH_KEY)
    row = TransferRejection(
        household_id=out_leg.household_id,
        out_transaction_id=out_leg.id,
        in_transaction_id=in_leg.id,
        rejected_by_id=batch_row.actor_id if batch_row is not None else None,
    )
    session.add(row)
    session.flush()
    return row


def _rejections(session: Session, out_leg: Transaction, in_leg: Transaction) -> list[TransferRejection]:
    return list(
        session.execute(
            select(TransferRejection).where(
                TransferRejection.household_id == out_leg.household_id,
                TransferRejection.out_transaction_id == out_leg.id,
                TransferRejection.in_transaction_id == in_leg.id,
            )
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# Finding them
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Pair:
    out_leg: Transaction
    in_leg: Transaction
    #: "strong" -- linked without asking; "suggested" -- a person decides.
    strength: str
    #: In words, for the screen: why these two.
    why: str
    #: The description words both rows carry, in the out-leg's order (#127).
    #: What breaks a tie between strong rivals, and what puts a suggestion
    #: first in "Worth a look".
    shared: tuple[str, ...] = ()
    #: What a strong pair's evidence was, and so what its link is recorded as:
    #: `named` or `history` (#131). None on a pair that was never strong.
    source: LinkSource | None = None

    @property
    def days_apart(self) -> int:
        return abs((self.in_leg.date - self.out_leg.date).days)


@dataclass(slots=True)
class Findings:
    strong: list[Pair] = field(default_factory=list)
    suggested: list[Pair] = field(default_factory=list)
    #: Rows whose descriptor says the household's own money is moving, with no
    #: other leg in the ledger yet: the other bank's statement has not been
    #: imported. They are matched when it is.
    awaiting: list[tuple[Transaction, str]] = field(default_factory=list)


@dataclass(slots=True)
class _Context:
    identifiers: list[AccountIdentifier]
    accounts: dict[str, Account]
    #: Pairs of accounts that have been linked to each other before, by a
    #: link that counts as history (#131).
    lanes: set[frozenset[str]]
    #: Words that name one of the household's own accounts. Two rows both
    #: saying "11112222" share that because they are transfers, not because
    #: they are *this* transfer, so it breaks no tie (#127).
    own_words: frozenset[str] = frozenset()
    #: The long account numbers among them, which a descriptor may glue to a
    #: word ("AC11112222").
    own_runs: tuple[str, ...] = ()
    #: Description -> its words, worked out once per distinct text.
    words: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: (out id, in id) of every pair a person said is not a transfer (#131).
    rejected: set[tuple[str, str]] = field(default_factory=set)
    #: Payees who have paid this household: a positive, categorised row that
    #: is not a transfer. Id -> the name as `_plain` spells it (#131).
    payers: dict[str, str] = field(default_factory=dict)
    #: Row id -> the accounts its bank text names, worked out once per row:
    #: pairing asks it of a row once per partner it could have (#240).
    named: dict[str, frozenset[str | None]] = field(default_factory=dict)


def _context(
    session: Session,
    household_id: str,
    *,
    extra: Sequence[AccountIdentifier] = (),
) -> _Context:
    """Everything matching needs to know about the household, read once.

    ``extra`` are identifiers matched as if they were stored -- what
    `strong_with` asks with, so "what would this identifier link?" is
    answered by the matcher itself and nothing is written (#130).
    """
    accounts = {
        row.id: row
        for row in session.execute(select(Account).where(Account.household_id == household_id)).scalars()
    }
    identifiers = [*identifier_service.list_for_household(session, household_id), *extra]
    own = [row for row in identifiers if row.account_id is not None]
    return _Context(
        identifiers=identifiers,
        accounts=accounts,
        lanes=_lanes(session, household_id, accounts, identifiers),
        rejected={
            (out_id, in_id)
            for out_id, in_id in session.execute(
                select(
                    TransferRejection.out_transaction_id, TransferRejection.in_transaction_id
                ).where(TransferRejection.household_id == household_id)
            ).all()
        },
        payers=_payers(session, household_id, identifiers),
        own_words=frozenset(
            word for row in own for word in (row.normalised, *_plain_words(row.normalised))
        ),
        own_runs=tuple(
            row.normalised for row in own if len(row.normalised) >= 6 and " " not in row.normalised
        ),
    )


def _lanes(
    session: Session,
    household_id: str,
    accounts: dict[str, Account],
    identifiers: list[AccountIdentifier],
) -> set[frozenset[str]]:
    """Pairs of accounts with a link between them that counts as history.

    `named` and `person` links count; `history` links never do, or one wrong
    link would make the next one strong (#131), and nor do `agent` links
    until a person confirms them (#134). A link from before links said
    how (`import`, or none) counts while one of its rows still names the
    other account -- which is exactly what `named` would have recorded.

    Read through `ix_transactions_transfer_account_id` (#100): naming the
    household's accounts lets it touch only the rows that are transfer legs,
    where `IS NOT NULL` alone walked the household's whole ledger.
    """
    legs = (
        Transaction.household_id == household_id,
        Transaction.transfer_account_id.in_(list(accounts) or [""]),
    )
    lanes = {
        frozenset((account_id, other_id))
        for account_id, other_id in session.execute(
            select(Transaction.account_id, Transaction.transfer_account_id)
            .where(*legs, Transaction.link_source.in_([LinkSource.named, LinkSource.person]))
            .distinct()
        ).all()
    }
    for account_id, other_id, texts in _legs_with_text(
        session,
        *legs,
        or_(Transaction.link_source.is_(None), Transaction.link_source == LinkSource.imported),
    ):
        lane = frozenset((account_id, other_id))
        if lane in lanes:
            continue
        if any(n.account_id == other_id for n in identifier_service.named_in(identifiers, *texts)):
            lanes.add(lane)
    return lanes


def _legs_with_text(
    session: Session, *where
) -> list[tuple[str, str, tuple[str | None, str | None]]]:
    """Transfer legs as (account, other account, their bank's words), one per
    distinct combination -- a ledger says the same few things many times.

    The bank's words and the memo, not the payee: a linked leg's payee is
    "Transfer : <the other account>", which the link wrote, not the bank.
    """
    return [
        (account_id, other_id, (original, memo))
        for account_id, other_id, original, memo in session.execute(
            select(
                Transaction.account_id,
                Transaction.transfer_account_id,
                Transaction.import_payee_original,
                Transaction.memo,
            )
            .where(*where)
            .distinct()
        ).all()
    ]


def _plain(text: str | None) -> str:
    """Upper-cased, every run of punctuation one space, padded for whole-word
    matching: " TRANSFERENCIA DE EMPLOYER SA "."""
    return " " + " ".join(re.sub(r"[\W_]+", " ", (text or "").upper()).split()) + " "


def _payers(
    session: Session, household_id: str, identifiers: list[AccountIdentifier]
) -> dict[str, str]:
    """Payees this household has had money *from* and categorised: income.

    A row naming one of them is somebody else's money arriving -- a salary, an
    employer's expense refund, a card's cashback -- not the household's own
    moving (#131). Names that are one of the household's identifiers are left
    out: a holder's name on an income row does not make every transfer naming
    that holder somebody else's money. So are names too short to find safely.
    """
    found: dict[str, str] = {}
    for payee_id, name in session.execute(
        select(Payee.id, Payee.name)
        .join(Transaction, Transaction.payee_id == Payee.id)
        .where(
            Payee.household_id == household_id,
            Payee.system.is_(None),
            Transaction.household_id == household_id,
            Transaction.amount > 0,
            Transaction.category_id.is_not(None),
            Transaction.transfer_account_id.is_(None),
        )
        .distinct()
    ).all():
        if identifier_service.named_in(identifiers, name):
            continue
        found[payee_id] = _plain(name)
    return found


def _third_party(ctx: _Context, txn: Transaction) -> str | None:
    """Why this row looks like somebody else's money, in words, or None."""
    payee = txn.payee
    if (
        payee is not None
        and payee.system is None
        and payee.categorisation is Categorisation.fixed
        and payee.default_category_id is not None
    ):
        return f"{payee.name} has a category rule"
    if txn.payee_id is not None and txn.payee_id in ctx.payers:
        return f"{payee.name if payee is not None else 'its payee'} has paid you before"
    words = _plain(txn.import_payee_original)
    for name in ctx.payers.values():
        if len(name.strip()) >= 4 and name in words:
            return f"it names {name.strip()}, who has paid you before"
    return None


def _plain_words(text: str | None) -> list[str]:
    """Upper-cased, punctuation removed, in order."""
    found = []
    for raw in (text or "").upper().split():
        word = re.sub(r"[\W_]", "", raw)
        if word:
            found.append(word)
    return found


def _words(ctx: _Context, txn: Transaction) -> tuple[str, ...]:
    """What this row's own description says about who the money was for.

    The bank's words (`import_payee_original`) only: the payee is ours, and a
    transfer payee names the account, which is no help between rivals. Common
    transfer words, words under three letters ("DE", "EN", initials) and the
    household's own account identifiers are left out.
    """
    text = txn.import_payee_original or ""
    if text not in ctx.words:
        kept: list[str] = []
        for word in _plain_words(text):
            if len(word) < 3 or word in TRANSFER_WORDS or word in ctx.own_words:
                continue
            if any(run in word for run in ctx.own_runs) or word in kept:
                continue
            kept.append(word)
        ctx.words[text] = tuple(kept)
    return ctx.words[text]


def _shared(ctx: _Context, out_leg: Transaction, in_leg: Transaction) -> tuple[str, ...]:
    theirs = set(_words(ctx, in_leg))
    return tuple(word for word in _words(ctx, out_leg) if word in theirs)


def _evidence(
    ctx: _Context, out_leg: Transaction, in_leg: Transaction
) -> tuple[str, str, LinkSource | None] | None:
    """How strongly two opposite rows look like one transfer, why, and -- for a
    strong pair -- what its link would be recorded as.

    None when a descriptor names a *third* household account: that row went
    somewhere else, whatever the amounts say.
    """
    found = _raw_evidence(ctx, out_leg, in_leg)
    if found is None or found[0] != "strong":
        return found
    strength, why, source = found
    # A card purchase and the refund, cashback or expense payment that
    # returns exactly its amount look like a transfer in every respect but
    # one: a purchase is not a transfer (#131). Still offered; never linked
    # without asking. A payment *into* the card is untouched.
    if ctx.accounts[out_leg.account_id].type is AccountType.credit_card:
        return "suggested", f"{why}, but money out of a card is a purchase until you say otherwise", None
    for leg in (out_leg, in_leg):
        payer = _third_party(ctx, leg)
        if payer is not None:
            return "suggested", f"{why}, but {payer}", None
    return strength, why, source


def _named(ctx: _Context, leg: Transaction) -> frozenset[str | None]:
    """The accounts this row's bank text names, read once per row (#240)."""
    found = ctx.named.get(leg.id)
    if found is None:
        found = ctx.named[leg.id] = frozenset(
            n.account_id for n in identifier_service.named_in(ctx.identifiers, *_text(leg))
        )
    return found


def _raw_evidence(
    ctx: _Context, out_leg: Transaction, in_leg: Transaction
) -> tuple[str, str, LinkSource | None] | None:
    named = {leg.id: _named(ctx, leg) for leg in (out_leg, in_leg)}
    accounts = {out_leg.account_id, in_leg.account_id}

    for leg, other in ((out_leg, in_leg), (in_leg, out_leg)):
        elsewhere = {a for a in named[leg.id] if a is not None and a not in accounts}
        if elsewhere and other.account_id not in named[leg.id]:
            return None

    for leg, other in ((out_leg, in_leg), (in_leg, out_leg)):
        if other.account_id in named[leg.id]:
            name = ctx.accounts[other.account_id].name
            return "strong", f"the {ctx.accounts[leg.account_id].name} row names {name}", LinkSource.named
    if frozenset(accounts) in ctx.lanes:
        return (
            "strong",
            "these two accounts have had transfers linked between them before",
            LinkSource.history,
        )
    if None in named[out_leg.id] | named[in_leg.id]:
        return "suggested", "a household member's name is on it, and the amounts match", None
    return "suggested", "the amounts match and the dates are close", None


def _is_candidate(txn: Transaction) -> bool:
    """The same test as :func:`_unlinked`, on a row already in hand.

    A categorised row is not a transfer (#125): a person, a payee rule or the
    payee's history has said what the money was *for*, and a transfer is not
    for anything. Clearing the category puts the row back.
    """
    if txn.transfer_transaction_id or txn.transfer_account_id or txn.split_id or txn.amount == 0:
        return False
    if txn.category_id is not None:
        return False
    # A work expense is money spent, whatever its amount matches (#131). The
    # other half -- a row that is a payment -- needs a query, so it is in
    # `_unlinked` and in `find`, which are where the rows come from.
    if txn.reimbursement is not None:
        return False
    return not (txn.payee is not None and txn.payee.system is SystemPayee.opening_balance)


def find(
    session: Session,
    household_id: str,
    *,
    among: list[Transaction] | None = None,
) -> Findings:
    """Every likely transfer among the household's unlinked rows.

    ``among`` narrows the question to pairs with at least one leg in it -- the
    rows an import has just written -- while the other leg may be anywhere in
    the ledger. Without it, this is the sweep over everything.
    """
    ctx = _context(session, household_id)
    if among is None:
        return _sweep(session, household_id, ctx)
    query = (
        select(Transaction)
        .options(selectinload(Transaction.payee))
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(*_unlinked(household_id))
    )
    wanted = {abs(t.amount) for t in among}
    if not wanted:
        return Findings()
    query = query.where(or_(Transaction.amount.in_(wanted), Transaction.amount.in_([-a for a in wanted])))
    pool = [t for t in session.execute(query).scalars() if _is_candidate(t)]
    focus = {t.id for t in among}
    # The rows asked about may not be in the database at all -- a preview
    # asks with stand-ins for lines it has not written. Those that are, and
    # that repaid a work expense, are not a leg however they got here.
    present = {t.id for t in pool} | _payments(session, [t.id for t in among])
    pool += [t for t in among if t.id not in present and _is_candidate(t)]
    return _match(ctx, pool, focus)


def _payments(session: Session, ids: list[str]) -> set[str]:
    """Which of these rows repaid a work expense."""
    # In chunks, as `_load` does: SQLite refuses more than 32,766 bound
    # variables (#231).
    found: set[str] = set()
    for start in range(0, len(ids), 500):
        found.update(
            session.execute(
                select(Transaction.reimbursed_by_id).where(
                    Transaction.reimbursed_by_id.in_(ids[start : start + 500])
                )
            ).scalars()
        )
    return found


def _unlinked(household_id: str) -> tuple:
    """The rows that could be a leg: unlinked, unsplit, uncategorised, moving
    money, and not an opening balance. Needs `payees` outer-joined.

    Uncategorised because a row with a category is not a transfer (#125) --
    which leaves it out of the pairs, the auto-link at import and the waiting
    list alike, with no column of its own: clearing the category puts it back.

    Not a work expense and not the payment that repaid one, for the same
    reason (#131): an employer refunding a card purchase exactly, within
    days, into another of the household's accounts looks like a transfer on
    amount and date alone, and six of them were linked as one on the real
    ledger. Taking the flag or the link off puts the row back.
    """
    expense = aliased(Transaction)
    return (
        Transaction.reimbursement.is_(None),
        Transaction.id.not_in(
            select(expense.reimbursed_by_id).where(
                expense.household_id == household_id,
                expense.reimbursed_by_id.is_not(None),
            )
        ),
        Transaction.household_id == household_id,
        Transaction.transfer_transaction_id.is_(None),
        Transaction.transfer_account_id.is_(None),
        Transaction.split_id.is_(None),
        Transaction.category_id.is_(None),
        Transaction.amount != 0,
        or_(Payee.system.is_(None), Payee.system != SystemPayee.opening_balance),
    )


def _sweep(session: Session, household_id: str, ctx: _Context) -> Findings:
    """The sweep over the whole ledger, loading only the rows it can use.

    It used to load every unlinked row of the household as an ORM object --
    1.4-2.3 s at 30k rows, 1.26 s of it building objects -- to compare them in
    Python and find that almost none had a partner (issue #105). Now SQL does
    the first cut, with the same test the pairing below applies: another
    unlinked row, same currency, exactly opposite amount, a different account,
    dated at most :data:`WINDOW_DAYS` apart. Only those rows become objects.

    The one other use of the whole pool is `awaiting`: a row with no partner
    whose descriptor names another account. That is read from the text columns
    alone, as plain tuples, and only the rows that do name one are loaded.

    **Order is part of the answer** -- the Transfers screen lists pairs as
    found, and pairs equally far apart go in pool order. The old query had no
    ORDER BY and walked `ix_transactions_household_date`, so the pool came in
    date order, then insertion order; that is now stated rather than left to
    the planner, which statistics (#100) could otherwise change under it. Each
    amount group is also visited in the order its *first* row appears in the
    whole ledger, not in the filtered pool, which is what walking every row
    did.
    """
    pool, first_seen = _pool(session, household_id)
    findings, paired = _pairs(ctx, pool, None, group_order=first_seen)
    return _awaiting(session, household_id, ctx, findings, paired)


_IN_ORDER = (Transaction.date, literal_column("transactions.rowid"))


def _pool(
    session: Session, household_id: str
) -> tuple[list[Transaction], dict[tuple[str, int], int]]:
    """The rows that have a possible partner, loaded, and the order their
    amount groups are visited in. Nothing here depends on the identifiers."""
    window = f"{WINDOW_DAYS} days"
    in_order = _IN_ORDER
    numbered = (
        select(
            Transaction.id.label("id"),
            func.row_number().over(order_by=in_order).label("seq"),
            Transaction.account_id.label("account_id"),
            Transaction.amount.label("amount"),
            Transaction.date.label("date"),
            Account.currency.label("currency"),
        )
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(*_unlinked(household_id))
        .cte("numbered")
    )
    base = select(
        numbered,
        func.min(numbered.c.seq)
        .over(partition_by=(numbered.c.currency, func.abs(numbered.c.amount)))
        .label("first_seq"),
    ).cte("unlinked")
    other = base.alias("other")
    partnered = (
        select(base.c.id, base.c.seq, base.c.currency, base.c.amount, base.c.first_seq)
        .where(
            select(literal_column("1"))
            .where(
                other.c.currency == base.c.currency,
                other.c.amount == -base.c.amount,
                other.c.account_id != base.c.account_id,
                other.c.date >= func.date(base.c.date, f"-{window}"),
                other.c.date <= func.date(base.c.date, f"+{window}"),
            )
            .exists()
        )
        .order_by(base.c.seq)
    )
    legs = session.execute(partnered).all()
    first_seen = {(row.currency, abs(row.amount)): row.first_seq for row in legs}

    loaded = _load(session, [row.id for row in legs])
    pool = [loaded[row.id] for row in legs if row.id in loaded and _is_candidate(loaded[row.id])]
    return pool, first_seen


def _awaiting(
    session: Session, household_id: str, ctx: _Context, findings: Findings, paired: set[str]
) -> Findings:
    """The rows with no partner whose words name another account."""
    in_order = _IN_ORDER
    if not any(n.kind is not IdentifierKind.file_tag for n in ctx.identifiers):
        return findings  # nothing a descriptor could name, so nothing awaits
    naming: list[tuple[str, str]] = []
    #: Bank text -> what it names. A ledger says the same few hundred things
    #: thousands of times -- one payee, one descriptor -- and reading each
    #: distinct one once is the same answer for a fraction of the work.
    named: dict[tuple[str | None, ...], list] = {}
    for txn_id, account_id, original, payee_name, memo in session.execute(
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.import_payee_original,
            Payee.name,
            Transaction.memo,
        )
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(*_unlinked(household_id))
        .order_by(*in_order)
    ).all():
        if txn_id in paired:
            continue
        texts = (original, payee_name, memo)
        if texts not in named:
            named[texts] = identifier_service.named_in(ctx.identifiers, *texts)
        why = _awaiting_why(ctx, account_id, named[texts])
        if why is not None:
            naming.append((txn_id, why))
    if naming:
        rows = _load(session, [txn_id for txn_id, _ in naming])
        findings.awaiting = [
            (rows[txn_id], why)
            for txn_id, why in naming
            if txn_id in rows and _is_candidate(rows[txn_id])
        ]
    return findings


def _load(session: Session, ids: list[str]) -> dict[str, Transaction]:
    """Rows by id, with their payees, in queries of a few hundred."""
    found: dict[str, Transaction] = {}
    for start in range(0, len(ids), 500):
        for txn in session.execute(
            select(Transaction)
            .options(selectinload(Transaction.payee))
            .where(Transaction.id.in_(ids[start : start + 500]))
        ).scalars():
            found[txn.id] = txn
    return found


def _awaiting_why(ctx: _Context, account_id: str, own: list) -> str | None:
    """Why a row with no partner looks like the household's own money moving,
    given what its text names."""
    others = [n for n in own if n.account_id != account_id]
    if not others:
        return None
    what = ctx.accounts[others[0].account_id].name if others[0].account_id else "a household member"
    return f"names {what}; its other side is not in the ledger yet"


def _match(ctx: _Context, pool: list[Transaction], focus: set[str] | None) -> Findings:
    """Pairs among `pool`, and the rows in it still waiting for their other leg."""
    findings, paired = _pairs(ctx, pool, focus)
    for txn in pool:
        if txn.id in paired or (focus is not None and txn.id not in focus):
            continue
        why = _awaiting_why(
            ctx, txn.account_id, identifier_service.named_in(ctx.identifiers, *_text(txn))
        )
        if why is not None:
            findings.awaiting.append((txn, why))
    return findings


def _pairs(
    ctx: _Context,
    pool: list[Transaction],
    focus: set[str] | None,
    *,
    group_order: dict[tuple[str, int], int] | None = None,
) -> tuple[Findings, set[str]]:
    """Every pair in `pool`, sorted into strong and suggested; and who is paired."""
    by_size: dict[tuple[str, int], list[Transaction]] = defaultdict(list)
    for txn in pool:
        currency = ctx.accounts[txn.account_id].currency
        by_size[(currency, abs(txn.amount))].append(txn)
    if group_order is not None:
        by_size = dict(sorted(by_size.items(), key=lambda item: group_order[item[0]]))

    window = timedelta(days=WINDOW_DAYS)
    pairs: list[Pair] = []
    for rows in by_size.values():
        outs = [t for t in rows if t.amount < 0]
        ins = [t for t in rows if t.amount > 0]
        # Only the ins within the window of each out, found by bisecting the
        # ins sorted by date -- not every out against every in, which was
        # quadratic in a group of hundreds of same-amount rows (#240). Taken
        # back into file order, so the pairs come out exactly as they did.
        by_date = sorted(range(len(ins)), key=lambda i: ins[i].date)
        dates = [ins[i].date for i in by_date]
        for out_leg in outs:
            low = bisect_left(dates, out_leg.date - window)
            high = bisect_right(dates, out_leg.date + window)
            for in_leg in (ins[i] for i in sorted(by_date[low:high])):
                if in_leg.account_id == out_leg.account_id:
                    continue
                if focus is not None and out_leg.id not in focus and in_leg.id not in focus:
                    continue
                if (out_leg.id, in_leg.id) in ctx.rejected:
                    continue
                found = _evidence(ctx, out_leg, in_leg)
                if found is not None:
                    strength, why, source = found
                    pairs.append(
                        Pair(out_leg, in_leg, strength, why, _shared(ctx, out_leg, in_leg), source)
                    )

    # Strong only when it is the one strong answer for both legs -- or, where a
    # leg has several strong partners (the same amount moved on nearby days),
    # when one of them is strictly the closest in date for *both* legs. Where
    # date cannot say -- two transfers of one amount on one day between
    # overlapping accounts -- the pair whose two rows share strictly more
    # description words than every rival's does wins (#127): each leg says
    # who it was for. That is resolved closest-first and repeated, because
    # settling one pair can leave its neighbours with a single answer. A tie
    # stays with a person.
    strong = [p for p in pairs if p.strength == "strong"]
    findings = Findings()
    taken: set[str] = set()
    progress = True
    while progress:
        progress = False
        open_by_leg: dict[str, list[Pair]] = defaultdict(list)
        for pair in strong:
            if pair.out_leg.id in taken or pair.in_leg.id in taken:
                continue
            open_by_leg[pair.out_leg.id].append(pair)
            open_by_leg[pair.in_leg.id].append(pair)
        for pair in sorted(strong, key=lambda p: p.days_apart):
            if pair.out_leg.id in taken or pair.in_leg.id in taken:
                continue
            rivals = [
                q
                for leg in (pair.out_leg.id, pair.in_leg.id)
                for q in open_by_leg[leg]
                if q is not pair
            ]
            if any(q.days_apart <= pair.days_apart for q in rivals):
                mine = len(pair.shared)
                if not mine or any(len(q.shared) >= mine for q in rivals):
                    continue
                pair.why = f"{pair.why}; both rows say {' '.join(pair.shared)}"
            elif rivals:
                pair.why = f"{pair.why}; the closest in date of {len(rivals) + 1} it could be"
            findings.strong.append(pair)
            taken |= {pair.out_leg.id, pair.in_leg.id}
            progress = True

    # Suggestions whose rows say the same thing come first, and say so (#127).
    # They are still only suggestions: words choose among pairs that already
    # have evidence, they are not evidence themselves.
    for pair in sorted(pairs, key=lambda p: (not p.shared, p.days_apart)):
        if pair.out_leg.id in taken or pair.in_leg.id in taken:
            continue
        if pair.strength == "strong":
            pair.strength = "suggested"
            pair.source = None
            pair.why = f"{pair.why}, but there is more than one row it could be"
        if pair.shared:
            pair.why = f"{pair.why}; both rows say {' '.join(pair.shared)}"
        findings.suggested.append(pair)

    paired = {t.id for p in pairs for t in (p.out_leg, p.in_leg)}
    return findings, paired


PairKey = tuple[str, str]


def strong_with(
    session: Session, household_id: str, extras: Sequence[AccountIdentifier]
) -> tuple[set[PairKey], list[set[PairKey]]]:
    """The strong pairs the sweep finds now, and with each of ``extras`` added.

    What "would link N pairs" is counted with (#130): the pairs a suggested
    identifier would add are the ones strong with it and not strong now. The
    matcher answers, not an estimate of it -- the same `_context` and
    `_pairs` that `find` runs, with the identifier matched as though stored.

    Nothing is written. The extras are never added to the session, and which
    rows *could* pair does not depend on identifiers at all, so that pool is
    read once and only the evidence is worked out again for each extra.

    **An extra that touches nothing is not run.** An identifier changes the
    answer only through text: a pool row it names, a word of it a pool row
    uses (the tie-break's own words), a link made before links said how that
    it would make history (`_lanes`), or a payer's name it would excuse
    (`_payers`). One that reaches none of those leaves every pair exactly as
    it is -- which is most of them -- and costs a scan, not a sweep.
    """
    pool, first_seen = _pool(session, household_id)

    def strong(ctx: _Context) -> set[PairKey]:
        findings, _ = _pairs(ctx, pool, None, group_order=first_seen)
        return {(p.out_leg.id, p.in_leg.id) for p in findings.strong}

    base = _context(session, household_id)
    now = strong(base)
    legacy = _legs_with_text(
        session,
        Transaction.household_id == household_id,
        Transaction.transfer_account_id.in_(list(base.accounts) or [""]),
        or_(Transaction.link_source.is_(None), Transaction.link_source == LinkSource.imported),
    )
    texts: list[tuple[str | None, ...]] = list(dict.fromkeys(_text(txn) for txn in pool))
    texts += [leg_texts for _, _, leg_texts in legacy]
    texts += [(name,) for name in base.payers.values()]
    pool_words = {
        word for txn in pool for word in _plain_words(txn.import_payee_original)
    }

    def touches(extra: AccountIdentifier) -> bool:
        if {extra.normalised, *_plain_words(extra.normalised)} & pool_words:
            return True
        return any(identifier_service.named_in([extra], *one) for one in texts)

    def with_extra(extra: AccountIdentifier) -> _Context:
        """`_context` as it would read with `extra` stored, from the one
        already read: what each part of it would add or drop."""
        own = extra.account_id is not None
        return replace(
            base,
            identifiers=[*base.identifiers, extra],
            lanes=base.lanes | {
                frozenset((account_id, other_id))
                for account_id, other_id, leg_texts in legacy
                if other_id == extra.account_id
                and identifier_service.named_in([extra], *leg_texts)
            },
            payers={
                payee_id: name
                for payee_id, name in base.payers.items()
                if not identifier_service.named_in([extra], name)
            },
            own_words=base.own_words | (
                {extra.normalised, *_plain_words(extra.normalised)} if own else set()
            ),
            own_runs=base.own_runs + (
                (extra.normalised,)
                if own and len(extra.normalised) >= 6 and " " not in extra.normalised
                else ()
            ),
            words={},
            # What each row names changes with `extra` stored, so the base
            # context's answers cannot be reused for it.
            named={},
        )

    return now, [strong(with_extra(one)) if touches(one) else set(now) for one in extras]


def link_strong(session: Session, findings: Findings) -> int:
    """Link every strong pair, each recorded as what made it strong. Returns
    how many were linked."""
    count = 0
    for pair in findings.strong:
        if pair.out_leg.transfer_transaction_id or pair.in_leg.transfer_transaction_id:
            continue
        link(session, pair.out_leg, pair.in_leg, source=pair.source or LinkSource.named)
        count += 1
    return count


def link_until_settled(
    session: Session, household_id: str, *, among: list[Transaction] | None = None
) -> int:
    """Link every strong pair, then ask again, until nothing new is strong.
    Returns how many were linked.

    One pass is not enough (#88). A `named` link makes its two accounts a
    lane, and a lane is evidence: a pair between them that was a suggestion
    a moment ago is strong now. Asking once left exactly those for a second
    press of "Link all", or for nobody at import. Each round links at least
    one pair or ends, so this stops; a `history` link adds no lane, so in
    practice it is two rounds.

    ``among`` is `find`'s: the rows an import has just written.
    """
    total = 0
    while True:
        session.flush()
        rows = None if among is None else [t for t in among if _is_candidate(t)]
        if rows is not None and not rows:
            return total
        linked = link_strong(session, find(session, household_id, among=rows))
        if not linked:
            return total
        total += linked


def link_on_evidence(session: Session, household_id: str, pairs: list[tuple[Transaction, Transaction]]) -> int:
    """Link pairs a person did not look at one by one -- "Link all" on the
    Transfers screen -- each recorded as what its evidence is *now*: `named`
    when a row names the other account, else `history` (#131).

    Clicking "Link all" is not a person vouching for each pair, and recording
    it as one is how a history-only link would come to count as history.

    Then whatever those links made strong, the same way, until nothing new is
    (#88): "Link all" is "link what is strong", and a second press finding
    more meant the first had not. Returns how many were linked in all.
    """
    ctx = _context(session, household_id)
    for first, second in pairs:
        out_leg, in_leg = (first, second) if first.amount < 0 else (second, first)
        link(
            session, first, second,
            source=LinkSource.named if _names_other(ctx, out_leg, in_leg) else LinkSource.history,
        )
    return len(pairs) + link_until_settled(session, household_id)


def _names_other(
    ctx: _Context, out_leg: Transaction, in_leg: Transaction, *, linked: bool = False
) -> bool:
    """Whether either row's text names the other row's account.

    On rows already ``linked`` the payee is left out: it is the "Transfer :
    <account>" payee the link wrote, not anything the bank said.
    """
    for leg, other in ((out_leg, in_leg), (in_leg, out_leg)):
        texts = (leg.import_payee_original, leg.memo) if linked else _text(leg)
        if any(
            n.account_id == other.account_id
            for n in identifier_service.named_in(ctx.identifiers, *texts)
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Reviewing links already made
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Linked:
    out_leg: Transaction
    in_leg: Transaction
    source: LinkSource | None
    why: str


def unproven(session: Session, household_id: str) -> list[Linked]:
    """Links no name vouches for, which a person did not make (#131), and
    every link a program made (#134).

    The "Linked by history only" list on the Transfers screen: how the wrong
    links account history already made are found and removed. A link is on it
    when neither row names the other account and it is not a person's -- so a
    `named` link whose identifier has since been removed is on it too, since
    what it was linked on no longer says so.
    """
    ctx = _context(session, household_id)
    outs = list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.payee))
            .where(
                Transaction.household_id == household_id,
                Transaction.transfer_account_id.in_(list(ctx.accounts) or [""]),
                Transaction.transfer_transaction_id.is_not(None),
                Transaction.amount < 0,
                or_(Transaction.link_source.is_(None), Transaction.link_source != LinkSource.person),
            )
            .order_by(Transaction.date, literal_column("transactions.rowid"))
        ).scalars()
    )
    ins = _load(session, [t.transfer_transaction_id for t in outs])
    found: list[Linked] = []
    for out_leg in outs:
        in_leg = ins.get(out_leg.transfer_transaction_id)
        if in_leg is None:
            continue
        # A program's link is listed whatever its rows say (#134): a name on
        # the row is why the matcher may link without asking, and an agent
        # linked it because the matcher did NOT -- so a person looks.
        if out_leg.link_source is not LinkSource.agent and _names_other(
            ctx, out_leg, in_leg, linked=True
        ):
            continue
        found.append(Linked(out_leg, in_leg, out_leg.link_source, _unproven_why(out_leg.link_source)))
    return found


def _unproven_why(source: LinkSource | None) -> str:
    if source is LinkSource.agent:
        return "linked by a program through an agent key; a person has not confirmed it"
    if source is LinkSource.history:
        return "linked because these two accounts had been linked before; neither row names the other"
    if source is LinkSource.named:
        return "linked by name, but neither row names the other account any more"
    return "linked before links recorded why, and neither row names the other account"


def confirm(session: Session, txn: Transaction) -> None:
    """A person says this link is right: it counts as history from now on,
    and leaves the "Linked by history only" list."""
    if not txn.transfer_transaction_id:
        raise Conflict("that transaction is not a transfer")
    other = session.get(Transaction, txn.transfer_transaction_id)
    for leg in (txn, other):
        if leg is not None:
            leg.link_source = LinkSource.person
    session.flush()
