"""The only network code in the one-time import: GETs against YNAB's API (#183).

Small on purpose, so a test replaces :func:`get` and nothing else reaches the
network. Read-only: there is no other verb here and there must never be one.

**The token is the person's, not ours.** It arrives with each request, is put
in one header, and goes nowhere else: not a log line, not an exception, not the
batch source. Every failure is re-raised as :class:`YnabError` with a sentence
this module wrote, and ``from None`` so the original exception -- whose repr can
carry the request, and so the header -- is not chained onto it.

Stdlib ``urllib`` rather than a client library: one more runtime dependency
for five GETs is not a trade worth making. Stdlib also forwards every header
across a redirect to any host, so :func:`_open` follows none (``outbound``), and
the answer is read against a wall-clock deadline rather than only a per-``recv``
timeout (#219).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ...errors import ValidationError
from .. import outbound

BASE = "https://api.ynab.com/v1"

#: A plan's whole transaction list is one response, and a decade of history is
#: a few megabytes. Generous, because a slow answer is still an answer.
TIMEOUT_SECONDS = 60

#: A guard against something that is not YNAB answering, not a size YNAB sends.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: The whole answer, however slowly it arrives. ``TIMEOUT_SECONDS`` bounds one
#: ``recv``; this bounds all of them together.
DEADLINE_SECONDS = 120

TOKEN_REJECTED = "YNAB rejected the token"


class YnabError(ValidationError):
    """A refusal from YNAB, or no answer, in words that carry no token."""


def get(token: str, path: str) -> dict:
    """``GET {BASE}{path}`` with the token, and the ``data`` object it answers.

    ``path`` is built by the callers below from ids YNAB itself returned or the
    client sent, each quoted; nothing here takes a URL from outside.
    """
    if not token or not token.strip():
        raise YnabError("a YNAB personal access token is needed", code="ynab.token_needed")
    request = urllib.request.Request(
        BASE + path,
        headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with _open(request) as answer:
            body = outbound.read_within(
                answer, limit=MAX_RESPONSE_BYTES, seconds=DEADLINE_SECONDS
            )
    except outbound.TooLarge:
        raise YnabError(
            "YNAB's answer was larger than this import will read", code="ynab.answer_too_large"
        ) from None
    except outbound.TooSlow:
        raise YnabError("YNAB took too long to answer; try again later", code="ynab.timeout") from None
    except urllib.error.HTTPError as refused:
        status = refused.code
        refused.close()
        if status == 401:
            raise YnabError(TOKEN_REJECTED, code="ynab.token_rejected") from None
        if status == 404:
            raise YnabError("YNAB has no such plan for this token", code="ynab.no_such_plan") from None
        if status == 429:
            raise YnabError(
                "YNAB is limiting requests from this token for now; try again in an hour",
                code="ynab.rate_limited",
            ) from None
        raise YnabError(
            f"YNAB answered {status}; try again later", code="ynab.status", params={"status": status}
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise YnabError(
            "YNAB could not be reached; check the connection and try again", code="ynab.unreachable"
        ) from None

    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: `[[[[...` nests deeper than the parser goes, and it
        # is not a ValueError (#223).
        raise YnabError("YNAB's answer could not be read", code="ynab.answer_unreadable") from None
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        raise YnabError("YNAB's answer could not be read", code="ynab.answer_unreadable")
    return data


def _open(request: urllib.request.Request):
    """The one place a request leaves: TLS verified, no redirect followed."""
    from ..platform import _trust

    return outbound.opener(_trust()).open(request, timeout=TIMEOUT_SECONDS)


def _plan(plan_id: str) -> str:
    return urllib.parse.quote(plan_id, safe="")


def _listed(value: object) -> list:
    """A list YNAB answered, or a refusal: ``list()`` of a number raises
    TypeError and of a string makes a list of characters (#223). What is in
    the list is checked by ``ynab_source``, where the shapes are known."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise YnabError("YNAB's answer could not be read", code="ynab.answer_unreadable")
    return value


def plans(token: str) -> list[dict]:
    """Every plan (YNAB's newer word for a budget) this token can see."""
    data = get(token, "/plans")
    return _listed(data.get("plans") or data.get("budgets"))


def accounts(token: str, plan_id: str) -> list[dict]:
    return _listed(get(token, f"/plans/{_plan(plan_id)}/accounts").get("accounts"))


def category_groups(token: str, plan_id: str) -> list[dict]:
    return _listed(get(token, f"/plans/{_plan(plan_id)}/categories").get("category_groups"))


#: Asked for explicitly, because YNAB's default is not "all of it": without a
#: ``since_date`` the transactions endpoint answers only the last year or so
#: (checked read-only against a live plan on 2026-09-30: 1,101 rows from one
#: year back, against 10,417 back to the plan's first month with this date).
#: Earlier than any plan can start, so it means "the whole history".
SINCE_DATE = "1970-01-01"


def transactions(token: str, plan_id: str) -> list[dict]:
    path = f"/plans/{_plan(plan_id)}/transactions?since_date={SINCE_DATE}"
    return _listed(get(token, path).get("transactions"))
