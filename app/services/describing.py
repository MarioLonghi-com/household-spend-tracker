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

**Every sentence is built as structure first** (#266): a *phrase* -- a key
from `SENTENCES` and its params -- whose params are raw values, never words
already put into English: money as integer minor units beside its currency, a
date as ISO, an enum as its value, a name as stored, and a word this module
supplies ("uncategorised", "a payee since removed") as a key into one of the
`WORDS` sets. The English every response has always carried is that structure
rendered by :func:`english`, so the two cannot disagree; the structure goes
out beside it for a client that words it in another language
(`client/src/lib/historyWords.ts`, held to the templates here by
`tests/test_describing.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date

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


# --------------------------------------------------------------------------- #
# Sentences as structure (#266)
# --------------------------------------------------------------------------- #
#
# A *phrase* is ``{"key": <a SENTENCES key>, "params": {...}}``. A param is a
# raw scalar (a count), a *value* (below), another phrase, or a *list* of
# those. A value is ``{"type": ...}``:
#
#   money     {"amount": <minor units>, "currency": "EUR"}
#   date      {"value": "2026-09-30"}, and "style": "medium" where the English
#             says "30 Sep 2026" rather than the ISO date
#   name      {"value": <as stored>}: a payee's, account's, person's or key's
#             own name, which is data and never translated
#   category  {"group": <name>, "name": <name>}
#   number    {"value": <int>}
#   text      {"value": <str>}: a stored value with no better reading
#   enum      {"column": <column>, "value": <the enum's value>}
#   word      {"set": <a WORDS set>, "key": <key>}: a word this module
#             supplies; "lower": true where the English lower-cases it
#   count     {"table": <table>, "count": <int>}: "3 transactions"
#   list      {"items": [...], "sep": ", "}: joined as the English joins them
#
# `english` renders any of them. A client in another language renders the same
# structure from its own catalog.

#: Every History sentence's English, by key, in ICU MessageFormat -- the syntax
#: the client's catalogs are written in. A plural says ``#`` for its number.
SENTENCES: dict[str, str] = {
    # One change, one line: the undo confirmation's lines, a row's history, a
    # changed row's summary, and a batch's detail when it changed one row.
    "history.line.added": "Added {table} {label}",
    "history.line.added_by_splitting": "Added {table} {label} by splitting {origin}",
    "history.line.added_by_workflow": "Added {table} {label} by {workflow}",
    "history.line.removed": "Removed {table} {label}",
    "history.line.touched": "Touched {table} {label}",
    "history.line.changed": "Changed {table} {label}",
    "history.line.fields": "{label}: {fields}",
    "history.field_change": "{field} {was} → {now}",
    "history.label.in_account": "in {account}",
    "history.payment": "{amount} into {account} on {date}",
    "history.split.origin": "{amount} on {date}",
    "history.split.origin_with_payee": "{payee} {amount} on {date}",
    "history.split.part_with_category": "{amount} ({category})",
    # A batch's detail.
    "history.detail.nothing": "Nothing was changed.",
    "history.detail.tally": "{tally}.",
    "history.tally.added": "{counts} added",
    "history.tally.removed": "{counts} removed",
    "history.tally.changed": "{counts} changed",
    "history.detail.put_back": "Put back {tally}.",
    "history.detail.reversed": "Reversed: {headline} — {detail}",
    "history.detail.import": "{made} into {account} from {filename}.",
    "history.detail.import_no_file": "{made} into {account}.",
    "history.import.created": "{count, plural, one {# new transaction} other {# new transactions}}",
    "history.import.matched": (
        "{count, plural, one {# matched to rows already there} other {# matched to rows already there}}"
    ),
    "history.import.changes": "{count, plural, one {# change} other {# changes}}",
    "history.detail.applied_by": "{sentence} Applied by {who}.",
    "history.detail.applied_by_somebody": "{sentence} Applied by somebody else.",
    "history.detail.one_time.transactions": (
        "{imported, plural, one {# transaction} other {# transactions}}"
        "{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}}."
    ),
    "history.detail.one_time.transactions_from": (
        "{imported, plural, one {# transaction} other {# transactions}}"
        "{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}} from {where}."
    ),
    "history.detail.one_time.changes": (
        "{changes, plural, one {# change} other {# changes}}"
        "{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}}."
    ),
    "history.detail.one_time.changes_from": (
        "{changes, plural, one {# change} other {# changes}}"
        "{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}} from {where}."
    ),
    "history.detail.account_import": "{count, plural, one {# account} other {# accounts}} from {file}.",
    "history.detail.keys": "{acts}.",
    "history.key.issued": "Issued the key “{label}”",
    "history.key.removed": "Removed the key “{label}”",
    "history.key.revoked": "Revoked the key “{label}”",
    "history.key.changed": "Changed the key “{label}”",
    "history.detail.locked": "{count, plural, one {# transaction} other {# transactions}} locked.",
    "history.detail.reconciled": (
        "{account} proved against a statement closing {date} at {balance} — "
        "{count, plural, one {# transaction} other {# transactions}} locked."
    ),
    "history.detail.split_unknown": (
        "Split — {count, plural, one {# transaction} other {# transactions}} changed."
    ),
    "history.detail.split": "{amount} on {date} split into {count}: {parts}.",
    "history.detail.split_with_payee": "{payee} {amount} on {date} split into {count}: {parts}.",
}

#: What an empty value is called, per field. "nothing" is true and useless:
#: a row with no category is uncategorised, which is a state somebody
#: recognises from the register.
EMPTY: dict[str, str] = {
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

#: The words this module supplies, by set and key. `table`, `field` and
#: `headline` are the dictionaries above and below; these are the rest.
WORDS: dict[str, dict[str, str]] = {
    "empty": {**EMPTY, "": "nothing"},
    "removed": {
        "payee": "a payee since removed",
        "category": "a category since removed",
        "account": "an account since removed",
        "transaction": "a transaction since removed",
    },
    "fallback": {"account": "an account"},
    "bool": {"yes": "yes", "no": "no"},
    "unknown": {"": "?"},
}

#: The enum values History words, by column. The English is the value's own
#: spelling, except where that does not belong in a sentence; any other enum
#: goes out as its value and reads as one.
ENUM_WORDS: dict[str, dict[str, str]] = {
    "reimbursement": {"expected": "expected", "written_off": "written off"},
    "cleared": {"uncleared": "uncleared", "cleared": "cleared", "reconciled": "reconciled"},
}

#: Columns holding an enum's value: sent as one, for the client to word.
ENUM_COLUMNS = frozenset(
    {"cleared", "reimbursement", "type", "role", "categorisation", "theme", "status", "kind", "system"}
)

#: Columns holding money, read in the row's account's currency.
MONEY_COLUMNS = frozenset({"amount", "statement_balance", "opening_balance"})


def phrase(key: str, **params: object) -> dict:
    assert key in SENTENCES, key
    return {"key": key, "params": params}


def _word(word_set: str, key: str, **extra: object) -> dict:
    return {"type": "word", "set": word_set, "key": key, **extra}


def _name(value: object) -> dict:
    return {"type": "name", "value": str(value)}


def _date(raw: object, **extra: object) -> dict:
    """A date out of a row image, which holds it as ISO text."""
    if isinstance(raw, Date):
        raw = raw.isoformat()
    if isinstance(raw, str):
        return {"type": "date", "value": raw, **extra}
    return {"type": "text", "value": str(raw)}


def _money(amount: object, currency: str) -> dict:
    try:
        return {"type": "money", "amount": int(amount), "currency": currency}  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"type": "text", "value": str(amount)}


def _list(items: list, sep: str = ", ") -> dict:
    return {"type": "list", "items": items, "sep": sep}


def _headline_word(key: str) -> str:
    kinds = {kind.value: words for kind, words in HEADLINES.items()}
    return SPECIAL_HEADLINES.get(key) or ONE_TIME_HEADLINES.get(key) or kinds.get(key, key)


def english(node: object) -> str:
    """A phrase, a value or a raw param, in the English History has always said."""
    if isinstance(node, dict) and "params" in node:
        params = {name: english(value) for name, value in node["params"].items()}
        raw = {name: value for name, value in node["params"].items() if not isinstance(value, dict)}
        return _format(SENTENCES[node["key"]], params, raw)
    if not isinstance(node, dict):
        return str(node)
    kind = node["type"]
    if kind == "money":
        return format_amount(node["amount"], node["currency"])
    if kind == "date":
        if node.get("style") == "medium":
            day = Date.fromisoformat(node["value"])
            return f"{day.day} {day.strftime('%b %Y')}"
        return node["value"]
    if kind in ("name", "text"):
        return node["value"]
    if kind == "number":
        return str(node["value"])
    if kind == "category":
        return f"{node['group']}: {node['name']}"
    if kind == "enum":
        return ENUM_WORDS.get(node["column"], {}).get(node["value"], node["value"])
    if kind == "count":
        singular, plural = NOUNS.get(node["table"], (node["table"], node["table"]))
        return f"{node['count']} {singular if node['count'] == 1 else plural}"
    if kind == "list":
        return node["sep"].join(english(one) for one in node["items"])
    if kind == "word":
        word_set, key = node["set"], node["key"]
        if word_set == "table":
            said = NOUNS.get(key, (key, key))[0]
        elif word_set == "field":
            said = FIELD_WORDS.get(key, key.replace("_", " "))
        elif word_set == "headline":
            said = _headline_word(key)
        elif word_set == "empty":
            said = EMPTY.get(key, "nothing")
        else:
            said = WORDS[word_set][key]
        return said.lower() if node.get("lower") else said
    raise ValueError(f"not a History value: {node!r}")


_PLACEHOLDER = re.compile(r"\{(\w+)(?:, plural, )?")


def _format(template: str, said: dict[str, str], raw: dict[str, object]) -> str:
    """Enough ICU MessageFormat for `SENTENCES`: ``{name}`` and plurals."""
    out, at = [], 0
    while True:
        start = template.find("{", at)
        if start < 0:
            out.append(template[at:])
            return "".join(out)
        out.append(template[at:start])
        end = _closing(template, start)
        body = template[start + 1 : end]
        name, _, rest = body.partition(",")
        name = name.strip()
        if not rest:
            out.append(said[name])
        else:
            options = rest.split(",", 1)[1]
            count = raw[name]
            chosen = _plural_option(options, count)  # type: ignore[arg-type]
            out.append(_format(chosen.replace("#", str(count)), said, raw))
        at = end + 1


def _closing(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unbalanced braces in {text!r}")


def _plural_option(options: str, count: int) -> str:
    found: dict[str, str] = {}
    at = 0
    while True:
        start = options.find("{", at)
        if start < 0:
            break
        selector = options[at:start].strip()
        end = _closing(options, start)
        found[selector] = options[start + 1 : end]
        at = end + 1
    if f"={count}" in found:
        return found[f"={count}"]
    if count == 1 and "one" in found:
        return found["one"]
    return found["other"]


@dataclass(slots=True)
class FieldChange:
    """One column that moved, with both sides said in words."""

    field: str
    was: str
    now: str
    #: The column itself, for a client that words `field` in another language
    #: (#57). `field` stays the English it always was.
    column: str = ""
    #: `was` and `now` as values rather than words (#266): money in minor
    #: units with its currency, a date as ISO, an enum as its value.
    was_value: dict | None = None
    now_value: dict | None = None


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
    #: The table itself, for a client that words `table` in another language.
    table_key: str = ""
    #: `summary` as a phrase (#266).
    summary_phrase: dict | None = None


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
    #: Which headline it is, as a key a client words in its own language
    #: (#57): a `BatchKind` value, or one of `HEADLINE_KEYS`' special ones.
    #: `headline` stays the English it always was.
    headline_key: str = ""
    #: `detail` and `lines` as phrases, for the same client (#266).
    detail_phrase: dict | None = None
    line_phrases: list[dict] = field(default_factory=list)


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
        self._payments: dict[str, dict] = {}
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
        #: category id -> (group name, category name).
        self.category_parts = {
            row[0]: (row[1], row[2])
            for row in session.execute(
                select(Category.id, CategoryGroup.name, Category.name)
                .join(CategoryGroup, CategoryGroup.id == Category.group_id)
                .where(Category.household_id == household_id)
            ).all()
        }
        self.categories = {
            key: f"{group}: {name}" for key, (group, name) in self.category_parts.items()
        }

    def actor(self, user_id: str) -> str | None:
        """Who did it, by name -- read once per person, not once per batch."""
        if user_id not in self._actors:
            user = self._session.get(User, user_id)
            self._actors[user_id] = user.display_name if user else None
        return self._actors[user_id]

    #: Kept under its old name for readers of this class.
    EMPTY = EMPTY
    WORDS = ENUM_WORDS

    def category(self, category_id: object) -> dict | None:
        found = self.category_parts.get(str(category_id))
        if found is None:
            return None
        return {"type": "category", "group": found[0], "name": found[1]}

    def account(self, account_id: object, *, missing: dict) -> dict:
        found = self.accounts.get(str(account_id))
        return _name(found) if found is not None else missing

    def payment(self, transaction_id: str) -> dict:
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
                self._payments[transaction_id] = _word("removed", "transaction")
            else:
                currency = self.currencies.get(txn.account_id, "EUR")
                self._payments[transaction_id] = phrase(
                    "history.payment",
                    amount=_money(txn.amount, currency),
                    account=self.account(txn.account_id, missing=_word("removed", "account")),
                    date=_date(txn.date, style="medium"),
                )
        return self._payments[transaction_id]

    def node(self, column: str, raw: object, *, currency: str = "EUR") -> dict:
        """One field's value, as structure."""
        if raw is None or raw == "":
            return _word("empty", column)
        if column == "payee_id":
            found = self.payees.get(str(raw))
            return _name(found) if found is not None else _word("removed", "payee")
        if column in ("category_id", "default_category_id"):
            return self.category(raw) or _word("removed", "category")
        if column == "account_id":
            return self.account(raw, missing=_word("removed", "account"))
        if column == "reimbursed_by_id":
            return self.payment(str(raw))
        if column in MONEY_COLUMNS:
            return _money(raw, currency)
        if isinstance(raw, bool):
            return _word("bool", "yes" if raw else "no")
        if column in ENUM_COLUMNS and isinstance(raw, str):
            return {"type": "enum", "column": column, "value": raw}
        if (column == "date" or column.endswith("_date")) and isinstance(raw, str):
            return _date(raw)
        if isinstance(raw, int):
            return {"type": "number", "value": raw}
        return {"type": "text", "value": str(raw)}

    def value(self, column: str, raw: object, *, currency: str = "EUR") -> str:
        """One field's value, as it should read in a sentence."""
        return english(self.node(column, raw, currency=currency))


def _row_label(change: Change, names: _Names) -> dict:
    """What the changed row *is*, so a sentence has a subject.

    Taken from whichever image exists: an insert has no before, a delete has no
    after, and the row itself is gone by the time anyone reads this.
    """
    image = change.after or change.before or {}
    if change.table_name == "transactions":
        currency = names.currencies.get(str(image.get("account_id")), "EUR")
        parts: list[dict] = [names.node("amount", image.get("amount"), currency=currency)]
        payee = image.get("payee_id")
        if payee:
            found = names.payees.get(str(payee))
            parts.append(_name(found) if found is not None else _word("unknown", ""))
        account = names.accounts.get(str(image.get("account_id")))
        if account:
            parts.append(phrase("history.label.in_account", account=_name(account)))
        return _list(parts, " · ")
    if name := image.get("name"):
        return _name(name)
    return _word("table", change.table_name)


def _changed_fields(change: Change) -> dict[str, tuple[object, object]]:
    before, after = change.before or {}, change.after or {}
    return {
        key: (before.get(key), value)
        for key, value in after.items()
        if key not in NOISE and before.get(key) != value
    }


def _line(change: Change, names: _Names, origin: dict | None = None) -> dict:
    """One change, as a phrase. `origin` names where an inserted row came from.

    A split part is an ordinary insert in the log, so its own history opened
    with a bare "Added transaction" -- which is the one thing it was not. It
    arrived because something else was divided, and that something else no
    longer exists to be looked up, so the sentence has to carry it.
    """
    table = _word("table", change.table_name)
    label = _row_label(change, names)

    if change.op is ChangeOp.insert:
        if origin:
            return phrase("history.line.added_by_splitting", table=table, label=label, origin=origin)
        # A row's own history is keyed on the row, not the batch, so the
        # workflow it arrived by has to be in the sentence (#183). The image
        # carries it: `import_source` is written on every such row.
        workflow = str((change.after or {}).get("import_source"))
        if workflow in ONE_TIME_HEADLINES and change.table_name == "transactions":
            return phrase(
                "history.line.added_by_workflow",
                table=table,
                label=label,
                workflow=_word("headline", workflow),
            )
        return phrase("history.line.added", table=table, label=label)
    if change.op is ChangeOp.delete:
        return phrase("history.line.removed", table=table, label=label)

    fields = _changed_fields(change)
    if not fields:
        return phrase("history.line.touched", table=table, label=label)

    image = change.after or {}
    currency = names.currencies.get(str(image.get("account_id")), "EUR")
    said = []
    for column, (was, now) in fields.items():
        if column not in FIELD_WORDS:
            continue
        said.append(
            phrase(
                "history.field_change",
                field=_word("field", column),
                was=names.node(column, was, currency=currency),
                now=names.node(column, now, currency=currency),
            )
        )
    if not said:
        return phrase("history.line.changed", table=table, label=label)
    return phrase("history.line.fields", label=label, fields=_list(said))


def _one_line(change: Change, names: _Names, origin: dict | None = None) -> str:
    """One change, in words."""
    return english(_line(change, names, origin))


#: The headlines that are not a batch kind's own, by key (#57).
SPECIAL_HEADLINES: dict[str, str] = {
    "agent_key": "Agent key",
    "account_import": "Account import",
}

HEADLINES: dict[BatchKind, str] = {
    BatchKind.imported: "Statement import",
    BatchKind.manual: "Edit",
    BatchKind.bulk_update: "Bulk edit",
    BatchKind.bulk_delete: "Bulk delete",
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

    def one_field(column: str, was: object, now: object, currency: str, *, update: bool) -> FieldChange:
        was_node = names.node(column, was, currency=currency) if update else None
        now_node = names.node(column, now, currency=currency)
        return FieldChange(
            field=FIELD_WORDS.get(column, column.replace("_", " ")),
            was=english(was_node) if was_node is not None else "",
            now=english(now_node),
            column=column,
            was_value=was_node,
            now_value=now_node,
        )

    for change in changes:
        image = change.after or change.before or {}
        currency = names.currencies.get(str(image.get("account_id")), "EUR")

        # Only an update has a "from". On an insert every column differs from
        # nothing, so a from/to table would read "household id: nothing →
        # 4a05..." for all fifteen of them -- true, and noise. The whole row
        # goes in `snapshot` instead, which is what there is to say.
        fields = (
            [
                one_field(column, was, now, currency, update=True)
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
                one_field(column, None, value, currency, update=False)
                for column, value in sorted(image.items())
                if column not in NOISE and value not in (None, "")
            ]

        summary = _line(change, names)
        out.append(
            ChangeDetail(
                seq=change.seq,
                table=NOUNS.get(change.table_name, (change.table_name, change.table_name))[0],
                row_id=change.row_id,
                op=change.op.value,
                summary=english(summary),
                fields=fields,
                snapshot=snapshot,
                redacted=list(change.redacted or []),
                table_key=change.table_name,
                summary_phrase=summary,
            )
        )
    return out


def line_phrases_of(changes: list[Change], names: _Names) -> list[dict]:
    """One phrase per change, in the order given: the undo confirmation's lines."""
    return [_line(one, names) for one in changes]


def lines_of(changes: list[Change], names: _Names) -> list[str]:
    """One sentence per change, in the order given: the undo confirmation's lines."""
    return [english(one) for one in line_phrases_of(changes, names)]


def describe_change_phrases(
    session: Session, household_id: str, changes: list[Change]
) -> dict[int, dict]:
    """One phrase per change, keyed by `seq`. See `describe_changes`."""
    names = _Names(session, household_id)
    origins = _split_origins(session, changes, names)
    return {
        change.seq: _line(change, names, origins.get(change.batch_id))
        for change in changes
    }


def describe_changes(
    session: Session, household_id: str, changes: list[Change]
) -> dict[int, str]:
    """One sentence per change, keyed by `seq`.

    The row-history view of the same engine the History screen uses. Sharing it
    is the point: "what happened to this transaction" and "what happened to this
    household" are the same question asked at two scopes, and two ways of
    putting a change into words would eventually disagree about one.
    """
    return {
        seq: english(said)
        for seq, said in describe_change_phrases(session, household_id, changes).items()
    }


def _subject(
    names: _Names, was: dict, key: str, key_with_payee: str, **params: object
) -> dict:
    """"Tesco €12.00 on 2026-03-01 ..." -- the payee when there is one, then the amount."""
    currency = names.currencies.get(str(was.get("account_id")), "EUR")
    payee = names.payees.get(str(was.get("payee_id")))
    amount = names.node("amount", was.get("amount"), currency=currency)
    when = _date(was.get("date"))
    if payee:
        return phrase(key_with_payee, payee=_name(payee), amount=amount, date=when, **params)
    return phrase(key, amount=amount, date=when, **params)


def _split_origins(
    session: Session, changes: list[Change], names: _Names
) -> dict[str, dict]:
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

    out: dict[str, dict] = {}
    for row in session.execute(
        select(Change).where(
            Change.batch_id.in_(splits),
            Change.table_name == "transactions",
            Change.op == ChangeOp.delete,
        )
    ).scalars():
        out[row.batch_id] = _subject(
            names, row.before or {}, "history.split.origin", "history.split.origin_with_payee"
        )
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

    key = batch.kind.value
    if batch.kind is BatchKind.admin and _about_keys(changes.tally()):
        # `admin` is the right kind -- issuing a credential is administration --
        # but "Setup change" names none of it, and a key is exactly the act
        # somebody scrolling History for "who gave what access" is looking for.
        key = "agent_key"
    elif batch.kind is BatchKind.admin and _accounts_file(batch):
        key = "account_import"
    elif one_time_headline(batch):
        key = f"one-time-import:ynab-{(batch.source or {}).get('via')}"
    headline = (
        SPECIAL_HEADLINES.get(key)
        or ONE_TIME_HEADLINES.get(key)
        or HEADLINES.get(batch.kind, batch.kind.value)
    )
    detail = _detail_phrase(batch, changes, names)
    lines = [_line(one, names) for one in changes.all()] if with_lines else []
    described = Described(
        headline=headline,
        detail=english(detail),
        actor=names.actor(batch.actor_id) if batch.actor_id else None,
        via=via_words(batch),
        lines=[english(one) for one in lines],
        headline_key=key,
        detail_phrase=detail,
        line_phrases=lines,
    )
    return described


def _agent_words(agent: object) -> str | None:
    if not isinstance(agent, dict):
        return None
    name, label = agent.get("name"), agent.get("label")
    if name and label:
        return f"{name} ({label})"
    return name or label or None


def _applied_by(batch: Batch, names: _Names, sentence: dict) -> dict:
    """"... Applied by Bob.", when somebody other than the stager applied it (#213).

    The actor stays whoever staged the import -- the batch is theirs -- so the
    person who pressed Commit, or whose key did, is said in the sentence.
    """
    committed = (batch.source or {}).get("committed")
    user_id = committed.get("user_id") if isinstance(committed, dict) else None
    if not user_id or user_id == batch.actor_id:
        return sentence
    who = names.actor(user_id)
    if who:
        return phrase("history.detail.applied_by", sentence=sentence, who=_name(who))
    return phrase("history.detail.applied_by_somebody", sentence=sentence)


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
    return english(_detail_phrase(batch, lazy, names))


def _detail_phrase(batch: Batch, lazy: _Changes | list[Change], names: _Names) -> dict:
    """The one sentence, as a phrase."""
    if isinstance(lazy, list):
        lazy = _Changes.of(lazy)

    # These two need a count, or nothing, and never read the rows for it.
    if batch.kind in (BatchKind.imported, BatchKind.undo):
        if not len(lazy):
            return phrase("history.detail.nothing")
        if one_time_headline(batch):
            return _one_time_detail(batch, len(lazy))
        if batch.kind is BatchKind.imported:
            return _applied_by(batch, names, _import_detail(batch, len(lazy), names))
        return _undo_detail(batch, lazy, names)

    # The rest read full rows only where the sentence quotes one: a bulk edit
    # of three hundred rows is said from its tally, not from three hundred
    # row images (#234, #238).
    if not len(lazy):
        return phrase("history.detail.nothing")
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
        return _line(lazy.of_table("transactions")[0], names)
    if money:
        return phrase("history.detail.tally", tally=_counted_tally({"transactions": money}))

    if len(lazy) == 1:
        return _line(lazy.all()[0], names)

    return phrase("history.detail.tally", tally=_counted_tally(tally))


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


def _one_time_detail(batch: Batch, change_count: int) -> dict:
    """"412 transactions from Budget as of ... - Register.csv." -- the rows, not the side effects."""
    source = batch.source or {}
    summary = batch.summary or {}
    imported = summary.get("imported")
    where = source.get("filename") or source.get("plan_name")
    linked = summary.get("transfers_linked") or 0
    tail = "_from" if where else ""
    extra = {"where": _name(where)} if where else {}
    if isinstance(imported, int):
        return phrase(
            f"history.detail.one_time.transactions{tail}", imported=imported, linked=linked, **extra
        )
    return phrase(
        f"history.detail.one_time.changes{tail}", changes=change_count, linked=linked, **extra
    )


def _accounts_file(batch: Batch) -> str | None:
    """The file an account import read, which is what marks one (#146)."""
    return (batch.source or {}).get("accounts_file") if batch.kind is BatchKind.admin else None


def _account_import_detail(batch: Batch, tally: dict[str, dict[ChangeOp, int]]) -> dict:
    """"3 accounts from accounts.csv." -- the accounts, not their side effects.

    Each account with an opening balance brings a transaction, an IBAN an
    identifier, the first balance a payee; counted, that reads "3 accounts,
    2 transactions, 2 account identifiers, 1 payee added", which is true and
    buries the act. The count comes from the change rows, not the summary, so
    it cannot say more accounts than the log holds.
    """
    count = tally.get("accounts", {}).get(ChangeOp.insert, 0)
    return phrase(
        "history.detail.account_import", count=count, file=_name(_accounts_file(batch))
    )


def _about_keys(tally: dict[str, dict[ChangeOp, int]]) -> bool:
    """Every change in the batch was to an agent key -- read off the tally."""
    return set(tally) == {"agent_keys"}


def _key_detail(changes: list[Change]) -> dict:
    """"Issued the key “receipt filer”" / "Revoked the key “receipt filer”"."""
    said = []
    for one in changes:
        image = one.after or one.before or {}
        label = image.get("label")
        named = _name(label) if label else _word("unknown", "")
        if one.op is ChangeOp.insert:
            said.append(phrase("history.key.issued", label=named))
        elif one.op is ChangeOp.delete:
            said.append(phrase("history.key.removed", label=named))
        elif (one.before or {}).get("revoked_at") is None and image.get("revoked_at"):
            said.append(phrase("history.key.revoked", label=named))
        else:
            said.append(phrase("history.key.changed", label=named))
    return phrase("history.detail.keys", acts=_list(said, "; "))


def _counted_tally(tally: dict[str, dict[ChangeOp, int]]) -> dict:
    """"12 transactions changed" -- for acts too big to spell out.

    From a tally, per table and op, so saying it needs no row image.
    """
    by_table = {table: sum(ops.values()) for table, ops in tally.items()}

    counts = [
        {"type": "count", "table": table, "count": count}
        for table, count in sorted(by_table.items(), key=lambda pair: -pair[1])
    ]

    ops = {op for per_table in tally.values() for op in per_table}
    verb = "changed"
    if ops == {ChangeOp.insert}:
        verb = "added"
    elif ops == {ChangeOp.delete}:
        verb = "removed"
    return phrase(f"history.tally.{verb}", counts=_list(counts))


def _undo_detail(batch: Batch, changes: _Changes, names: _Names) -> dict:
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
        return phrase("history.detail.put_back", tally=_counted_tally(changes.tally()))

    inner = describe(session, reversed_batch, names=names)  # type: ignore[arg-type]
    return phrase(
        "history.detail.reversed",
        headline=_word("headline", inner.headline_key, lower=True),
        detail=inner.detail_phrase,
    )


def _session_of(instance: Batch) -> Session | None:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(instance).session


def _import_detail(batch: Batch, change_count: int, names: _Names) -> dict:
    source = batch.source or {}
    summary = batch.summary or {}
    created = summary.get("created", 0)
    absorbed = summary.get("matched_existing", 0)

    account = names.account(source.get("account_id"), missing=_word("fallback", "account"))
    filename = source.get("filename")

    made = []
    if created:
        made.append(phrase("history.import.created", count=created))
    if absorbed:
        made.append(phrase("history.import.matched", count=absorbed))
    if not made:
        made.append(phrase("history.import.changes", count=change_count))

    if filename:
        return phrase(
            "history.detail.import", made=_list(made), account=account, filename=_name(filename)
        )
    return phrase("history.detail.import_no_file", made=_list(made), account=account)


def _split_detail(changes: list[Change], names: _Names) -> dict:
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
        return phrase("history.detail.split_unknown", count=touched)

    was = original.before or {}
    currency = names.currencies.get(str(was.get("account_id")), "EUR")

    # The parts in the order the log wrote them, which is the order they were
    # entered -- so the sentence reads the way the panel looked.
    pieces = []
    for one in parts:
        image = one.after or {}
        figure = names.node("amount", image.get("amount"), currency=currency)
        category = names.category(image.get("category_id"))
        pieces.append(
            phrase("history.split.part_with_category", amount=figure, category=category)
            if category
            else figure
        )

    return _subject(
        names,
        was,
        "history.detail.split",
        "history.detail.split_with_payee",
        count=len(parts),
        parts=_list(pieces),
    )


def _reconcile_detail(changes: _Changes, names: _Names) -> dict:
    record = next((one for one in changes.of_table("reconciliations") if one.after), None)
    locked = sum(changes.tally().get("transactions", {}).values())

    if record is None:
        return phrase("history.detail.locked", count=locked)

    image = record.after or {}
    account_id = str(image.get("account_id"))
    currency = names.currencies.get(account_id, "EUR")
    return phrase(
        "history.detail.reconciled",
        account=names.account(account_id, missing=_word("fallback", "account")),
        date=_date(image.get("statement_date")),
        balance=names.node("statement_balance", image.get("statement_balance"), currency=currency),
        count=locked,
    )
