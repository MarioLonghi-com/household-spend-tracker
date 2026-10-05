"""A thin client for the Spend Tracker agent API.

**Standard library only, and it imports nothing from `app/`.** That is not
tidiness: this directory is a client of the HTTP API like any other, and the
test that it really is one is that it still works when you copy it somewhere
else. If it ever needs something from `app/`, the API is missing an endpoint.

It is also deliberately separate from `server.py`. The MCP protocol needs a
third-party package; this file does not, so the half that does the real work
can be tested without installing anything, and a caller who wants the API but
not MCP can import this alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: The prefix every token carries. Checked before anything is sent, so a
#: mispasted value fails here rather than as a puzzling 401 from the server.
TOKEN_PREFIX = "stk_"
API_VERSION = 1
DEFAULT_TIMEOUT = 30


class AgentError(RuntimeError):
    """The server refused, and said why in a sentence. Carries it."""

    def __init__(self, status: int, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        #: Present on a 429. Seconds to wait -- the server computes when there
        #: is room again, so honouring it is cheaper than guessing.
        self.retry_after = retry_after


class SpendTracker:
    """Everything a key can reach, as methods.

    >>> st = SpendTracker("https://home.example", "stk_...")
    >>> st.manifest()["household"]["name"]
    'Home'
    """

    def __init__(
        self, base_url: str, token: str, *, timeout: int = DEFAULT_TIMEOUT
    ) -> None:
        if not token.startswith(TOKEN_PREFIX):
            raise ValueError(
                f"an agent key starts with {TOKEN_PREFIX!r}. Got something else -- if you "
                "pasted from the app, take the whole value it showed you once."
            )
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._household: str | None = None

    # -- the wire ---------------------------------------------------------- #

    def _get(self, path: str, **params: Any) -> Any:
        query = [
            (k, item)
            for k, v in params.items()
            if v is not None
            for item in (v if isinstance(v, list | tuple) else [v])
        ]
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._send(urllib.request.Request(  # noqa: S310 -- the scheme is the caller's
            url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        ))

    def _write(self, method: str, path: str, body: dict) -> Any:
        """A POST or PATCH, carrying an Idempotency-Key.

        The key is derived from the body, so a retry of the same request --
        after a timeout, say, when nobody knows whether the first one landed --
        is answered with the first answer instead of acting twice. Two
        genuinely different requests have different bodies and so different
        keys.
        """
        data = json.dumps(body, sort_keys=True).encode()
        key = hashlib.sha256(method.encode() + path.encode() + data).hexdigest()[:32]
        return self._send(urllib.request.Request(  # noqa: S310 -- the scheme is the caller's
            f"{self.base}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        ))

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                detail = json.loads(body).get("detail", "")
            except (ValueError, AttributeError):
                detail = body.decode("utf-8", "replace")[:200]
            if not isinstance(detail, str):
                # A 422 names the fields as a list; a model can read that as JSON.
                detail = json.dumps(detail)[:1000]
            retry = exc.headers.get("Retry-After") if exc.headers else None
            raise AgentError(
                exc.code, detail or f"HTTP {exc.code}",
                retry_after=int(retry) if retry and retry.isdigit() else None,
            ) from exc

    def _house(self, tail: str) -> str:
        return f"/api/agent/v{API_VERSION}/households/{self.household_id}/{tail}"

    # -- what a key can reach ---------------------------------------------- #

    def manifest(self) -> dict:
        """Start here. One call, and you know the household and your own reach."""
        found = self._get(f"/api/agent/v{API_VERSION}/manifest")
        self._household = found["household"]["id"]
        return found

    @property
    def household_id(self) -> str:
        """Cached from the manifest, because every other call needs it.

        A key reaches exactly one household, so this is a constant for the life
        of the client rather than something to pass around.
        """
        if self._household is None:
            self.manifest()
        assert self._household is not None
        return self._household

    def summary(
        self,
        *,
        group_by: str = "category",
        since: str | None = None,
        until: str | None = None,
        account_id: str | None = None,
        search: str | None = None,
        cleared: str | None = None,
    ) -> dict:
        """Totals, grouped, and already partitioned by currency.

        Ask for this rather than the register whenever the question is "how
        much": the arithmetic is exact integers in SQL, and the answer is a few
        hundred tokens instead of several megabytes.
        """
        return self._get(
            f"/api/agent/v{API_VERSION}/households/{self.household_id}/summary",
            group_by=group_by, since=since, until=until,
            account_id=account_id, search=search, cleared=cleared,
        )

    def timeseries(self, *, bucket: str = "month", **filters: Any) -> dict:
        return self._get(
            f"/api/agent/v{API_VERSION}/households/{self.household_id}/timeseries",
            bucket=bucket, **filters,
        )

    def balances(self, *, as_of: str | None = None) -> dict:
        """What each account holds. A list -- there is no total across currencies."""
        return self._get(
            f"/api/agent/v{API_VERSION}/households/{self.household_id}/balances", as_of=as_of
        )

    def changed_since(self, since_seq: int = 0, *, limit: int = 1000) -> dict:
        """Only what moved. Keep `server_seq` and send it back next time.

        A recurring agent that polls this reads the rows that changed rather
        than the whole register, which is the difference between eleven rows
        and four thousand.
        """
        return self._get(
            f"/api/agent/v{API_VERSION}/households/{self.household_id}/transactions",
            since_seq=since_seq, limit=limit,
        )


    # -- reviewing: stories 2 and 4 of #134 --------------------------------- #

    def register(self, *, limit: int = 200, offset: int = 0, **filters: Any) -> dict:
        """Rows to LOOK at, newest first, paged. Never to add up -- that is
        `summary` or a report. Keep calling with `next_offset` while
        `has_more`."""
        return self._get(self._house("register"), limit=limit, offset=offset, **filters)

    def categorisation_review(self, **params: Any) -> dict:
        """Per payee, the categories its rows carry; the rows that disagree with
        their payee's usual one; uncategorised rows that have a usual one."""
        return self._get(self._house("categorisation/review"), **params)

    def income_expense(self, currency: str, **params: Any) -> dict:
        """The Income v Expense report for ONE currency."""
        return self._get(self._house("reports/income-expense"), currency=currency, **params)

    def reimbursements(self, currency: str, **params: Any) -> dict:
        """What work owes, repaid and wrote off, for ONE currency."""
        return self._get(self._house("reports/reimbursements"), currency=currency, **params)

    def fx_observed(self, **params: Any) -> dict:
        """The rates the household actually got on its own currency transfers."""
        return self._get(self._house("fx/observed"), **params)

    # -- matching: stories 1 and 3 ----------------------------------------- #

    def match(self, queries: list[dict], *, limit: int = 5) -> dict:
        """Which rows do these receipts or portal lines describe? Up to 50."""
        return self._write("POST", self._house("transactions/match"), {"queries": queries, "limit": limit})

    def upload_receipt(
        self, path: str, *, extracted: dict | None = None,
        transaction_id: str | None = None, note: str | None = None,
    ) -> dict:
        """Send a receipt file as it is. The app sniffs, compresses and strips it."""
        with open(path, "rb") as handle:
            raw = handle.read()
        body = {
            "filename": os.path.basename(path),
            "content_base64": base64.b64encode(raw).decode("ascii"),
            "extracted": extracted, "transaction_id": transaction_id, "note": note,
        }
        return self._write("POST", self._house("receipts"), {k: v for k, v in body.items() if v is not None})

    def receipts(self, *, unlinked: bool = False, limit: int = 100) -> list:
        return self._get(self._house("receipts"), unlinked=str(unlinked).lower(), limit=limit)

    def candidates(self, receipt_id: str, **params: Any) -> dict:
        return self._get(f"/api/agent/v{API_VERSION}/receipts/{receipt_id}/candidates", **params)

    def link_receipt(self, receipt_id: str, transaction_id: str, *, move: bool = False) -> dict:
        return self._write(
            "POST", f"/api/agent/v{API_VERSION}/receipts/{receipt_id}/link",
            {"transaction_id": transaction_id, "move": move},
        )

    # -- changing things --------------------------------------------------- #

    def categorise(self, assignments: list[dict]) -> dict:
        """[{transaction_id, category_id}], one act and one undo."""
        return self._write("PATCH", self._house("transactions"), {"assignments": assignments})

    def write_memos(self, assignments: list[dict]) -> dict:
        """[{transaction_id, memo}], one act and one undo. Replaces the memo."""
        return self._write("PATCH", self._house("transactions/memo"), {"assignments": assignments})

    def flag_work_expenses(self, assignments: list[dict]) -> dict:
        """[{transaction_id, state: expected|written_off|null, settled_by?}]."""
        return self._write("PATCH", self._house("transactions/reimbursement"), {"assignments": assignments})

    def split(self, splits: list[dict]) -> dict:
        """[{transaction_id, parts: [{amount|amount_minor, category_id?, memo?,
        reimbursement: keep|clear}]}]. Needs may_commit; one act, one undo."""
        return self._write("POST", self._house("transactions/split"), {"splits": splits})

    def transfer_findings(self, *, limit: int = 100) -> dict:
        """The matcher's suggested pairs, lone legs, and links awaiting a person."""
        return self._get(self._house("transfers/findings"), limit=limit)

    def link_transfers(self, pairs: list[dict]) -> dict:
        """[{out_id, in_id}]: link each as the two legs of one transfer."""
        return self._write("POST", self._house("transfers/link"), {"pairs": pairs})

    def reject_transfers(self, pairs: list[dict]) -> dict:
        """[{out_id, in_id}]: not a transfer; never suggest it again."""
        return self._write("POST", self._house("transfers/reject"), {"pairs": pairs})

def from_environment() -> SpendTracker:
    """Build one from `SPENDTRACKER_URL` and `SPENDTRACKER_TOKEN`.

    A key belongs in the environment, not on a command line where it lands in
    shell history and in `ps`.
    """
    url = os.environ.get("SPENDTRACKER_URL")
    token = os.environ.get("SPENDTRACKER_TOKEN")
    if not url or not token:
        raise SystemExit(
            "set SPENDTRACKER_URL and SPENDTRACKER_TOKEN.\n"
            "  export SPENDTRACKER_URL=https://your-instance\n"
            "  export SPENDTRACKER_TOKEN=stk_...\n"
            "A person issues the token from Your account -> Keys for programs."
        )
    return SpendTracker(url, token)
