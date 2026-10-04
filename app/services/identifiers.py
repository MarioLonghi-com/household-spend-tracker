"""What banks call an account, and finding the account they meant.

Two questions are answered here, and both used to be answered by a person
reading a string (issue #66):

- **Which account is this file for?** Revolut puts a tag per account in its
  download's file name; an OFX file states its `ACCTID`; a Santander export
  names its IBAN in the preamble. `recognise_file` reads all three.
- **Which account does this descriptor name?** `TO A/C <number>`, `Desde Cuenta
  <card>`, `To Instant Access Savings`. `named_in` finds the household's
  identifiers inside a bank's text, which is what transfer matching needs.

Matching is deliberately conservative. A short number is found only as a whole
token, never as a substring, because four digits appear inside any reference
number; a word alias is found only as whole words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from statements import ofx, sniffing

from ..errors import Conflict, NotFound, ValidationError
from ..models import Account, AccountIdentifier, IdentifierKind, Payee, Transaction

#: Kinds compared as a run of letters and digits, spacing ignored.
_COMPACT = frozenset({IdentifierKind.iban, IdentifierKind.number, IdentifierKind.card, IdentifierKind.file_tag})

#: A compact identifier this long is specific enough to find inside a longer
#: run -- an IBAN in a file name, a card number glued to a word. Shorter ones
#: must stand as their own token.
_SUBSTRING_SAFE = 6

#: How much of a statement file is read looking for an account number.
_CONTENT_WINDOW = 64 * 1024


def normalise(kind: IdentifierKind, value: str) -> str:
    """The form matching compares. Stored, because uniqueness is about it."""
    text = (value or "").upper()
    if kind in _COMPACT:
        return re.sub(r"[^0-9A-Z]", "", text)
    return " ".join(re.sub(r"[^0-9A-Z]+", " ", text).split())


def _compact(text: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


def _words(text: str) -> str:
    return " " + " ".join(re.sub(r"[^0-9A-Z]+", " ", (text or "").upper()).split()) + " "


def _tokens(text: str) -> set[str]:
    return set(re.split(r"[^0-9A-Z]+", (text or "").upper())) - {""}


def list_for_household(session: Session, household_id: str) -> list[AccountIdentifier]:
    return list(
        session.execute(
            select(AccountIdentifier)
            .where(AccountIdentifier.household_id == household_id)
            .order_by(AccountIdentifier.kind, AccountIdentifier.value)
        ).scalars()
    )


def add(
    session: Session,
    *,
    household_id: str,
    kind: IdentifierKind | str,
    value: str,
    account: Account | None,
) -> AccountIdentifier:
    kind = IdentifierKind(kind)
    if kind is IdentifierKind.holder:
        if account is not None:
            raise ValidationError(
                "a holder's name belongs to the household, not to one account -- one person "
                "holds several"
            )
    elif account is None:
        raise ValidationError(f"a {kind.value} identifier says which account it is for")
    if account is not None and account.household_id != household_id:
        raise NotFound("no such account")

    normalised = normalise(kind, value)
    minimum = 4 if kind in _COMPACT else 3
    if len(normalised) < minimum:
        raise ValidationError(
            f"{value.strip()!r} is too short to recognise anything by -- at least {minimum} "
            "letters or digits"
        )
    clash = session.execute(
        select(AccountIdentifier).where(
            AccountIdentifier.household_id == household_id,
            AccountIdentifier.normalised == normalised,
        )
    ).scalar_one_or_none()
    if clash is not None:
        owner = session.get(Account, clash.account_id) if clash.account_id else None
        where = f"on {owner.name}" if owner else "as a household member's name"
        raise Conflict(f"{value.strip()!r} is already an identifier {where}")

    row = AccountIdentifier(
        household_id=household_id,
        account_id=account.id if account else None,
        kind=kind,
        value=value.strip(),
        normalised=normalised,
    )
    session.add(row)
    session.flush()
    return row


def get_for_household(session: Session, identifier_id: str, household_id: str) -> AccountIdentifier:
    row = session.get(AccountIdentifier, identifier_id)
    if row is None or row.household_id != household_id:
        raise NotFound("no such identifier")
    return row


def remove(session: Session, row: AccountIdentifier) -> None:
    session.delete(row)
    session.flush()


@dataclass(frozen=True, slots=True)
class _Text:
    """One piece of bank text, read every way a matcher needs it."""

    compact: str
    words: str
    tokens: set[str]
    #: The words in order, for the holder matcher.
    sequence: tuple[str, ...]
    #: Positions in `sequence` that end one of the texts given -- where a bank
    #: cuts a name off at its field width.
    ends: frozenset[int]

    @classmethod
    def read(cls, *texts: str | None) -> _Text:
        sequence: list[str] = []
        ends: set[int] = set()
        for text in texts:
            added = _words(text or "").split()
            sequence += added
            if added:
                ends.add(len(sequence) - 1)
        joined = " ".join(t for t in texts if t)
        return cls(
            _compact(joined), _words(joined), _tokens(joined), tuple(sequence), frozenset(ends)
        )


def _found_in(row: AccountIdentifier, text: _Text) -> bool:
    if row.kind in _COMPACT:
        if len(row.normalised) >= _SUBSTRING_SAFE:
            return row.normalised in text.compact
        return row.normalised in text.tokens
    if row.kind is IdentifierKind.holder:
        return holder_found_in(row.normalised.split(), text.sequence, text.ends)
    return f" {row.normalised} " in text.words


# --------------------------------------------------------------------------- #
# A person's name, as banks write it (issue #132)
# --------------------------------------------------------------------------- #

#: A word this long is specific enough to anchor a partial name ("surname
#: length"), and a cut-off word must keep at least this much to count.
_NAME_ANCHOR = 4

_FULL, _CUT, _INITIAL = "full", "cut", "initial"


def _cover(word: str, name_word: str, *, at_end: bool) -> str | None:
    """How one word of bank text can stand for one word of a holder's name."""
    if word == name_word:
        return _FULL
    if at_end and len(word) >= _NAME_ANCHOR and name_word.startswith(word):
        return _CUT
    if len(word) == 1 and name_word.startswith(word):
        return _INITIAL
    return None


