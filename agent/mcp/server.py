"""An MCP server for Spend Tracker, in a few hundred lines.

**This is a sample, not a component of the app.** It lives in the repository
so nobody has to work out the shape of one, and it can be deleted without the
app noticing. The app speaks HTTP and knows about no vendor and no protocol;
MCP is one client among several, and the day it is replaced by something else
only this file changes.

Everything it does goes through `client.py`, which is standard library only and
imports nothing from `app/`. If this file ever needs something the client
cannot do, the HTTP API is missing an endpoint -- that is the signal, and the
fix belongs upstream rather than here.

Run it:

    pip install "mcp[cli]"
    export SPENDTRACKER_URL=https://your-instance
    export SPENDTRACKER_TOKEN=stk_...
    python server.py

Reads and writes are both exposed, grouped by the jobs in `../README.md`. A
read-only key simply gets a sentence back from a write tool ("this key may
only read"); nothing here tries to hide a tool the key cannot use, because the
server's refusal is the one that is enforced.
"""

from __future__ import annotations

import json

from client import AgentError, from_environment

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - the sample's only dependency
    raise SystemExit(
        'this sample needs the MCP package:  pip install "mcp[cli]"\n'
        "The client beside it (client.py) needs nothing and works on its own."
    ) from None

mcp = FastMCP("household-spend-tracker")
_api = from_environment()


def _answer(call) -> str:
    """Run a call and hand back JSON, turning a refusal into a sentence.

    A model reads the error, so it has to say what to do next. A bare
    "HTTPError 429" tells it nothing; "wait 43 seconds" tells it everything.
    """
    try:
        return json.dumps(call(), indent=2)
    except AgentError as exc:
        if exc.status == 429 and exc.retry_after:
            return f"Rate limited. Wait {exc.retry_after} seconds, then try again. {exc.detail}"
        return f"Refused ({exc.status}): {exc.detail}"


@mcp.tool()
def manifest() -> str:
    """What this household is, what this key may do, and which accounts exist.

    Call this first. It replaces a dozen exploratory calls and it states the
    conventions -- money is integer minor units, and no answer ever sums two
    currencies.
    """
    return _answer(_api.manifest)


