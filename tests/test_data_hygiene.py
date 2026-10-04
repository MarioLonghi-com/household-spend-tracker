"""Two controls for two claims that were only ever written down.

Both of these exist because of the same morning. On 2026-09-19 the statement
fixtures' own docstring said they were synthetic, and two of them carried a real
name and a card's last four -- through every commit, every review and every test
run, because **a docstring is a claim and only a test is a control**. The same
week, `scripts/db_view.py` was found to classify the tables it recognised and
skip the rest in silence, which is the identical failure one layer down.

So: one test that the fixtures carry nobody real, and one that the snapshot
cannot be built while a table is unaccounted for.

The tree-wide half grew before the repository went public (issue #201): first
names and personal email addresses as digests, the markers of private notes
and of one machine's layout, the shapes of real secrets, binaries outside the
two folders anybody scans, a PDF's own metadata, and account numbers beside
the words banks put in front of them.

Each detector here is checked against a string that *should* trip it, in the
same test. A guard that has never fired is a guard nobody has proved.
"""

from __future__ import annotations

import codecs
import functools
import hashlib
import pathlib
import re
import sqlite3

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

FIXTURES = pathlib.Path(__file__).parent / "statement_files"

#: Addresses a fixture is allowed to contain. RFC 2606 reserves these precisely
#: so that example data can be obviously example data.
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "example.edu"}

_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
_EMAIL_PARTS = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
#: 13-19 digits, optionally in groups of four. The length alone is a poor
#: signal -- a timestamp and a reference number both reach it -- so every hit is
#: put through Luhn below, which is what a card number passes and a reference
#: number does not.
_DIGIT_RUN = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
#: Two letters, two check digits, then the account part. Validated mod-97, for
#: the same reason.
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")

#: The names that were actually in the tree. An explicit denylist on top of the
#: shape detectors, because no checksum catches a surname -- and these are the
#: exact strings a careless re-copy of a real statement would bring back.
#:
#: Kept as salted digests rather than the words themselves, because the repo is
#: public and a list of the names it must never carry is a list of those names.
#: A digest only hides them from a reader, not from a determined guesser; the
#: point is that the file stops being the leak it guards against. To add one,
#: append `_digest("<word>")`'s output -- lowercase, letters only.
#:
#: The first names are on it too. They are common words in some languages, so
#: a hit may be innocent -- reword it anyway ("the owner", "the maintainer").
REAL_NAME_DIGESTS = frozenset({
    "22104cb7310ff67d949284447e57d6ab",
    "48279d86ac18d27a6ce0b8cf5cabf844",
    "999b74915a90f07ad715769923721431",
    "2af484ed38cd3ead831baf939ceec992",
    "237100faf00448072d3544a580f77423",
    "4aa99d639db27c48bcbcb2ffb3d6f180",
    "dc1480e8b5e755936479a484a771d2c9",
})

#: Personal email local-parts, by digest, in canonical form (see
#: `_canonical_local_part`). Domains cannot be denylisted: `@gmail.com` is used
#: on purpose by the canonicalisation tests, with made-up people in front of it.
PRIVATE_LOCALPART_DIGESTS = frozenset({
    "237100faf00448072d3544a580f77423",
    "560dfd1d59aa1815535c227dcf02c1e7",
})

#: Whole lowercase runs, and the same text split at camelCase humps, so
#: `JaneDoe` is checked as `jane` and `doe` as well as `janedoe`.
_RUN = re.compile(r"[a-z]+")
_HUMP = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


@functools.cache
def _digest(word: str) -> str:
    return hashlib.sha256(f"spend-tracker:{word}".encode()).hexdigest()[:32]


def _canonical_local_part(local: str) -> str:
    """Lowercase, no `+tag`, no dots -- how Gmail reads it, and the strictest."""
    return local.lower().split("+", 1)[0].replace(".", "")


