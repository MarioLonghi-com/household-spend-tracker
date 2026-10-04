"""A statement file costs what a statement costs, however it was built.

Issues #85, #86, #87 and #90, from the 2026-09-24 review. Each reader trusted
its input to be the size it looked: an OFX regex that went quadratic on tags
that never closed, an .xls that expanded one far-corner cell into the whole
65,536 x 256 grid, a PDF reader with no page count and no ceiling on what a
compressed stream unpacks to, and a CSV cell or a figure too large to hold that
turned into a server error instead of a sentence.

Every hostile file here is built in the test, a few kilobytes at most, and
every test is bounded in time -- the old code took seconds to hours on these.
"""

from __future__ import annotations

import io
import time
import zlib

import pytest

from app.audit.batch import batch
from app.models import Account, AccountType, BatchKind, BatchStatus, ImportOutcome
from app.services import importing
from statements import (
    UnreadablePdf,
    UnreadableSpreadsheet,
    UnreadableStatement,
    ofx,
    parse,
    parsing,
    pdf_limits,
    sniffing,
    spreadsheet,
)
from tests.conftest import HEADERS
from tests.test_api import _household_with_accounts


class _Clock:
    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.started


# --------------------------------------------------------------------------- #
# #85 -- OFX
# --------------------------------------------------------------------------- #


def test_opening_transaction_tags_that_never_close_read_in_linear_time():
    """The issue's own test: half a million unclosed tags, well under a second.

    The old regex restarted its scan at every opening tag; 35 KiB of these took
    three seconds and this is 4.5 MB.
    """
    raw = b"<OFX>" + b"<STMTTRN>" * 500_000
    with _Clock() as clock:
        statement = ofx.read(raw)
    assert statement.transactions == []
    assert clock.seconds < 1.5


def test_transactions_are_what_sits_between_each_opening_and_closing_tag():
    """The linear walk finds exactly the blocks the regex found."""
    raw = (
        b"<OFX><BANKTRANLIST>"
        b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260105<TRNAMT>-12.34<FITID>A1<NAME>Corner Shop"
        b"</STMTTRN>"
        # Lower case, and a stray opening tag inside: still one transaction.
        b"<stmttrn><TRNTYPE>CREDIT<STMTTRN><DTPOSTED>20260106<TRNAMT>100.00<FITID>B2"
        b"<NAME>Employer Ltd</stmttrn>"
        # Opened and never closed: not a transaction.
        b"<STMTTRN><DTPOSTED>20260107<TRNAMT>-1.00<FITID>C3"
    )
    statement = ofx.read(raw)
    assert [(t.fitid, t.name, str(t.amount)) for t in statement.transactions] == [
        ("A1", "Corner Shop", "-12.34"),
        ("B2", "Employer Ltd", "100.00"),
    ]


def test_a_megabyte_date_costs_a_moment_and_one_line():
    raw = b"<OFX><STMTTRN><DTPOSTED>" + b"[" * 1_000_000 + b"<TRNAMT>1.00</STMTTRN>"
    with _Clock() as clock:
        statement = ofx.read(raw)
    assert clock.seconds < 1.0
    assert statement.transactions[0].posted is None
    assert "no usable date" in statement.transactions[0].problem


# --------------------------------------------------------------------------- #
# #86 -- .xls
# --------------------------------------------------------------------------- #


def _workbook(*sheets: dict[tuple[int, int], object]) -> bytes:
    import xlwt

    book = xlwt.Workbook()
    for index, cells in enumerate(sheets):
        sheet = book.add_sheet(f"s{index}")
        for (row, column), value in cells.items():
            sheet.write(row, column, value)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_one_cell_in_the_far_corner_does_not_expand_the_grid():
    """A 5.6 KB file used to become 16.7 million cells per sheet, six sheets over.

    Only the first sheet is read, and a row is as long as its own last cell.
    What comes out is the same two rows it always was -- 256 wide, because that
    is how wide the sheet is.
    """
    corner = {(0, 0): "Fecha", (65535, 255): "y"}
    raw = _workbook(corner, *[{(0, 0): "other sheet", (65535, 255): "z"}] * 5)
    with _Clock() as clock:
        out = spreadsheet.to_csv_bytes(raw).decode()
    assert clock.seconds < 3.0
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == "Fecha" + "," * 255
    assert lines[1] == "," * 255 + "y"
    assert "other sheet" not in out