def _assign(edges: list[list[int]], size: int) -> bool:
    """Whether every name word can be given its own text word (Kuhn's matching)."""
    taken: dict[int, int] = {}

    def place(name_index: int, seen: set[int]) -> bool:
        for position in edges[name_index]:
            if position in seen:
                continue
            seen.add(position)
            if position not in taken or place(taken[position], seen):
                taken[position] = name_index
                return True
        return False

    return all(place(index, set()) for index in range(size))


def _window_covers(name: list[str], window: range, words: tuple[str, ...], ends: frozenset[int]) -> bool:
    """Every name word stands on its own word of this window, at least one of them in full."""
    kinds = {
        (i, p): how
        for i, name_word in enumerate(name)
        for p in window
        if (how := _cover(words[p], name_word, at_end=p in ends)) is not None
    }
    for (i, p), how in kinds.items():
        if how != _FULL:
            continue
        rest = [j for j in range(len(name)) if j != i]
        edges = [[q for q in window if q != p and (j, q) in kinds] for j in rest]
        if _assign(edges, len(rest)):
            return True
    return False


def holder_found_in(name: list[str], words: tuple[str, ...], ends: frozenset[int] = frozenset()) -> bool:
    """Whether a household member's name is in this bank text, however it is written.

    One person's name reaches statements reordered, with middle names dropped,
    as initials, and cut off at the bank's field width. A holder matches when:

    - **it is one word**, and that word is in the text whole. Nothing looser:
      a four-letter first name is inside too many other words and texts.
    - **all its words** are in the text as whole words, in any order;
    - **all its words are covered by as many words next to each other**, in any
      order, where a single letter may stand for a word (an initial) and the
      last word of a text may be the start of one, four letters or more (cut
      off) -- as long as at least one word is there in full;
    - **it has three words or more**, and two of them stand next to each other
      in the text, whole or cut off, one of them a whole word of four letters
      or more. Initials do not count here: "J DOE" is not enough of a
      five-word name.

    Holders only ever make a transfer *suggested*, never strong
    (`transfers._evidence`), so looser matching cannot link anything by itself.
    """
    name = [w for w in name if w]
    if not name or not words:
        return False
    present = set(words)
    if len(name) == 1:
        return name[0] in present
    if not present.intersection(name):
        return False  # every form below needs one name word in full
    if all(w in present for w in name):
        return True

    size = len(name)
    for start in range(len(words) - size + 1):
        if _window_covers(name, range(start, start + size), words, ends):
            return True

    if size >= 3:
        for p in range(len(words) - 1):
            left, right = words[p], words[p + 1]
            for i, first in enumerate(name):
                a = _cover(left, first, at_end=p in ends)
                if a not in (_FULL, _CUT):
                    continue
                for j, second in enumerate(name):
                    if j == i:
                        continue
                    b = _cover(right, second, at_end=p + 1 in ends)
                    if b not in (_FULL, _CUT):
                        continue
                    if (a == _FULL and len(first) >= _NAME_ANCHOR) or (
                        b == _FULL and len(second) >= _NAME_ANCHOR
                    ):
                        return True
    return False