def private_emails_in(line: str) -> list[str]:
    """Addresses on `line` whose local-part is on the denylist, masked."""
    return [
        f"{local[:2]}…@{domain}"
        for local, domain in _EMAIL_PARTS.findall(line)
        if _digest(_canonical_local_part(local)) in PRIVATE_LOCALPART_DIGESTS
    ]


def real_names_in(text: str, digests: frozenset[str] = REAL_NAME_DIGESTS) -> list[str]:
    """The words in `text` whose digest is on the denylist, masked for printing.

    Masked because a failing run prints its message into a public CI log, and
    the name is what must not be there.
    """
    words = set(_RUN.findall(text.lower()))
    words |= {hump.lower() for hump in _HUMP.findall(text)}
    return sorted(f"{w[:2]}…({len(w)})" for w in words if _digest(w) in digests)


def looks_like_a_timestamp(digits: str) -> bool:
    """An OFX ``DTPOSTED`` is fourteen digits, and one in ten passes Luhn.

    `bank_sgml.ofx` carries `20251001120000` and it does. Length alone cannot
    tell a statement's own timestamps from a card number, so the ones that parse
    as a date in a range a bank statement could plausibly carry are set aside
    first. A card number would have to be exactly 8 or 14 digits *and* open with
    a real date to hide here.
    """
    if len(digits) not in {8, 14}:
        return False
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return False
    if len(digits) == 8:
        return True
    hour, minute, second = int(digits[8:10]), int(digits[10:12]), int(digits[12:14])
    return hour <= 23 and minute <= 59 and second <= 59


def luhn_ok(digits: str) -> bool:
    """The check digit every card number carries and few other numbers do."""
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_ok(candidate: str) -> bool:
    """IBAN mod-97: move the first four characters to the end, then check."""
    moved = candidate[4:] + candidate[:4]
    expanded = "".join(str(int(c, 36)) if c.isalpha() else c for c in moved)
    return int(expanded) % 97 == 1


def readable_text(path: pathlib.Path) -> str:
    """Whatever a person would see in this file.

    The bytes are not enough. A PDF keeps its text in a compressed stream, which
    is exactly why the two card bills survived every grep anyone ran at them --
    and why they could then be patched in place with same-length strings without
    the parsers noticing. So each format is opened the way the app opens it.

    The document's own metadata counts as text too. A regenerated PDF can carry
    its author in the Info dictionary and an XLS the last user who saved it,
    and neither is on any page.
    """
    if path.suffix.lower() == ".pdf":
        import pdfplumber

        with pdfplumber.open(path) as document:
            meta = [str(value) for value in (document.metadata or {}).values()]
            pages = [page.extract_text() or "" for page in document.pages]
            return "\n".join(meta + pages)
    if path.suffix.lower() in {".xls", ".xlsx"}:
        import xlrd

        book = xlrd.open_workbook(str(path))
        return "\n".join(
            [str(book.user_name or "")]
            + [
                str(sheet.cell_value(row, col))
                for sheet in book.sheets()
                for row in range(sheet.nrows)
                for col in range(sheet.ncols)
            ]
        )
    raw = path.read_bytes()
    # A UTF-16 export puts a NUL between every letter, which hides a name from
    # every pattern below as well as a PDF's compression does. The mark says
    # how to read it.
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("latin-1")


def test_a_utf_16_fixture_is_read_as_its_text(tmp_path: pathlib.Path):
    """The detectors see the letters, not the NULs between them."""
    path = tmp_path / "statement.csv"
    path.write_bytes(codecs.BOM_UTF16_LE + "Payee,someone@private.test\n".encode("utf-16-le"))
    assert _EMAIL.findall(readable_text(path)) == ["private.test"]


#: The words a bank puts in front of an account number, and what follows them.
#: An 8-12 digit run is neither an IBAN nor a card, so neither checksum above
#: sees one; the word beside it is the only signal.
_ACCOUNT_WORD = re.compile(
    r"kontonummer|acctid|a/c|contrato|\bccc\b|account\s*(?:no|number)", re.IGNORECASE
)
_ACCOUNT_INLINE = re.compile(
    rf"(?:{_ACCOUNT_WORD.pattern})\D{{0,40}}?(?<!\d)(\d{{8,12}})(?!\d)", re.IGNORECASE
)
_CELL_SPLIT = re.compile(r"[,;\t]")

