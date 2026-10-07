"""Which characters a bank means as a minus sign.

A typographically formatted export, and the text layer of a PDF, write a debit
with a real minus sign or a dash rather than the ASCII hyphen-minus. Read only
for ``-``, every one of them was dropped as decoration and the debit imported
as money in (issue #262).

Kept apart from `sniffing` so that `pdf_statement`, which `sniffing` imports,
can use the same table without a cycle.
"""

from __future__ import annotations

import re

#: Every character read as ``-`` in an amount. A named table on purpose:
#: `app/services/payees.py` holds its own dash table, `UNICODE_DASHES` (#268),
#: which must be a superset of this; `tests/test_payee_fold_unicode.py` says so.
MINUS_SIGNS: dict[str, str] = {
    "\u2212": "MINUS SIGN",
    "\u2012": "FIGURE DASH",
    "\u2013": "EN DASH",
    "\u2014": "EM DASH",
    "\ufe63": "SMALL HYPHEN-MINUS",
    "\uff0d": "FULLWIDTH HYPHEN-MINUS",
}

_TO_HYPHEN_MINUS = str.maketrans(dict.fromkeys(MINUS_SIGNS, "-"))


def ascii_minus(text: str) -> str:
    """``text`` with every minus-like character in `MINUS_SIGNS` made ``-``."""
    return text.translate(_TO_HYPHEN_MINUS)


#: ``12.50 DR`` and ``12.50 CR``: a debit or a credit said in letters after the
#: figure, as ledger-style statements write it. Only after something ending in a
#: digit, a bracket or a sign, so a word that merely ends in "cr" is not one
#: (#84) -- and "12.50- CR" is signed twice rather than a debit.
_DEBIT_CREDIT = re.compile(r"^(?P<figure>.*[\d)+-])\s*(?P<marker>DR|CR)\.?$", re.IGNORECASE)


def debit_credit(text: str) -> tuple[str, str | None]:
    """``(the figure, "-" for DR, "+" for CR)``, or ``(text, None)`` without one.

    Before #84 the letters were stripped as decoration with the rest of the
    non-digits, so ``12.50 DR`` -- a debit -- imported as money in.
    """
    match = _DEBIT_CREDIT.match((text or "").strip())
    if match is None:
        return text, None
    return match["figure"].rstrip(), "-" if match["marker"].upper() == "DR" else "+"


def written_negative(text: str) -> bool:
    """Does this amount, as written, carry a minus sign, brackets or ``DR``?

    At either end: ``-12.50`` and ``12.50-`` are both debits (issue #261), and
    so is ``12.50 DR`` (#84).
    """
    figure, marker = debit_credit(ascii_minus(text or ""))
    if marker is not None:
        return marker == "-"
    stripped = figure.strip()
    if not stripped:
        return False
    return stripped[0] == "-" or stripped[-1] == "-" or ("(" in stripped and ")" in stripped)
