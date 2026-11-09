"""Exchange rates: public ones, a household's own, and the record of fetching (#396).

**The ledger never converts.** Nothing here is a converted amount; these are
the rates a *report* may convert with, read through `app/services/fx.py` and
nowhere else. Every rate is a decimal stored as text, quoted as units of
`currency` per one `quote_currency`, so 0.8346 GBP per 1 EUR is stored as
``quote_currency="EUR", currency="GBP", rate="0.8346"``.

Two rate tables rather than one with a nullable `household_id`. SQLite treats
every NULL as distinct inside a UNIQUE constraint, so a shared table could
never make the fetch's upsert idempotent: every fetch would add the same ECB
day again. The split also settles the audit question -- public rates can be
fetched again and are nobody's data, manual rates are a person's and come back
with Undo.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, EnumStr, Timestamped, UUIDPrimaryKey, utcnow
from .domain import Household
from .enums import FxFetchMode, FxFetchOutcome, FxPeriod, FxSource

#: Wide enough for any rate a source publishes or a person types, at the
#: precision the manual editor accepts.
RATE_LENGTH = 40


class FxRate(Base, UUIDPrimaryKey):
    """One published rate, shared by every household on the instance.

    **Not audited, and excluded explicitly** (`EXPECTED_EXCLUDED`). A fetch
    writes it, not a person, and outside a batch -- which the audit hook allows
    only for a table it does not watch. It is public data that can be fetched
    again, so it is not household data either: a restored household never
    needs it, and an empty table only means nobody has pressed Fetch now.

    Every column of the unique key is non-null, so fetching the same day twice
    leaves one row.
    """

    __tablename__ = "fx_rates"
    __audit__ = False

    source: Mapped[FxSource] = mapped_column(EnumStr(FxSource, 32), nullable=False)
    #: The currency the rate is against: EUR for the ECB.
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: The publication date.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Units of `currency` per one `quote_currency`, as the source wrote it.
    rate: Mapped[str] = mapped_column(String(RATE_LENGTH), nullable=False)
    #: When it was stored or last replaced. The `fx_fetches` row has who.
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "quote_currency", "currency", "date", name="uq_fx_rates_key"),
        Index("ix_fx_rates_currency_date", "currency", "date"),
    )


class FxManualRate(Base, UUIDPrimaryKey, Timestamped):
    """A rate a member typed or imported: the household's own data.

    **Audited**, and written inside a batch like every other person-made row,
    so an edit or a delete comes back with Undo in History. `source = manual`
    in a response means a row of this table.
    """

    __tablename__ = "fx_manual_rates"
    __audit__ = True

    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The household's base currency when the rate was entered. A rate keeps
    #: its meaning when the base changes later: it is still so many units per
    #: one of *this* currency, and the conversion crosses through it.
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: The day, or for a monthly rate the first day of the month.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[FxPeriod] = mapped_column(EnumStr(FxPeriod, 8), nullable=False)
    #: Units of `currency` per one `quote_currency`.
    rate: Mapped[str] = mapped_column(String(RATE_LENGTH), nullable=False)
    #: NULL once that user is deleted.
    created_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )

    household: Mapped[Household] = relationship("Household", back_populates="fx_manual_rates")

    __table_args__ = (
        UniqueConstraint(
            "household_id", "quote_currency", "currency", "date", "period",
            name="uq_fx_manual_rates_key",
        ),
    )


class FxFetch(Base, UUIDPrimaryKey):
    """One source fetched by one press of Fetch now (#396 §4.4).

    The dashboard's history and the "fetched ... by ..." of the Rates as of
    line. **Not audited, and excluded explicitly**: an operational log, like
    the housekeeping counts, not a person's data -- the `fx_rates` rows a fetch
    writes are the data. Swept after `services.fx_fetching.RETENTION` by
    `app/auth/housekeeping.py`.
    """

    __tablename__ = "fx_fetches"
    __audit__ = False

    #: The household on whose page the button was pressed. A database cascade
    #: is enough here: the table is not audited, so there is nothing for the
    #: hook to miss when a household goes.
    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL once that user is deleted.
    started_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )
    source: Mapped[FxSource] = mapped_column(EnumStr(FxSource, 32), nullable=False)
    mode: Mapped[FxFetchMode] = mapped_column(EnumStr(FxFetchMode, 16), nullable=False)
    requested_since: Mapped[date] = mapped_column(Date, nullable=False)
    requested_until: Mapped[date] = mapped_column(Date, nullable=False)
    #: The codes asked for, as a JSON list.
    currencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    #: NULL while it runs.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[FxFetchOutcome] = mapped_column(
        EnumStr(FxFetchOutcome, 16), nullable=False, default=FxFetchOutcome.running
    )
    rates_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rates_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The reason in words when it failed.
    error: Mapped[str | None] = mapped_column(Text)
    #: How far it got (#409). The range is asked for one calendar year per
    #: request, and each year is stored as it arrives: `chunks_total` requests
    #: in all, `chunks_done` of them stored. A fetch from before #409 made one
    #: request, and reads as 1 of 1.
    chunks_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    chunks_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: The dates of the request in flight while it runs, and of the one it
    #: stopped on when it failed. NULL once it finished.
    chunk_since: Mapped[date | None] = mapped_column(Date)
    chunk_until: Mapped[date | None] = mapped_column(Date)