#: The synthetic account numbers each fixture is allowed, and nothing else. A
#: real one re-copied from a statement is not on this list, so it trips.
ACCOUNT_NUMBER_ALLOWLIST: dict[str, frozenset[str]] = {
    "bank_sgml.ofx": frozenset({"12345678"}),
    "preamble_swedish.csv": frozenset({"8921964985"}),
}


def account_numbers_in(text: str) -> set[str]:
    """Digit runs beside an account word, inline or in that word's column.

    Inline is `<ACCTID>12345678` or `A/C 12345678`. A CSV puts the word in its
    header and the number rows below, so a header cell that *is* the word marks
    its column, and every row's cell in that column is read.
    """
    found = set(_ACCOUNT_INLINE.findall(text))
    columns: set[int] = set()
    for line in text.splitlines():
        cells = [cell.strip().strip('"') for cell in _CELL_SPLIT.split(line)]
        marked = {i for i, cell in enumerate(cells) if _ACCOUNT_WORD.fullmatch(cell)}
        if marked:
            columns = marked
            continue
        for index in columns:
            if index < len(cells):
                digits = re.sub(r"[ -]", "", cells[index])
                if re.fullmatch(r"\d{8,12}", digits):
                    found.add(digits)
    return found


def fixture_files() -> list[pathlib.Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_there_are_fixtures_to_check():
    """The rest of this file passes trivially against an empty directory."""
    assert len(fixture_files()) >= 8


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: p.name)
def test_no_statement_fixture_carries_anybody_real(path: pathlib.Path):
    text = readable_text(path)

    found = real_names_in(text)
    assert not found, f"{path.name} contains a denylisted name: {', '.join(found)}"

    for domain in _EMAIL.findall(text):
        assert domain.lower() in ALLOWED_EMAIL_DOMAINS, (
            f"{path.name} contains an address at {domain}. "
            "Fixtures use example.com."
        )

    for run in _DIGIT_RUN.findall(text):
        digits = re.sub(r"\D", "", run)
        if looks_like_a_timestamp(digits):
            continue
        assert not luhn_ok(digits), (
            f"{path.name} contains {run!r}, which passes the Luhn check -- "
            "that is the shape of a real card number."
        )

    for candidate in _IBAN.findall(text):
        assert not iban_ok(candidate), (
            f"{path.name} contains {candidate!r}, which is a valid IBAN."
        )

    allowed = ACCOUNT_NUMBER_ALLOWLIST.get(path.name, frozenset())
    unlisted = sorted(n for n in account_numbers_in(text) if n not in allowed)
    assert not unlisted, (
        f"{path.name} carries an account number nobody listed as synthetic: "
        + ", ".join(f"{n[:2]}…({len(n)})" for n in unlisted)
    )


#: The one line in the tree allowed to carry the owner's name: the copyright
#: notice, which is a legal statement rather than example data.
_COPYRIGHT = re.compile(r"copyright \(c\) \d{4}", re.IGNORECASE)

#: The pattern below spells the owner's handle in a form the pattern itself
#: cannot strip, so its line carries this marker and is the one line skipped.
_REPO_ADDRESS_LINE = "# hygiene: the repo's own address"

#: Where the project lives and who publishes it -- the repository, the author
#: page and the security contact. Addresses chosen to be public, not example
#: data that happens to be real.
#:
#: Both the old address and the new one are stripped here, so the name check
#: does not report the handle inside either; `OLD_ADDRESS` below is what says
#: the old one has to go. `@handle` is how CODEOWNERS and FUNDING.yml name it.
_REPO_ADDRESS = re.compile(
    r"(github\.com[/:])?mariolonghi(-com)?/(household-)?spend-tracker[\w.-]*|[\w.]*@?mariolonghi\.com[\w/.-]*|@mariolonghi\b",  # hygiene: the repo's own address
    re.IGNORECASE,
)
#: The private repository this was copied out of. Nothing should still point
#: at it: its issues are not public and its version check answers 404.
OLD_ADDRESS = re.compile(r"mariolonghi/spend-tracker\b", re.IGNORECASE)  # hygiene: the repo's own address

