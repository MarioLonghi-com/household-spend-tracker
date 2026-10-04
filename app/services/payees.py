"""Payees, and the rules that turn a raw bank string into one.

With no categories in iteration 1 a rule does exactly one thing: turn
``CARREFOUR MADRID 4432`` into ``Carrefour``. That alone is most of what makes a
register readable.

Lookup is case- and space-insensitive. The previous build matched names exactly,
so ``MERCADONA`` from a statement and ``Mercadona`` typed by hand became two
payees and the list fragmented on its own.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date as Date

import regex
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import ValidationError
from ..models import Account, MatchType, Payee, PayeeRule, RuleAction, SystemPayee

#: A user-supplied regex runs against every row of every import, so it is capped
#: rather than trusted. The cap bounds the *compile*, and nothing else: this
#: used to claim that "a short pattern is a poor vehicle for" catastrophic
#: backtracking, which measurement disproved -- `(a|a)*$` is six characters and
#: took 23.8 seconds. What actually bounds the match is the deadline on
#: `REGEX_TIMEOUT_SECONDS` below.
MAX_PATTERN_LENGTH = 300


#: Dashes that stand in for a hyphen (#268). A bank's app, its CSV and its PDF
#: do not agree on which one a sort code or a reference is written with, so
#: ``TO A/C 12-34-56`` and the same with en dashes are one payee. U+2010 to
#: U+2015 are the General Punctuation dashes, U+2212 the minus sign, U+FE63 and
#: U+FF0D the small and full-width hyphen-minus.
#:
#: The statement reader keeps a minus table of its own for reading amounts:
#: `MINUS_SIGNS` in `statements/signs.py`. The two are separate on purpose --
#: `statements/` is a library and never imports `app/`. MINUS_SIGNS must be a
#: subset of this, and `tests/test_payee_fold_unicode.py` fails if it is not.
UNICODE_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"

#: Spaces nobody can see (#268): no-break and narrow no-break space, zero-width
#: space, non-joiner and joiner, word joiner, and the byte-order mark a CSV
#: export leaves on its first cell. Each reads as an ordinary space, which the
#: whitespace collapse then folds away.
INVISIBLE_SPACES = "\u00a0\u202f\u200b\u200c\u200d\u2060\ufeff"

_FOLD_TABLE = str.maketrans(
    {**dict.fromkeys(UNICODE_DASHES, "-"), **dict.fromkeys(INVISIBLE_SPACES, " ")}
)

#: The combining diacritical marks NFKD splits an accented letter into: ``é``
#: becomes ``e`` and U+0301, and the mark is dropped.
_COMBINING_MARKS = re.compile("[\u0300-\u036f]")


def _fold_piece(text: str) -> str:
    """Everything `fold` does except the whitespace collapse."""
    text = _COMBINING_MARKS.sub("", unicodedata.normalize("NFKD", text))
    return text.translate(_FOLD_TABLE).casefold()


def fold(name: str) -> str:
    r"""The form two spellings of one payee agree on.

    Accents, dash style and invisible spaces are not part of a name (#268).
    Spanish banks drop accents inconsistently between the app, the CSV and the
    PDF, so ``Café Sol`` and ``CAFE SOL`` fold to one key. Then whitespace is
    collapsed and the result casefolded, as it always was.

        >>> fold("Café\u00a0Sol") == fold("CAFE SOL") == "cafe sol"
        True
        >>> fold("TO A/C 12\u201334\u201356") == fold("to a/c 12-34-56")
        True

    Never applied to a regex *pattern*: casefolding ``\D`` gives ``\d``, which
    means the opposite. See :func:`matches`.
    """
    return " ".join(_fold_piece(name or "").split())


def _folded_with_origins(text: str) -> tuple[str, list[int]]:
    """`fold(text)`, and for each character of it the index in `text` it came from.

    Folding changes lengths -- an accent is dropped, ``ß`` becomes ``ss``, a
    run of spaces becomes one -- so an offset found in the folded string is not
    an offset in the original. A rewrite rule has to cut the *original*, so it
    finds its pattern in this and maps the span back.
    """
    out: list[str] = []
    origins: list[int] = []
    for at, char in enumerate(text):
        for one in _fold_piece(char):
            if one.isspace():
                if not out or out[-1] == " ":
                    continue
                one = " "
            out.append(one)
            origins.append(at)
    if out and out[-1] == " ":
        out.pop()
        origins.pop()
    return "".join(out), origins


def keyed(payees) -> dict[str, Payee]:
    """These payees by the key `fold` gives their name *today*.

    Not by the stored `name_folded`: a payee made before #268 keeps the key the
    old fold gave it until the migration or a merge brings it up to date, and a
    lookup keyed by that would miss ``Café Sol`` when the row says
    ``CAFE SOL``. Where two payees now share a key -- the pairs
    :func:`collisions` lists for somebody to merge -- the one already holding
    that key in the column wins, and the first by name after it, so the answer
    does not depend on query order.
    """
    found: dict[str, Payee] = {}
    ranked = sorted(payees, key=lambda p: (p.name_folded != fold(p.name), p.name, p.id))
    for payee in ranked:
        found.setdefault(fold(payee.name), payee)
    return found


# --------------------------------------------------------------------------- #
# Card acquirers
# --------------------------------------------------------------------------- #

#: The acquirer is noise in front of the merchant: keep what follows it.
KEEP_TAIL = "tail"
#: The acquirer *is* the merchant and what follows is a transaction reference:
#: keep what comes before it.
KEEP_HEAD = "head"

#: Descriptor prefixes that say who took the card payment rather than who was
#: paid, as ``(prefix_pattern, keep)``.
#:
#: This table is why a `contains` rule of `SQ` claimed every Square descriptor
#: in a file -- `SQ *GULL KITCHEN LTD`, `SQ *CEDAR ROOMS` and `SQ *EVENTIM
#: APOLLO` all collapsed into one payee, because `SQ` is the acquirer and not a
#: shop. Each entry is only here because the prefix can be attributed:
#:
#: - ``SQ *`` -- Square's card-present descriptor prefix.
#: - ``SumUp *`` and ``Zettle_`` -- the same thing from SumUp and from Zettle
#:   (PayPal). Both appear with the separator doubled or spaced, and Zettle's
#:   is written both `Zettle_` and `ZETTLE_`.
#: - ``LSP*`` and ``SRT*`` -- two more acquirer prefixes seen in front of a
#:   merchant name on the statements this was written against. What the letters
#:   abbreviate is *not* established here, and nothing below depends on it: the
#:   only claim being made is that the merchant is what follows the separator.
#: - ``AMZNMktplace*`` and ``AIRBNB *`` -- the other direction. Amazon and
#:   Airbnb are the merchant, and the tail is an order or booking reference, so
#:   the head is the half worth keeping.
#:
#: Nothing is guessed at: a prefix that is not in this table leaves the
#: descriptor exactly as the bank wrote it.
_ACQUIRER_PREFIXES: tuple[tuple[str, str], ...] = (
    (r"SQ", KEEP_TAIL),
    (r"SUMUP", KEEP_TAIL),
    (r"ZETTLE", KEEP_TAIL),
    (r"LSP", KEEP_TAIL),
    (r"SRT", KEEP_TAIL),
    (r"AMZN\s*MKTP(?:LACE)?", KEEP_HEAD),
    (r"AIRBNB", KEEP_HEAD),
)

#: At least one real separator, repeated or spaced as banks actually write it:
#: `SQ *`, `SumUp **`, `Zettle_*`, `AIRBNB * `. A space alone is deliberately
#: not enough -- `SQ FOOT LTD` is a shop, not an acquirer and a merchant.
_SEPARATOR = r"[ \t]*[*_][*_ \t]*"

_COMPILED_ACQUIRERS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"^({prefix}){_SEPARATOR}(\S.*)$", re.IGNORECASE), keep)
    for prefix, keep in _ACQUIRER_PREFIXES
)

#: The same prefixes, without requiring anything after the separator. This
#: answers a different question -- "was this string written against a raw bank
#: descriptor?" -- which is what decides whether a rule is also compared the old
#: way. See :func:`matches`.
_ACQUIRER_HEADS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"^(?:{prefix})[ \t]*[*_]", re.IGNORECASE)
    for prefix, _keep in _ACQUIRER_PREFIXES
)

#: Below this many characters, what is left is not a merchant name.
MIN_NORMALISED_LENGTH = 3


def _reads_as_a_reference(kept: str) -> bool:
    """Is this a transaction id rather than something somebody could read?

    Deliberately narrow. Two shapes only: nothing but digits and punctuation,
    and a single unbroken token where the digits outnumber the letters. A name
    with a number in it -- `SQ *CAFE 42` -- is a name.
    """
    letters = sum(1 for ch in kept if ch.isalpha())
    digits = sum(1 for ch in kept if ch.isdigit())
    if letters == 0:
        return True
    return " " not in kept and digits > letters


def written_against_a_descriptor(pattern: str) -> bool:
    """Does this pattern itself start with an acquirer prefix?

    `Zettle_*Grey Heron` and `AIRBNB *` do; `SQ` does not, because there is no
    separator after it and so nothing saying it was copied off a statement
    rather than typed as a guess at a shop's name.
    """
    text = " ".join((pattern or "").split())
    return any(head.match(text) is not None for head in _ACQUIRER_HEADS)


def normalise_acquirer(descriptor: str) -> str:
    """A descriptor with the card acquirer taken out of it, where there is one.

    Pure, and the *stored* string is never touched: `import_lines.raw`,
    `transactions.import_payee_original` and the line-detail panel all go on
    showing exactly what the bank sent. This is only what rule matching and
    payee lookup compare *through*, which is why it has to be applied to both
    sides of a comparison or not at all.

        >>> normalise_acquirer("SQ *GULL KITCHEN LTD")
        'GULL KITCHEN LTD'
        >>> normalise_acquirer("AMZNMktplace*PK0TG5D25")
        'AMZNMktplace'
        >>> normalise_acquirer("MERCADONA 1234")
        'MERCADONA 1234'

    Falls back to the descriptor -- with its runs of whitespace collapsed, the
    same thing :func:`fold` does -- whenever stripping would leave nothing to
    match on: empty, under three characters, or a reference rather than a name.
    A descriptor that is *only* an acquirer prefix (`Zettle_*`) is the common
    version of that, and it answers with itself rather than with an empty
    string that would then match every rule in the household.
    """
    text = " ".join((descriptor or "").split())
    if not text:
        return text

    for pattern, keep in _COMPILED_ACQUIRERS:
        found = pattern.match(text)
        if found is None:
            continue
        kept = (found.group(1) if keep == KEEP_HEAD else found.group(2)).strip()
        if len(kept) < MIN_NORMALISED_LENGTH or _reads_as_a_reference(kept):
            return text
        return kept
    return text


def get_or_create(
    session: Session, household_id: str, name: str, *, system: SystemPayee | None = None
) -> Payee:
    """Find this household's payee by its folded name, or make it.

    ``system`` marks a payee the *app* is creating -- currently only the
    "Opening balance" one. It is applied to an existing payee as well as a new
    one, because the household that already has an unmarked "Opening balance"
    from before this column existed is the common case, not the rare one, and
    the second account they add is what would otherwise leave them with one
    marked payee and one not.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise ValidationError("a payee needs a name")

    folded = fold(cleaned)
    existing = by_folded(session, household_id, [cleaned]).get(folded)
    if existing is not None:
        if system is not None and existing.system is not system:
            existing.system = system
        return existing

    payee = Payee(household_id=household_id, name=cleaned, name_folded=folded, system=system)
    session.add(payee)
    session.flush()
    return payee


def by_folded(session: Session, household_id: str, names) -> dict[str, Payee]:
    """This household's payees for these names, keyed by folded name.

    One query for a whole import's worth of names, where `get_or_create` asks
    once per name -- for a statement of reference-bearing descriptors, every
    line a new name, that was a SELECT, an INSERT and a flush per line (#236).

    Keyed by today's :func:`fold`, and agreeing with :func:`keyed` -- which is
    what the rules resolve through -- about which payee a name belongs to. A
    payee whose stored key predates #268 is found by its name too, so a third
    spelling of a name two old payees share resolves to one of them rather than
    becoming a third payee.
    """
    wanted = sorted({fold(name) for name in names} - {""})
    found: dict[str, Payee] = {}
    stale: list[Payee] = []
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        matching = Payee.name_folded.in_(chunk)
        if start == 0:
            # The possibly-stale payees ride along with the first read rather
            # than costing one of their own: an import's payee reads are
            # counted (`test_ledger_performance`), and this is per call, not
            # per name.
            matching = matching | _POSSIBLY_STALE
        for payee in session.execute(
            select(Payee).where(Payee.household_id == household_id, matching)
        ).scalars():
            if payee.name_folded in chunk:
                found[payee.name_folded] = payee
            if start == 0 and payee.name_folded != fold(payee.name):
                stale.append(payee)
    missing = set(wanted) - found.keys()
    if missing and stale:
        for key, payee in keyed(stale).items():
            if key in missing:
                found[key] = payee
    return found


#: A stored key that may predate today's fold. The old fold differed from
#: today's only where a name held an accent, a Unicode dash, an invisible
#: character or a compatibility form -- every one of them outside printable
#: ASCII -- so a key that is all printable ASCII is already current. A superset:
#: a current key can be non-ASCII too (``ø``, Cyrillic), and those are told
#: apart by folding the name.
_POSSIBLY_STALE = Payee.name_folded.op("GLOB")("*[^ -~]*")


def add_pending(session: Session, household_id: str, name: str) -> Payee:
    """A new payee, added and not flushed, for a caller that has already
    looked for it with `by_folded` and will flush many rows at once."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise ValidationError("a payee needs a name")
    payee = Payee(household_id=household_id, name=cleaned, name_folded=fold(cleaned))
    session.add(payee)
    return payee


def transfer_payee(session: Session, household_id: str, account: Account) -> Payee:
    """The auto-managed "Transfer : <Account>" payee for the other side."""
    existing = session.execute(
        select(Payee).where(
            Payee.household_id == household_id, Payee.transfer_account_id == account.id
        )
    ).scalar_one_or_none()
    name = f"Transfer : {account.name}"
    if existing is not None:
        if existing.name != name:  # the account was renamed
            existing.name = name
            existing.name_folded = fold(name)
        # Marked here as well as at creation, for the transfer payees that
        # predate the column and would otherwise stay invisible to a report.
        if existing.system is not SystemPayee.transfer:
            existing.system = SystemPayee.transfer
        return existing

    payee = Payee(
        household_id=household_id,
        name=name,
        name_folded=fold(name),
        transfer_account_id=account.id,
        system=SystemPayee.transfer,
    )
    session.add(payee)
    session.flush()
    return payee


def list_for_household(session: Session, household_id: str) -> list[Payee]:
    return list(
        session.execute(
            select(Payee).where(Payee.household_id == household_id).order_by(Payee.name)
        ).scalars()
    )


#: Rows a merge rewrites between flushes (#240).
MERGE_CHUNK = 500


def merge(session: Session, *, source: Payee, target: Payee) -> Payee:
    """Fold one payee into another, moving everything that points at it."""
    if source.id == target.id:
        raise ValidationError("a payee cannot be merged into itself")
    if source.household_id != target.household_id:
        raise ValidationError("those payees are in different households")

    from ..models import Transaction

    # Ids first, as one column, then the rows MERGE_CHUNK at a time with a
    # flush after each (#240): a year of "Amazon" is ten thousand rows, and
    # holding every one as an audited object at once was ~120 MiB. Each row
    # still goes through the ORM, so each still gets its change row -- the
    # audit is the point. Not `yield_per` over the payee's own rows: that
    # would walk the index on the very column being rewritten.
    ids = list(
        session.execute(select(Transaction.id).where(Transaction.payee_id == source.id)).scalars()
    )
    for start in range(0, len(ids), MERGE_CHUNK):
        for txn in session.execute(
            select(Transaction).where(Transaction.id.in_(ids[start : start + MERGE_CHUNK]))
        ).scalars():
            txn.payee_id = target.id
        session.flush()
    for rule in session.execute(
        select(PayeeRule).where(PayeeRule.payee_id == source.id)
    ).scalars():
        rule.payee_id = target.id
    session.delete(source)
    session.flush()
    # After the delete has reached the database, because the source is often
    # what was holding the key: `Café Sol` folded into `CAFE SOL`'s twin is the
    # case #268's preview exists for.
    refresh_key(session, target)
    return target


def refresh_key(session: Session, payee: Payee) -> bool:
    """Bring a payee's stored key up to what `fold` gives its name today.

    Only when no other payee in the household holds it: a key two payees share
    is a merge somebody has to decide (#268), and the unique constraint would
    refuse it anyway. Returns whether the key changed.
    """
    wanted = fold(payee.name)
    if payee.name_folded == wanted:
        return False
    holder = session.execute(
        select(Payee.id).where(
            Payee.household_id == payee.household_id,
            Payee.name_folded == wanted,
            Payee.id != payee.id,
        )
    ).first()
    if holder is not None:
        return False
    payee.name_folded = wanted
    session.flush()
    return True


@dataclass(slots=True)
class Collision:
    """Payees of one household whose names now fold to one key (#268)."""

    key: str
    payees: list[Payee]


def collisions(session: Session, household_id: str) -> list[Collision]:
    """Which payees this household has that are now spellings of one name.

    #268 taught `fold` that accents, dash style and invisible spaces are not
    part of a name. Payees made before that were told apart by them, and
    merging those silently would move transactions nobody asked to move. So
    this only *lists* them, for the payee screen to offer the existing merge.

    Transfer payees are left out: each stands for an account and is kept in
    step with it, and the screen does not let one be merged.
    """
    groups: dict[str, list[Payee]] = {}
    for payee in list_for_household(session, household_id):
        if payee.transfer_account_id is not None:
            continue
        groups.setdefault(fold(payee.name), []).append(payee)
    return [
        Collision(key=key, payees=sorted(members, key=lambda p: (p.name, p.id)))
        for key, members in sorted(groups.items())
        if len(members) > 1
    ]


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def create_rule(
    session: Session,
    *,
    household_id: str,
    match_type: MatchType | str,
    pattern: str,
    payee: Payee | None = None,
    priority: int = 100,
    action: RuleAction | str = RuleAction.map,
    replacement: str | None = None,
) -> PayeeRule:
    """A rule of either kind, with the payee requirement enforced here.

    `payee_rules.payee_id` is nullable in the database because a rewrite rule
    has nothing to point at. That makes *this* the only place that can say
    "a map rule needs a payee" in a sentence somebody can act on, so it says it.
    """
    kind = MatchType(match_type)
    does = RuleAction(action)
    cleaned = (pattern or "").strip()
    if not cleaned:
        raise ValidationError("a rule needs something to match on")
    if len(cleaned) > MAX_PATTERN_LENGTH:
        raise ValidationError(f"that pattern is too long (max {MAX_PATTERN_LENGTH} characters)")
    if kind is MatchType.regex:
        try:
            re.compile(cleaned)
        except re.error as exc:
            raise ValidationError(f"that is not a valid regular expression: {exc}") from exc

    instead = (replacement or "").strip() or None
    if instead is not None and len(instead) > MAX_PATTERN_LENGTH:
        raise ValidationError(
            f"that replacement is too long (max {MAX_PATTERN_LENGTH} characters)"
        )

    if does is RuleAction.map:
        if payee is None:
            raise ValidationError("a rule that names a payee needs a payee to point at")
        if instead is not None:
            raise ValidationError(
                "a replacement only means something on a rule that rewrites; "
                "this one names a payee"
            )
    else:
        # Not an error to pass one -- the rules screen sends the whole form --
        # but it is not stored, because a rewrite rule with a payee on it would
        # read as though the payee were the answer, and it is not.
        payee = None
        if kind is MatchType.regex and instead is not None:
            _check_template(cleaned, instead)

    rule = PayeeRule(
        household_id=household_id,
        match_type=kind,
        action=does,
        pattern=cleaned,
        payee_id=payee.id if payee is not None else None,
        replacement=instead,
        priority=priority,
    )
    session.add(rule)
    session.flush()
    return rule


#: `\1`, `\g<1>` and `\g<name>` in a replacement template. Deliberately not a
#: general escape parser: these are the three forms that refer to a *group*,
#: which is the only thing that can be wrong in a way the pattern knows about.
_TEMPLATE_REFERENCE = re.compile(r"\\(?:(\d+)|g<([^>]*)>)")


def _check_template(pattern: str, replacement: str) -> None:
    r"""Refuse a replacement that names a group the pattern does not have.

    `regex.sub` raises at *substitution* time -- which is to say on somebody's
    import, against the first row that matches, rather than on the screen where
    the mistake was made. The rule would then be set aside for the rest of that
    file in silence.

    Checked by reading the template rather than by a dry run, because a dry run
    needs a string the pattern matches and there is no way to conjure one:
    `regex.sub(r"^SQ \*", r"\1", "")` substitutes nothing and raises nothing,
    which is exactly the case this has to catch.
    """
    try:
        compiled = regex.compile(pattern, flags=regex.IGNORECASE)
    except regex.error as exc:  # pragma: no cover - create_rule checked it first
        raise ValidationError(f"that is not a valid regular expression: {exc}") from exc

    for number, name in _TEMPLATE_REFERENCE.findall(replacement):
        if number:
            if int(number) > compiled.groups:
                raise ValidationError(
                    f"that replacement uses \\{number}, and the pattern has "
                    + (
                        "no bracketed groups"
                        if compiled.groups == 0
                        else f"only {compiled.groups}"
                    )
                    + ". Put brackets round the part you want to keep."
                )
        elif name.isdigit():
            if int(name) > compiled.groups:
                raise ValidationError(
                    f"that replacement uses \\g<{name}>, and the pattern has "
                    f"only {compiled.groups} bracketed groups"
                )
        elif name not in compiled.groupindex:
            raise ValidationError(
                f"that replacement uses \\g<{name}>, and the pattern has no "
                f"group called {name}"
            )


#: How long any one regex rule may spend on any one row.
#:
#: `(a|a)*$` is six characters. Against a 26-character payee string the stdlib
#: engine took 23.8 seconds, doubling per additional character -- and `stage()`
#: runs the rules against *every row of the file*, on a threadpool worker, so a
#: handful of imports stopped the instance answering at all. The code used to
#: assert that "a short pattern is a poor vehicle for it"; measurement said
#: otherwise. `regex` is used instead of `re` solely because it can be given a
#: deadline; `re` offers no way to interrupt a match.
REGEX_TIMEOUT_SECONDS = 0.1


class RegexBudget:
    """Remembers which rules ran out of time, for the life of one import.

    Without this a pathological rule costs the timeout on every row -- bounded,
    but still a 300-row file paying for it 300 times. Once a rule blows its
    deadline it is set aside for the rest of the file and the import continues
    without it, the same way a rule that stopped compiling is set aside.
    """

    def __init__(self) -> None:
        self.exhausted: set[str] = set()

    def matches(self, rule: PayeeRule, raw: str) -> bool:
        if rule.id in self.exhausted:
            return False
        try:
            return matches(rule, raw)
        except TimeoutError:
            self.exhausted.add(rule.id)
            return False

    def rewrite(self, rule: PayeeRule, text: str) -> str:
        """The same bargain for the rewrite stage: out of time, set aside.

        A rewrite rule that runs out of time leaves the string alone, which
        means the row keeps the bank's own words -- the same outcome as having
        written no rule at all, rather than a half-applied one.
        """
        if rule.id in self.exhausted:
            return text
        try:
            return rewrite_with(rule, text)
        except TimeoutError:
            self.exhausted.add(rule.id)
            return text


def matches(rule: PayeeRule, raw: str) -> bool:
    r"""Whether this rule claims this string.

    The acquirer prefix is taken off **both sides** for `contains`, `equals` and
    `prefix`. Symmetry is the whole point: a rule somebody typed against a raw
    descriptor -- `Zettle_*Grey Heron` -- still matches the row it was written
    for, while a rule of `SQ` no longer claims every Square descriptor in the
    file, because the text it is compared against no longer contains the
    acquirer either.

    Both sides of a `contains`, `equals` or `prefix` comparison go through
    :func:`fold`, so accents, dash style and invisible spaces do not stop a
    rule written as ``CAFE SOL`` from claiming ``Café Sol`` (#268).

    A `regex` pattern is **never** rewritten: `^SQ \*` is a deliberate anchor
    and normalising it would change what its author wrote -- and folding it
    would invert it, since ``\D``.casefold() is ``\d``. The text is tried
    several ways instead, raw first, then without the acquirer, then each of
    those folded, and any of them counts as a hit. Raw first means a pattern
    that matched before still does -- ``Café`` against ``Café Sol`` -- and the
    folded spellings only add hits, such as ``\d\d-\d\d`` against a sort
    code written with en dashes.

    Raises ``TimeoutError`` if a regex rule exceeds its deadline; callers
    working through a whole file should go through :class:`RegexBudget`, which
    turns that into "this rule is set aside".
    """
    text = " ".join((raw or "").split())
    pattern = rule.pattern.strip()

    if rule.match_type is MatchType.regex:
        stripped = normalise_acquirer(text)
        candidates: list[str] = []
        for one in (text, stripped, fold(text), fold(stripped)):
            if one not in candidates:
                candidates.append(one)
        for candidate in candidates:
            try:
                if (
                    regex.search(
                        pattern, candidate, flags=regex.IGNORECASE,
                        timeout=REGEX_TIMEOUT_SECONDS,
                    )
                    is not None
                ):
                    return True
            except regex.error:
                # A rule that stopped compiling must not break an import. It
                # simply never matches, and the rules screen is where it gets
                # fixed.
                return False
        return False

    pairs = [(fold(normalise_acquirer(text)), fold(normalise_acquirer(pattern)))]
    if written_against_a_descriptor(pattern):
        # A rule somebody copied off a statement -- `Zettle_*Grey Heron`,
        # `AIRBNB *` -- is compared the old way as well. Normalising both sides
        # covers most of those already, but not the ones where only one side
        # survives it: `AIRBNB *` alone has no merchant after the separator, so
        # it falls back to itself while the row it was written for becomes
        # `AIRBNB`, and an anchored prefix rule would stop matching the only
        # thing it was ever for.
        #
        # This is deliberately not "match either way round" in general: a bare
        # `SQ` does not start with an acquirer prefix -- there is no separator
        # after it -- so it gets no raw comparison, and the defect this whole
        # section exists for stays fixed.
        pairs.append((fold(text), fold(pattern)))

    for subject, needle in pairs:
        if not needle:
            # A pattern of nothing but invisible spaces folds to nothing, and
            # "" is a prefix of, and contained in, every string there is.
            continue
        if rule.match_type is MatchType.equals:
            if subject == needle:
                return True
        elif rule.match_type is MatchType.prefix:
            if subject.startswith(needle):
                return True
        elif needle in subject:
            return True
    return False


# --------------------------------------------------------------------------- #
# The rewrite stage
# --------------------------------------------------------------------------- #
#
# `SQ *`, `PAGO MOVIL`, `COMPRA INTERNET` -- payment rails, each hiding one
# distinct merchant per line. A rule of the only shape this table could express
# before today, pattern -> one fixed payee, is wrong for them by construction:
# there is no one payee to point at.
#
# So a rewrite rule does not produce a payee. It produces a *string*, which the
# mapping rules then run against. That composition is the whole payoff: one
# `COMPRA INTERNET` strip rule plus one `AMAZON` mapping rule handles the entire
# Amazon-over-Santander family, and `COMPRA INTERNET WWW.AMAZON 3318R6AX5`
# reaches the Amazon rule instead of becoming a payee called `WWW.AMAZON
# 3318R6AX5`. Issue #58.


def _literal_span(rule: PayeeRule, text: str) -> tuple[int, int] | None:
    """Where a non-regex pattern sits in this text, or None.

    Through `re` with the pattern escaped rather than through `str.find` on two
    casefolded strings: casefolding is not length-preserving -- German ``ss``
    folds from one character to two -- so an offset taken in the folded string
    can land mid-character in the original. Escaped, the pattern is a literal,
    so there is no backtracking to bound and no deadline needed.

    Since #268 the comparison is through :func:`fold`, the one `matches` makes,
    so a rule that claims ``PAGO MÓVIL`` written as ``PAGO MOVIL`` also cuts
    it. The literal is found in the folded text and the span mapped back to
    the original through `_folded_with_origins` -- which is the length problem
    above, solved by keeping the offsets rather than by not folding.
    """
    needle = fold(rule.pattern)
    if not needle:
        return None
    folded, origins = _folded_with_origins(text)
    if rule.match_type is MatchType.equals:
        places = [0] if folded == needle else []
    elif rule.match_type is MatchType.prefix:
        places = [0] if folded.startswith(needle) else []
    else:
        places = [at for at in range(len(folded)) if folded.startswith(needle, at)]

    def whole(at: int) -> bool:
        # Only whole original characters are cut. `STRAS` against `Straße`
        # ends inside the `ß` that folded to `ss`: cutting the `ß` would take a
        # letter the rule never named, and keeping it would leave half of one.
        last = at + len(needle) - 1
        starts_clean = at == 0 or origins[at - 1] != origins[at]
        ends_clean = last + 1 == len(origins) or origins[last + 1] != origins[last]
        return starts_clean and ends_clean

    at = next((one for one in places if whole(one)), None)
    if at is None:
        return None
    start = origins[at]
    end = origins[at + len(needle) - 1] + 1
    # A mark NFKD would have dropped belongs to the letter before it: cutting
    # `CAFE` out of a decomposed `CAFE` + U+0301 must not leave the accent
    # behind on whatever follows.
    while end < len(text) and not _fold_piece(text[end]):
        end += 1
    return start, end


def rewrite_with(rule: PayeeRule, raw: str) -> str:
    r"""One rewrite rule applied to one string. Unchanged if it does not claim it.

        >>> from types import SimpleNamespace
        >>> rail = SimpleNamespace(match_type=MatchType.prefix, pattern="PAGO MOVIL",
        ...                        replacement=None, action=RuleAction.rewrite)
        >>> rewrite_with(rail, "PAGO MOVIL BAR MARISOL")
        'BAR MARISOL'

    `replacement` is **a literal** for `contains`, `equals` and `prefix`, and
    **a template** for `regex`, where ``\1`` is the first group. That difference
    is not a wart: a literal pattern has no groups to refer to, and a person
    writing `^SQ \*(.+)$` is writing a regex and expects regex replacement.

    The acquirer normalisation is deliberately **not** applied here. It is
    applied inside `matches()`, to both sides of a comparison, and doing it
    again to the subject of a rewrite would mean a rule written against
    `SQ *SOMETHING` never saw the `SQ *` it was written to remove.

    Raises ``TimeoutError`` for a regex rule over its deadline; go through
    :meth:`RegexBudget.rewrite` when working through a whole file.
    """
    text = " ".join((raw or "").split())
    pattern = (rule.pattern or "").strip()
    if not text or not pattern:
        return text
    instead = rule.replacement or ""

    if rule.match_type is MatchType.regex:
        try:
            produced = regex.sub(
                pattern, instead, text, count=1,
                flags=regex.IGNORECASE, timeout=REGEX_TIMEOUT_SECONDS,
            )
        except regex.error:
            # A rule that stopped compiling -- or a template naming a group the
            # pattern lost -- must not break an import. Same answer as
            # `matches`: it does nothing, and the rules screen is where it gets
            # fixed.
            return text
    else:
        span = _literal_span(rule, text)
        if span is None:
            return text
        start, end = span
        produced = text[:start] + instead + text[end:]

    produced = " ".join(produced.split())
    # The same floor `normalise_acquirer` keeps, for the same reason: a rewrite
    # that reduces a descriptor to nothing, or to a transaction reference,
    # would then match every rule in the household. The row keeps the bank's
    # own words instead, which is recoverable; a payee called `3318R6AX5` is
    # not.
    if len(produced) < MIN_NORMALISED_LENGTH or _reads_as_a_reference(produced):
        return text
    return produced


def tidy(name: str) -> str:
    """Case-normalise a string that a rewrite rule produced.

        >>> tidy("BAR MARISOL")
        'Bar Marisol'
        >>> tidy("NOVA BAKEHOUSE SL")
        'Nova Bakehouse SL'
        >>> tidy("COMPRA INTERNET BOLD.FIT")
        'Compra Internet BOLD.FIT'

    Uniform and dull on purpose. One rule: an all-capitals **alphabetic** token
    of three characters or more gets its case normalised; everything else is
    left exactly as the bank wrote it. So `MARISOL` becomes `Marisol`, `SL` and `C`
    are left alone because two characters is a legal form or an initial rather
    than a word, and `BOLD.FIT` is left alone because a token with punctuation
    in it is a brand's own spelling and not shouting.

    What this deliberately does **not** do is strip `SL` / `S.L.` / a trailing
    `C`. Issue #58 floats it with "if that proves safe" and it is not: `CASTO
    HOUSE SL` and `NARDO HOUSE` are one shop, but `BAR C` is a name and `SL` is
    an ordinary word in other languages. A household that wants it writes a
    second rule, which is exactly what the second stage is for.

    Applied **only when a rewrite actually fired**. Running it over every
    imported descriptor would re-case every payee name in every household that
    has written no rules at all, which is a change nobody asked for.
    """
    return " ".join(
        word.capitalize() if word.isalpha() and word.isupper() and len(word) > 2 else word
        for word in name.split()
    )


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the two stages made of one bank string."""

    #: Exactly what the bank sent, whitespace collapsed.
    raw: str
    #: What the rewrite stage left. Equal to `raw` when no rewrite rule fired,
    #: and that equality is what callers test to know whether to use it.
    candidate: str
    #: The **mapping** rule that claimed it, if one did. Rewrite rules are
    #: deliberately left out: `_warn_about_broad_rules` counts what each rule
    #: claimed, and a rail rule claiming two hundred rows is the rule working,
    #: not a rule that is too broad.
    rule: PayeeRule | None
    payee: Payee | None

    @property
    def rewritten(self) -> bool:
        return self.candidate != self.raw

    @property
    def name(self) -> str:
        """What a payee should be called when no rule and no known payee answered."""
        return self.candidate


@dataclass(slots=True)
class RuleSet:
    """Rules loaded once, for a whole file.

    The previous build re-queried the rule table for every imported row, and ran
    a payee lookup per row on top of that.
    """

    rules: list[PayeeRule]
    by_folded_name: dict[str, Payee]
    #: Shared across every row of the file, so a rule that runs out of time is
    #: set aside once rather than re-attempted on each line.
    budget: RegexBudget = field(default_factory=RegexBudget)

    @property
    def rewrite_rules(self) -> list[PayeeRule]:
        return [r for r in self.rules if r.action is RuleAction.rewrite]

    @property
    def map_rules(self) -> list[PayeeRule]:
        return [r for r in self.rules if r.action is not RuleAction.rewrite]

    def candidate_for(self, raw_payee: str) -> str:
        """What the rewrite stage leaves of this string. Issue #58.

        Every enabled rewrite rule in priority order, **each one feeding the
        next**, so `COMPRA INTERNACIONAL` and a trailing-reference rule compose
        instead of competing. First-rule-wins is a property of the *mapping*
        stage and is left intact there; here, every rule gets a turn.

        Returns the string untouched -- not tidied, not re-cased -- when no
        rewrite rule fired. That is the case every household that has written
        none is in, and it has to stay byte-for-byte what it was.
        """
        text = " ".join((raw_payee or "").split())
        produced = text
        for rule in self.rewrite_rules:
            produced = self.budget.rewrite(rule, produced)
        return tidy(produced) if produced != text else text

    def resolve(self, raw_payee: str) -> Resolution:
        """Both stages, and everything a caller could want to know about them.

        1. **Rewrite.** The rails come off, producing a candidate string.
        2. **Map.** Exactly what happened before, run against the candidate
           rather than the raw -- so first rule wins, and an unmatched string
           falls back to a payee this household already has.
        """
        text = " ".join((raw_payee or "").split())
        candidate = self.candidate_for(text)

        for rule in self.map_rules:
            if self.budget.matches(rule, candidate):
                return Resolution(raw=text, candidate=candidate, rule=rule, payee=rule.payee)

        known = self.by_folded_name.get(fold(candidate))
        if known is None:
            # The same fallback, through the acquirer: a household that already
            # has a "Gull Kitchen Ltd" should find it from `SQ *GULL KITCHEN LTD`
            # without anybody having to write a rule for it.
            known = self.by_folded_name.get(fold(normalise_acquirer(candidate)))
        return Resolution(raw=text, candidate=candidate, rule=None, payee=known)

    def match_for(self, raw_payee: str) -> tuple[PayeeRule | None, Payee | None]:
        """The rule that claimed this string, if any, and the payee it resolves to.

        The rule is handed back as well as the payee so a caller working through
        a whole file can count what each rule claimed -- which is what the
        preview's warning about an over-broad rule is counted from. `payee_for`
        is this without the bookkeeping.
        """
        found = self.resolve(raw_payee)
        return found.rule, found.payee

    def payee_for(self, raw_payee: str) -> Payee | None:
        """First rule by priority wins; otherwise an exact payee we know."""
        return self.resolve(raw_payee).payee


def load_rules(session: Session, household_id: str) -> RuleSet:
    rules = list(
        session.execute(
            select(PayeeRule)
            .where(PayeeRule.household_id == household_id, PayeeRule.enabled.is_(True))
            .order_by(PayeeRule.priority, PayeeRule.created_at)
        ).scalars()
    )
    payees = keyed(list_for_household(session, household_id))
    return RuleSet(rules=rules, by_folded_name=payees)


# --------------------------------------------------------------------------- #
# Rules, applied to what is already here
# --------------------------------------------------------------------------- #
#
# Rules ran in exactly one place: `stage()`, as a statement is read. The answer
# was frozen onto the ImportLine and nothing ever consulted a rule again, so
# writing a new rule did not touch a single row already in the ledger -- and
# the rows a person is looking at when they decide to write the rule are, by
# definition, already in the ledger.
#
# Everything below is about closing that. Issue #60, with #59 and #61 leaning
# on the same two primitives: what would this rule claim, and what is one rule
# away from being fixed.


@dataclass(frozen=True, slots=True)
class Move:
    """One transaction, and the payee a re-apply would give it."""

    transaction_id: str
    raw: str
    #: What it says now. None when the payee row has gone.
    from_name: str | None
    #: **None means the payee does not exist yet**, and `apply_reapply` creates
    #: it from `to_name`. A rewrite rule that strips `PAGO MOVIL` off two
    #: hundred rows is naming shops this household has never had a payee for --
    #: which is the point of the rule, and the reason the plan cannot assume
    #: the target is already there. A preview must not create rows, so the
    #: decision is carried rather than acted on. Issue #58.
    to_payee_id: str | None
    to_name: str


@dataclass(frozen=True, slots=True)
class ReapplyPlan:
    """What re-applying the rules would do, before anything is written.

    The same bargain the import preview makes, for the same reason: a bulk
    re-categorisation of somebody's whole ledger is not something to find out
    about afterwards.
    """

    #: Whose ledger this is. Carried because a move may name a payee that does
    #: not exist yet -- see `Move.to_payee_id` -- and creating it needs to know
    #: the household without another query or another argument.
    household_id: str
    #: Grouped by the payee they would land on, largest first -- which is the
    #: shape the question is asked in ("what becomes Amazon?").
    moves: list[Move]
    #: How many rows were looked at, so "12 of 396" can be said rather than "12".
    considered: int
    #: Payees that would be left with nothing pointing at them.
    orphaned: list[str]

    @property
    def changing(self) -> int:
        return len(self.moves)


def _banks_spellings(raw: str, rules: RuleSet | None = None) -> frozenset[str]:
    """The folded spellings a row carrying `raw` can honestly be sitting at.

    Per distinct bank string, not per row: the same descriptor repeats
    thousands of times in a ledger, and the rewrite pass is not free (#231).
    """
    spellings = {fold(raw), fold(normalise_acquirer(raw))}
    if rules is not None:
        spellings.add(fold(rules.candidate_for(raw)))
    return frozenset(spellings)


def _still_the_banks_own_words(payee: Payee | None, spellings: frozenset[str]) -> bool:
    """True when nobody has corrected this row's payee by hand.

    The default scope, and the important half of the design: re-running rules
    over a row somebody fixed themselves would undo their work, silently, in a
    bulk operation. A row is untouched when the payee it carries still folds to
    what the bank wrote, or to what the acquirer normalisation makes of it --
    which is exactly what `stage` would have given it with no rule in play.

    **A rewrite rule adds a third spelling a row can honestly be sitting at.**
    Once `PAGO MOVIL BAR MARISOL` has been imported under a rail rule, the row
    says `Bar Marisol` -- which is not the bank's words and was not typed by a
    person either. Without the candidate in this set, the second re-apply after
    writing a rail rule would read the *first* re-apply's work as a human
    correction and refuse to touch the row again, which is how a rule stops
    being able to be corrected. Issue #58. The set is `_banks_spellings`.
    """
    if payee is None:
        return True  # nothing to lose
    return payee.name_folded in spellings


#: Plans already worked out, by household, scope and ledger version, so the
#: Apply that follows a preview is one plan rather than two (#231). Only ever
#: reused while nothing has been written since: see `_ledger_version`.
_PLANS: OrderedDict[tuple, ReapplyPlan] = OrderedDict()
_PLANS_KEPT = 8
_PLANS_LOCK = threading.Lock()

#: Ids per `IN (...)`, the size `undo` and `transfers._load` already use.
_IN_CHUNK = 500


def _ledger_version(session: Session) -> tuple[int, str] | None:
    """Where the audit log stands: its last `seq`, and the batch that wrote it.

    Transactions, payees and rules are all audited, so no write that could
    change a plan lands without moving this. The batch id is in it as well as
    the seq because a rolled-back seq can be handed out again, and a batch id
    never is.
    """
    from ..models import Change

    row = session.execute(
        select(Change.seq, Change.batch_id).order_by(Change.seq.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row is not None else None


def plan_reapply(
    session: Session,
    household_id: str,
    *,
    account_id: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    only_untouched: bool = True,
) -> ReapplyPlan:
    """What re-applying today's rules would do to rows already in the ledger.

    Reads `Transaction.import_payee_original`, which is the stored raw string
    the rule was always meant to see. A row without one was entered by hand and
    is out of scope by definition -- there is no bank string to match against.

    **Rules run once per distinct bank string, and rows are read as columns**
    (#231): a 40k-row ledger has hundreds of descriptors, and resolving each
    row's own copy cost 11 s. A plan asked for again with nothing written in
    between -- the preview, then Apply -- is the one already worked out.
    """
    key = (
        household_id, account_id, since, until, only_untouched, _ledger_version(session)
    )
    with _PLANS_LOCK:
        cached = _PLANS.get(key)
        if cached is not None:
            _PLANS.move_to_end(key)
            return cached

    plan = _plan_reapply(
        session,
        household_id,
        account_id=account_id,
        since=since,
        until=until,
        only_untouched=only_untouched,
    )
    with _PLANS_LOCK:
        _PLANS[key] = plan
        while len(_PLANS) > _PLANS_KEPT:
            _PLANS.popitem(last=False)
    return plan


def _plan_reapply(
    session: Session,
    household_id: str,
    *,
    account_id: str | None,
    since: Date | None,
    until: Date | None,
    only_untouched: bool,
) -> ReapplyPlan:
    from ..models import Transaction

    rules = load_rules(session, household_id)
    payee_by_id = {p.id: p for p in list_for_household(session, household_id)}

    stmt = select(
        Transaction.id, Transaction.payee_id, Transaction.import_payee_original
    ).where(
        Transaction.household_id == household_id,
        Transaction.import_payee_original.is_not(None),
        Transaction.import_payee_original != "",
    )
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    if since:
        stmt = stmt.where(Transaction.date >= since)
    if until:
        stmt = stmt.where(Transaction.date <= until)

    considered = 0
    moves: list[Move] = []
    losing: dict[str, int] = {}
    gaining: set[str] = set()
    spellings_of: dict[str, frozenset[str]] = {}
    #: What the rules make of each distinct string: (payee id, name, folded name),
    #: or None when they make nothing of it.
    target_of: dict[str, tuple[str | None, str, str] | None] = {}
    for txn_id, payee_id, raw in session.execute(stmt).all():
        considered += 1
        was = payee_by_id.get(payee_id or "")
        if only_untouched:
            spellings = spellings_of.get(raw)
            if spellings is None:
                spellings = spellings_of[raw] = _banks_spellings(raw, rules)
            if not _still_the_banks_own_words(was, spellings):
                continue

        if raw in target_of:
            target = target_of[raw]
        else:
            found = rules.resolve(raw)
            if found.payee is not None:
                target = (found.payee.id, found.payee.name, fold(found.payee.name))
            elif found.rewritten:
                # A rail rule named a shop this household has no payee for yet.
                # Nothing is created here -- `apply_reapply` does that, once, per
                # distinct name, inside the caller's batch.
                target = (None, found.candidate, fold(found.candidate))
            else:
                target = None
            target_of[raw] = target
        if target is None:
            continue
        to_payee_id, to_name, to_folded = target
        if to_payee_id is not None and to_payee_id == payee_id:
            continue
        if was is not None and was.name_folded == to_folded:
            continue  # already says it, under a payee row that is not indexed here
        moves.append(
            Move(
                transaction_id=txn_id,
                raw=raw,
                from_name=was.name if was else None,
                to_payee_id=to_payee_id,
                to_name=to_name,
            )
        )
        if payee_id:
            losing[payee_id] = losing.get(payee_id, 0) + 1
        if to_payee_id is not None:
            gaining.add(to_payee_id)

    moves.sort(key=lambda m: (m.to_name.casefold(), m.raw.casefold()))
    return ReapplyPlan(
        household_id=household_id,
        moves=moves,
        considered=considered,
        orphaned=sorted(_would_be_orphaned(session, household_id, losing, gaining)),
    )


def _would_be_orphaned(
    session: Session, household_id: str, losing: dict[str, int], gaining: set[str]
) -> list[str]:
    """Payees that would end up with no transactions and no rules.

    Worth saying in the preview rather than only offering afterwards: "this
    turns 28 payees into 1" is the sentence that makes a re-apply worth doing,
    and a payee list still 300 long afterwards is the thing that makes it feel
    like it did not work.
    """
    from ..models import Transaction

    candidates = set(losing) - gaining
    if not candidates:
        return []

    counts = dict(
        session.execute(
            select(Transaction.payee_id, func.count())
            .where(Transaction.payee_id.in_(candidates))
            .group_by(Transaction.payee_id)
        ).all()
    )
    ruled = {
        row[0]
        for row in session.execute(
            select(PayeeRule.payee_id).where(PayeeRule.payee_id.in_(candidates))
        ).all()
    }
    names = {
        p.id: p.name for p in list_for_household(session, household_id) if p.id in candidates
    }
    return [
        names[payee_id]
        for payee_id in candidates
        if payee_id not in ruled
        and counts.get(payee_id, 0) - losing.get(payee_id, 0) <= 0
        and payee_id in names
    ]


def apply_reapply(session: Session, plan: ReapplyPlan) -> int:
    """Carry out a plan. Returns how many rows moved.

    Through loaded objects, one at a time: `transactions` is audited and a
    bulk `update()` bypasses the ORM events the log listens to. The caller
    opens the batch, because **this has to be one batch** -- a bulk
    re-categorisation is precisely the operation somebody wants to take back
    whole, and that is the entire reason the audit log exists.
    """
    from ..models import Transaction

    if not plan.moves:
        return 0

    # A move with no `to_payee_id` is a rewrite rule naming a shop this
    # household has no payee for. Created once per distinct name rather than
    # once per row, inside the batch the caller opened, so an undo takes the
    # payees back with the moves.
    made: dict[str, Payee] = {}
    for move in plan.moves:
        if move.to_payee_id is None and fold(move.to_name) not in made:
            made[fold(move.to_name)] = get_or_create(
                session, plan.household_id, move.to_name
            )

    wanted = {
        move.transaction_id: (
            move.to_payee_id
            if move.to_payee_id is not None
            else made[fold(move.to_name)].id
        )
        for move in plan.moves
    }
    # In chunks: SQLite refuses more than 32,766 bound variables, and a rail
    # rule on a large ledger moves more rows than that (#231).
    ids = list(wanted)
    moved = 0
    for start in range(0, len(ids), _IN_CHUNK):
        rows = session.execute(
            select(Transaction).where(Transaction.id.in_(ids[start : start + _IN_CHUNK]))
        ).scalars()
        for txn in rows:
            if wanted[txn.id] is None or txn.payee_id == wanted[txn.id]:
                continue
            txn.payee_id = wanted[txn.id]
            moved += 1
    session.flush()
    return moved


def delete_orphans(session: Session, household_id: str) -> list[str]:
    """Remove payees with no transactions and no rules. Returns their names.

    Offered after a re-apply rather than done with it: the payees that were
    only ever bank noise have nothing pointing at them once the rows move, and
    leaving them is what makes a list that is still 300 long after the work.

    A `system` payee is never taken -- "Opening balance" and the transfer
    payees have no transactions at certain moments and are not noise.
    """
    from ..models import Transaction

    used = {
        row[0]
        for row in session.execute(
            select(Transaction.payee_id).where(
                Transaction.household_id == household_id,
                Transaction.payee_id.is_not(None),
            ).distinct()
        ).all()
    }
    ruled = {
        row[0]
        for row in session.execute(
            select(PayeeRule.payee_id).where(PayeeRule.household_id == household_id).distinct()
        ).all()
    }

    gone: list[str] = []
    for payee in list_for_household(session, household_id):
        if payee.id in used or payee.id in ruled or payee.system is not None:
            continue
        gone.append(payee.name)
        session.delete(payee)
    session.flush()
    return sorted(gone)


# --------------------------------------------------------------------------- #
# What a rule would do, and what is one rule away
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Trial:
    """What a pattern would claim, tried against what is already here.

    The rules screen had no preview at all: a rule was written blind and the
    result appeared at the next import. This is the "test this rule" the screen
    never had, and #59's suggestion panel shows the same thing before offering
    to create one. Issue #61.
    """

    #: How many of the household's raw strings it matches.
    matches: int
    #: Out of how many distinct strings were tried.
    considered: int
    #: A sample, so somebody can see whether it is claiming the right things.
    examples: list[str]
    #: True when the pattern is a regex that blew its deadline -- which is a
    #: thing to find out here rather than by an import going quiet.
    timed_out: bool = False


#: How many examples a trial hands back. Enough to recognise a mistake, few
#: enough that the answer stays readable.
TRIAL_EXAMPLES = 10


def distinct_raw_strings(session: Session, household_id: str) -> list[tuple[str, int]]:
    """Every bank string this household has, with how many rows carry it.

    `Transaction.import_payee_original` holds exactly what the bank sent, per
    row, and until now it was read by nothing except the transaction panel's
    "Where did this come from?" block -- so the app knew something the person
    did not and never said it.
    """
    from ..models import Transaction

    rows = session.execute(
        select(Transaction.import_payee_original, func.count())
        .where(
            Transaction.household_id == household_id,
            Transaction.import_payee_original.is_not(None),
            Transaction.import_payee_original != "",
        )
        .group_by(Transaction.import_payee_original)
    ).all()
    return sorted(((str(raw), int(count)) for raw, count in rows), key=lambda r: -r[1])


def try_pattern(
    session: Session,
    household_id: str,
    *,
    match_type: MatchType | str,
    pattern: str,
    action: RuleAction | str = RuleAction.map,
    replacement: str | None = None,
) -> Trial:
    """What would this rule claim, against the strings already in the ledger?

    For a rewrite rule the examples are `before -> after` rather than the bare
    strings. "217 rows match" is not the question anybody has about a rewrite;
    "what does it turn them into" is, and it is the only way to see a template
    that drops the half you meant to keep before it reaches an import.
    """
    kind = MatchType(match_type)
    does = RuleAction(action)
    cleaned = (pattern or "").strip()
    if not cleaned:
        raise ValidationError("a rule needs something to match on")
    if len(cleaned) > MAX_PATTERN_LENGTH:
        raise ValidationError(f"that pattern is too long (max {MAX_PATTERN_LENGTH} characters)")
    if kind is MatchType.regex:
        try:
            re.compile(cleaned)
        except re.error as exc:
            raise ValidationError(f"that is not a valid regular expression: {exc}") from exc

    instead = (replacement or "").strip() or None
    if does is RuleAction.rewrite and kind is MatchType.regex and instead is not None:
        _check_template(cleaned, instead)

    # A throwaway rule, never added to the session: `matches` and `rewrite_with`
    # take the object rather than the strings, so trying a pattern and running
    # one have to be the same code or they will eventually disagree about `SQ *`.
    trial = PayeeRule(
        household_id=household_id, match_type=kind, pattern=cleaned,
        action=does, payee_id=None, replacement=instead, priority=0,
    )
    trial.id = "trial"

    strings = distinct_raw_strings(session, household_id)
    budget = RegexBudget()
    hit: list[str] = []
    rows = 0
    for raw, count in strings:
        if does is RuleAction.rewrite:
            produced = budget.rewrite(trial, " ".join((raw or "").split()))
            claimed = produced != " ".join((raw or "").split())
            shown = f"{raw} \u2192 {tidy(produced)}" if claimed else raw
        else:
            claimed = budget.matches(trial, raw)
            shown = raw
        if claimed:
            rows += count
            if len(hit) < TRIAL_EXAMPLES:
                hit.append(shown)
    return Trial(
        matches=rows,
        considered=sum(count for _raw, count in strings),
        examples=hit,
        timed_out=bool(budget.exhausted),
    )


#: A group has to be at least this many distinct strings before it is worth
#: showing. Two is the real floor -- two spellings of one shop is exactly the
#: case -- and anything smaller is not a group.
MIN_GROUP = 2

#: And the shared prefix has to be at least this long, or every string starting
#: with "C" is a group.
MIN_PREFIX = 5


def _is_reference_token(token: str) -> bool:
    """Is this trailing token a per-transaction reference?

    Deliberately **not** `_reads_as_a_reference`, which answers a different
    question for the acquirer normalisation and is narrow on purpose: it says
    `BX1EG1ST5` is a name, because there it is protecting `SQ *CAFE 42` from
    losing its number. Reusing it here found none of the Amazon group, and
    loosening it would change what the acquirer rules match.

    The discriminator that separates the two cases is **where the digits
    are**. A reference interleaves them -- `BX1EG1ST5`, `IJ6YX2P45` -- while a
    name with a number in it puts the number at the end and stops. So: all
    digits, or a letter appearing after a digit.
    """
    if len(token) < 4:
        return False
    if token.isdigit():
        return True
    seen_digit = False
    for ch in token:
        if ch.isdigit():
            seen_digit = True
        elif ch.isalpha() and seen_digit:
            return True
    return False


def _reference_stripped(raw: str) -> str:
    """The string with its trailing reference tokens taken off.

    `WWW.AMAZON_ BX1EG1ST5` and `WWW.AMAZON_ IJ6YX2P45` are one shop and a
    per-transaction reference, and the reference is what makes this class of
    payee grow one new row per purchase. Amazon alone will have hundreds within
    a year.

    Deliberately not clever. Dropping trailing tokens that are mostly digits or
    that read as a reference catches all four groups in the review, and a
    cleverer rule would start claiming shops whose names contain numbers.
    """
    tokens = " ".join((raw or "").split()).split(" ")
    while len(tokens) > 1 and _is_reference_token(tokens[-1]):
        tokens.pop()
    trimmed = " ".join(tokens)
    # A reference welded to the name with no space -- `HOTELCOM61963202362647`.
    # Anchored to the start of the digit run: free to start at every digit, a
    # long run ending in a letter is quadratic (#221).
    return re.sub(r"(?<!\d)\d{4,}$", "", trimmed).strip() or trimmed


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A set of raw strings that look like one shop, and the rule that fixes it."""

    #: The pattern to propose, and what to match it with.
    pattern: str
    match_type: str
    #: How many distinct bank strings it gathers, and how many rows they carry.
    strings: int
    transactions: int
    #: How many payees those rows are spread across right now. The number that
    #: makes the case: four payees becoming one is worth a click.
    payees: int
    examples: list[str]


#: How many suggestions the Rules screen lists, largest first. A count of every
#: group says so when it is over this (#269); the client's `RULES_LISTED`
#: matches it, and `test_client_agrees` holds the two together.
SUGGESTIONS_LISTED = 20


def suggest_rules(
    session: Session, household_id: str, *, limit: int = SUGGESTIONS_LISTED
) -> list[Suggestion]:
    """Which payees are one rule apart? Issue #59.

    Four of the groups in the review are many-to-one -- every line is the same
    merchant and the noise is a per-transaction reference -- and **today's rule
    engine already handles every one of them**. `contains WWW.AMAZON -> Amazon`
    is four minutes' work.

    The gap is not capability. It is that nothing tells you the groups exist:
    the Payees screen lists payees alphabetically, the Rules screen lists
    rules, and neither looks at the raw strings the bank sent. So four payees
    become twenty-eight and the only way to notice is to scroll several hundred
    names and spot the runs by eye.

    Largest first, because the largest group is the one worth the click.
    """
    from ..models import Transaction

    rows = session.execute(
        select(
            Transaction.import_payee_original,
            Transaction.payee_id,
            func.count(),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.import_payee_original.is_not(None),
            Transaction.import_payee_original != "",
        )
        .group_by(Transaction.import_payee_original, Transaction.payee_id)
    ).all()

    grouped: dict[str, dict] = {}
    for raw, payee_id, count in rows:
        key = _reference_stripped(str(raw))
        if len(key) < MIN_PREFIX:
            continue
        bucket = grouped.setdefault(
            key, {"strings": set(), "payees": set(), "rows": 0}
        )
        bucket["strings"].add(str(raw))
        bucket["payees"].add(payee_id)
        bucket["rows"] += int(count)

    # Only what a rule would actually improve: one string is not a group, and a
    # group already resolving to a single payee needs no rule.
    found = [
        Suggestion(
            pattern=key,
            match_type=MatchType.contains.value,
            strings=len(bucket["strings"]),
            transactions=bucket["rows"],
            payees=len(bucket["payees"]),
            examples=sorted(bucket["strings"])[:TRIAL_EXAMPLES],
        )
        for key, bucket in grouped.items()
        if len(bucket["strings"]) >= MIN_GROUP and len(bucket["payees"]) > 1
    ]
    found.sort(key=lambda s: (-s.transactions, -s.strings, s.pattern.casefold()))
    return found[:limit]
