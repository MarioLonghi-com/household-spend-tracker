"""An import's sentences read back as codes (#267, `app/notices.py`).

Held here: every registered sentence reads back to its own code and params;
what the importers really write reads as a code; a sentence nobody registered
reads as nothing rather than as the wrong code; the client's templates are
the server's; and an agent's answer carries none of it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Formatter

import pytest

from app import notices
from app.error_codes import REGISTRY
from app.money import format_amount
from tests.conftest import HEADERS
from tests.test_agent_imports import world  # noqa: F401  (the fixture)
from tests.test_api import SANTANDER_CSV, _household_with_accounts, _upload

ROOT = Path(__file__).resolve().parent.parent

#: A sentence each nested param can hold: one that reads by itself.
NESTED = "the amounts match and the dates are close"


def _sample(code: str) -> tuple[str, dict]:
    """The sentence a notice writes for sample params, and the params it should read as."""
    notice = notices.NOTICES[code]
    said, expected = {}, {}
    money = False
    for _literal, name, _spec, conversion in Formatter().parse(notice.english):
        if name is None or name in said:
            continue
        kind = notice.kinds.get(name)
        if conversion == "r":
            said[name], expected[name] = "Café 12", "Café 12"
        elif kind == "int":
            said[name] = expected[name] = 7
        elif kind == "count":
            said[name], expected[name] = "100,000", 100_000
        elif kind == "date":
            said[name] = expected[name] = "2026-03-04"
        elif kind == "money":
            money = True
            said[name], expected[name] = format_amount(-123_456, "EUR"), -123_456
        elif kind == "nested":
            said[name], expected[name] = NESTED, {"code": "transfer.why.amounts_match", "params": {}}
        else:
            said[name] = expected[name] = "Alpha"
    if money:
        expected["currency"] = "EUR"
    text = notice.english.format(**{k: v for k, v in said.items()})
    # `format` applies !r itself; the sample values were written unquoted.
    return text, expected


@pytest.mark.parametrize("code", sorted(notices.NOTICES))
def test_every_notice_reads_back_as_itself(code):
    text, params = _sample(code)
    assert notices.read(text, currency="EUR") == {"code": code, "params": params}, text


def test_a_sentence_nobody_registered_reads_as_nothing():
    assert notices.read("the moon is made of cheese") is None
    assert notices.read("") is None and notices.read(None) is None


def test_a_sentence_with_a_but_is_not_claimed_by_the_generic_one():
    """`{why}, but {payer}` matches any sentence with ", but" in it; only one
    whose two halves are themselves notices may be read as it."""
    assert notices.read("the cat sat, but the dog did not") is None
    assert notices.read(f"{NESTED}, but Acme has paid you before") == {
        "code": "transfer.why.but",
        "params": {
            "why": {"code": "transfer.why.amounts_match", "params": {}},
            "payer": {"code": "transfer.payer.paid_before", "params": {"payee": "Acme"}},
        },
    }


def test_an_amount_reads_back_only_in_a_currency_it_was_given():
    text = (
        "This statement starts from £1,234.56 on 2026-02-01, but the ledger holds -£10.00 for "
        "the day before -- £1,244.56 apart. A statement between the last import and this one "
        "may be missing: download from the day after the last one imported."
    )
    assert notices.read(text) is None
    assert notices.read(text, currency="GBP")["params"] == {
        "opening": 123_456,
        "date": "2026-02-01",
        "held": -1_000,
        "gap": 124_456,
        "currency": "GBP",
    }


def test_a_refusal_repeated_on_a_line_reads_as_the_refusals_own_code():
    from app.money import MoneyError, parse_exact

    for typed, currency in (("12.345", "EUR"), ("12.5", "JPY"), ("1,234", "SEK")):
        with pytest.raises(MoneyError) as refused:
            parse_exact(typed, currency)
        assert notices.read(str(refused.value)) == refused.value.wire()
    assert notices.refusal_codes() == {
        code for code, notice in notices.NOTICES.items() if notice.template is None
    }
    assert notices.refusal_codes() <= set(REGISTRY)


# --------------------------------------------------------------------------- #
# What the importers really write
# --------------------------------------------------------------------------- #

OVERLAP_CSV = (
    "Fecha;Concepto;Importe;Saldo\r\n"
    "07/01/2026;NOMINA ENERO;2.100,00;10.910,07\r\n"
    "09/01/2026;SQ *EL BAR;-12,50;10.897,57\r\n"
    "10/01/2026;FARMACIA;0,00;10.897,57\r\n"
)


def test_a_statement_preview_sends_codes_beside_its_sentences(client):
    home = _household_with_accounts(client)
    house, checking = home["household"]["id"], home["checking"]["id"]
    first = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(f"/api/households/{house}/imports/{first['batch_id']}/commit", json={}, headers=HEADERS)

    preview = _upload(client, house, checking, OVERLAP_CSV, name="enero-2.csv")
    assert preview.status_code == 201, preview.text
    body = preview.json()

    said = {line["reason"]: (line["reason_code"], line["reason_params"]) for line in body["lines"] if line["reason"]}
    assert said["this line is already in the account"] == ("import.line.already_there", {})
    assert said["this line moves no money"] == ("import.line.no_money", {})
    assert len(body["warning_codes"]) == len(body["warnings"])
    for line in body["lines"]:
        if line["reason"]:
            assert line["reason_code"] is not None, line["reason"]


@pytest.mark.parametrize(
    "path", sorted((ROOT / "tests" / "statement_files").glob("*")), ids=lambda p: p.name
)
def test_every_sentence_staging_a_real_file_writes_has_a_code(client, path):
    home = _household_with_accounts(client)
    house = home["household"]["id"]
    pounds = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Pounds", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    ).json()

    def stage(account_id: str):
        return client.post(
            f"/api/households/{house}/imports",
            data={"account_id": account_id},
            files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
            headers=HEADERS,
        )

    answer = stage(home["checking"]["id"])
    if answer.status_code == 422 and answer.json().get("code") == "import.wrong_currency":
        answer = stage(pounds["id"])
    if answer.status_code != 201:
        pytest.skip(f"{path.name} is refused whole: {answer.json().get('detail')}")
    body = answer.json()
    unread = [line["reason"] for line in body["lines"] if line["reason"] and not line["reason_code"]]
    unread += [one for one, code in zip(body["warnings"], body["warning_codes"], strict=True) if code is None]
    assert unread == []


def test_an_agent_staging_rows_gets_no_codes(client, world):  # noqa: F811 (the imported fixture)
    """The agent's answer is the one its documentation describes, byte for byte."""
    from tests.test_agent_imports import _post, _rows

    rows = _rows(2)
    rows.append({**rows[0]})  # the same row twice: a line with a reason
    answer = _post(client, world, rows)
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert "warning_codes" not in body
    assert any(line["reason"] for line in body["lines"]), "a line that says why"
    assert all("reason_code" not in line and "reason_params" not in line for line in body["lines"])


