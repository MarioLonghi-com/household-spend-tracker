"""What the library raises when a file will not read.

The library has no web framework and no opinion about HTTP, but a caller that
has one needs to tell "this file is unreadable" (the user's problem, worth a
sentence on screen) apart from "this code is wrong" (a traceback). One base
class does that: catch `UnreadableStatement` and you have caught every refusal
this library makes on purpose.

Each message is a full sentence, because the message *is* what the user reads.
"""

from __future__ import annotations


class UnreadableStatement(Exception):
    """This file cannot be turned into transactions, and here is why."""


class UnreadableSpreadsheet(UnreadableStatement):
    """An .xls/.xlsx that could not be unpacked into rows."""


class UnreadablePdf(UnreadableStatement):
    """A PDF with no statement table in it — a scan, or a summary page."""


class LayoutMismatch(UnreadableStatement):
    """A named layout recognised the document and then failed its own check.

    Raised rather than returned: the layout said "this is an Itau fatura", read
    it, and could not make the rows add up to the total the document itself
    states. Falling back to the generic reader here would hand over numbers that
    are known to be wrong, which is worse than refusing.
    """