#: A line that *defines* one of the patterns below necessarily contains what it
#: looks for, so it carries this marker and the marker check skips it.
_PATTERN_LINE = "# hygiene: a pattern, not a value"

#: What a public tree must never carry: pointers into private notes or one
#: machine's layout, and the shapes of real credentials. A sha256 image digest
#: is 64 hex characters by design, so a `sha256:` prefix exempts one.
PRIVATE_MARKERS = re.compile(
    r"\bCLAUDETE\b|\[\[[A-Z][^\[\]\"'`()]*\]\]|\bProject-1\b|/Users/[a-z][\w.-]*|\bDevlog\.md\b"  # hygiene: a pattern, not a value
    r"|\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|BEGIN [A-Z ]*PRIVATE KEY"  # hygiene: a pattern, not a value
    r"|(?<!sha256:)\b[0-9a-f]{64}\b|\bstk_[A-Za-z0-9]{24,}"  # hygiene: a pattern, not a value
)
#: A TOTP secret is base32: 16 or 32 characters of A-Z and 2-7.
_BASE32 = re.compile(r"\b[A-Z2-7]{16}\b|\b[A-Z2-7]{32}\b")
#: Every base32-shaped word the tree is allowed: the two RFC example secrets the
#: QR test uses, the alphabet itself, and one environment variable that happens
#: to be sixteen capitals.
ALLOWED_BASE32 = frozenset({
    "JBSWY3DPEHPK3PXP",
    "KRSXG5CTMVRXEZLU",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
    "PYTHONUNBUFFERED",
})


def private_markers_in(line: str) -> list[str]:
    """Why `line` must not be published, one reason per finding."""
    found = []
    if PRIVATE_MARKERS.search(line):
        found.append("carries a private marker or a secret's shape")
    if OLD_ADDRESS.search(line):
        found.append("still names the old repository")
    found += [
        f"carries a base32 secret shape {hit[:2]}…"
        for hit in _BASE32.findall(line)
        if hit not in ALLOWED_BASE32
    ]
    found += [f"carries a private email {masked}" for masked in private_emails_in(line)]
    return found


ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Files no text scan can read. Each one is somebody's to check by hand, so
#: they may live only where somebody does: the statement fixtures, which the
#: fixture scan opens properly, and the app's own icons.
BINARY_SUFFIXES = frozenset({
    ".pdf", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".heic", ".heif",
    ".tif", ".tiff", ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".avif", ".webp",
    ".sqlite", ".sqlite3", ".db", ".zip", ".gz", ".tgz",
})
BINARY_HOMES = (FIXTURES, ROOT / "client" / "public")


def _all_tracked() -> list[pathlib.Path]:
    """Every file git tracks, binaries included."""
    import subprocess

    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout, so there is no list of tracked files")
    return [ROOT / name for name in listed.decode().split("\0") if name]


def tracked_text_files() -> list[pathlib.Path]:
    """Every file git tracks, minus binaries -- the code as it will be published.

    The fixture scan above covers what was copied from a statement. It did not
    cover a docstring written in the same week: four of them used the owner's
    full name as the example History row (issue #75), and nothing that ran on
    every commit would ever have said so.
    """
    return [p for p in _all_tracked() if p.is_file() and p.suffix.lower() not in BINARY_SUFFIXES]


def stray_binaries(paths: list[pathlib.Path]) -> list[str]:
    """Binaries outside the two folders anybody scans, relative to the root."""
    return sorted(
        str(p.relative_to(ROOT))
        for p in paths
        if p.suffix.lower() in BINARY_SUFFIXES and p.parent not in BINARY_HOMES
    )


