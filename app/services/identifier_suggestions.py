"""Identifiers the ledger already names, offered for a person to confirm (#130).

Transfer matching and file recognition both run on account identifiers (#66),
and on any install most of them are never typed in, because nothing says which
ones are missing. The ledger usually does: rows saying `Deposit to '<pocket>'`,
`TO A/C <number>`, a card number the other bank prints in full, a statement
file whose name carries the same tag every month. This module reads them off.

Four sources, none of them specific to one bank:

1. **Confirmed links.** The words on the other side of transfers already
   linked into an account -- by a name or by a person, never by account
   history alone (#131) -- that recur and name no identifier yet.
2. **Patterns** in rows no transfer has claimed, each with a check of its own
   so a reference number is not offered as an account: IBANs (mod-97), card
   numbers (Luhn) and their masked last four, UK `A/C` and sort-code numbers,
   Spanish CCC (its two check digits) and `Contrato` numbers, quoted pocket
   names, and `To` / `From` an account's name.
3. **Account names.** An account's own name, or its name without the bank
   (`Savings Challenge-Revolut` -> `Savings Challenge`), in someone's text.
4. **File names.** The token every statement file of an account carries.

**Never automatic.** An identifier is evidence for linking without asking, so
a wrong one makes wrong links. Each suggestion says how many rows mention it
and how many transfer pairs it would link -- counted by the matcher itself,
with the identifier added and nothing written -- and waits for Add or Ignore.

Nothing here writes, except :func:`ignore`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..audit.hook import BATCH_KEY
from ..models import (
    Account,
    AccountIdentifier,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    IdentifierKind,
    IgnoredIdentifierSuggestion,
    LinkSource,
    Payee,
    Transaction,
)
from . import identifiers as identifier_service
from . import payees as payee_service
from . import transfers as transfer_service

#: A value that names none of the household's accounts is only worth a
#: person's time when it keeps coming back -- the issue's "appears 6 times and
#: isn't one of your accounts". So is a phrase read off confirmed links.
RECURS = 3

#: How many suggestions get a "would link N pairs" count. Each count is one
#: pass of the matcher over the rows that could pair (the pool is read once,
#: the evidence worked out again per suggestion), which on a large ledger is
#: tens of milliseconds -- fine twenty times, not two hundred. Suggestions past
#: this, fewest mentions first, say "not counted" rather than slow the screen.
COUNT_LIMIT = 20

#: How many distinct texts are read, most recent first. Bank descriptors carry
#: a per-row reference, so distinct texts grow with the ledger; an identifier
#: worth suggesting is in the recent ones, and 40k texts cost seconds (#232).
SCAN_LIMIT = 5_000

#: Words that say what kind of account something is, not which one. An alias
#: made of nothing else would name every account of that kind.
GENERIC = frozenset(
    {
        "ACCOUNT", "ACC", "CURRENT", "CHECKING", "SAVINGS", "SAVING", "SAVER", "CARD",
        "CREDIT", "DEBIT", "CASH", "MAIN", "JOINT", "PERSONAL", "BUSINESS", "WALLET",
        "POCKET", "BANK", "PAYMENT", "PAGO", "CUENTA", "TARJETA", "TRANSFERENCIA",
        "DEPOSIT", "INGRESO", "RECIBO", "THE", "AND", "FOR", "DEL", "LOS", "LAS",
        "EUR", "GBP", "USD",
    }
)

#: Words a transfer descriptor opens or closes with, stripped off the edges of
#: a phrase read from confirmed links: "TO <pocket>" suggests "<pocket>".
_EDGE_WORDS = transfer_service.TRANSFER_WORDS | frozenset(
    {"A", "DE", "DESDE", "PARA", "EN", "AL", "ON", "PAYMENT", "TRANSFERENCIA", "TRASPASO"}
)


@dataclass(slots=True)
class Suggestion:
    """One identifier the ledger suggests, and what it would do."""

    kind: IdentifierKind
    #: As it would be stored -- what Add sends.
    value: str
    normalised: str
    #: The account it is suggested for. None when it matches none of them:
    #: the person picks one, or ignores it.
    account_id: str | None
    #: `links`, `pattern`, `account_name` or `file_name`.
    source: str
    #: In words, for the screen.
    why: str
    #: Rows whose text mentions it -- for a file tag, statement files.
    mentions: int = 0
    #: One of those texts, most recent first.
    sample: str | None = None
    #: Transfer pairs it would link, with nothing written. None when not
    #: counted: past :data:`COUNT_LIMIT`, or with no account to count for.
    would_link: int | None = None
    #: Those pairs, as (out id, in id): what the Transfers screen's total is
    #: the union of, since two suggestions may link the same pair.
    pairs: frozenset[tuple[str, str]] = frozenset()

    @property
    def unit(self) -> str:
        return "files" if self.kind is IdentifierKind.file_tag else "rows"


@dataclass(slots=True)
class Suggestions:
    items: list[Suggestion] = field(default_factory=list)
    #: Distinct pairs every counted suggestion would link between them. None
    #: when nothing was counted: see `suggest`'s `count_pairs`.
    would_link: int | None = 0


# --------------------------------------------------------------------------- #
# Validity checks: what keeps a random number from being suggested
# --------------------------------------------------------------------------- #

#: IBAN lengths by country, for cutting one out of a longer run of text. A
#: country not listed is tried at every length, longest first.
IBAN_LENGTHS = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GI": 23, "GR": 27, "HR": 21, "HU": 28,
    "IE": 22, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31,
    "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24, "SI": 19, "SK": 24, "SM": 27,
}


def iban_ok(candidate: str) -> bool:
    """Mod-97: the first four characters moved to the end, letters as numbers."""
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", candidate):
        return False
    moved = candidate[4:] + candidate[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def luhn_ok(digits: str) -> bool:
    """The check digit every card number carries and few other numbers do."""
    if not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


_CCC_WEIGHTS = (1, 2, 4, 8, 5, 10, 9, 7, 3, 6)


def _ccc_digit(ten: str) -> int:
    rest = 11 - sum(int(d) * w for d, w in zip(ten, _CCC_WEIGHTS, strict=True)) % 11
    return {11: 0, 10: 1}.get(rest, rest)


def ccc_ok(digits: str) -> bool:
    """A Spanish CCC: bank 4, branch 4, two check digits, account 10."""
    if len(digits) != 20 or not digits.isdigit():
        return False
    bank_branch, check, account = digits[:8], digits[8:10], digits[10:]
    return check == f"{_ccc_digit('00' + bank_branch)}{_ccc_digit(account)}"


def _looks_like_a_date(digits: str) -> bool:
    if len(digits) not in (6, 8, 14):
        return False
    if len(digits) == 6:
        return 1 <= int(digits[2:4]) <= 12 and 1 <= int(digits[4:6]) <= 31
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    return 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


# --------------------------------------------------------------------------- #
# Source 2: the pattern extractors
# --------------------------------------------------------------------------- #

_IBAN_RUN = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,32})")
_CCC = re.compile(r"(?<![0-9])(\d{4})[ -]?(\d{4})[ -]?(\d{2})[ -]?(\d{10})(?![0-9])")
_CONTRACT = re.compile(r"\bCONTRA(?:TO|CT)\b\s*(?:N[Oº.]*\s*)?(\d(?:[ -]?\d){9,19})(?![0-9])")
_UK_AC = re.compile(r"\bA/?C\s*(?:NO\.?\s*)?(\d{8})(?![0-9])")
_SORT_CODE = re.compile(r"(?<![0-9])\d{2}[- ]\d{2}[- ]\d{2}\s+(\d{8})(?![0-9])")
_CARD = re.compile(r"(?<![0-9])(\d(?:[ -]?\d){12,18})(?![0-9])")
#: Only the start of a run may begin a match: `\*+` free to start at every
#: star of a 100k run is quadratic, and a statement cell can be that (#221).
_MASKED = re.compile(r"(?:(?<!\*)\*+|(?<!•)•+|\bX{4,})\s?(\d{4})(?![0-9])")
_TO_FROM = re.compile(r"^\s*(?:TO|FROM)\s+(.{3,60}?)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Found:
    """One value one extractor read out of one text."""

    kind: IdentifierKind
    value: str
    #: What read it, in words: "an IBAN", "a card number".
    what: str
    #: `quoted` for a pocket name in quotes, `to_from` for "To <name>": they
    #: are placed differently from a number.
    shape: str = "number"


def _cut_iban(run: str) -> str | None:
    compact = run.replace(" ", "")
    length = IBAN_LENGTHS.get(compact[:2])
    tries = [length] if length else range(min(len(compact), 34), 14, -1)
    for size in tries:
        if size and len(compact) >= size and iban_ok(compact[:size]):
            return compact[:size]
    return None


def extract(text: str | None) -> list[Found]:
    """Every identifier-shaped value in one bank text that passes its check.

    Longer shapes are read first and blanked out, so the digits of an IBAN are
    not read again as a card number, nor a CCC's as anything else.
    """
    if not text:
        return []
    upper = text.upper()
    found: list[Found] = []

    def blank(match: re.Match, source: str) -> str:
        start, end = match.span()
        return source[:start] + " " * (end - start) + source[end:]

    work = upper
    for match in list(_IBAN_RUN.finditer(work)):
        iban = _cut_iban(match.group(1))
        if iban:
            found.append(Found(IdentifierKind.iban, iban, "an IBAN"))
            work = blank(match, work)
    for match in list(_CONTRACT.finditer(work)):
        digits = re.sub(r"\D", "", match.group(1))
        found.append(Found(IdentifierKind.number, digits, "a contract number"))
        work = blank(match, work)
    for match in list(_CCC.finditer(work)):
        digits = "".join(match.groups())
        if ccc_ok(digits):
            found.append(Found(IdentifierKind.number, digits, "a Spanish account number (CCC)"))
            work = blank(match, work)
    for pattern, what in ((_UK_AC, "an A/C number"), (_SORT_CODE, "a sort code and account number")):
        for match in list(pattern.finditer(work)):
            if set(match.group(1)) != {"0"}:
                found.append(Found(IdentifierKind.number, match.group(1), what))
                work = blank(match, work)
    for match in list(_CARD.finditer(work)):
        digits = re.sub(r"\D", "", match.group(1))
        if 13 <= len(digits) <= 19 and luhn_ok(digits) and not _looks_like_a_date(digits):
            found.append(Found(IdentifierKind.card, digits, "a card number"))
            work = blank(match, work)
    for match in list(_MASKED.finditer(work)):
        found.append(Found(IdentifierKind.card, match.group(1), "a card's last four"))
    for pocket in identifier_service.pockets_named(text):
        if re.search(r"[A-Za-z]{3}", pocket):
            found.append(Found(IdentifierKind.alias, pocket.strip(), "a quoted pocket name", "quoted"))
    to_from = _TO_FROM.match(text)
    if to_from and "'" not in to_from.group(1) and not re.search(r"\d{4}", to_from.group(1)):
        found.append(Found(IdentifierKind.alias, to_from.group(1), "a To / From name", "to_from"))
    return found


# --------------------------------------------------------------------------- #
# Source 3: an account's own name
# --------------------------------------------------------------------------- #

_SEPARATOR = re.compile(r"\s*(?:[-–—|@(/]|\bAT\b)\s*", re.IGNORECASE)


def name_variants(account: Account) -> list[str]:
    """The account's name, and its name without the bank it is at.

    `Savings Challenge-Revolut` -> `Savings Challenge`. The bank is the
    account's own `institution` when it ends the name, or else whatever
    follows the last separator, when that is a single word.
    """
    name = " ".join(account.name.split())
    variants = [name]
    institution = (account.institution or "").strip()
    stripped = None
    if institution and name.lower().endswith(institution.lower()) and len(name) > len(institution):
        stripped = _SEPARATOR.sub(" ", name[: -len(institution)]).strip(" ()-")
    else:
        parts = _SEPARATOR.split(name)
        if len(parts) >= 2 and len(parts[-1].split()) == 1:
            stripped = " ".join(" ".join(parts[:-1]).split()).strip(" ()-")
    if stripped:
        variants.insert(0, stripped)
    return list(dict.fromkeys(variants))


def _specific(words: str) -> bool:
    """Whether an alias says *which* account, not only what kind."""
    return any(
        len(word) >= 3 and word.isalpha() and word not in GENERIC and word not in _EDGE_WORDS
        for word in words.split()
    )


# --------------------------------------------------------------------------- #
# Source 4: a statement file's name
# --------------------------------------------------------------------------- #


def file_tokens(filename: str | None) -> list[str]:
    """The parts of a file name that could be an account's tag.

    `account-statement_2026-01-01_2026-02-01_en-gb_d4e5f6.csv` -> `d4e5f6`:
    letters and digits together, five or more, or a run of six or more
    digits that is not a date. Words, dates and language codes are what every
    file of every account says, so they tag nothing.
    """
    if not filename:
        return []
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename.rsplit("/", 1)[-1])
    kept: list[str] = []
    for token in re.split(r"[^0-9A-Za-z]+", stem):
        has_digit = any(c.isdigit() for c in token)
        has_alpha = any(c.isalpha() for c in token)
        if (has_digit and has_alpha and len(token) >= 5) or (
            token.isdigit() and len(token) >= 6 and not _looks_like_a_date(token)
        ):
            kept.append(token)
    return list(dict.fromkeys(kept))


def _file_kind(token: str) -> IdentifierKind:
    """An IBAN in a file name is better kept as an IBAN: it names the account
    in descriptors too, where a file tag is never looked for."""
    return IdentifierKind.iban if iban_ok(token.upper()) else IdentifierKind.file_tag


def for_file(session: Session, household_id: str, filename: str | None) -> tuple[IdentifierKind, str] | None:
    """What to offer as the tag of an unrecognised statement file (#130).

    For the Import screen, once a person has picked the account the file did
    not name: its file name's one stable token, when it has exactly one, is
    not an identifier already, and has not been ignored.
    """
    tokens = file_tokens(filename)
    if len(tokens) != 1:
        return None
    token = tokens[0]
    kind = _file_kind(token)
    normalised = identifier_service.normalise(kind, token)
    if _is_known(identifier_service.list_for_household(session, household_id), kind, token):
        return None
    if (kind, normalised) in ignored(session, household_id):
        return None
    return kind, token


# --------------------------------------------------------------------------- #
# Ignoring
# --------------------------------------------------------------------------- #


def ignored(session: Session, household_id: str) -> set[tuple[IdentifierKind, str]]:
    return {
        (IdentifierKind(kind), normalised)
        for kind, normalised in session.execute(
            select(IgnoredIdentifierSuggestion.kind, IgnoredIdentifierSuggestion.normalised).where(
                IgnoredIdentifierSuggestion.household_id == household_id
            )
        ).all()
    }


def ignore(
    session: Session, household_id: str, *, kind: IdentifierKind | str, value: str
) -> IgnoredIdentifierSuggestion:
    """Never offer this value, of this kind, again. Returns the record -- the
    existing one when it was already ignored, so a second click is harmless."""
    kind = IdentifierKind(kind)
    normalised = identifier_service.normalise(kind, value)
    existing = session.execute(
        select(IgnoredIdentifierSuggestion).where(
            IgnoredIdentifierSuggestion.household_id == household_id,
            IgnoredIdentifierSuggestion.kind == kind,
            IgnoredIdentifierSuggestion.normalised == normalised,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    batch_row = session.info.get(BATCH_KEY)
    row = IgnoredIdentifierSuggestion(
        household_id=household_id,
        kind=kind,
        value=value.strip()[:120],
        normalised=normalised[:120],
        ignored_by_id=batch_row.actor_id if batch_row is not None else None,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Putting it together
# --------------------------------------------------------------------------- #


def _transient(household_id: str, kind: IdentifierKind, value: str, account_id: str | None) -> AccountIdentifier:
    """An identifier matched as though stored, and never added to the session."""
    return AccountIdentifier(
        household_id=household_id,
        account_id=account_id,
        kind=kind,
        value=value,
        normalised=identifier_service.normalise(kind, value),
    )


def _is_known(identifiers: list[AccountIdentifier], kind: IdentifierKind, value: str) -> bool:
    """Whether an identifier already stored matches this value -- so adding it
    would recognise nothing new. Never suggested (#130).

    A file tag only recognises files, and transfer matching never reads it, so
    a tag and an account number with the same digits are two identifiers: an
    account whose statements are named after its number still needs the
    number for "TO A/C <number>" to name it.
    """
    normalised = identifier_service.normalise(kind, value)
    tagging = kind is IdentifierKind.file_tag
    if any(
        row.normalised == normalised and (row.kind is IdentifierKind.file_tag) == tagging
        for row in identifiers
    ):
        return True
    if kind is IdentifierKind.file_tag:
        return bool(identifier_service.named_in(
            [row for row in identifiers if row.account_id], value, include_file_tags=True
        ))
    return bool(identifier_service.named_in(identifiers, value))


@dataclass(slots=True)
class _Candidate:
    kind: IdentifierKind
    value: str
    source: str
    why: str
    account_id: str | None = None
    #: Rows it was read from in quotes, by the account they are on (source 2).
    quoted_on: Counter = field(default_factory=Counter)
    shapes: set[str] = field(default_factory=set)
    files: int = 0
    #: Two accounts' names both say it: it names neither.
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class _Row:
    """One distinct text in the ledger, and how many rows say it."""

    account_id: str
    linked_to: str | None
    link_source: LinkSource | None
    bank: str | None
    payee: str | None
    memo: str | None
    count: int

    @property
    def texts(self) -> tuple[str | None, ...]:
        return (self.bank, self.payee, self.memo)

    @property
    def shown(self) -> str:
        return " · ".join(dict.fromkeys(t.strip() for t in self.texts if t and t.strip()))


def _rows(session: Session, household_id: str) -> list[_Row]:
    """The ledger's texts, one per distinct combination, most recent first.

    A payee the app made -- "Transfer : <account>", an opening balance -- is
    left out: it is the app's words, not a bank's, and every linked leg would
    otherwise "mention" the account it was linked to.
    """
    return [
        _Row(*row)
        for row in session.execute(
            select(
                Transaction.account_id,
                Transaction.transfer_account_id,
                Transaction.link_source,
                Transaction.import_payee_original,
                case((Payee.system.is_(None), Payee.name), else_=None),
                Transaction.memo,
                func.count(),
            )
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .where(Transaction.household_id == household_id)
            .group_by(
                Transaction.account_id,
                Transaction.transfer_account_id,
                Transaction.link_source,
                Transaction.import_payee_original,
                Payee.name,
                Payee.system,
                Transaction.memo,
            )
            .order_by(func.max(Transaction.date).desc())
            .limit(SCAN_LIMIT)
        ).all()
    ]


_FOUR_DIGITS = re.compile(r"\d{4}")


@lru_cache(maxsize=16_384)
def _folded(text: str | None) -> str | None:
    """The text with the per-row references taken off its end.

    `MERCHANT MADRID BX1EG1ST5` and `MERCHANT MADRID IJ6YX2P45` are one text
    read twice, and reading each one's own copy is what made this screen cost
    seconds (#232). A trailing token goes when it reads as a reference the way
    `payees._reference_stripped` reads one, **and has no run of four digits**:
    every number an extractor reads -- a last four, an A/C number, a card, an
    IBAN -- has one, so `TO A/C 11112222` and `CARD ****4242` keep theirs.
    """
    if not text:
        return text
    tokens = text.split()
    kept = len(tokens)
    while (
        kept > 1
        and not _FOUR_DIGITS.search(tokens[kept - 1])
        and payee_service._is_reference_token(tokens[kept - 1])
    ):
        kept -= 1
    return text if kept == len(tokens) else " ".join(tokens[:kept])


def _stem(text: str | None) -> str | None:
    """The phrase in a transfer descriptor that could name its account.

    The longest run of words with no digit in them -- a date or a reference
    changes every month, the words around it do not -- with the transfer
    words at its edges taken off: `TO ACC-PRODUCT REF 0412` -> `ACC PRODUCT`.
    Contiguous, because an alias is matched as consecutive whole words.
    """
    words = identifier_service.normalise(IdentifierKind.alias, text or "").split()
    runs: list[list[str]] = [[]]
    for word in words:
        if any(c.isdigit() for c in word):
            runs.append([])
        else:
            runs[-1].append(word)
    best = max(runs, key=lambda run: len(" ".join(run)))
    while best and best[0] in _EDGE_WORDS:
        best = best[1:]
    while best and best[-1] in _EDGE_WORDS:
        best = best[:-1]
    phrase = " ".join(best)
    return phrase if len(phrase) >= 4 and _specific(phrase) else None


def _account_for_number(
    kind: IdentifierKind,
    normalised: str,
    identifiers: list[AccountIdentifier],
    accounts: dict[str, Account],
) -> str | None:
    """The one account a number belongs to, judged by what is stored.

    A stored number inside an IBAN (a UK IBAN ends in its account number, a
    Spanish one in its CCC), a stored IBAN containing this number, a stored
    last four this card ends with, a file tag that is this very number (its
    statements are named after it), or the last four in the account's own
    name ("Visa 1234"). Two accounts is no answer.
    """
    hits: set[str] = set()
    for row in identifiers:
        # Statements named after the account's own number: the file tag is
        # that number, so the number is that account's.
        if (
            kind is IdentifierKind.number and row.kind is IdentifierKind.file_tag
            and row.account_id is not None and len(normalised) >= 6
            and row.normalised == normalised
        ):
            hits.add(row.account_id)
            continue
        if row.account_id is None or row.kind not in (
            IdentifierKind.iban, IdentifierKind.number, IdentifierKind.card
        ):
            continue
        stored = row.normalised
        if len(stored) < 4:
            continue
        if (len(stored) >= 6 and stored in normalised) or (
            len(normalised) >= 6 and normalised in stored
        ) or kind is IdentifierKind.card and (
            normalised.endswith(stored) or stored.endswith(normalised)
        ):
            hits.add(row.account_id)
    if kind is IdentifierKind.card:
        last_four = normalised[-4:]
        for account in accounts.values():
            if last_four in re.split(r"[^0-9]+", account.name):
                hits.add(account.id)
    return hits.pop() if len(hits) == 1 else None


def _account_for_words(normalised: str, accounts: dict[str, Account]) -> str | None:
    """The one account whose name, or name without its bank, is these words."""
    hits = {
        account.id
        for account in accounts.values()
        for variant in name_variants(account)
        if identifier_service.normalise(IdentifierKind.alias, variant) == normalised
    }
    return hits.pop() if len(hits) == 1 else None


def suggest(session: Session, household_id: str, *, count_pairs: bool = True) -> Suggestions:
    """Every identifier the ledger suggests, best first. Reads only.

    `count_pairs` runs the transfer matcher once per suggestion to say what
    each would link -- the dearest part, so the list is served without it and
    the counts asked for on their own (#232).
    """
    accounts = {
        row.id: row
        for row in session.execute(select(Account).where(Account.household_id == household_id)).scalars()
    }
    if not accounts:
        return Suggestions()
    identifiers = identifier_service.list_for_household(session, household_id)
    skip = ignored(session, household_id)
    rows = _rows(session, household_id)
    candidates: dict[tuple[IdentifierKind, str], _Candidate] = {}

    def offer(kind: IdentifierKind, value: str, source: str, why: str) -> _Candidate | None:
        value = " ".join(value.split())
        normalised = identifier_service.normalise(kind, value)
        minimum = 4 if kind in identifier_service._COMPACT else 3
        if len(normalised) < minimum or (kind, normalised) in skip:
            return None
        key = (kind, normalised)
        if key not in candidates:
            if _is_known(identifiers, kind, value):
                return None
            candidates[key] = _Candidate(kind, value, source, why)
        return candidates[key]

    # 1. The words on the far side of links a name or a person vouched for.
    by_stem: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        if row.linked_to is None or row.link_source not in (LinkSource.named, LinkSource.person):
            continue
        stem = _stem(" ".join(t for t in (row.bank, row.memo) if t))
        if stem:
            by_stem[stem][row.linked_to] += row.count
    for stem, into in by_stem.items():
        if len(into) != 1:
            continue  # the same words on links into two accounts name neither
        (account_id, times), = into.items()
        if times < RECURS:
            continue
        found = offer(
            IdentifierKind.alias, stem, "links",
            f"the other side of {times} transfers linked into {accounts[account_id].name} says it",
        )
        if found is not None and found.source == "links":
            found.account_id = account_id

    # 3. An account's own name in somebody's words. Before the patterns, so
    # "To <that name>" is credited to the account it names.
    for account in accounts.values():
        for variant in name_variants(account):
            normalised = identifier_service.normalise(IdentifierKind.alias, variant)
            if not _specific(normalised):
                continue
            found = offer(
                IdentifierKind.alias, variant, "account_name",
                f"the name of {account.name}, as descriptions write it",
            )
            if found is not None and found.source == "account_name":
                if found.account_id not in (None, account.id):
                    found.ambiguous = True
                found.account_id = found.account_id or account.id

    # 2. Patterns in the rows no transfer has claimed. Read once per text with
    # its reference folded off, not once per reference (#232).
    read: dict[str | None, list[Found]] = {}

    def extracted(text: str | None) -> list[Found]:
        key = _folded(text)
        if key not in read:
            read[key] = extract(key)
        return read[key]

    for row in rows:
        if row.linked_to is not None:
            continue
        seen: set[tuple[IdentifierKind, str]] = set()
        for text in (row.bank or row.payee, row.memo):
            for one in extracted(text):
                key = (one.kind, identifier_service.normalise(one.kind, one.value))
                if key in seen:
                    continue
                seen.add(key)
                found = offer(one.kind, one.value, "pattern", f"{one.what} in the descriptions")
                if found is not None:
                    found.shapes.add(one.shape)
                    if one.shape == "quoted":
                        found.quoted_on[row.account_id] += row.count

    # 4. The token every statement file of an account carries.
    by_account: dict[str, list[str]] = defaultdict(list)
    for (source,) in session.execute(
        select(Batch.source).where(
            Batch.household_id == household_id,
            Batch.kind == BatchKind.imported,
            Batch.status.in_([BatchStatus.applied, BatchStatus.preview]),
        )
    ).all():
        source = source or {}
        if source.get("account_id") in accounts and source.get("filename"):
            by_account[source["account_id"]].append(source["filename"])
    token_accounts: dict[str, set[str]] = defaultdict(set)
    for account_id, names in by_account.items():
        for name in names:
            for token in file_tokens(name):
                token_accounts[token.upper()].add(account_id)
    for account_id, names in by_account.items():
        counts = Counter(token.upper() for name in names for token in file_tokens(name))
        own = {t: n for t, n in counts.items() if token_accounts[t] == {account_id}}
        if not own:
            continue
        most = max(own.values())
        best = [t for t, n in own.items() if n == most]
        if len(best) != 1:
            continue  # two tokens in every file: which one is the tag is a guess
        token = next(
            t for name in names for t in file_tokens(name) if t.upper() == best[0]
        )
        found = offer(
            _file_kind(token), token, "file_name",
            f"in the name of {most} of {len(names)} statement files imported into "
            f"{accounts[account_id].name}",
        )
        if found is not None:
            found.account_id = account_id
            found.files = most

    # Where each pattern-read value belongs, and whether it is worth asking.
    items: list[Suggestion] = []
    for (kind, normalised), found in candidates.items():
        if found.ambiguous:
            continue
        if found.source == "pattern":
            if kind is IdentifierKind.alias:
                found.account_id = _account_for_words(normalised, accounts)
                if found.account_id is None and "quoted" in found.shapes:
                    # A savings statement quotes its own pocket on every row.
                    homes = [a for a in found.quoted_on if accounts[a].type is AccountType.savings]
                    if len(found.quoted_on) == 1 and homes:
                        found.account_id = homes[0]
                        found.why = f"{accounts[homes[0]].name}'s own statement quotes it"
                if found.account_id is None and "quoted" not in found.shapes:
                    continue  # "To <somebody>" is a payee, not an account
            else:
                found.account_id = _account_for_number(kind, normalised, identifiers, accounts)
            if found.account_id is None:
                found.why = f"{found.why}, and it is not one of your accounts"
        items.append(
            Suggestion(
                kind=kind, value=found.value, normalised=normalised,
                account_id=found.account_id, source=found.source, why=found.why,
                mentions=found.files,
            )
        )

    # How many rows mention each: read the way transfer matching reads them.
    wording = [
        (item, _transient(household_id, item.kind, item.value, item.account_id))
        for item in items
        if item.kind is not IdentifierKind.file_tag
    ]
    if wording:
        by_object = {id(stand_in): item for item, stand_in in wording}
        stand_ins = [stand_in for _, stand_in in wording]
        # Rows that differ only by a reference are asked about once, with
        # their counts added; the first of them, most recent, is the sample.
        texts: dict[tuple[str | None, ...], list] = {}
        for row in rows:
            key = tuple(_folded(text) for text in row.texts)
            if key in texts:
                texts[key][0] += row.count
            else:
                texts[key] = [row.count, row.shown]
        for key, (count, shown) in texts.items():
            for named in identifier_service.named_in(stand_ins, *key):
                item = by_object[id(named.identifier)]
                item.mentions += count
                if item.sample is None:
                    item.sample = shown
    items = [
        item
        for item in items
        if item.mentions >= (RECURS if item.account_id is None else 1)
        or item.kind is IdentifierKind.file_tag
    ]

    items.sort(key=lambda item: (-item.mentions, item.kind.value, item.normalised))
    if count_pairs:
        _count_pairs(session, household_id, items)
    items.sort(
        key=lambda item: (
            -(item.would_link or 0), -item.mentions, item.kind.value, item.normalised
        )
    )
    if not count_pairs:
        return Suggestions(items=items, would_link=None)
    union = {pair for item in items for pair in item.pairs}
    return Suggestions(items=items, would_link=len(union))


def _count_pairs(session: Session, household_id: str, items: list[Suggestion]) -> None:
    """Fill in "would link N pairs" for the first :data:`COUNT_LIMIT` that can
    link anything: an identifier with an account, that descriptors are read
    for. A file tag is looked for in file names only, so it links nothing."""
    counted: list[Suggestion] = []
    for item in items:
        if item.kind is IdentifierKind.file_tag:
            item.would_link = 0
        elif item.account_id is not None and len(counted) < COUNT_LIMIT:
            counted.append(item)
    if not counted:
        return
    now, each = transfer_service.strong_with(
        session,
        household_id,
        [_transient(household_id, item.kind, item.value, item.account_id) for item in counted],
    )
    for item, strong in zip(counted, each, strict=True):
        item.pairs = frozenset(strong - now)
        item.would_link = len(item.pairs)
