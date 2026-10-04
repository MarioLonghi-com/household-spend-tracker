"""Which characters a bank means as a minus sign.

A typographically formatted export, and the text layer of a PDF, write a debit
with a real minus sign or a dash rather than the ASCII hyphen-minus. Read only
for ``-``, every one of them was dropped as decoration and the debit imported
as money in (issue #262).

Kept apart from `sniffing` so that `pdf_statement`, which `sniffing` imports,
can use the same table without a cycle.
"""

from __future__ import annotations

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


def written_negative(text: str) -> bool:
    """Does this amount, as written, carry a minus sign or brackets?

    At either end: ``-12.50`` and ``12.50-`` are both debits (issue #261).
    """
    stripped = ascii_minus(text or "").strip()
    if not stripped:
        return False
    return stripped[0] == "-" or stripped[-1] == "-" or ("(" in stripped and ")" in stripped)
