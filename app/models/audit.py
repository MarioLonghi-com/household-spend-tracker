"""The audit log: batches, changes, import lines.

This replaces the previous build's ``knowledge`` counter, which recorded *that*
a row changed and never what, why, or as part of which operation. The unit here
is the **operation**, because that is the level people ask questions at: "what
did the September import do?" and "undo that import" are both questions about a
batch.

None of these three tables is itself audited -- auditing the audit log is a
recursion with no base case, and the log's own integrity comes from being
written in the same transaction as the rows it describes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, orm_insert_sentinel, relationship

from .base import Base, EnumStr, Timestamped, UUIDPrimaryKey, utcnow
from .enums import BatchKind, BatchStatus, ChangeOp, ImportOutcome


class Batch(Base, UUIDPrimaryKey):
    """One operation that touched the ledger."""

    __tablename__ = "batches"
    __audit__ = False

    kind: Mapped[BatchKind] = mapped_column(EnumStr(BatchKind, 16), nullable=False)
    #: Null for instance-wide acts -- setup, creating a user.
    household_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="SET NULL"), index=True
    )
    actor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: For an import: filename, sha256, byte size, target account, sniffed format.
    source: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[BatchStatus] = mapped_column(
        EnumStr(BatchStatus, 12), default=BatchStatus.running, nullable=False
    )
    #: Counts: created, updated, skipped, matched.
    summary: Mapped[dict | None] = mapped_column(JSON)
    undone_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("batches.id"))
    #: Which key acted, when a program did. **Beside `actor_id`, never instead
    #: of it**: an agent borrows a person's authority, so History names both.
    #:
    #: SET NULL, not RESTRICT. `actor_id` is RESTRICT because a user is never
    #: deleted while their history stands; a key is revoked and swept on a
    #: thirty-day window, and History has to keep reading correctly afterwards.
    #: The key's *name* is denormalised into `source["agent"]` for exactly that
    #: moment -- the foreign key is the live link, and the copy is what makes
    #: the sentence survive the sweep.
    agent_key_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("agent_keys.id", ondelete="SET NULL"), index=True
    )

    #: Declared so the unit of work can sort inserts by dependency. A bare
    #: ForeignKey column is not enough: without a relationship SQLAlchemy will
    #: happily insert a child before its parent and hit the constraint.
    actor: Mapped[User] = relationship("User")  # noqa: F821
    changes: Mapped[list[Change]] = relationship(
        "Change", back_populates="batch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_batches_household_started", "household_id", "started_at"),
        #: The instance's log by time, whatever the household. The notice of
        #: resets and new owners asks for "the last fourteen days" of it on
        #: every owner's page load, and without this read every batch the
        #: ledger had ever written to answer (#286).
        Index("ix_batches_started_at", "started_at"),
    )


class Change(Base):
    """One row-level effect, written in the same transaction as the effect."""

    __tablename__ = "changes"
    __audit__ = False

    #: The position in the log. Commit order is the only reliable ordering:
    #: timestamps tie and skew, and a batch id says which act but not the order
    #: of effects inside it. Undo replays in reverse seq.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Never read by the application. A server-generated `seq` is something
    #: SQLAlchemy will only fetch back from a many-row `INSERT ... RETURNING`
    #: when each row also carries a value of its own to match the answers up
    #: by; without one it sent one INSERT per change row, 11,202 of them for a
    #: 10k-row import (#229). With it the log goes in batches, and each row's
    #: `seq` still comes back in the order the rows were added -- which is the
    #: order undo replays backwards.
    _sentinel: Mapped[int | None] = orm_insert_sentinel()
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Denormalised so "the history of this row" is a household-scoped read that
    #: uses one index, rather than a join through batches on every query.
    household_id: Mapped[str | None] = mapped_column(String(32), index=True)
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    row_id: Mapped[str] = mapped_column(String(32), nullable=False)
    op: Mapped[ChangeOp] = mapped_column(EnumStr(ChangeOp, 8), nullable=False)
    #: Full row images. ``before`` is null on insert, ``after`` null on delete.
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    #: Columns deliberately omitted from both images -- secrets. Recorded by
    #: name so a reader can tell "not captured" from "was null".
    redacted: Mapped[list | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    batch: Mapped[Batch] = relationship("Batch", back_populates="changes")

    __table_args__ = (
        Index("ix_changes_row_history", "table_name", "row_id", "seq"),
        #: A household's log of one table, by op and row. `ix_changes_row_history`
        #: leads with the table, so "who entered this household's transactions"
        #: read every transaction change on the instance and filtered by
        #: household afterwards (#238).
        Index("ix_changes_household_table_row", "household_id", "table_name", "op", "row_id", "seq"),
    )


class ImportLine(Base, UUIDPrimaryKey, Timestamped):
    """The verdict on one line of an imported statement.

    ``changes`` only sees rows that changed, and a line skipped as a duplicate
    changes nothing -- which is exactly the line you need to see when a monthly
    import appears to have lost a transaction.
    """

    __tablename__ = "import_lines"
    __audit__ = False

    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The line exactly as it appeared in the file. Keeping this is what lets
    #: "hash only" be a real choice: the file need not be retained for the
    #: import to be re-read, committed or explained a year later.
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the parser made of it: date, amount, payee.
    parsed: Mapped[dict | None] = mapped_column(JSON)
    outcome: Mapped[ImportOutcome] = mapped_column(EnumStr(ImportOutcome, 20), nullable=False)
    #: A category chosen by hand on the preview screen, overriding whatever the
    #: payee's rule would have picked. Only the choice is stored: what the rule
    #: *would* pick is derived on every read, so correcting a payee's history
    #: between staging and committing is reflected rather than frozen.
    #:
    #: SET NULL rather than CASCADE: archiving is the normal way to retire a
    #: category, and a staged line pointing at a deleted one should lose its
    #: override, not take the line with it.
    category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.id", ondelete="SET NULL")
    )
    #: The row it produced, or the existing row it was absorbed into. Indexed
    #: for the transaction panel's "where did this come from", which asks it
    #: of a table that only grows (#238).
    transaction_id: Mapped[str | None] = mapped_column(String(32), index=True)
    #: Why it was skipped or held, in words.
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_import_lines_batch_line", "batch_id", "line_no"),)
