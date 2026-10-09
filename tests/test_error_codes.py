"""Domain errors carry a stable code and raw params beside the English detail (#65).

Three things are held here.

- **The ratchet.** Every construction of a `DomainError` (or a subclass) in
  `app/` and `statements/` that carries no `code=` is counted, by walking the
  syntax tree rather than grepping, and the count may not go up. New code
  raises with a code; old code is converted a wave at a time, and whoever
  converts one lowers `CODELESS` here.
- **The registry.** Every `code=` raised is in `app.error_codes.REGISTRY`,
  every registry entry is raised somewhere, and each raise passes exactly the
  params its entry names.
- **The wire.** A converted refusal answers with `detail` byte-identical to
  what it said before, plus `code` and `params` holding raw values: money as
  integer minor units and a currency code, a date as ISO. An agent gets the
  old body exactly.

The scanner is a function of source text, so each rule is also shown biting
on a planted example: a ratchet that has never failed is not known to work.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
from starlette.requests import Request

from app.error_codes import REGISTRY, Code
from app.errors import Conflict, DomainError, Unauthorized, ValidationError
from app.money import MoneyError, parse_exact
from tests.conftest import HEADERS
from tests.test_api import _household_with_accounts

ROOT = Path(__file__).resolve().parent.parent

#: `DomainError` constructions in `app/` and `statements/` that carry no code,
#: as of #57's first wave. **Lower it** when you convert sites; the test fails
#: if it rises.
CODELESS = 219


@dataclass
class Scan:
    codeless: list[str] = field(default_factory=list)
    #: (code, the param names passed, where) for every raise with a code.
    coded: list[tuple[str, frozenset[str] | None, str]] = field(default_factory=list)
    #: Raises whose code is not a string literal, which nothing can check.
    opaque: list[str] = field(default_factory=list)


def _bases(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def error_classes(trees: dict[str, ast.Module]) -> set[str]:
    """`DomainError` and every class that descends from it, by name, anywhere.

    A fixed point over every module's class statements, so a subclass of a
    subclass defined in another file (`MoneyError`, `YnabError`) is found.
    """
    known = {"DomainError"}
    classes = [
        node for tree in trees.values() for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ]
    grew = True
    while grew:
        grew = False
        for node in classes:
            if node.name not in known and _bases(node) & known:
                known.add(node.name)
                grew = True
    return known


def scan(sources: dict[str, str]) -> Scan:
    trees = {where: ast.parse(text, filename=where) for where, text in sources.items()}
    classes = error_classes(trees)
    found = Scan()
    for where, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name not in classes:
                continue
            at = f"{where}:{node.lineno}"
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            code = keywords.get("code")
            if code is None:
                found.codeless.append(at)
                continue
            if not (isinstance(code, ast.Constant) and isinstance(code.value, str)):
                found.opaque.append(at)
                continue
            params = keywords.get("params")
            if params is None:
                names: frozenset[str] | None = frozenset()
            elif isinstance(params, ast.Dict) and all(
                isinstance(key, ast.Constant) and isinstance(key.value, str) for key in params.keys
            ):
                names = frozenset(key.value for key in params.keys)  # type: ignore[union-attr]
            else:
                names = None
            found.coded.append((code.value, names, at))
    return found


def problems(found: Scan, registry: dict[str, Code]) -> list[str]:
    """What is wrong between the raises and the registry, as sentences."""
    wrong = [f"{at}: the code is not a string literal, so it cannot be checked" for at in found.opaque]
    raised = set()
    for code, names, at in found.coded:
        raised.add(code)
        entry = registry.get(code)
        if entry is None:
            wrong.append(f"{at}: {code!r} is not in app/error_codes.py")
        elif names is None:
            wrong.append(f"{at}: params must be a dict literal with string keys")
        elif names != set(entry.params):
            wrong.append(
                f"{at}: {code!r} passes {sorted(names)} and its entry names {sorted(entry.params)}"
            )
    for code in sorted(set(registry) - raised):
        wrong.append(f"{code!r} is registered and raised nowhere")
    return wrong


def _repo_sources() -> dict[str, str]:
    files = sorted([*ROOT.glob("app/**/*.py"), *ROOT.glob("statements/**/*.py")])
    return {str(path.relative_to(ROOT)): path.read_text(encoding="utf-8") for path in files}


# --------------------------------------------------------------------------- #
# The ratchet and the registry, on the repository
# --------------------------------------------------------------------------- #


def test_the_ratchet_holds():
    codeless = scan(_repo_sources()).codeless
    assert len(codeless) <= CODELESS, (
        f"{len(codeless)} DomainError raises carry no code, and the ceiling is {CODELESS}. "
        "A new raise needs code= and params= (registered in app/error_codes.py) -- "
        "see the standing rule in CLAUDE.md."
    )


def test_the_registry_and_the_raises_agree():
    assert problems(scan(_repo_sources()), REGISTRY) == []


def test_every_template_names_its_params_and_only_those():
    for code, entry in REGISTRY.items():
        for name in entry.params:
            if name == "currency" and "{currency}" not in entry.template:
                # How a money param is read; the template shows the amount.
                continue
            # `{count}`, or `{count, plural, ...}` where the words follow the number.
            assert "{" + name + "}" in entry.template or "{" + name + "," in entry.template, (code, name)
        assert code.count(".") >= 1 and code == code.lower(), code


def test_a_money_param_always_travels_with_its_currency():
    """An amount in minor units means nothing without the exponent of its currency."""
    for code, entry in REGISTRY.items():
        money = {"difference", "total", "amount"} & set(entry.params)
        if money:
            assert "currency" in entry.params, code


# --------------------------------------------------------------------------- #
# Each rule bites, on a planted example
# --------------------------------------------------------------------------- #

PLANTED_BASE = """
class DomainError(Exception): ...
class ValidationError(DomainError): ...
class MoneyError(ValidationError, ValueError): ...
"""


def test_a_new_codeless_raise_raises_the_count():
    before = scan({"errors.py": PLANTED_BASE, "a.py": "raise ValidationError('x', code='a.b')"})
    after = scan(
        {
            "errors.py": PLANTED_BASE,
            "a.py": "raise ValidationError('x', code='a.b')\nraise MoneyError('not money')",
        }
    )
    assert (len(before.codeless), len(after.codeless)) == (0, 1)
    assert after.codeless == ["a.py:2"]


def test_a_subclass_in_another_module_is_still_counted():
    found = scan(
        {
            "errors.py": PLANTED_BASE,
            "other.py": "from x import MoneyError\nclass Mine(MoneyError): ...\nraise Mine('no')",
        }
    )
    assert found.codeless == ["other.py:3"]


def test_an_unregistered_code_is_named():
    found = scan({"errors.py": PLANTED_BASE, "a.py": "raise ValidationError('x', code='not.here')"})
    assert problems(found, {}) == ["a.py:1: 'not.here' is not in app/error_codes.py"]


def test_an_unused_registry_entry_is_named():
    assert problems(scan({"errors.py": PLANTED_BASE}), {"dead.code": Code("Dead")}) == [
        "'dead.code' is registered and raised nowhere"
    ]


def test_a_param_mismatch_is_named():
    found = scan(
        {
            "errors.py": PLANTED_BASE,
            "a.py": "raise ValidationError('x', code='a.b', params={'amount': 1})",
        }
    )
    assert problems(found, {"a.b": Code("{amount} {currency}", ("amount", "currency"))}) == [
        "a.py:1: 'a.b' passes ['amount'] and its entry names ['amount', 'currency']"
    ]


def test_a_computed_code_is_refused():
    found = scan({"errors.py": PLANTED_BASE, "a.py": "raise ValidationError('x', code=name)"})
    assert problems(found, {}) == ["a.py:1: the code is not a string literal, so it cannot be checked"]


# --------------------------------------------------------------------------- #
# The exception itself
# --------------------------------------------------------------------------- #


def test_a_formatted_or_float_param_is_refused_at_the_raise():
    with pytest.raises(TypeError, match="raw value"):
        ValidationError("x", code="split.does_not_add_up", params={"total": 12.5})
    with pytest.raises(TypeError, match="cannot be translated"):
        ValidationError("x", params={"total": 1})


def test_the_wire_form_carries_iso_dates_and_nothing_without_a_code():
    refusal = ValidationError(
        "x",
        code="reconcile.row_after_statement",
        params={"row_date": date(2026, 3, 2), "statement_date": date(2026, 2, 28)},
    )
    assert refusal.wire() == {
        "code": "reconcile.row_after_statement",
        "params": {"row_date": "2026-03-02", "statement_date": "2026-02-28"},
    }
    assert ValidationError("x").wire() == {}
    assert str(refusal) == "x"


@pytest.mark.parametrize(
    ("typed", "currency", "code", "params"),
    [
        ("12.345", "eur", "money.too_many_decimals", {"value": "12.345", "currency": "EUR", "places": 2}),
        ("12.5", "JPY", "money.decimals_in_whole_currency", {"value": "12.5", "currency": "JPY"}),
        ("1,234", "SEK", "money.not_an_amount", {"value": "1,234"}),
        ("9" * 30, "BRL", "money.too_many_digits", {"digits": 30}),
        ("99999999999999999.99", "GBP", "money.too_large", {"value": "99999999999999999.99"}),
    ],
)
def test_the_money_parsers_refusals_carry_their_codes(typed, currency, code, params):
    with pytest.raises(MoneyError) as refused:
        parse_exact(typed, currency)
    assert (refused.value.code, refused.value.params) == (code, params)


def test_the_money_parsers_sentences_are_unchanged():
    """The sentence is what the account import shows per row; it must not move."""
    with pytest.raises(MoneyError) as refused:
        parse_exact("12.345", "eur")
    assert str(refused.value) == "'12.345' has more decimals than EUR has (2)"


# --------------------------------------------------------------------------- #
# Over HTTP: detail unchanged, code and params beside it
# --------------------------------------------------------------------------- #


def _two_currencies(client) -> dict:
    """The test_api household, plus a SEK account for the cross-currency cases."""
    world = _household_with_accounts(client)
    world["savings"] = client.post(
        f"/api/households/{world['household']['id']}/accounts",
        json={"name": "Sparkonto", "type": "savings", "currency": "SEK"},
        headers=HEADERS,
    ).json()
    assert world["savings"]["currency"] == "SEK"
    return world


def _row(client, world, account, day, amount) -> dict:
    made = client.post(
        f"/api/households/{world['household']['id']}/transactions",
        json={"account_id": world[account]["id"], "date": day, "amount": amount},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()


def test_a_transfer_to_the_same_account_says_so_with_a_code(client):
    world = _two_currencies(client)
    answer = client.post(
        f"/api/households/{world['household']['id']}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["checking"]["id"],
            "date": "2026-01-15",
            "amount": 30_000,
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "an account cannot transfer to itself",
        "code": "transfer.same_account",
        "params": {},
    }


def test_a_cross_currency_transfer_without_the_arriving_amount_names_both(client):
    world = _two_currencies(client)
    answer = client.post(
        f"/api/households/{world['household']['id']}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["savings"]["id"],
            "date": "2026-01-15",
            "amount": 30_000,
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": (
            "a EUR to SEK transfer needs the amount that arrives; we never invent a rate"
        ),
        "code": "transfer.needs_amount_arriving",
        "params": {"from_currency": "EUR", "to_currency": "SEK"},
    }


def test_a_reconciliation_that_does_not_balance_sends_minor_units_and_currency(client):
    world = _two_currencies(client)
    row = _row(client, world, "savings", "2026-02-10", 123_456)
    answer = client.post(
        f"/api/accounts/{world['savings']['id']}/reconciliation",
        json={
            "statement_date": "2026-02-28",
            "statement_balance": 100_000,
            "transaction_ids": [row["id"]],
        },
        headers=HEADERS,
    )
    assert answer.status_code == 409
    body = answer.json()
    # The English sentence still carries the amount formatted for English, and
    # only the sentence does: the params are the figure and its currency.
    assert body["detail"].startswith("that does not balance: ")
    assert "234.56" in body["detail"]
    assert (body["code"], body["params"]) == (
        "reconcile.does_not_balance",
        {"difference": -23_456, "currency": "SEK"},
    )
    assert set(body) == {"detail", "code", "params"}


def test_a_row_after_the_statement_sends_both_dates_as_iso(client):
    world = _two_currencies(client)
    row = _row(client, world, "checking", "2026-03-02", -4_250)
    answer = client.post(
        f"/api/accounts/{world['checking']['id']}/reconciliation",
        json={
            "statement_date": "2026-02-28",
            "statement_balance": -4_250,
            "transaction_ids": [row["id"]],
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "a row dated 2026-03-02 is after the statement closes on 2026-02-28, so it cannot be on it",
        "code": "reconcile.row_after_statement",
        "params": {"row_date": "2026-03-02", "statement_date": "2026-02-28"},
    }


def test_a_split_that_does_not_add_up_sends_both_figures_and_the_currency(client):
    world = _two_currencies(client)
    row = _row(client, world, "savings", "2026-02-10", -10_000)
    answer = client.post(
        f"/api/transactions/{row['id']}/split",
        json={"parts": [{"amount": -6_000}, {"amount": -3_000}]},
        headers=HEADERS,
    )
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": (
            "the parts come to -9000 and the transaction is -10000. "
            "A split has to add up, or it moves the balance."
        ),
        "code": "split.does_not_add_up",
        "params": {"total": -9_000, "amount": -10_000, "currency": "SEK"},
    }


def test_an_unconverted_refusal_still_answers_with_detail_alone(client):
    world = _two_currencies(client)
    answer = client.post(
        f"/api/households/{world['household']['id']}/accounts",
        json={"name": "Spare", "type": "savings", "opening_date": "2999-01-01"},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {"detail": "an account cannot have been opened in the future"}


# --------------------------------------------------------------------------- #
# Agents: byte-identical
# --------------------------------------------------------------------------- #


def _request(path: str, authorization: str | None = None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""})


@pytest.mark.parametrize(
    ("path", "authorization", "carries_code"),
    [
        ("/api/agent/v1/households/h1/transactions/split", None, False),
        ("/api/households/h1/transfers", "Bearer stk_abc", False),
        ("/api/households/h1/transfers", None, True),
        ("/api/households/h1/transfers", "Basic abc", True),
    ],
)
def test_an_agent_gets_the_body_it_got_before(client, path, authorization, carries_code):
    from app.api import deps

    if authorization and authorization.startswith("Bearer "):
        authorization = deps.BEARER + "abc"
    refusal = ValidationError(
        "an account cannot transfer to itself", code="transfer.same_account"
    )
    answer = client.app_module.handle_domain_error(_request(path, authorization), refusal)
    expected = {"detail": "an account cannot transfer to itself"}
    if carries_code:
        expected |= {"code": "transfer.same_account", "params": {}}
    assert answer.body == client.app_module.JSONResponse(expected).body


def test_fields_and_params_never_collide(client):
    refusal = Unauthorized(
        "x", fields={"key_replaced": True}, code="transfer.same_account"
    )
    answer = client.app_module.handle_domain_error(_request("/api/x"), refusal)
    assert answer.body == client.app_module.JSONResponse(
        {"detail": "x", "key_replaced": True, "code": "transfer.same_account", "params": {}}
    ).body


def test_the_classes_still_answer_their_own_status():
    assert (Conflict("x", code="a.b").status_code, DomainError("x").status_code) == (409, 400)


# --------------------------------------------------------------------------- #
# The client's half (#53)
# --------------------------------------------------------------------------- #


def _client_messages() -> dict[str, str]:
    """`client/src/lib/errorMessages.ts`, as code -> English template."""
    import re

    text = (ROOT / "client/src/lib/errorMessages.ts").read_text(encoding="utf-8")
    found = {}
    for block in re.finditer(r'id: "error\.([\w.]+)",\s*message:\s*((?:"[^"]*"\s*)+)', text):
        found[block.group(1)] = "".join(re.findall(r'"([^"]*)"', block.group(2)))
    return found


@pytest.mark.repo_wide
def test_the_client_has_every_code_with_the_same_template():
    """The catalogs are seeded from the client file; it must say what the registry says."""
    assert _client_messages() == {code: entry.template for code, entry in REGISTRY.items()}