# --------------------------------------------------------------------------- #
# The client's half
# --------------------------------------------------------------------------- #


def _client_notices() -> dict[str, tuple[str, list[str], list[str]]]:
    text = (ROOT / "client/src/lib/noticeMessages.ts").read_text(encoding="utf-8")
    body = text.split("export const NOTICE_MESSAGES")[1].split("\n};")[0]
    found = {}
    for block in re.finditer(
        r'"([\w.]+)": \{\s*message: msg\(\{ id: "notice\.[\w.]+", message: ("(?:[^"\\]|\\.)*")[^\n]*\n'
        r'(?:\s*money: (\[[^\]]*\]),\n)?(?:\s*dates: (\[[^\]]*\]),\n)?',
        body,
    ):
        found[block.group(1)] = (
            json.loads(block.group(2)),
            json.loads(block.group(3) or "[]"),
            json.loads(block.group(4) or "[]"),
        )
    return found


@pytest.mark.repo_wide
def test_the_client_has_every_notice_with_the_same_template_and_kinds():
    expected = {
        code: (
            notice.template,
            [k for k, v in notice.kinds.items() if v == "money"],
            [k for k, v in notice.kinds.items() if v == "date"],
        )
        for code, notice in notices.NOTICES.items()
        if notice.template
    }
    assert _client_notices() == expected


def test_a_remembered_reading_is_handed_out_as_a_copy():
    """Readings are cached per sentence; changing one must not change the next."""
    first = notices.read("the bank charged this on top of line 7")
    first["params"]["line"] = 99
    assert notices.read("the bank charged this on top of line 7") == {
        "code": "import.line.fee_of",
        "params": {"line": 7},
    }
