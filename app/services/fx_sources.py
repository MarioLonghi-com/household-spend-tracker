"""Where published exchange rates are fetched from: one adapter per source (#396 §2).

v1 has one remote source, the European Central Bank's euro reference rates.
Another central bank or a commercial API would be another adapter here, with
its own `FxSource` value.

**Importing this module does nothing.** No request is made, no connection
opened, no timer started: a request leaves only from :func:`fetch_ecb`, and
only `fx_fetching.run` calls it, after a member pressed Fetch now. The request
carries currency codes and dates and nothing else -- no household data, no
address, no identifying user agent -- and keeps the two rules in
`services/outbound.py`: no redirects, and a deadline on the whole read.
"""

from __future__ import annotations

import csv
import io
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal, InvalidOperation

from . import outbound

#: The one host a fetch talks to. Named in the docs, so a firewalled install
#: knows what to allow.
ECB_HOST = "data-api.ecb.europa.eu"

#: The ECB's SDMX REST API: daily (`D`) reference rates (`SP00`, average
#: `A`) of each currency against the euro, as CSV, data only.
ECB_URL = (
    f"https://{ECB_HOST}/service/data/EXR/D.{{codes}}.EUR.SP00.A"
    "?startPeriod={since}&endPeriod={until}&format=csvdata&detail=dataonly"
)

#: Thirty currencies back to 1999 is about 210,000 lines of 60 bytes.
ANSWER_LIMIT = 32 * 1024 * 1024
#: The whole answer, not one `recv`.
ANSWER_SECONDS = 120.0
#: Each `recv`.
SOCKET_TIMEOUT = 30.0

#: The currencies the ECB publishes a reference rate for today, and the ones it
#: published until they were replaced or suspended (its history stays). For
#: the dashboard's "Not covered" list only: a fetch asks for whatever the
#: household holds, and a code the ECB does not know simply comes back empty.
ECB_CURRENT = frozenset(
    ["USD", "JPY", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR"]
)
ECB_HISTORICAL = frozenset(["BGN", "HRK", "RUB", "CYP", "EEK", "LTL", "LVL", "MTL", "SIT", "SKK"])
ECB_PUBLISHES = ECB_CURRENT | ECB_HISTORICAL | {"EUR"}


class SourceError(Exception):
    """A fetch that did not work, with the reason in words."""


@dataclass(frozen=True, slots=True)
class Published:
    """One rate as a source published it: units of `currency` per one `quote`."""

    quote: str
    currency: str
    date: Date
    rate: str


def _get(url: str) -> bytes:
    """The body of one GET, by the rules in `services/outbound.py`."""
    opener = outbound.opener(ssl.create_default_context())
    # Only what the API needs. urllib's own user agent names the library and
    # its version, nothing about this instance or anybody in it.
    request = urllib.request.Request(url, headers={"Accept": "text/csv"})
    with opener.open(request, timeout=SOCKET_TIMEOUT) as answer:
        return outbound.read_within(answer, limit=ANSWER_LIMIT, seconds=ANSWER_SECONDS)


def ecb_url(currencies: Iterable[str], since: Date, until: Date) -> str:
    codes = "+".join(sorted({c.upper() for c in currencies}))
    return ECB_URL.format(codes=codes, since=since.isoformat(), until=until.isoformat())


def fetch_ecb(
    currencies: Iterable[str],
    since: Date,
    until: Date,
    *,
    get: Callable[[str], bytes] | None = None,
) -> list[Published]:
    """Every ECB reference rate for ``currencies`` from ``since`` to ``until``.

    One request for all of them: the SDMX API returns every currency of a date
    range in one answer. ``get`` is for tests, which never reach the network.
    """
    codes = sorted({c.upper() for c in currencies} - {"EUR"})
    if not codes:
        return []
    url = ecb_url(codes, since, until)
    try:
        body = (get or _get)(url)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"the ECB answered {exc.code} {exc.reason}".strip()) from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"could not reach {ECB_HOST}: {exc.reason}") from exc
    except outbound.TooLarge as exc:
        raise SourceError("the ECB's answer was larger than a fetch accepts") from exc
    except (outbound.TooSlow, TimeoutError) as exc:
        raise SourceError(f"{ECB_HOST} took too long to answer") from exc
    except OSError as exc:
        raise SourceError(f"could not reach {ECB_HOST}: {exc}") from exc
    return parse_ecb_csv(body)


def parse_ecb_csv(body: bytes) -> list[Published]:
    """The rows of an SDMX ``csvdata`` answer. An empty body is no rates."""
    text = body.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    needed = {"CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE"}
    if not reader.fieldnames or not needed <= set(reader.fieldnames):
        raise SourceError("the ECB's answer was not the CSV a fetch expects")
    out: list[Published] = []
    for row in reader:
        value = (row.get("OBS_VALUE") or "").strip()
        try:
            day = Date.fromisoformat((row.get("TIME_PERIOD") or "").strip())
            rate = Decimal(value)
        except (ValueError, InvalidOperation):
            # A holiday the API marks without a figure is not a rate.
            continue
        if not rate.is_finite() or rate <= 0:
            continue
        out.append(
            Published(
                quote=(row["CURRENCY_DENOM"] or "").strip().upper(),
                currency=(row["CURRENCY"] or "").strip().upper(),
                date=day,
                rate=value,
            )
        )
    return out