def test_a_sheet_larger_than_any_statement_is_refused(monkeypatch):
    monkeypatch.setattr(spreadsheet, "MAX_CELLS", 100)
    raw = _workbook({(row, column): "x" for row in range(20) for column in range(6)})
    with pytest.raises(UnreadableSpreadsheet, match="more than 100 cells"):
        spreadsheet.to_csv_bytes(raw)
    # And through the front door, as the refusal every caller already catches.
    with pytest.raises(UnreadableStatement, match="export it from your bank as CSV"):
        sniffing.sniff(raw)


def test_a_sheet_within_the_limit_reads_every_row(monkeypatch):
    monkeypatch.setattr(spreadsheet, "MAX_CELLS", 120)
    raw = _workbook({(row, column): f"{row}.{column}" for row in range(20) for column in range(6)})
    lines = spreadsheet.to_csv_bytes(raw).decode().splitlines()
    assert len(lines) == 20
    assert lines[19] == "19.0,19.1,19.2,19.3,19.4,19.5"


# --------------------------------------------------------------------------- #
# #87 -- PDF
# --------------------------------------------------------------------------- #


def _pdf(
    streams: list[bytes],
    *,
    pages: int = 1,
    filter_name: bytes | None = b"/FlateDecode",
    encode=None,
) -> bytes:
    """A minimal PDF whose every page runs the same content streams, in order.

    ``encode`` turns a stream's plain bytes into what the file carries; by
    default zlib, to match ``filter_name``.
    """
    if encode is None:
        encode = (lambda data: zlib.compress(data, 9)) if filter_name else (lambda data: data)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, filled in below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Font << /F1 3 0 R >> >>",
    ]
    content_refs = []
    for stream in streams:
        body = encode(stream)
        filters = b" /Filter " + filter_name if filter_name else b""
        objects.append(
            b"<< /Length %d%s >>\nstream\n" % (len(body), filters) + body + b"\nendstream"
        )
        content_refs.append(b"%d 0 R" % len(objects))
    first_page = len(objects) + 1
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 4 0 R /Contents ["
        + b" ".join(content_refs)
        + b"] >>"
    )
    objects.extend([page] * pages)
    kids = b" ".join(b"%d 0 R" % (first_page + i) for i in range(pages))
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _text(words: str) -> bytes:
    return b"BT /F1 10 Tf 72 700 Td (" + words.encode() + b") Tj ET"


def _refused(raw: bytes, match: str, *, within: float = 5.0) -> None:
    """Refused quickly, and in the limit's own words -- the sentence starts the
    message, rather than arriving wrapped in "could not be read (...)"."""
    with _Clock() as clock, pytest.raises(UnreadablePdf, match="^" + match):
        sniffing.sniff(raw)
    assert clock.seconds < within


def test_five_hundred_pages_are_refused_before_any_is_read():
    """Counted, not read: a page that were read would cost its content."""
    raw = _pdf([_text("Date Description Amount")], pages=500)
    _refused(raw, "this PDF has more than 50 pages")


def test_a_statement_at_the_page_limit_is_still_read():
    """Refused for having no table, which means every page was looked at."""
    raw = _pdf([_text("nothing here")], pages=pdf_limits.MAX_PAGES)
    with pytest.raises(UnreadablePdf, match="no statement table"):
        sniffing.sniff(raw)


def test_a_zero_filled_stream_is_refused_without_unpacking_it():
    """40 MB of zeros compresses to 40 KB. It stops at the ceiling, not after."""
    raw = _pdf([b"\0" * (40 * 1024 * 1024)])
    assert len(raw) < 100_000
    _refused(raw, "this PDF contains a compressed block that unpacks to more than 16 MB")


