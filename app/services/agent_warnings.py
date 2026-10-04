"""What staging noticed, for a caller deciding whether to commit.

`ImportPreview.warnings` is the channel the stage-then-commit split exists to
carry: *look at this before you commit*. On the file path it holds whatever the
format sniffer could not settle. On the agent path it was wired to a literal
``[]`` -- ``_preview(session, batch_row, lines, [])`` -- so an agent staging two
real Santander statements got ``warnings: []`` back and correctly read it as
"all clear". On that run it was not. Issue #43.

Nothing here refuses anything. Every one of these is a legitimate thing to
want, and a preview that blocked on a suspicion would be worse than one that
never mentioned it. They exist so a decision is *made* rather than defaulted
into, at the one moment it is still cheap.

Written against the staged lines rather than against the request, because the
lines are what would actually land. An agent's belief about what it sent and
what the ledger made of it are exactly the two things worth comparing, which is
what `declared` is for.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date as Date

from ..models import Account, AccountType, ImportLine, ImportOutcome

#: How many ids to name before the list stops being useful and starts being a
#: wall. The count is always exact; the examples are a sample.
NAMED = 8

#: Account types whose money mostly goes *out*, so a batch dominated by
#: incoming value is worth a second look. Not `credit_card`: on a card,
#: purchases are negative and the occasional large positive is a payment, which
#: is entirely normal and would make this fire on every card statement.
_MOSTLY_OUTGOING = (AccountType.checking, AccountType.savings, AccountType.cash)

#: The share of a batch's absolute value that has to arrive as *incoming* money
#: before the sign convention is worth questioning.
#:
#: Both conditions have to hold -- this share, and the batch netting positive --
#: and that pairing is the whole reason this is quiet enough to be worth
#: having. A salary month clears the share easily and nets positive too, but a
#: spending month with one salary in it does not net positive; the run that
#: prompted this was +2,026.00 across two months of heavy spending, with 38% of
#: the value incoming, and that combination is what a misfiled card statement
#: looks like.
INCOMING_SHARE = 0.30


@dataclass(frozen=True, slots=True)
class Declared:
    """What the agent believes it sent, for the server to check. Issue #50.

    All optional, all advisory. An agent that declares nothing behaves exactly
    as it did before. The division is deliberate: the agent is better at
    reading the document, and the ledger is the only thing that knows what
    actually got staged.
    """

    period_start: Date | None = None
    period_end: Date | None = None
    row_count: int | None = None
    total_minor: int | None = None


def after_staging(
    *,
    account: Account,
    lines: list[ImportLine],
    declared: Declared | None = None,
) -> list[str]:
    """Everything worth saying about this batch, in the caller's language."""
    said: list[str] = []
    said += _refused(lines)
    said += _already_here(lines)
    said += _matched(lines)
    said += _sign_convention(account, lines)
    if declared is not None:
        said += _against_declaration(declared, lines)
    return said


# --------------------------------------------------------------------------- #
# What staging did with the rows
# --------------------------------------------------------------------------- #


def _landing(lines: list[ImportLine]) -> list[ImportLine]:
    """The rows that would actually become transactions."""
    return [line for line in lines if line.outcome is ImportOutcome.created]


def _ids(lines: list[ImportLine]) -> str:
    """A sample of import ids, so a caller can find the rows it is told about."""
    found = [str((line.parsed or {}).get("import_id") or line.line_no) for line in lines]
    shown = ", ".join(found[:NAMED])
    return shown if len(found) <= NAMED else f"{shown} and {len(found) - NAMED} more"


def _already_here(lines: list[ImportLine]) -> list[str]:
    """`duplicate_skipped` reads like the importer doing its job. Issue #44.

    Sometimes it is. The rest of the time it is a genuinely new purchase that
    collided with a derived key an earlier batch already used -- the occurrence
    counter restarts per batch while `seen_ids` is seeded from the account --
    and the caller cannot tell the two apart from a count. So it is said out
    loud, with ids, rather than left in line detail that the slimmed response
    does not return by default.
    """
    skipped = [line for line in lines if line.outcome is ImportOutcome.duplicate_skipped]
    if not skipped:
        return []
    return [
        f"{len(skipped)} of {len(lines)} rows were already in this account and were "
        f"not staged: {_ids(skipped)}. If any of those are genuinely separate "
        "purchases, give each one its own external_id and send them again -- a row "
        "with an external_id is deduped on that alone."
    ]


