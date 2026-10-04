"""What every one-time import workflow reads its source into (#183).

A workflow's only job is to turn another app's export -- a file, an API -- into
a :class:`Source`: accounts, and rows in the shape below. Everything after that
(mapping, duplicates, transfers, the batch) is `engine`'s, and the same for
every app. A second workflow is a second reader, not a second importer.

Plain data, because it has to outlive the rollback a preview ends in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...models import AccountType


@dataclass(slots=True)
class SourceRow:
    """One money movement as the other app recorded it, not yet interpreted.

    Amount and date stay raw here -- ``amount_text`` for a file, ``milliunits``
    for an API -- because reading them needs the currency and the date format,
    which the person confirms after the file has been read.
    """

    #: Stable per incoming row: the other app's id, or a digest of what the
    #: row says (#267).
    ref: str
    account_key: str
    date_text: str
    #: The name the other app's person kept. Becomes the row's payee.
    payee: str = ""
    #: What the *bank* wrote, when the other app kept it (YNAB's API does, as
    #: ``import_payee_name_original``; its Register.csv does not). Stored as
    #: ``Transaction.import_payee_original``, which ``suggest_rules`` reads as
    #: bank text -- so a clean name never goes here (#265).
    payee_original: str | None = None
    #: The other app's own id for the bank line, when it has one. YNAB's is
    #: ``YNAB:<milliunits>:<date>:<occurrence>`` and names the whole bank line,
    #: so each part of a split carries its parent's. Read and carried only;
    #: nothing matches on it yet (#264).
    source_import_id: str | None = None
    #: The ``ref`` of the line a split part was cut from, when the source says
    #: (the API does), so what counts lines rather than rows can.
    parent_ref: str | None = None
    category: str = ""
    group: str = ""
    memo: str = ""
    #: (outflow, inflow) as the file wrote them.
    amount_text: tuple[str, str] | None = None
    #: Thousandths of the currency's major unit, as YNAB's API sends them.
    milliunits: int | None = None
    #: The other app's own word, lowercased: reconciled, cleared, uncleared.
    cleared: str = "uncleared"
    flag: str = ""
    #: The account the other leg of a transfer is in, when this row is one.
    transfer_account_key: str | None = None
    #: The other leg's `ref`, when the source says it outright (the API does).
    transfer_ref: str | None = None
    #: Ids an older build gave this same row, recognised as already imported.
    alt_refs: tuple[str, ...] = ()
    split: bool = False
    starting_balance: bool = False
    #: The physical line, for a file. Only for the report.
    line: int | None = None


@dataclass(slots=True)
class SourceAccount:
    key: str
    name: str
    type_hint: AccountType | None = None
    closed: bool | None = None
    #: What the other app says the account holds, in thousandths of the major
    #: unit, when it says (YNAB's API does; its Register.csv does not). What
    #: the import is checked against (#266). Raw, because reading it needs the
    #: currency the person confirms.
    balance_milliunits: int | None = None
    #: The same for cleared rows only. Read and carried; nothing compares it,
    #: since everything this import writes arrives uncleared.
    cleared_balance_milliunits: int | None = None


@dataclass(slots=True)
class Source:
    via: str
    rows: list[SourceRow]
    accounts: dict[str, SourceAccount]
    filename: str | None = None
    plan_name: str | None = None
    sha256: str | None = None
    currency: str | None = None
    symbol: str | None = None
    #: A file only guesses its currency from a symbol, so a person confirms it.
    currency_confirmed_needed: bool = False
    #: Every date format that reads every date, most likely first.
    date_formats: list[str] = field(default_factory=list)
    #: Amounts written with a decimal comma ("12,34"), which a file can do.
    decimal_comma: bool = False
    #: Category name -> the groups it appeared under.
    category_groups: dict[str, list[str]] = field(default_factory=dict)
    #: The import_source every row written from this source carries.
    import_source: str = ""
    #: What the History headline calls this workflow.
    headline: str = ""