def test_many_streams_under_the_ceiling_are_refused_together(monkeypatch):
    monkeypatch.setattr(pdf_limits, "MAX_DECODED_BYTES", 150_000)
    raw = _pdf([b"%" + b" " * 60_000 + b"\n"] * 3)
    _refused(raw, "this PDF unpacks to more than")


def test_a_run_length_stream_is_capped_too(monkeypatch):
    """Two bytes per 128 out: the other filter that makes data bigger."""
    monkeypatch.setattr(pdf_limits, "MAX_STREAM_BYTES", 10_000)
    raw = _pdf([b""], filter_name=b"/RunLengthDecode", encode=lambda _: b"\x81 " * 1000 + b"\x80")
    _refused(raw, "this PDF contains a compressed block that unpacks to more than")


def _lzw_run(codes: int) -> bytes:
    """LZW that grows by one byte per code: 1 + 2 + 3 + ... bytes of zeros.

    The clear code, a zero, and then each code naming the entry about to be
    made -- the case every LZW decoder has to handle, and the fastest-growing
    one. Code widths follow pdfminer's decoder (9 bits until the table holds
    511 entries).
    """
    bits: list[str] = []
    table, width = 258, 9

    def put(code: int) -> None:
        bits.append(format(code, f"0{width}b"))

    put(256)
    put(0)
    for _ in range(codes):
        put(table)
        table += 1
        width = 10 if table >= 511 else width
    packed = "".join(bits)
    packed += "0" * (-len(packed) % 8)
    return int(packed, 2).to_bytes(len(packed) // 8, "big")


def test_an_lzw_stream_is_capped_too(monkeypatch):
    monkeypatch.setattr(pdf_limits, "MAX_STREAM_BYTES", 10_000)
    raw = _pdf([b""], filter_name=b"/LZWDecode", encode=lambda _: _lzw_run(240))
    _refused(raw, "this PDF contains a compressed block that unpacks to more than")


def test_pages_sharing_one_stream_are_charged_for_every_page(monkeypatch):
    """The review's 8 KB file: twenty pages, one stream, five seconds.

    Decoded once and interpreted twenty times, so it is the interpreting that is
    counted -- each page that runs the stream pays for it again.
    """
    monkeypatch.setattr(pdf_limits, "MAX_CONTENT_BYTES", 50_000)
    stream = b"q Q " * 1_000
    _refused(_pdf([stream], pages=20), "the pages of this PDF hold more drawing instructions")
    # Ten pages of the same is 40 KB, inside the limit: read, and no table.
    with pytest.raises(UnreadablePdf, match="no text in this PDF"):
        sniffing.sniff(_pdf([stream], pages=10))


def test_a_page_of_a_hundred_thousand_glyphs_is_refused(monkeypatch):
    monkeypatch.setattr(pdf_limits, "MAX_OBJECTS", 1_000)
    _refused(_pdf([_text("x" * 5_000)]), "this PDF puts more than 1,000 characters and shapes")


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda good: good[:-1] + bytes([good[-1] ^ 0xFF]), id="wrong-checksum"),
        pytest.param(lambda good: good[:-4], id="no-checksum"),
    ],
)
def test_a_damaged_checksum_reads_as_it_always_did(damage):
    """pdfminer keeps what a stream held when only its checksum is bad or gone,
    and so does the capped reader -- byte for byte, the last few included."""
    from pdfminer.pdftypes import PDFStream
    from pdfminer.psparser import LIT

    plain = _text("Hello, the last bytes of this stream matter too")
    damaged = damage(zlib.compress(plain))

    def decoded() -> bytes:
        return PDFStream({"Filter": LIT("FlateDecode")}, damaged).get_data()

    with pdf_limits.budget():
        assert decoded() == plain
    assert decoded() == plain


def test_the_limits_apply_only_inside_a_budget(monkeypatch):
    """Anything else in the process that uses pdfminer sees it unchanged."""
    from pdfminer.pdftypes import PDFStream
    from pdfminer.psparser import LIT

    monkeypatch.setattr(pdf_limits, "MAX_STREAM_BYTES", 1_000)
    pdf_limits._install()

    def stream() -> PDFStream:
        return PDFStream({"Filter": LIT("FlateDecode")}, zlib.compress(b"a" * 5_000))

    assert stream().get_data() == b"a" * 5_000
    with pdf_limits.budget(), pytest.raises(UnreadablePdf):
        stream().get_data()


