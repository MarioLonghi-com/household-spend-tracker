"""Turning the audit log into sentences.

The log stores full row images, which is what makes undo exact -- and which
makes it useless to read. "manual · applied" tells you an act happened, not what
it did, so the History screen was a list of timestamps you could only act on by
guessing. That is the wrong thing to attach an irreversible button to.

So the description is computed from the changes on every read, never stored.
Storing it would be a second copy of what the log already says, free to drift
from it, and the rule in this codebase is that one stored number per concept
means one stored *anything* per concept.

Nothing here is authoritative: if a description and the change rows disagree,
the change rows are right. This module only reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Account,
    Batch,
    BatchKind,
    Category,
    CategoryGroup,
    Change,
    ChangeOp,
    Payee,
    Transaction,
    User,
)
from ..money import format_amount

#: Columns that change on every write and say nothing about intent.
NOISE = frozenset({"updated_at", "created_at", "id"})

#: What each table is called in a sentence: (singular, plural).
NOUNS: dict[str, tuple[str, str]] = {
    "transactions": ("transaction", "transactions"),
    "accounts": ("account", "accounts"),
    "payees": ("payee", "payees"),
    "payee_rules": ("payee rule", "payee rules"),
    "categories": ("category", "categories"),
    "category_groups": ("category group", "category groups"),
    "households": ("household", "households"),
    "household_members": ("member", "members"),
    "reconciliations": ("reconciliation", "reconciliations"),
    "users": ("user", "users"),
    "invitations": ("invitation", "invitations"),
    "receipts": ("receipt", "receipts"),
    "agent_keys": ("agent key", "agent keys"),
    "transfer_rejections": ("pair marked not a transfer", "pairs marked not a transfer"),
    "ignored_identifier_suggestions": ("ignored identifier suggestion", "ignored identifier suggestions"),
}

#: Fields worth naming when they change, and how to say them.
FIELD_WORDS: dict[str, str] = {
    "amount": "amount",
    "date": "date",
    "payee_id": "payee",
    "category_id": "category",
    "memo": "memo",
    "cleared": "state",
    "name": "name",
    "archived": "archived",
    "closed": "closed",
    "theme": "colour",
    "accent": "accent",
    "note": "note",
    "group_id": "group",
    "sort_order": "order",
    "categorisation": "categorisation",
    "default_category_id": "default category",
    "base_currency": "currency",
    "institution": "bank",
    "reimbursement": "work expense",
    "reimbursed_by_id": "reimbursed by",
}


@dataclass(slots=True)
class FieldChange:
    """One column that moved, with both sides said in words."""

    field: str
    was: str
    now: str


@dataclass(slots=True)
class ChangeDetail:
    """One changed row, spelled out as far as the log allows."""

    seq: int
    table: str
    row_id: str
    op: str
    #: The same sentence the History list uses.
    summary: str
    #: What each column went from and to. Empty on an insert or a delete, where
    #: the whole row is the change and there is no "from".
    fields: list[FieldChange]
    #: The full row, for an insert or a delete, as field/value pairs.
    snapshot: list[FieldChange]
    #: Columns deliberately kept out of the log -- password hashes, TOTP
    #: secrets. Named rather than starred over: an undo writes a "***" back
    #: verbatim, so these are *absent*, and saying so is the honest version.
    redacted: list[str]


@dataclass(slots=True)
class Described:
    """A batch, in words."""

    #: "Statement import", "Edit", "Reconciliation" -- what kind of act.
    headline: str
    #: One sentence saying what it did. The thing a person reads.
    detail: str
    #: Who did it, by name.
    actor: str | None = None
    #: The program that did it on their behalf, when one did. Beside `actor`,
    #: never instead of it: an agent borrows a person's authority, so a row
    #: reads "Jane Doe · via Claude (receipt filer)".
    #:
    #: Read from `source["agent"]`, not from the foreign key, because the key
    #: is swept thirty days after revocation and "via a key since removed" is
    #: a worse sentence than the name. The copy outlives the link on purpose.
    via: str | None = None
    #: One line per change, for the confirmation before an undo.
    lines: list[str] = field(default_factory=list)


class _Names:
    """Every id this household might mention, resolved in three queries.

    Loaded once per request rather than per change row: a bulk edit of two
    hundred transactions would otherwise be two hundred lookups to write one
    sentence. And once per *request*, not per batch: the History list used to
    build a fresh one for every entry it showed, four whole-table reads each,
    a hundred entries to a page (issue #102). Build one with :func:`names_for`
    and hand it to every :func:`describe` of the same household.
    """

    def __init__(self, session: Session, household_id: str) -> None:
        self._session = session
        #: user id -> display name, or None for a user since removed.
        self._actors: dict[str, str | None] = {}
        #: transaction id -> how a payment reads; see `payment`.
        self._payments: dict[str, str] = {}
        self.accounts: dict[str, str] = {}
        self.currencies: dict[str, str] = {}
        for row in session.execute(
            select(Account).where(Account.household_id == household_id)
        ).scalars():
            self.accounts[row.id] = row.name
            self.currencies[row.id] = row.currency
        self.payees = {
            row.id: row.name
            for row in session.execute(
                select(Payee).where(Payee.household_id == household_id)
            ).scalars()
        }
        self.categories = {
            row[0]: f"{row[1]}: {row[2]}"
            for row in session.execute(
                select(Category.id, CategoryGroup.name, Category.name)
                .join(CategoryGroup, CategoryGroup.id == Category.group_id)
                .where(Category.household_id == household_id)
            ).all()
        }

    def actor(self, user_id: str) -> str | None:
        """Who did it, by name -- read once per person, not once per batch."""
        if user_id not in self._actors:
            user = self._session.get(User, user_id)
            self._actors[user_id] = user.display_name if user else None
        return self._actors[user_id]

    #: What an empty value is called, per field. "nothing" is true and useless:
    #: a row with no category is uncategorised, which is a state somebody
    #: recognises from the register.
    EMPTY = {
        "category_id": "uncategorised",
        "default_category_id": "no default",
        "payee_id": "no payee",
        "memo": "no memo",
        "note": "no note",
        "accent": "the palette's own",
        "institution": "no bank",
        "reimbursement": "not a work expense",
        "reimbursed_by_id": "not yet reimbursed",
    }

    #: Enum values whose own spelling does not belong in a sentence.
    WORDS = {
        "reimbursement": {"expected": "expected", "written_off": "written off"},
    }

    def payment(self, transaction_id: str) -> str:
        """The payment that repaid a work expense, as somebody would recognise
        it: "€324.50 into Checking on 30 Sep 2026". History is read months
        later, and an id then is no answer at all.

        Read on demand and remembered, rather than loaded with the rest: the
        household's transactions are the one table too big to fetch whole for
        the odd sentence that names one.
        """
        if transaction_id not in self._payments:
            txn = self._session.get(Transaction, transaction_id)
            if txn is None:
                self._payments[transaction_id] = "a transaction since removed"
            else:
                currency = self.currencies.get(txn.account_id, "EUR")
                account = self.accounts.get(txn.account_id, "an account since removed")
                self._payments[transaction_id] = (
                    f"{format_amount(txn.amount, currency)} into {account} on "
                    f"{txn.date.day} {txn.date.strftime('%b %Y')}"
                )
        return self._payments[transaction_id]

    def value(self, column: str, raw: object, *, currency: str = "EUR") -> str:
        """One field's value, as it should read in a sentence."""
        if raw is None or raw == "":
            return self.EMPTY.get(column, "nothing")
        if column == "payee_id":
            return self.payees.get(str(raw), "a payee since removed")
        if column in ("category_id", "default_category_id"):
            return self.categories.get(str(raw), "a category since removed")
        if column == "account_id":
            return self.accounts.get(str(raw), "an account since removed")
        if column == "reimbursed_by_id":
            return self.payment(str(raw))
        if column in self.WORDS:
            return self.WORDS[column].get(str(raw), str(raw))
        if column in ("amount", "statement_balance", "opening_balance"):
            try:
                return format_amount(int(raw), currency)
            except (TypeError, ValueError):
                return str(raw)
        if isinstance(raw, bool):
            return "yes" if raw else "no"
        return str(raw)


def _row_label(change: Change, names: _Names) -> str:
    """What the changed row *is*, so a sentence has a subject.

    Taken from whichever image exists: an insert has no before, a delete has no
    after, and the row itself is gone by the time anyone reads this.
    """
    image = change.after or change.before or {}
    if change.table_name == "transactions":
        currency = names.currencies.get(str(image.get("account_id")), "EUR")
        parts = [names.value("amount", image.get("amount"), currency=currency)]
        payee = image.get("payee_id")
        if payee:
            parts.append(names.payees.get(str(payee), "?"))
        account = names.accounts.get(str(image.get("account_id")))
        if account:
            parts.append(f"in {account}")
        return " · ".join(parts)
    if name := image.get("name"):
        return str(name)
    return NOUNS.get(change.table_name, (change.table_name, change.table_name))[0]


def _changed_fields(change: Change) -> dict[str, tuple[object, object]]:
    before, after = change.before or {}, change.after or {}
    return {
        key: (before.get(key), value)
        for key, value in after.items()
        if key not in NOISE and before.get(key) != value
    }


def _one_line(change: Change, names: _Names, origin: str | None = None) -> str:
    """One change, in words. `origin` names where an inserted row came from.

    A split part is an ordinary insert in the log, so its own history opened
    with a bare "Added transaction" -- which is the one thing it was not. It
    arrived because something else was divided, and that something else no
    longer exists to be looked up, so the sentence has to carry it.
    """
    noun = NOUNS.get(change.table_name, (change.table_name, change.table_name))[0]
    label = _row_label(change, names)

    if change.op is ChangeOp.insert:
        if origin:
            return f"Added {noun} {label} by splitting {origin}"
        # A row's own history is keyed on the row, not the batch, so the
        # workflow it arrived by has to be in the sentence (#183). The image
        # carries it: `import_source` is written on every such row.
        workflow = ONE_TIME_HEADLINES.get(str((change.after or {}).get("import_source")))
        if workflow and change.table_name == "transactions":
            return f"Added {noun} {label} by {workflow}"
        return f"Added {noun} {label}"
    if change.op is ChangeOp.delete:
        return f"Removed {noun} {label}"

    fields = _changed_fields(change)
    if not fields:
        return f"Touched {noun} {label}"

    image = change.after or {}
    currency = names.currencies.get(str(image.get("account_id")), "EUR")
    said = []
    for column, (was, now) in fields.items():
        word = FIELD_WORDS.get(column)
        if word is None:
            continue
        said.append(
            f"{word} {names.value(column, was, currency=currency)} "
            f"→ {names.value(column, now, currency=currency)}"
        )
    if not said:
        return f"Changed {noun} {label}"
    return f"{label}: " + ", ".join(said)


HEADLINES: dict[BatchKind, str] = {
    BatchKind.imported: "Statement import",
    BatchKind.manual: "Edit",
    BatchKind.bulk_update: "Bulk edit",
    BatchKind.undo: "Undo",
    BatchKind.admin: "Setup change",
    BatchKind.setup: "First setup",
    BatchKind.seed: "Demo data",
    BatchKind.reconciled: "Reconciliation",
    BatchKind.split: "Split",
}


def detail_of(
    session: Session,
    household_id: str,
    changes: list[Change],
    *,
    names: _Names | None = None,
) -> list[ChangeDetail]:
    """Each of these changes, as far down as the log goes.

    The History list answers "what happened"; this answers "what exactly", for
    the times the sentence is not enough -- which is most of the times somebody
    opens an audit log at all. The caller chooses which changes: a batch's
    detail asks for a page of them, not the whole batch (#234).
    """
    names = names if names is not None else _Names(session, household_id)
    out: list[ChangeDetail] = []

    for change in changes:
        image = change.after or change.before or {}
        currency = names.currencies.get(str(image.get("account_id")), "EUR")

        # Only an update has a "from". On an insert every column differs from
        # nothing, so a from/to table would read "household id: nothing →
        # 4a05..." for all fifteen of them -- true, and noise. The whole row
        # goes in `snapshot` instead, which is what there is to say.
        fields = (
            [
                FieldChange(
                    field=FIELD_WORDS.get(column, column.replace("_", " ")),
                    was=names.value(column, was, currency=currency),
                    now=names.value(column, now, currency=currency),
                )
                for column, (was, now) in _changed_fields(change).items()
            ]
            if change.op is ChangeOp.update
            else []
        )

        # An insert has nothing to compare against and a delete has nothing
        # left, so for those the whole row is the interesting thing.
        snapshot: list[FieldChange] = []
        if change.op is not ChangeOp.update:
            snapshot = [
                FieldChange(
                    field=FIELD_WORDS.get(column, column.replace("_", " ")),
                    was="",
                    now=names.value(column, value, currency=currency),
                )
                for column, value in sorted(image.items())
                if column not in NOISE and value not in (None, "")
            ]

        out.append(
            ChangeDetail(
                seq=change.seq,
                table=NOUNS.get(change.table_name, (change.table_name, change.table_name))[0],
                row_id=change.row_id,
                op=change.op.value,
                summary=_one_line(change, names),
                fields=fields,
                snapshot=snapshot,
                redacted=list(change.redacted or []),
            )
        )
    return out


def lines_of(changes: list[Change], names: _Names) -> list[str]:
    """One sentence per change, in the order given: the undo confirmation's lines."""
    return [_one_line(one, names) for one in changes]


def describe_changes(
    session: Session, household_id: str, changes: list[Change]
) -> dict[int, str]:
    """One sentence per change, keyed by `seq`.

    The row-history view of the same engine the History screen uses. Sharing it
    is the point: "what happened to this transaction" and "what happened to this
    household" are the same question asked at two scopes, and two ways of
    putting a change into words would eventually disagree about one.
    """
    names = _Names(session, household_id)
    origins = _split_origins(session, changes, names)
    return {
        change.seq: _one_line(change, names, origins.get(change.batch_id))
        for change in changes
    }


def _split_origins(
    session: Session, changes: list[Change], names: _Names
) -> dict[str, str]:
    """For each split batch in `changes`, the transaction it divided.

    The row history of a split *part* is filtered to that part's own id, so the
    sibling delete that holds the original is not in the list -- it belongs to
    a different row. One query per batch that needs it, and only for batches
    that are actually splits, so an ordinary edit costs nothing.
    """
    wanted = {
        change.batch_id
        for change in changes
        if change.op is ChangeOp.insert and change.table_name == "transactions"
    }
    if not wanted:
        return {}

    splits = set(
        session.execute(
            select(Batch.id).where(Batch.id.in_(wanted), Batch.kind == BatchKind.split)
        ).scalars()
    )
    if not splits:
        return {}

    out: dict[str, str] = {}
    for row in session.execute(
        select(Change).where(
            Change.batch_id.in_(splits),
            Change.table_name == "transactions",
            Change.op == ChangeOp.delete,
        )
    ).scalars():
        was = row.before or {}
        currency = names.currencies.get(str(was.get("account_id")), "EUR")
        payee = names.payees.get(str(was.get("payee_id")))
        amount = names.value("amount", was.get("amount"), currency=currency)
        subject = f"{payee} {amount}" if payee else amount
        out[row.batch_id] = f"{subject} on {was.get('date')}"
    return out


def names_for(session: Session, household_id: str) -> _Names:
    """The name lookups for one household, to share across many `describe`s."""
    return _Names(session, household_id)


class _Changes:
    """A batch's change rows, read only if a sentence actually needs them.

    An import's sentence needs how many there were and nothing else, and an
    undo's needs them only when the batch it reversed cannot be found. Reading
    three hundred full row images to count them was most of what the History
    list cost (issue #102).
    """

    def __init__(self, session: Session, batch_id: str, count: int | None) -> None:
        self._session, self._batch_id = session, batch_id
        self._count, self._rows = count, None
        self._tally: dict[str, dict[ChangeOp, int]] | None = None

    @classmethod
    def of(cls, rows: list[Change]) -> _Changes:
        """Rows already in hand."""
        known = cls(None, "", len(rows))  # type: ignore[arg-type]
        known._rows = rows
        return known

    def all(self) -> list[Change]:
        if self._rows is None:
            self._rows = list(
                self._session.execute(
                    select(Change).where(Change.batch_id == self._batch_id).order_by(Change.seq)
                ).scalars()
            )
        return self._rows

    def __len__(self) -> int:
        if self._rows is not None:
            return len(self._rows)
        if self._count is None:
            self._count = self._session.execute(
                select(func.count()).select_from(Change).where(Change.batch_id == self._batch_id)
            ).scalar_one()
        return self._count

    def tally(self) -> dict[str, dict[ChangeOp, int]]:
        """How many changes, per table and per op, tables in the order they first appear.

        What "300 transactions changed" is made of, from one grouped read
        rather than from 300 full row images (#234, #238).
        """
        if self._tally is None:
            tally: dict[str, dict[ChangeOp, int]] = {}
            if self._rows is not None:
                for one in self._rows:
                    ops = tally.setdefault(one.table_name, {})
                    ops[one.op] = ops.get(one.op, 0) + 1
            else:
                grouped = self._session.execute(
                    select(Change.table_name, Change.op, func.count(), func.min(Change.seq))
                    .where(Change.batch_id == self._batch_id)
                    .group_by(Change.table_name, Change.op)
                ).all()
                first: dict[str, int] = {}
                for table, _op, _count, seq in grouped:
                    first[table] = min(first.get(table, seq), seq)
                tally = {table: {} for table in sorted(first, key=first.__getitem__)}
                for table, op, count, _seq in grouped:
                    tally[table][op] = count
            self._tally = tally
        return self._tally

    def of_table(self, table: str) -> list[Change]:
        """The full change rows of one table only, in order."""
        if self._rows is not None:
            return [one for one in self._rows if one.table_name == table]
        return list(
            self._session.execute(
                select(Change)
                .where(Change.batch_id == self._batch_id, Change.table_name == table)
                .order_by(Change.seq)
            ).scalars()
        )


def describe(
    session: Session,
    batch: Batch,
    *,
    with_lines: bool = False,
    names: _Names | None = None,
    change_count: int | None = None,
) -> Described:
    """What this batch did, in words.

    `with_lines` builds a line per change, which is what the confirmation before
    an undo shows. The list screen does not need them and does not pay for them.

    `names` and `change_count` are for a caller describing many batches of one
    household: the lookups built once, and the counts from one grouped query.
    Neither changes a word of the result.
    """
    household_id = batch.household_id or ""
    names = names if names is not None else _Names(session, household_id)
    changes = _Changes(session, batch.id, change_count)

    headline = HEADLINES.get(batch.kind, batch.kind.value)
    if batch.kind is BatchKind.admin and _about_keys(changes.tally()):
        # `admin` is the right kind -- issuing a credential is administration --
        # but "Setup change" names none of it, and a key is exactly the act
        # somebody scrolling History for "who gave what access" is looking for.
        headline = "Agent key"
    elif batch.kind is BatchKind.admin and _accounts_file(batch):
        headline = "Account import"
    elif one_time_headline(batch):
        headline = one_time_headline(batch)
    described = Described(
        headline=headline,
        detail=_detail(batch, changes, names),
        actor=names.actor(batch.actor_id) if batch.actor_id else None,
        via=via_words(batch),
        lines=[_one_line(one, names) for one in changes.all()] if with_lines else [],
    )
    return described


def _agent_words(agent: object) -> str | None:
    if not isinstance(agent, dict):
        return None
    name, label = agent.get("name"), agent.get("label")
    if name and label:
        return f"{name} ({label})"
    return name or label or None


def _applied_by(batch: Batch, names: _Names) -> str:
    """The sentence "Applied by Bob.", when somebody other than the stager applied it (#213).

    The actor stays whoever staged the import -- the batch is theirs -- so the
    person who pressed Commit, or whose key did, is said in the sentence.
    """
    committed = (batch.source or {}).get("committed")
    user_id = committed.get("user_id") if isinstance(committed, dict) else None
    if not user_id or user_id == batch.actor_id:
        return ""
    who = names.actor(user_id)
    return f" Applied by {who}." if who else " Applied by somebody else."


def via_words(batch: Batch) -> str | None:
    """`via_name`, and the key that applied it when that was a different one.

    What History and a row's change log show. A key that commits an import a
    person -- or another key -- staged is named beside the stager rather than
    in place of them, because staging and applying are one batch (#213).
    """
    staged = via_name(batch)
    committed = (batch.source or {}).get("committed")
    applied = _agent_words(committed.get("agent")) if isinstance(committed, dict) else None
    if applied is None or applied == staged:
        return staged
    if staged is None:
        return f"{applied}, which applied it"
    return f"{staged}, applied via {applied}"


def via_name(batch: Batch) -> str | None:
    """The program behind a batch, in the words a person should see.

    From `source["agent"]`, which is a copy, and deliberately not from
    `agent_key_id`, which is the live link. Keys are swept thirty days after
    revocation and `agent_key_id` is SET NULL; the copy is what keeps History
    reading correctly afterwards.

    "Claude (receipt filer)" when the key said what was holding it, and just
    the label otherwise -- an agent that did not name itself still named its
    job, and the job is the more useful half.
    """
    return _agent_words((batch.source or {}).get("agent"))


def _detail(batch: Batch, lazy: _Changes | list[Change], names: _Names) -> str:
    """The one sentence. Specific when it can be, honest when it cannot."""
    if isinstance(lazy, list):
        lazy = _Changes.of(lazy)

    # These two need a count, or nothing, and never read the rows for it.
    if batch.kind in (BatchKind.imported, BatchKind.undo):
        if not len(lazy):
            return "Nothing was changed."
        if one_time_headline(batch):
            return _one_time_detail(batch, len(lazy))
        if batch.kind is BatchKind.imported:
            return _import_detail(batch, len(lazy), names) + _applied_by(batch, names)
        return _undo_detail(batch, lazy, names)

    # The rest read full rows only where the sentence quotes one: a bulk edit
    # of three hundred rows is said from its tally, not from three hundred
    # row images (#234, #238).
    if not len(lazy):
        return "Nothing was changed."
    if batch.kind is BatchKind.reconciled:
        return _reconcile_detail(lazy, names)
    if batch.kind is BatchKind.split:
        return _split_detail(lazy.all(), names)
    tally = lazy.tally()
    if _about_keys(tally):
        return _key_detail(lazy.all())
    if _accounts_file(batch):
        return _account_import_detail(batch, tally)

    # A transaction is the subject whenever there is one. Naming a new payee on
    # a row creates the payee in the same batch, so "add a coffee at a shop you
    # have not used before" arrives here as two changes and used to describe
    # itself as "1 payee, 1 transaction added" -- true, and about the wrong one
    # of the two. The payee is a side effect of the act, not the act.
    money = tally.get("transactions", {})
    if sum(money.values()) == 1:
        return _one_line(lazy.of_table("transactions")[0], names)
    if money:
        return _counted_tally({"transactions": money})

    if len(lazy) == 1:
        return _one_line(lazy.all()[0], names)

    return _counted_tally(tally)


#: What a one-time import's workflow is called in History, by how it read (#183).
ONE_TIME_HEADLINES: dict[str, str] = {
    "one-time-import:ynab-csv": "One-time Import · YNAB (CSV)",
    "one-time-import:ynab-api": "One-time Import · YNAB (API)",
}


def one_time_headline(batch: Batch) -> str | None:
    """"One-time Import · YNAB (CSV)", for a batch that was one; None otherwise."""
    source = batch.source or {}
    if batch.kind is not BatchKind.imported or source.get("one_time_import") != "ynab":
        return None
    return ONE_TIME_HEADLINES.get(f"one-time-import:ynab-{source.get('via')}")


def _one_time_detail(batch: Batch, change_count: int) -> str:
    """"412 transactions from Budget as of ... - Register.csv." -- the rows, not the side effects."""
    source = batch.source or {}
    summary = batch.summary or {}
    imported = summary.get("imported")
    where = source.get("filename") or source.get("plan_name")
    what = (
        f"{imported} transaction{'' if imported == 1 else 's'}"
        if isinstance(imported, int)
        else f"{change_count} change{'' if change_count == 1 else 's'}"
    )
    linked = summary.get("transfers_linked")
    extra = f", {linked} transfer{'' if linked == 1 else 's'} linked" if linked else ""
    return f"{what}{extra}" + (f" from {where}." if where else ".")


def _accounts_file(batch: Batch) -> str | None:
    """The file an account import read, which is what marks one (#146)."""
    return (batch.source or {}).get("accounts_file") if batch.kind is BatchKind.admin else None


def _account_import_detail(batch: Batch, tally: dict[str, dict[ChangeOp, int]]) -> str:
    """"3 accounts from accounts.csv." -- the accounts, not their side effects.

    Each account with an opening balance brings a transaction, an IBAN an
    identifier, the first balance a payee; counted, that reads "3 accounts,
    2 transactions, 2 account identifiers, 1 payee added", which is true and
    buries the act. The count comes from the change rows, not the summary, so
    it cannot say more accounts than the log holds.
    """
    count = tally.get("accounts", {}).get(ChangeOp.insert, 0)
    return f"{count} account{'' if count == 1 else 's'} from {_accounts_file(batch)}."


def _about_keys(tally: dict[str, dict[ChangeOp, int]]) -> bool:
    """Every change in the batch was to an agent key -- read off the tally."""
    return set(tally) == {"agent_keys"}


def _key_detail(changes: list[Change]) -> str:
    """"Issued the key “receipt filer”" / "Revoked the key “receipt filer”"."""
    said = []
    for one in changes:
        image = one.after or one.before or {}
        label = f"the key “{image.get('label') or '?'}”"
        if one.op is ChangeOp.insert:
            said.append(f"Issued {label}")
        elif one.op is ChangeOp.delete:
            said.append(f"Removed {label}")
        elif (one.before or {}).get("revoked_at") is None and image.get("revoked_at"):
            said.append(f"Revoked {label}")
        else:
            said.append(f"Changed {label}")
    return "; ".join(said) + "."


def _counted_tally(tally: dict[str, dict[ChangeOp, int]]) -> str:
    """"12 transactions changed" -- for acts too big to spell out.

    From a tally, per table and op, so saying it needs no row image.
    """
    by_table = {table: sum(ops.values()) for table, ops in tally.items()}

    parts = []
    for table, count in sorted(by_table.items(), key=lambda pair: -pair[1]):
        singular, plural = NOUNS.get(table, (table, table))
        parts.append(f"{count} {singular if count == 1 else plural}")

    ops = {op for per_table in tally.values() for op in per_table}
    verb = "changed"
    if ops == {ChangeOp.insert}:
        verb = "added"
    elif ops == {ChangeOp.delete}:
        verb = "removed"
    return f"{', '.join(parts)} {verb}."


def _undo_detail(batch: Batch, changes: _Changes, names: _Names) -> str:
    """An undo's subject is the act it reversed, not the rows it touched.

    "4 transactions changed" is technically what happened and tells you nothing
    about why. The batch it reversed is reachable: that one carries
    `undone_by_id` pointing here.
    """
    session = _session_of(batch)
    reversed_batch = None
    if session is not None:
        reversed_batch = session.execute(
            select(Batch).where(Batch.undone_by_id == batch.id)
        ).scalar_one_or_none()

    if reversed_batch is None:
        return f"Put back {_counted_tally(changes.tally()).rstrip('.')}."

    inner = describe(session, reversed_batch, names=names)  # type: ignore[arg-type]
    return f"Reversed: {inner.headline.lower()} — {inner.detail}"


def _session_of(instance: Batch) -> Session | None:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(instance).session


def _import_detail(batch: Batch, change_count: int, names: _Names) -> str:
    source = batch.source or {}
    summary = batch.summary or {}
    created = summary.get("created", 0)
    absorbed = summary.get("matched_existing", 0)

    account = names.accounts.get(str(source.get("account_id")), "an account")
    filename = source.get("filename")

    made = []
    if created:
        made.append(f"{created} new transaction{'' if created == 1 else 's'}")
    if absorbed:
        made.append(f"{absorbed} matched to rows already there")
    if not made:
        made.append(f"{change_count} change{'' if change_count == 1 else 's'}")

    where = f" into {account}"
    what = f" from {filename}" if filename else ""
    return f"{', '.join(made)}{where}{what}."


def _split_detail(changes: list[Change], names: _Names) -> str:
    """Which transaction was divided, and into what.

    A split is the one act whose subject no longer exists when you read about
    it. The original is *replaced* -- `services/transactions.split` deletes it
    and inserts the parts -- so the generic sentence for a multi-row batch said
    "3 transactions changed", which is true, names nothing, and is useless to
    somebody scrolling History for the purchase they divided last week. The row
    they are looking for is the one the log recorded as a delete.

    So read the shape rather than counting it: the delete is the original, the
    inserts are the parts. Both images are complete rows, which is exactly what
    makes this reconstructable at all.
    """
    original = next(
        (one for one in changes
         if one.table_name == "transactions" and one.op is ChangeOp.delete and one.before),
        None,
    )
    parts = [
        one for one in changes
        if one.table_name == "transactions" and one.op is ChangeOp.insert and one.after
    ]

    if original is None or not parts:
        # A shape this function does not recognise. Say what is certain rather
        # than guessing at a sentence -- an audit log that invents detail is
        # worse than one that is brief.
        touched = sum(1 for one in changes if one.table_name == "transactions")
        return f"Split — {touched} transaction{'' if touched == 1 else 's'} changed."

    was = original.before or {}
    account_id = str(was.get("account_id"))
    currency = names.currencies.get(account_id, "EUR")

    payee = names.payees.get(str(was.get("payee_id")))
    amount = names.value("amount", was.get("amount"), currency=currency)
    when = was.get("date")

    # The parts in the order the log wrote them, which is the order they were
    # entered -- so the sentence reads the way the panel looked.
    pieces = []
    for one in parts:
        image = one.after or {}
        figure = names.value("amount", image.get("amount"), currency=currency)
        category = names.categories.get(str(image.get("category_id")))
        pieces.append(f"{figure} ({category})" if category else figure)

    subject = f"{payee} {amount}" if payee else amount
    return (
        f"{subject} on {when} split into {len(parts)}: " + ", ".join(pieces) + "."
    )


def _reconcile_detail(changes: _Changes, names: _Names) -> str:
    record = next((one for one in changes.of_table("reconciliations") if one.after), None)
    locked = sum(changes.tally().get("transactions", {}).values())

    if record is None:
        return f"{locked} transaction{'' if locked == 1 else 's'} locked."

    image = record.after or {}
    account_id = str(image.get("account_id"))
    account = names.accounts.get(account_id, "an account")
    currency = names.currencies.get(account_id, "EUR")
    balance = names.value("statement_balance", image.get("statement_balance"), currency=currency)
    when = image.get("statement_date")

    return (
        f"{account} proved against a statement closing {when} at {balance} — "
        f"{locked} transaction{'' if locked == 1 else 's'} locked."
    )