# The SWIFT IBAN registry's published example for each country the tests use.
# Valid by construction and nobody's account; anything else valid is a leak.
REGISTRY_EXAMPLE_IBANS = frozenset({
    "GB82WEST12345698765432",
    "ES9121000418450200051332",
})


def test_no_tracked_file_names_anybody_real():
    """No real surname in code, docs or tests, and no valid IBAN anywhere.

    Example data is `Jane Doe` and the IBAN registry's own examples in
    `REGISTRY_EXAMPLE_IBANS` -- all obviously made up, which is the point of them.
    """
    offenders: list[str] = []
    for path in tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _COPYRIGHT.search(line) or _REPO_ADDRESS_LINE in line:
                continue
            # The repository's own address is where it lives, not example data.
            stripped = _REPO_ADDRESS.sub("", line)
            for name in real_names_in(stripped):
                offenders.append(f"{path.name}:{number} names {name}")
            for candidate in _IBAN.findall(line):
                if candidate not in REGISTRY_EXAMPLE_IBANS and iban_ok(candidate):
                    offenders.append(f"{path.name}:{number} carries a valid IBAN")
    assert not offenders, "\n".join(offenders)


def test_no_tracked_file_carries_private_markers():
    """Nothing that points into private notes, one machine, or a real secret.

    Before the public copy the tree still named the private notes folder, a
    home directory's project layout and wiki links into notes nobody else can
    open. None of it was secret; all of it was somebody's. And the old
    repository's address, which a public reader cannot follow.
    """
    offenders: list[str] = []
    for path in tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _REPO_ADDRESS_LINE in line or _PATTERN_LINE in line:
                continue
            offenders += [f"{path.name}:{number} {why}" for why in private_markers_in(line)]
    assert not offenders, "\n".join(offenders)


def test_the_only_tracked_binaries_are_the_fixtures_and_the_icons():
    """A PDF dropped under `docs/` would be read by nothing at all."""
    stray = stray_binaries(_all_tracked())
    assert stray == [], f"binaries nobody scans: {stray}. Keep them out, or scan them."