@dataclass(frozen=True, slots=True)
class Named:
    """One identifier found in a piece of bank text."""

    identifier: AccountIdentifier

    @property
    def account_id(self) -> str | None:
        return self.identifier.account_id

    @property
    def is_holder(self) -> bool:
        return self.identifier.kind is IdentifierKind.holder


def named_in(
    identifiers: list[AccountIdentifier], *texts: str | None, include_file_tags: bool = False
) -> list[Named]:
    """Every identifier this bank text mentions.

    Takes the list rather than a session, so a caller matching a whole
    statement reads the identifiers once, not once per row. File tags are left
    out unless asked for: they live in file names, and a six-character hex tag
    turning up in a descriptor is a coincidence, not evidence.
    """
    text = _Text.read(*texts)
    return [
        Named(row)
        for row in identifiers
        if (include_file_tags or row.kind is not IdentifierKind.file_tag)
        and _found_in(row, text)
    ]


@dataclass(slots=True)
class HolderSamples:
    """What one holder's name catches in the ledger, for a person to check."""

    identifier_id: str
    #: Rows whose bank text names this holder.
    rows: int = 0
    #: A few of those texts, most recent first, each once.
    samples: list[str] = field(default_factory=list)


def holder_samples(session: Session, household_id: str, *, limit: int = 5) -> list[HolderSamples]:
    """For each holder, the bank text it matches (issue #132).

    Looser matching is only safe if a person can see what it catches, so the
    Accounts screen shows this beside each name. Read-only. Each distinct text
    is matched once -- a ledger says the same few hundred things thousands of
    times -- and read the way transfer matching reads a row: the bank's words,
    then the payee, then the memo.
    """
    holders = [
        row for row in list_for_household(session, household_id) if row.kind is IdentifierKind.holder
    ]
    found = {row.id: HolderSamples(row.id) for row in holders}
    if not holders:
        return []
    texts = session.execute(
        select(
            Transaction.import_payee_original,
            Payee.name,
            Transaction.memo,
            func.count(),
            func.max(Transaction.date).label("latest"),
        )
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Transaction.household_id == household_id)
        .group_by(Transaction.import_payee_original, Payee.name, Transaction.memo)
        .order_by(func.max(Transaction.date).desc())
    ).all()
    for original, payee, memo, count, _ in texts:
        named = {n.identifier.id for n in named_in(holders, original, payee, memo)}
        if not named:
            continue
        shown = " · ".join(dict.fromkeys(t.strip() for t in (original, payee, memo) if t and t.strip()))
        for identifier_id in named:
            entry = found[identifier_id]
            entry.rows += count
            if len(entry.samples) < limit and shown not in entry.samples:
                entry.samples.append(shown)
    return [found[row.id] for row in holders]


@dataclass(frozen=True, slots=True)
class Recognised:
    account: Account
    #: In words, for the Import screen: "the file name carries its tag".
    how: str


@dataclass(frozen=True)
class FileEvidence:
    """What a statement file says about itself, read with no session.

    Split out of `recognise_file` for issue #84: reading an OFX file or
    unpacking a spreadsheet is CPU, and the route that asks this runs on the
    event loop the moment somebody picks a file. So the bytes are read here, on
    a worker thread, and the matching against the household's identifiers --
    which needs the session, and a session is not for sharing across threads --
    stays in the handler.
    """

    #: The account number an OFX file states, if it is one and states one.
    stated: str | None = None
    #: The first part of the file as text, or None when it cannot be read.
    head: str | None = None


