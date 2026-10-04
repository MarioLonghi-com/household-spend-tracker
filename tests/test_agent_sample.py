"""The sample client in `agent/mcp/`, exercised against the real app.

A sample that has never been run is a sample that is wrong, and this one is
the first thing somebody wiring up an agent will copy. So it is pointed at a
live TestClient instance and asked to do the things the README claims it does.

It also pins the property the directory exists to demonstrate: **it imports
nothing from `app/`**. If that ever stops being true, the HTTP API is missing
an endpoint and the fix belongs upstream, not here.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import AgentScope, BatchKind, Household, Transaction, User
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

SAMPLE = Path(__file__).resolve().parent.parent / "agent" / "mcp"


@pytest.fixture()
def sample():
    """The sample on `sys.path`, exactly as running it from its own directory."""
    sys.path.insert(0, str(SAMPLE))
    try:
        import client

        yield client
    finally:
        sys.path.remove(str(SAMPLE))
        sys.modules.pop("client", None)


@pytest.fixture()
def wired(client, sample, monkeypatch):
    """A seeded household, a key, and the sample pointed at the TestClient.

    `urlopen` is redirected onto the TestClient rather than a socket: the point
    is to exercise the sample's own request building, response parsing and
    error handling against the real application, not to prove that TCP works.
    """
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    made = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    client.post(
        f"/api/households/{house['id']}/transactions",
        json={"account_id": made["id"], "date": "2026-07-05",
              "amount": -1250, "payee_name": "Mercadona"},
        headers=HEADERS,
    )

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, world["user"]["id"])
        house_row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house_row.id):
            _key, token = key_service.issue(
                own, user=user, household=house_row, label="the sample",
                scope=AgentScope.write,
            )
        own.commit()
    client.cookies.clear()

    class _Response:
        """Enough of an http.client response for `json.loads(r.read())`."""

        def __init__(self, raw: bytes) -> None:
            self._raw = raw

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    def fake_urlopen(request, timeout=None):
        import urllib.error

        answer = client.request(
            request.get_method(),
            request.full_url.replace("https://testserver", ""),
            content=request.data,
            headers=dict(request.headers),
        )
        if answer.status_code >= 400:
            # A real body, because the client reads it to get the sentence the
            # server wrote. Passing None here makes `exc.read()` empty and the
            # test passes against a client that never parses anything.
            raise urllib.error.HTTPError(
                request.full_url, answer.status_code, answer.reason_phrase,
                answer.headers, io.BytesIO(answer.content),
            )
        return _Response(answer.content)

    monkeypatch.setattr(sample.urllib.request, "urlopen", fake_urlopen)
    return sample.SpendTracker("https://testserver", token), house


# --------------------------------------------------------------------------- #
# The property the directory exists to demonstrate
# --------------------------------------------------------------------------- #


def test_the_sample_imports_nothing_from_the_app(sample):
    """`statements/` has the same rule and a test that keeps it.

    A client that needs the app's internals is not a client -- it is a second
    copy of the app, and the API it is reaching past is the thing to fix.
    """
    source = (SAMPLE / "client.py").read_text()
    assert "from app" not in source
    assert "import app" not in source

    # And nothing outside the standard library either, so it runs anywhere.
    import ast

    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), f"not stdlib: {roots - set(sys.stdlib_module_names)}"


def test_a_token_without_the_prefix_is_refused_before_anything_is_sent(sample):
    """A mispaste should fail where it happened, not as a puzzling 401."""
    with pytest.raises(ValueError, match="stk_"):
        sample.SpendTracker("https://testserver", "eyJhbGciOiJIUzI1NiJ9")


# --------------------------------------------------------------------------- #
# It does what the README says it does
# --------------------------------------------------------------------------- #


def test_the_manifest_call_works_and_caches_the_household(wired):
    api, house = wired
    found = api.manifest()

    assert found["household"]["name"] == "Home"
    assert api.household_id == house["id"]


def test_the_household_is_discovered_without_being_asked_for(wired):
    """A key reaches one household, so a caller should never have to pass it."""
    api, house = wired
    assert api.household_id == house["id"]


def test_summary_returns_figures_partitioned_by_currency(wired):
    api, _house = wired
    answer = api.summary(group_by="payee")

    assert answer["by_currency"]["EUR"]["groups"][0]["name"] == "Mercadona"
    assert answer["by_currency"]["EUR"]["total_minor"] == -1250


def test_balances_and_timeseries_answer(wired):
    api, _house = wired
    assert api.balances()["accounts"][0]["balance_minor"] == -1250
    assert "EUR" in api.timeseries(bucket="month")["by_currency"]


def test_changed_since_gives_a_sequence_to_come_back_with(wired):
    api, _house = wired
    first = api.changed_since()
    assert first["server_seq"] > 0
    assert api.changed_since(first["server_seq"])["changed"] == []


def test_a_refusal_arrives_as_a_sentence_and_not_a_traceback(wired, sample):
    """A model reads the error, so it has to say what happened."""
    api, _house = wired
    api.token = "stk_invented"

    with pytest.raises(sample.AgentError) as refused:
        api.manifest()
    assert refused.value.status == 401
    assert "not valid" in refused.value.detail


# --------------------------------------------------------------------------- #
# The jobs of #134, through the sample: find a row, look at rows, change one
# --------------------------------------------------------------------------- #


def test_match_finds_the_row_a_portal_line_describes(wired):
    """The expense-portal story: nothing stored first, `ref` comes back."""
    api, _house = wired
    answer = api.match([
        {"ref": "claim-7", "amount": "12.50", "currency": "EUR",
         "date": "2026-07-06", "text": "mercadona"},
    ])

    (result,) = answer["results"]
    assert result["ref"] == "claim-7"
    assert result["candidates"][0]["payee"] == "Mercadona"
    assert result["candidates"][0]["amount_minor"] == -1250


def test_register_and_review_are_reachable(wired):
    api, _house = wired
    page = api.register(search="Mercadona")
    assert [row["amount_minor"] for row in page["rows"]] == [-1250]
    assert api.categorisation_review()["rows_considered"] >= 1


def test_a_write_goes_through_and_is_what_it_changed(wired, client):
    """PATCH with a body, not only GETs: the category is really on the row."""
    api, _house = wired
    row_id = api.register()["rows"][0]["id"]
    category_id = api.manifest()["categories"][0]["id"]

    done = api.categorise([{"transaction_id": row_id, "category_id": category_id}])

    assert done["changed"] == 1
    with Session(client.app_module.db_engine) as own:
        assert own.get(Transaction, row_id).category_id == category_id


def test_a_retried_write_carries_the_same_idempotency_key(wired, sample, monkeypatch):
    """The key is derived from the body, so a retry after a timeout is a retry."""
    api, _house = wired
    seen: list[str] = []
    real = sample.urllib.request.urlopen

    def spying(request, timeout=None):
        if request.get_method() == "POST":
            seen.append(request.headers.get("Idempotency-key"))
        return real(request, timeout=timeout)

    monkeypatch.setattr(sample.urllib.request, "urlopen", spying)
    query = [{"ref": "r", "amount_minor": 1250, "currency": "EUR", "date": "2026-07-05"}]
    api.match(query)
    api.match(query)
    api.match([{**query[0], "ref": "other"}])

    assert len(seen) == 3
    assert seen[0] and seen[0] == seen[1] != seen[2]