def _refused(lines: list[ImportLine]) -> list[str]:
    refused = [line for line in lines if line.outcome is ImportOutcome.rejected]
    if not refused:
        return []
    why = Counter(line.reason or "no reason recorded" for line in refused)
    reasons = "; ".join(f"{reason} ({count})" for reason, count in why.most_common(3))
    return [f"{len(refused)} of {len(lines)} rows were refused and will not land: {reasons}."]


def _matched(lines: list[ImportLine]) -> list[str]:
    """Not an error, and not a no-op either: it changes a row already there."""
    matched = [line for line in lines if line.outcome is ImportOutcome.matched_existing]
    if not matched:
        return []
    return [
        f"{len(matched)} of {len(lines)} rows matched entries already in this account. "
        "Committing marks those as seen by the bank rather than adding them again."
    ]


# --------------------------------------------------------------------------- #
# Whether the money is pointing the right way
# --------------------------------------------------------------------------- #


def _sign_convention(account: Account, lines: list[ImportLine]) -> list[str]:
    """Credit-card rows imported into a checking account. Issue #46.

    A legitimate choice the operator is entitled to make, and an easy one to
    get wrong: on a card `INGRESO DE TARJETA` is money *into* the card, and in
    a checking account the same row is money out. On the run that prompted this
    roughly EUR 23.4k of card payments landed as income and nothing said
    anything -- the account simply read +10,541.43 until somebody looked at a
    balance.

    Warn, never block. This is exactly what stage-then-commit is for.
    """
    if account.type not in _MOSTLY_OUTGOING:
        return []

    amounts = [int((line.parsed or {}).get("amount") or 0) for line in _landing(lines)]
    incoming = sum(a for a in amounts if a > 0)
    outgoing = -sum(a for a in amounts if a < 0)
    value = incoming + outgoing
    net = incoming - outgoing
    if not value or net <= 0:
        return []

    share = incoming / value
    if share < INCOMING_SHARE:
        return []

    count = sum(1 for a in amounts if a > 0)
    return [
        f"{share:.0%} of this batch's value is money coming IN to a "
        f"{account.type.value} account ({count} of {len(amounts)} rows), and it nets "
        f"positive overall. Confirm these are income rather than, say, credit-card "
        f"repayments read from a card statement -- on a card those are money in, and "
        f"in a {account.type.value} account the same rows are money out."
    ]


# --------------------------------------------------------------------------- #
# Whether the agent's arithmetic survived the trip
# --------------------------------------------------------------------------- #


def _against_declaration(declared: Declared, lines: list[ImportLine]) -> list[str]:
    """What the agent said it was sending, against what was staged. Issue #50.

    The agent reads the document and the ledger checks the reading, which is
    the right division: an LLM is better at working out which blocks of a PDF
    are rows, and the server is the only thing that knows what actually landed.
    It also gives the agent a reason to read the statement's own printed
    totals, which is cheap and catches a lot.
    """
    said: list[str] = []
    landing = _landing(lines)

    if declared.row_count is not None and declared.row_count != len(lines):
        said.append(
            f"you declared {declared.row_count} rows and {len(lines)} arrived. "
            "A dropped page or a mis-split line looks exactly like this."
        )

    if declared.total_minor is not None:
        staged_total = sum(int((line.parsed or {}).get("amount") or 0) for line in landing)
        if staged_total != declared.total_minor:
            difference = staged_total - declared.total_minor
            said.append(
                f"you declared a total of {declared.total_minor} minor units and the "
                f"rows that would land come to {staged_total}, a difference of "
                f"{difference}. A merged row, a missing row or a flipped sign all "
                "show up here. Rows that were skipped or refused are not in this "
                "figure -- see the other warnings if there are any."
            )

    dates = [d for d in (_date_of(line) for line in landing) if d is not None]
    if dates:
        if declared.period_start is not None and min(dates) < declared.period_start:
            said.append(
                f"you declared the period starting {declared.period_start.isoformat()} "
                f"and the earliest row staged is {min(dates).isoformat()}."
            )
        if declared.period_end is not None and max(dates) > declared.period_end:
            said.append(
                f"you declared the period ending {declared.period_end.isoformat()} "
                f"and the latest row staged is {max(dates).isoformat()}."
            )

    return said


def _date_of(line: ImportLine) -> Date | None:
    raw = (line.parsed or {}).get("date")
    if not isinstance(raw, str):
        return None
    try:
        return Date.fromisoformat(raw)
    except ValueError:  # pragma: no cover - `stage` writes these itself
        return None