def _tiny_pdf(author: str) -> bytes:
    """A one-page PDF whose only personal detail is its Info dictionary."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        f"<< /Author ({author}) /Producer (a test) >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


def test_the_detectors_actually_fire(tmp_path):
    """Mutation test. Each guard above is shown refusing something it should.

    House rule: a test that guards something gets its own guard planted back.
    Three of the checks below passed against the broken fixtures once, which is
    why none of them is trusted on sight.
    """
    # 4111 1111 1111 1111 is Visa's documented test number: valid Luhn, no
    # account behind it.
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert _DIGIT_RUN.search("card 4111 1111 1111 1111 on file")

    # GB82 WEST 1234 5698 7654 32 is the IBAN registry's own example.
    assert iban_ok("GB82WEST12345698765432")
    assert not iban_ok("GB82WEST12345698765433")
    assert _IBAN.search("paid to GB82WEST12345698765432 today")

    # The name check, against a digest planted for a made-up name -- the real
    # ones cannot be spelt here without undoing the point of hashing them.
    planted = frozenset({_digest("doe")})
    assert real_names_in("paid Jane Doe", planted) == ["do…(3)"]
    assert real_names_in("JaneDoe on the line", planted) == ["do…(3)"]
    assert real_names_in("paid Jane Dough", planted) == []
    assert len(REAL_NAME_DIGESTS) == 7

    # The repository's own address is stripped, old and new, so the handle
    # inside it is not reported as a name; the old one is reported as stale.
    assert real_names_in(_REPO_ADDRESS.sub("", "see github.com/MarioLonghi-com/household-spend-tracker")) == []
    assert real_names_in(_REPO_ADDRESS.sub("", "* @mariolonghi")) == []  # hygiene: the repo's own address
    assert private_markers_in("see " + "mariolonghi" + "/spend-tracker/issues") == [  # hygiene: the repo's own address
        "still names the old repository"
    ]
    assert private_markers_in("github.com/MarioLonghi-com/household-spend-tracker/issues") == []

    # Personal email local-parts, by digest and in canonical form: dots and a
    # `+tag` do not get a real address past it, and a made-up one passes.
    assert _canonical_local_part("Jane.Doe+bills") == "janedoe"
    planted_mail = frozenset({_digest("janedoe")})
    assert [
        local for local, _ in _EMAIL_PARTS.findall("mail jane.doe+x@gmail.com")
        if _digest(_canonical_local_part(local)) in planted_mail
    ] == ["jane.doe+x"]
    assert private_emails_in("mail someone@gmail.com") == []
    assert len(PRIVATE_LOCALPART_DIGESTS) == 2

    # Private markers and secret shapes, each assembled here so this file does
    # not trip its own check.
    for planted_line in (
        "see $CLAU" + "DETE/notes",
        "per [" + "[Some Private Note]]",
        "cd ~/Proj" + "ect-1/thing",
        "open /" + "Users/someone/notes",
        "add to " + "Devlog" + ".md",
        "token gh" + "p_" + "a" * 36,
        "token github" + "_pat_" + "a" * 40,
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "YNAB token " + "0123456789abcdef" * 4,
        "key stk" + "_" + "A1b2C3d4" * 4,
    ):
        assert private_markers_in(planted_line), planted_line
    assert private_markers_in("FROM node@sha256:" + "0123456789abcdef" * 4) == []
    assert private_markers_in('rows = [["EUR", None]]') == [], "code, not a wiki link"
    assert private_markers_in("secret GEZDGNBV" + "GY3TQOJQ") == ["carries a base32 secret shape GE…"]
    assert private_markers_in("secret JBSWY3DPEHPK3PXP") == []

    # Binaries outside the fixtures and the icons.
    assert stray_binaries(
        [ROOT / "docs" / "scan.pdf", FIXTURES / "card_bill.pdf", ROOT / "client" / "public" / "icon-192.png"]
    ) == ["docs/scan.pdf"]

    # A PDF's metadata is read with its pages.
    pdf = tmp_path / "authored.pdf"
    pdf.write_bytes(_tiny_pdf("Jane Doe"))
    assert real_names_in(readable_text(pdf), planted) == ["do…(3)"]

    # Account numbers beside an account word, inline and in a CSV column.
    assert account_numbers_in("<ACCTID>98765432\n<BANKID>1") == {"98765432"}
    assert account_numbers_in("Radnummer,Kontonummer,Belopp\n1,1234567890,-2.00") == {"1234567890"}
    assert account_numbers_in("Radnummer,Belopp\n1,-2.00\nref 1234567890") == set()

    assert _EMAIL.findall("someone@gmail.com") == ["gmail.com"]
    assert "gmail.com" not in ALLOWED_EMAIL_DOMAINS

    # The timestamp exemption is the one place a real number could hide, so it
    # is kept as narrow as it can be and shown refusing to widen.
    assert looks_like_a_timestamp("20251001120000")  # the OFX DTPOSTED above
    assert looks_like_a_timestamp("20251001")
    assert not looks_like_a_timestamp("4111111111111111")  # 16 digits, not 14
    assert not looks_like_a_timestamp("20251301120000")  # month 13
    assert not looks_like_a_timestamp("20251001990000")  # hour 99


# --------------------------------------------------------------------------- #
# The snapshot classifies every table, or refuses to be built
# --------------------------------------------------------------------------- #


def test_every_table_in_the_schema_is_classified_for_the_snapshot():
    """No table reaches `/db` without somebody having decided about it.

    The same promise `Base.__audit__` makes about the audit log, made about the
    browsable copy: every table is either emptied, blanked, or deliberately
    carried whole.
    """
    from app.models import Base
    from scripts.db_view import CARRIED_WHOLE, PURGE, REDACTIONS

    classified = set(PURGE) | set(REDACTIONS) | set(CARRIED_WHOLE)
    unclassified = sorted(set(Base.metadata.tables) - classified)
    assert not unclassified, (
        f"{', '.join(unclassified)} would be copied into the snapshot as-is. "
        "Add each to PURGE, REDACTIONS or CARRIED_WHOLE in scripts/db_view.py."
    )

    # And the other way: a list naming a table that no longer exists is a
    # redaction quietly doing nothing.
    known = set(Base.metadata.tables) | {"alembic_version"}
    assert not sorted(classified - known), (
        f"{', '.join(sorted(classified - known))} is classified but is not a table."
    )


def test_the_viewer_serves_the_snapshot_the_script_writes(tmp_path, monkeypatch):
    """`make db-view`, `/db` and `make snapshot` all mean the same file (#64).

    The Makefile used to hand Datasette `data/snapshot.sqlite3` by name. When
    the default data directory moved to `~/.local/share/spend-tracker` the
    script followed it and the Makefile did not, so every `make db-view` on a
    fresh install built a snapshot and then failed to find it.
    """
    import dataclasses

    import app.api.dbview as dbview
    import scripts.db_view as db_view

    # One settings object in both, because the suite reloads `app.config` per
    # client and each module otherwise holds whichever one it last imported.
    pinned = dataclasses.replace(dbview.settings, data_dir=tmp_path / "somewhere")
    monkeypatch.setattr(dbview, "settings", pinned)
    monkeypatch.setattr(db_view, "settings", pinned)

    assert db_view.default_destination() == dbview.snapshot_path()
    assert db_view.default_destination() == tmp_path / "somewhere" / "snapshot.sqlite3"

    makefile = (pathlib.Path(__file__).resolve().parent.parent / "Makefile").read_text()
    recipe = makefile.split("\ndb-view:", 1)[1].split("\n\n", 1)[0]
    assert "snapshot.sqlite3" not in recipe, (
        "db-view names the snapshot itself again. Let scripts/db_view.py --serve "
        "say where it wrote it."
    )
    assert "--serve" in recipe


def test_building_a_snapshot_refuses_a_table_nobody_classified(tmp_path):
    """Planted back: add a table, and the build stops instead of shipping it."""
    from scripts.db_view import build

    source = tmp_path / "live.sqlite3"
    live = sqlite3.connect(source)
    live.execute("CREATE TABLE transactions (id TEXT)")
    live.execute("CREATE TABLE oauth_tokens (id TEXT, access_token TEXT)")
    live.execute("INSERT INTO oauth_tokens VALUES ('1', 'the-actual-secret')")
    live.commit()
    live.close()

    destination = tmp_path / "snapshot.sqlite3"
    with pytest.raises(SystemExit) as refused:
        build(source, destination)
    assert "oauth_tokens" in str(refused.value)

    # The refusal is worth nothing if the secret went into the file anyway.
    # It did, on the first attempt at this fix: the check sat one line after
    # `VACUUM INTO`, so the copy already existed with `the-actual-secret` in it
    # and the refusal only stopped the blanking that would have followed.
    assert not destination.exists(), "the snapshot was written despite the refusal"


def test_the_snapshot_empties_login_attempts(tmp_path):
    """Every email and IP that ever tried the door, out of the browsable copy."""
    from scripts.db_view import PURGE, build

    assert "login_attempts" in PURGE

    source = tmp_path / "live.sqlite3"
    live = sqlite3.connect(source)
    for table in ("sessions", "trusted_devices", "pending_sign_ins"):
        live.execute(f"CREATE TABLE {table} (id_hash TEXT)")
    live.execute(
        "CREATE TABLE login_attempts (id TEXT, email_canonical TEXT, ip TEXT, ok INT)"
    )
    live.execute(
        "INSERT INTO login_attempts VALUES ('1', 'someone@example.com', '100.64.0.7', 0)"
    )
    live.execute("CREATE TABLE transactions (id TEXT)")
    live.commit()
    live.close()

    wiped = build(source, tmp_path / "snapshot.sqlite3")
    assert wiped["login_attempts"] == 1

    snap = sqlite3.connect(tmp_path / "snapshot.sqlite3")
    remaining = snap.execute("SELECT count(*) FROM login_attempts").fetchone()[0]
    snap.close()
    assert remaining == 0


def test_a_snapshot_carries_the_receipt_and_not_the_photograph(tmp_path):
    """Test 17. The row is browsable; the bytes are not in the file at all.

    `/db` is a file you point a browser at, and Datasette will run read-only
    SQL against it for whoever reaches it. `receipts` is ledger and belongs
    there whole -- including, deliberately, the coordinates, because the owner
    who can read the snapshot can read the receipt itself in the app. The
    picture is a different question: fifty times the size, unrenderable in a
    browsable copy, and a photograph of a household's life.
    """
    from scripts.db_view import build

    source = tmp_path / "live.sqlite3"
    live = sqlite3.connect(source)
    live.execute(
        "CREATE TABLE receipts (id TEXT, original_filename TEXT, gps_lat REAL, gps_lon REAL)"
    )
    live.execute("CREATE TABLE receipt_blobs (sha256 TEXT, role TEXT, data BLOB)")
    live.execute("INSERT INTO receipts VALUES ('r1', 'IMG_0042.jpeg', 40.4215, -3.6889)")
    live.execute("INSERT INTO receipt_blobs VALUES ('abc', 'display', ?)", (b"JPEG-ish bytes",))
    live.commit()
    live.close()

    destination = tmp_path / "snapshot.sqlite3"
    build(source, destination)

    snap = sqlite3.connect(destination)
    try:
        assert snap.execute("select count(*) from receipt_blobs").fetchone()[0] == 0, (
            "the bytes must not be in a file anybody points a browser at"
        )
        row = snap.execute(
            "select original_filename, gps_lat from receipts"
        ).fetchone()
        assert row == ("IMG_0042.jpeg", 40.4215), (
            "and the receipt itself is carried whole -- it is ledger, and the "
            "snapshot is owner-only behind the same session check as the app"
        )
    finally:
        snap.close()


def test_no_committed_fixture_is_a_photograph(tmp_path):
    """Test 18. The hygiene rule, extended to the format it is worst for.

    A PDF keeps its strings in a compressed stream, which is how two real
    statements survived every grep aimed at them. **An image is worse**: a JPEG
    of a real receipt carries a real shop, a real card's last four, a real date
    and, before stripping, real coordinates -- and none of it is greppable at
    all.

    So the rule for receipts is stronger than "no real data in the fixtures":
    there are no committed image fixtures. They are drawn by `Image.new()` in
    `tests/receipt_fixtures.py` at the moment each test needs one.
    """
    repo = pathlib.Path(__file__).resolve().parent.parent
    images = [
        path
        for path in (repo / "tests").rglob("*")
        if path.suffix.lower()
        in {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".avif", ".tif", ".tiff", ".gif"}
    ]
    assert images == [], (
        "committed image fixtures: " + ", ".join(str(one.relative_to(repo)) for one in images)
        + ". Generate them in the test instead -- a photograph carries a shop, a "
        "card's last four and a set of coordinates, none of which any grep finds."
    )


def test_the_generated_fixtures_carry_no_real_location(tmp_path):
    """And the one coordinate the tests *do* use is a landmark, not a home.

    A guard that has never fired is a guard nobody has proved, so this also
    checks that the fixture really does carry the tags it claims to.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from receipt_fixtures import MADRID_LAT_DEGREES, has_exif_block, with_exif

    raw = with_exif()
    assert has_exif_block(raw), "the fixture has to carry EXIF or test 5 proves nothing"
    assert 40.0 < MADRID_LAT_DEGREES < 40.5, "central Madrid, which is nobody's doorstep"
