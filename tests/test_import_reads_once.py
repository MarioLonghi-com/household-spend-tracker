"""An upload unpacks a spreadsheet or a PDF once, not once per question.

`sniff` and `parse` each call `as_table_bytes`, so that the two can never
disagree about what they are reading. Called one after the other by the upload
route, that laid a PDF out -- the most expensive step of any import, and the one
the #87 limits exist to bound -- twice per file. `parsing.read` converts once
and hands both the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from statements import parsing, pdf_statement, sniffing, spreadsheet
from tests.conftest import HEADERS
from tests.test_import_queue import _world

FILES = Path(__file__).parent / "statement_files"


class Counted:
    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.real(*args, **kwargs)


@pytest.mark.parametrize(
    ("name", "module"),
    [("preamble_spanish.xls", spreadsheet), ("card_bill.pdf", pdf_statement)],
)
def test_read_converts_once_and_agrees_with_sniff_then_parse(monkeypatch, name, module):
    raw = (FILES / name).read_bytes()
    expected_sniff = sniffing.sniff(raw)
    expected_rows = parsing.parse(raw, expected_sniff.format)

    counted = Counted(module.to_csv_bytes)
    monkeypatch.setattr(module, "to_csv_bytes", counted)
    sniffed, rows = parsing.read(raw)

    assert counted.calls == 1, f"{name} was converted {counted.calls} times"
    assert sniffed.format == expected_sniff.format
    assert sniffed.warnings == expected_sniff.warnings
    assert rows == expected_rows
    assert len(rows) > 0


def test_an_uploaded_pdf_is_laid_out_once(client, monkeypatch):
    world = _world(client)
    counted = Counted(pdf_statement.to_csv_bytes)
    monkeypatch.setattr(pdf_statement, "to_csv_bytes", counted)

    answer = client.post(
        f"/api/households/{world['house']}/imports",
        data={"account_id": world["card"]},
        files={"file": ("card_bill.pdf", (FILES / "card_bill.pdf").read_bytes(), "application/pdf")},
        headers=HEADERS,
    )

    assert answer.status_code == 201, answer.text
    assert answer.json()["lines"], "the statement staged no lines"
    assert counted.calls == 1, f"the PDF was laid out {counted.calls} times"
