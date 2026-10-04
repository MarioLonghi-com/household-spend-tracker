"""Rules for documents that are not a table.

The generic reader in `pdf_statement` measures a header row and assigns words to
columns. That covers any statement laid out as one table, which is most of them,
and it costs nothing when a new bank turns up.

A designed credit-card bill is not that. The Itau fatura puts the transaction
list in a narrow left column and a *future instalments* table beside it, with
summary boxes, marketing copy and a payment slip interleaved. There is no header
row that describes the page, and reading it as a table produced 146 rows of
nonsense.

So it gets a rule. **And a rule for one bank's design is the least durable code
in this project** -- the bank will redesign it, and nothing in the file says it
has. The mitigation is the only one that actually works: every layout here must
name a figure the document states about itself, and the sum of what the rule
extracted is checked against it. When Itau moves things around, the sum stops
matching and the import is refused with a sentence saying so. A layout rule that
cannot be checked this way does not belong here.

That is the difference between this file and the rest of the importer: the rest
degrades (a column it cannot place becomes a rejected line you can see in the
preview); a layout rule would *silently* mis-read, so it is made to fail loudly
instead.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .errors import LayoutMismatch


@dataclass(slots=True)
class Extracted:
    """What a layout found, and the figure it must reconcile against."""

    header: list[str]
    rows: list[list[str]]
    #: The document's own statement of the total, and what it is called there.
    stated_total: Decimal
    stated_as: str
    #: The sum the rule believes it extracted.
    extracted_total: Decimal
    warnings: list[str] = field(default_factory=list)


def fold(text: str) -> str:
    """Accents removed, for matching the phrases a layout is anchored on.

    A PDF's text layer is whatever the producer embedded, and accents survive
    it unevenly -- the same bill can give "lançamentos" or "lancamentos"
    depending on the font's encoding. Anchoring on the exact bytes makes a rule
    fail for a reason that has nothing to do with the layout.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    ).upper()


def _money(text: str) -> Decimal:
    """Brazilian formatting: 1.098,90 and a sign that may be a separate word."""
    cleaned = text.replace(" ", "").replace("\u00a0", "").replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise LayoutMismatch(f"could not read {text!r} as an amount") from exc


# --------------------------------------------------------------------------- #
# Itau fatura (credit card bill)
# --------------------------------------------------------------------------- #

#: `20/02 PROGRAMA DE CASHBACK - 8,00`, `01/02 Uber UBER *TRIP HELP.U 178,98`
_ENTRY = re.compile(r"^(\d{2}/\d{2})\s+(.*?)\s+(-?\s?[\d.]*\d,\d{2})$")
_TOTAL = "Total dos lançamentos atuais"
_ISSUED = re.compile(r"Emiss[ãa]o:\s*(\d{2})/(\d{2})/(\d{4})")
def _lines(pages: list[list[list[dict]]]) -> list[tuple[int, list[dict]]]:
    return [(index, line) for index, page in enumerate(pages) for line in page]


def itau_fatura(pages: list[list[list[dict]]]) -> Extracted | None:
    """Itau's credit-card bill.

    Returns None when this is not that document, so the caller can try the next
    rule or fall back to reading it as a table.
    """
    everything = _lines(pages)
    flat = [" ".join(w["text"] for w in line) for _, line in everything]
    folded = [fold(text) for text in flat]
    if not any(fold(_TOTAL) in text for text in folded):
        return None
    if not any("ESTABELECIMENTO" in text for text in folded):
        return None

    # Where the transaction table stops. Measured from its own amount heading
    # -- "DATA ESTABELECIMENTO VALOR EM R$" and "DATA PRODUTOS/SERVIÇOS VALOR
    # EM R$" -- rather than from the instalments heading beside it, which
    # starts further right and let "Próxima fatura 88,00" bleed into the rows.
    # The narrowest such heading is the one that does not span both columns.
    edges: list[float] = []
    for _, line in everything:
        ordered = sorted(line, key=lambda w: w["x0"])
        texts = [fold(w["text"]) for w in ordered]
        if not any(t.startswith(("ESTABELECIMENTO", "PRODUTOS")) for t in texts):
            continue
        anchor = next(
            (i for i, t in enumerate(texts) if t.startswith(("ESTABELECIMENTO", "PRODUTOS"))),
            None,
        )
        after = [w for w, t in zip(ordered[anchor:], texts[anchor:], strict=True) if t == "R$"]
        if after:
            edges.append(after[0]["x1"])
    if not edges:
        return None
    cut = min(edges) + 5

    issued: date | None = None
    for text in flat:
        found = _ISSUED.search(text)
        if found:
            day, month, year = (int(part) for part in found.groups())
            issued = date(year, month, day)
            break
    if issued is None:
        return None

    stated: Decimal | None = None
    rows: list[list[str]] = []
    total = Decimal(0)

    for _, line in everything:
        left = [w for w in sorted(line, key=lambda w: w["x0"]) if w["x1"] <= cut]
        if not left:
            continue
        text = " ".join(w["text"] for w in left)

        if fold(_TOTAL) in fold(text):
            stated = _money(text.split()[-1])
            continue

        entry = _ENTRY.match(text)
        if not entry:
            continue
        stamp, description, amount = entry.groups()
        value = _money(amount)
        total += value

        day, month = (int(part) for part in stamp.split("/"))
        # The entries carry no year. A bill covers the weeks before it was
        # issued, so a month later than the issue month belongs to the year
        # before -- a January entry on a February bill is this year, a December
        # entry on a January bill is not.
        year = issued.year - 1 if month > issued.month else issued.year
        rows.append([date(year, month, day).isoformat(), description, str(-value)])

    if stated is None:
        return None

    return Extracted(
        header=["date", "description", "amount"],
        rows=rows,
        stated_total=stated,
        stated_as=_TOTAL,
        # Negated on the way out: the bill lists what you spent as a positive
        # number, and spending on a card account is money going out.
        extracted_total=total,
        warnings=[
            "amounts are reversed from the bill's own signs, so a purchase is money out of "
            "the card account and a refund is money in",
        ],
    )


LAYOUTS = (itau_fatura,)


def apply(pages: list[list[list[dict]]]) -> bytes | None:
    """CSV from the first layout that recognises this document, or None.

    Refuses rather than returning anything whose sum does not match the figure
    the document states about itself.
    """
    for layout in LAYOUTS:
        found = layout(pages)
        if found is None:
            continue
        if found.extracted_total != found.stated_total:
            raise LayoutMismatch(
                f"this looks like a document this app has a rule for, but the entries it found "
                f"add up to {found.extracted_total} and the document says "
                f'"{found.stated_as}" is {found.stated_total}. The layout has probably changed, '
                "so nothing has been imported -- importing part of a bill would be worse."
            )
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(found.header)
        writer.writerows(found.rows)
        return out.getvalue().encode("utf-8")
    return None