def read_evidence(raw: bytes | None) -> FileEvidence:
    """Everything `recognise_file` needs from the bytes. Pure: bytes in, facts out."""
    if not raw:
        return FileEvidence()
    stated = ofx.read(raw).account if ofx.looks_like_ofx(raw) else None
    # A spreadsheet or a PDF has to be unpacked whole to be read at all;
    # text is read from its head, where the preamble names the account.
    sample = raw if _is_binary(raw) else raw[:_CONTENT_WINDOW]
    try:
        text, _ = sniffing.decode(sniffing.as_table_bytes(sample))
    except Exception:  # an unreadable file is the importer's to refuse, not this
        return FileEvidence(stated=stated)
    return FileEvidence(stated=stated, head=text[:_CONTENT_WINDOW])


def recognise_file(
    session: Session,
    household_id: str,
    *,
    filename: str | None,
    raw: bytes | None = None,
    evidence: FileEvidence | None = None,
) -> Recognised | None:
    """Which of the household's accounts this statement is for, if it says.

    Evidence in order of how much it can be trusted: the account number an OFX
    file states about itself, then an identifier in the file name, then one in
    the first part of the file's text. Two accounts matched at the same level is
    no answer at all -- a pre-selected wrong account is worse than none.

    Pass `evidence` from `read_evidence` when the bytes were read elsewhere --
    the route reads them on a worker thread. `raw` alone reads them here.
    """
    identifiers = [
        row for row in list_for_household(session, household_id) if row.account_id is not None
    ]
    if not identifiers:
        return None
    if evidence is None:
        evidence = read_evidence(raw)

    def unique(found: list[AccountIdentifier], how: str) -> Recognised | None:
        accounts = {row.account_id for row in found}
        if len(accounts) != 1:
            return None
        account = session.get(Account, accounts.pop())
        return Recognised(account, how) if account is not None else None

    if evidence.stated:
        target = _compact(evidence.stated)
        hit = [
            row
            for row in identifiers
            if row.kind in _COMPACT and row.normalised and row.normalised == target
        ] or [
            row
            for row in identifiers
            if row.kind in _COMPACT
            and len(row.normalised) >= _SUBSTRING_SAFE
            and (row.normalised in target or target in row.normalised)
        ]
        answer = unique(hit, "the file states its account number")
        if answer:
            return answer

    if filename:
        found = [n.identifier for n in named_in(identifiers, filename, include_file_tags=True)]
        answer = unique(found, "the file name carries one of its identifiers")
        if answer:
            return answer

    if evidence.head is not None:
        found = [
            n.identifier
            for n in named_in(identifiers, evidence.head)
            if n.identifier.kind in (IdentifierKind.iban, IdentifierKind.number)
            and len(n.identifier.normalised) >= _SUBSTRING_SAFE
        ]
        answer = unique(found, "the file's own text names one of its numbers")
        if answer:
            return answer
    return None


def _is_binary(raw: bytes) -> bool:
    """A spreadsheet or PDF has to be read whole to be read at all."""
    return raw[:4] in (b"%PDF", b"\xd0\xcf\x11\xe0") or raw[:2] == b"PK"


def pockets_named(text: str) -> list[str]:
    """Names a statement quotes as the account it is about.

    Revolut's savings statements say `Deposit to 'Savings Challenge'` on every
    row. That quoted name is the file telling us which pocket it is.
    """
    return re.findall(r"'([^']{3,60})'", text or "")


def check_pocket(session: Session, account: Account, payees: list[str]) -> None:
    """Refuse a savings statement dropped on the wrong pocket (issue #69).

    Only when the file is unambiguous about it: every quoted pocket name that
    is an alias of one of our accounts names the *same* other account, and none
    names this one. Anything less is left to the preview.
    """
    identifiers = [
        row
        for row in list_for_household(session, account.household_id)
        if row.kind is IdentifierKind.alias and row.account_id
    ]
    if not identifiers:
        return
    named: set[str] = set()
    for payee in payees:
        for pocket in pockets_named(payee):
            key = normalise(IdentifierKind.alias, pocket)
            named |= {row.account_id for row in identifiers if row.normalised == key}
    if named and account.id not in named and len(named) == 1:
        other = session.get(Account, named.pop())
        raise Conflict(
            f"this statement is for {other.name}, not {account.name}: every row names that "
            "account's pocket. Import it there instead."
        )