# --------------------------------------------------------------------------- #
# #90 -- a cell or a figure too large
# --------------------------------------------------------------------------- #

HUGE_CELL = b'Date,Amount,Description\n2026-01-05,-1.00,"' + b"x" * 200_000 + b'"\n'


def test_a_cell_past_the_csv_field_limit_is_a_sentence_not_a_crash():
    with pytest.raises(UnreadableStatement, match="longer than 131,072 characters"):
        sniffing.sniff(HUGE_CELL)
    fmt = sniffing.Format(date_column="Date", amount_column="Amount")
    with pytest.raises(UnreadableStatement, match="longer than 131,072 characters"):
        parse(HUGE_CELL, fmt)


def test_the_upload_answers_422_for_a_file_that_hits_a_limit(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    said = {}
    for name, raw in [
        ("huge.csv", HUGE_CELL),
        ("long.pdf", _pdf([_text("Date Amount")], pages=60)),
    ]:
        answer = client.post(
            f"/api/households/{house}/imports",
            data={"account_id": checking, "force": "false"},
            files={"file": (name, raw, "application/octet-stream")},
            headers=HEADERS,
        )
        assert answer.status_code == 422, (name, answer.text)
        said[name] = answer.json()["detail"]
    assert "longer than 131,072 characters" in said["huge.csv"]
    assert "more than 50 pages" in said["long.pdf"]


SAVINGS_HEADER = b"Date,Description,Amount,Fee,Balance\n"


@pytest.fixture()
def two_accounts(session, owner, household):
    """Two accounts in two currencies: the figure too large is too large in both."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            "eur": Account(
                household_id=household.id, name="Euro", type=AccountType.checking, currency="EUR"
            ),
            "gbp": Account(
                household_id=household.id, name="Pound", type=AccountType.checking, currency="GBP"
            ),
        }
        session.add_all(made.values())
    return made


def _stage(session, owner, household, account, raw: bytes):
    sniffed = sniffing.sniff(raw)
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={"filename": "s.csv", "sha256": importing.file_digest(raw)},
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged
        )
        staged.status = BatchStatus.preview
    return staged, sorted(lines, key=lambda line: (line.line_no, line.outcome.value))


TOO_LARGE = "99999999999999999999.00"


@pytest.mark.parametrize("which", ["eur", "gbp"])
@pytest.mark.parametrize("column", ["Fee", "Balance"])
def test_one_figure_too_large_rejects_its_line_and_not_the_file(
    session, owner, household, two_accounts, which, column
):
    """A fee or a balance that overflows costs its own line, like an amount does."""
    rows = [
        ["2026-01-05", "Salary", "100.00", "0.00", "100.00"],
        ["2026-01-06", "Corner Shop", "-10.00", "0.50", "89.50"],
        ["2026-01-07", "Cafe", "-2.00", "0.00", "87.50"],
    ]
    rows[1][3 if column == "Fee" else 4] = TOO_LARGE
    raw = SAVINGS_HEADER + b"".join(",".join(row).encode() + b"\n" for row in rows)

    staged, lines = _stage(session, owner, household, two_accounts[which], raw)

    outcomes = {line.line_no: line.outcome for line in lines if line.line_no != 3}
    assert outcomes == {2: ImportOutcome.created, 4: ImportOutcome.created}
    (bad,) = [line for line in lines if line.line_no == 3]
    assert bad.outcome is ImportOutcome.rejected
    assert "too large to record as money" in bad.reason
    assert [line.parsed["amount"] for line in lines if line.parsed] == [10_000, -200]
    notes = (staged.source or {}).get("warnings") or []
    assert any("running balance was not checked" in note for note in notes), notes


# --------------------------------------------------------------------------- #
# #222 -- rows of junk, each paying for every date format
# --------------------------------------------------------------------------- #

#: Well clear of a shared CI runner under xdist and coverage, which took 2.6 s
#: and 5.3 s for what a laptop does in half a second, and still a fraction of
#: the 43 s the defect took.
JUNK_WITHIN = 10


def test_four_hundred_thousand_rows_of_junk_are_refused_in_a_moment():
    """43 seconds before: every `zz` tried all 25 date formats."""
    with _Clock() as clock, pytest.raises(UnreadableStatement, match="more than 200,000 rows"):
        parsing.read(b"zz,1\n" * 400_000)
    assert clock.seconds < JUNK_WITHIN


def test_a_file_whose_dates_do_not_read_is_refused_past_the_cap():
    """Under the row cap, the undated-row cap is what stops it."""
    raw = b"Date,Amount\n" + b"zz,1\n" * 150_000
    with _Clock() as clock, pytest.raises(UnreadableStatement, match="no readable date"):
        parsing.read(raw)
    assert clock.seconds < JUNK_WITHIN


def test_a_few_undated_rows_still_cost_one_line_each():
    raw = b"Date,Amount,Description\n" + b"2026-01-05,-1.00,Shop\n" * 10 + b"Total,-10.00,\n"
    _, rows = parsing.read(raw)
    assert [row.problem for row in rows[:10]] == [None] * 10
    assert rows[10].problem == "could not read 'Total' as a date"


def test_a_real_sized_export_still_parses_every_row():
    lines = [b"Date,Description,Amount"]
    for n in range(5_000):
        day = f"{1 + n % 28:02d}/{1 + n % 12:02d}/2025".encode()
        lines.append(day + b",Shop number " + str(n).encode() + b",-" + str(n % 900 + 1).encode() + b".25")
    with _Clock() as clock:
        sniffed, rows = parsing.read(b"\n".join(lines) + b"\n")
    assert sniffed.format.date_format == "%d/%m/%Y"
    assert len(rows) == 5_000
    assert [row.problem for row in rows if row.problem] == []
    assert rows[-1].when.isoformat() == "2025-08-16"
    assert str(rows[-1].amount) == "-500.25"
    assert clock.seconds < 5


def test_the_cheap_date_tries_come_first_and_the_long_list_still_reads():
    assert sniffing.parse_date("2025-12-24 11:58:09").isoformat() == "2025-12-24"
    assert sniffing.parse_date("Dec 24, 2025", "%d/%m/%Y").isoformat() == "2025-12-24"
    assert sniffing.parse_date("24.12.25").isoformat() == "2025-12-24"
    with pytest.raises(ValueError, match="could not read 'zz' as a date"):
        sniffing.parse_date("zz", "%d/%m/%Y")


# --------------------------------------------------------------------------- #
# #224 -- a refusal is a sentence, not the reader's internals
# --------------------------------------------------------------------------- #

LIBRARY_WORDS = ("xlrd", "struct", "Errno", "pdfminer", "pdfplumber", "Traceback", "unpack")


def test_a_truncated_workbook_or_pdf_is_refused_in_the_apps_own_words(client, caplog):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    said = {}
    for name, raw in [
        ("truncated.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40),
        ("truncated.pdf", b"%PDF-1.4\n1 0 obj << /Type /Catalog"),
    ]:
        with caplog.at_level("DEBUG", logger="statements"):
            answer = client.post(
                f"/api/households/{house}/imports",
                data={"account_id": checking, "force": "false"},
                files={"file": (name, raw, "application/octet-stream")},
                headers=HEADERS,
            )
        assert answer.status_code == 422, (name, answer.text)
        said[name] = answer.json()["detail"]
    assert said["truncated.xls"].startswith("this file could not be read as a spreadsheet.")
    assert said["truncated.pdf"].startswith("this file could not be opened as a PDF.")
    for detail in said.values():
        assert not [word for word in LIBRARY_WORDS if word.lower() in detail.lower()], detail
    # What the reader said is still somewhere a developer can find it.
    assert any(record.exc_info for record in caplog.records if record.name.startswith("statements"))