@mcp.tool()
def spend_summary(
    group_by: str = "category",
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Totals grouped by category, category_group, payee, account or month.

    Dates are ISO, `2026-07-01`. The answer is split by currency with no grand
    total, because this ledger holds no exchange rate.

    Prefer this over listing transactions whenever the question is "how much":
    the arithmetic is done exactly, in the database.
    """
    return _answer(lambda: _api.summary(group_by=group_by, since=since, until=until))


@mcp.tool()
def spend_over_time(bucket: str = "month", since: str | None = None, until: str | None = None) -> str:
    """The same totals gathered by day, week or month."""
    return _answer(lambda: _api.timeseries(bucket=bucket, since=since, until=until))


@mcp.tool()
def balances(as_of: str | None = None) -> str:
    """What each account holds, optionally as of a date.

    A list per account. There is no household total: the accounts may be in
    different currencies and nothing here converts between them.
    """
    return _answer(lambda: _api.balances(as_of=as_of))


@mcp.tool()
def changed_since(since_seq: int = 0) -> str:
    """Rows that changed since a point in the log, and ids that were deleted.

    Keep the `server_seq` you are given and send it back next time; you will
    receive only what moved. Start at 0 to read everything once.
    """
    return _answer(lambda: _api.changed_since(since_seq))


# --------------------------------------------------------------------------- #
# Looking at rows: categorisation review (README, job 2)
# --------------------------------------------------------------------------- #


@mcp.tool()
def register(
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    payee_id: str | None = None,
    uncategorised: bool | None = None,
    reimbursement: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> str:
    """Rows to LOOK at, newest first, paged -- never to add up.

    For "how much", use spend_summary or a report: they add exactly, per
    currency. Continue with `next_offset` while `has_more` is true.
    reimbursement is work|owed|paid|off.
    """
    return _answer(lambda: _api.register(
        since=since, until=until, search=search, account_id=account_id,
        category_id=category_id, payee_id=payee_id,
        uncategorised=None if uncategorised is None else str(uncategorised).lower(),
        reimbursement=reimbursement, limit=limit, offset=offset,
    ))


@mcp.tool()
def categorisation_review(
    since: str | None = None, until: str | None = None,
    min_rows: int = 3, dominant_percent: int = 80,
) -> str:
    """Facts for spotting wrong categories: each payee's categories with
    counts, rows that disagree with their payee's usual category, and
    uncategorised rows whose payee has a usual one. Judge them yourself, show
    the person, then use `categorise`."""
    return _answer(lambda: _api.categorisation_review(
        since=since, until=until, min_rows=min_rows, dominant_percent=dominant_percent,
    ))


@mcp.tool()
def categorise(assignments: list[dict]) -> str:
    """Set categories: [{"transaction_id": ..., "category_id": ...}]. One act,
    one undo for a person. category_id null empties it. Needs a write key."""
    return _answer(lambda: _api.categorise(assignments))


# --------------------------------------------------------------------------- #
# Receipts and expense-portal lines: finding the row (README, jobs 1 and 3)
# --------------------------------------------------------------------------- #


@mcp.tool()
def match_transactions(queries: list[dict], limit: int = 5) -> str:
    """Which rows do these receipts or portal lines describe? Up to 50 queries.

    Each query: {"ref": your own label, "amount": "12.50" (a STRING) or
    "amount_minor": 1250, "currency": "EUR", "date": "2026-07-05",
    "text": "merchant words", "window_days": 4}. Send the currency printed on
    the paper: a row in another currency is matched on date and words only and
    comes back with amount_matched false -- check it before acting on it.
    """
    return _answer(lambda: _api.match(queries, limit=limit))


@mcp.tool()
def upload_receipt(path: str, extracted: dict | None = None, transaction_id: str | None = None) -> str:
    """Store a receipt file (image or PDF) from a local path. The app sniffs,
    compresses and strips it; send it as it is. `extracted` is what you read
    off it -- {"merchant", "date", "currency", "total_minor"} -- kept as a
    claim. Pass transaction_id if you already know the row. Needs a write key."""
    return _answer(lambda: _api.upload_receipt(path, extracted=extracted, transaction_id=transaction_id))


@mcp.tool()
def receipt_inbox(limit: int = 100) -> str:
    """Stored receipts with no transaction yet."""
    return _answer(lambda: _api.receipts(unlinked=True, limit=limit))


@mcp.tool()
def receipt_candidates(
    receipt_id: str, date: str | None = None,
    currency: str | None = None, total_minor: int | None = None,
) -> str:
    """Up to five rows a stored receipt could belong to, ranked, with reasons.
    Pass the date printed on it when the image is a scan or screenshot."""
    return _answer(lambda: _api.candidates(
        receipt_id, date=date, currency=currency, total_minor=total_minor
    ))


@mcp.tool()
def link_receipt(receipt_id: str, transaction_id: str, move: bool = False) -> str:
    """Attach a stored receipt to a row. Refused if it is already on another
    row unless move is true -- only send that if you mean it."""
    return _answer(lambda: _api.link_receipt(receipt_id, transaction_id, move=move))


@mcp.tool()
def flag_work_expenses(assignments: list[dict]) -> str:
    """Mark rows as work expenses an employer will pay back:
    [{"transaction_id": ..., "state": "expected"}] ("written_off" if they will
    not, null to clear). Only money out that is not a transfer can be flagged;
    refused rows come back with a reason and the rest still apply. Needs a
    write key."""
    return _answer(lambda: _api.flag_work_expenses(assignments))


@mcp.tool()
def split_transactions(splits: list[dict]) -> str:
    """Split rows into 2-5 parts that add up to them, e.g. the work and personal
    shares of a partial claim: [{"transaction_id": ..., "parts": [{"amount":
    "-30.00"}, {"amount": "-12.50", "reimbursement": "clear"}]}]. Amounts are
    decimal STRINGS in the account's currency. Needs a key with may_commit (a
    split replaces the row). Refused rows come back with the reason; the rest
    still split, as one undo for a person."""
    return _answer(lambda: _api.split(splits))


# --------------------------------------------------------------------------- #
# Transfers the matcher missed (README, job 5)
# --------------------------------------------------------------------------- #


@mcp.tool()
def transfer_findings(limit: int = 100) -> str:
    """What the automatic matcher sees: suggested pairs with why, legs still
    waiting for a partner, and links waiting for a person to keep them. The
    matcher never pairs two currencies -- those you find yourself."""
    return _answer(lambda: _api.transfer_findings(limit=limit))


@mcp.tool()
def link_transfers(pairs: list[dict]) -> str:
    """Link [{"out_id": money-out row, "in_id": money-in row}] as transfers.
    Different currencies are allowed; the rate comes back as fx_rate. Your
    links wait under "Linked by history only" until a person keeps them.
    Needs a write key."""
    return _answer(lambda: _api.link_transfers(pairs))


@mcp.tool()
def reject_transfers(pairs: list[dict]) -> str:
    """Say a suggested pair is NOT a transfer, so it is never suggested again.
    Not for a pair that is already linked -- unlinking is a person's."""
    return _answer(lambda: _api.reject_transfers(pairs))


# --------------------------------------------------------------------------- #
# The combined position, across currencies (README, job 4)
# --------------------------------------------------------------------------- #


@mcp.tool()
def income_expense(currency: str, since: str | None = None, until: str | None = None) -> str:
    """The Income v Expense report for ONE currency. Call once per currency in
    the manifest; never add the answers together without saying the rate."""
    return _answer(lambda: _api.income_expense(currency, since=since, until=until))


@mcp.tool()
def reimbursements_report(currency: str, since: str | None = None, until: str | None = None) -> str:
    """What work owes, repaid and wrote off, for ONE currency."""
    return _answer(lambda: _api.reimbursements(currency, since=since, until=until))


@mcp.tool()
def observed_fx(pair: str | None = None, since: str | None = None, until: str | None = None) -> str:
    """The exchange rates this household actually got on its own transfers
    between currencies (pair like "EUR/GBP"). Rates are decimal strings. Use
    them to convert in your answer, and say which rate and date you used."""
    return _answer(lambda: _api.fx_observed(pair=pair, since=since, until=until))


if __name__ == "__main__":
    mcp.run()
