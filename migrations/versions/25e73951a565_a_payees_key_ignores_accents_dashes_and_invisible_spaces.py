"""a payee's key ignores accents, dash style and invisible spaces

`payees.name_folded` is what a statement's payee is looked up by, and #268
changed the fold that makes it: NFKD with the combining marks dropped, Unicode
dashes read as `-`, no-break and zero-width spaces read as a space, then the
whitespace collapse and casefold it always had. So `Café Sol` and `CAFE SOL`
are one key now, where they were two.

This recomputes the stored key for every payee whose name folds differently
today. Where two or more payees of one household would now share a key,
`uq_payees_household_folded` allows only one of them to hold it: the one with
the most transactions, then the lowest id, so lookups and rules have one
answer. The others keep the key they had -- choosing which spelling survives
moves transactions, which is a merge, and a merge is somebody's decision. The
payee screen lists those groups (`GET /households/{id}/payee-collisions`) and
the existing merge brings the survivor's key up to date when it runs.

No row is added or removed and no other column changes. The fold is copied
here rather than imported: a migration carries no application import, so it
still runs after `app.services.payees.fold` has moved on again.

The audit log does not see this -- Alembic writes on a plain connection -- and
loses nothing by it: the key is derived from `name`, which is unchanged.

Reversible: clean -- the key is derived from the name; the downgrade recomputes the old fold the same way, and the payee that took a shared key gets its own old one back.

Revision ID: 25e73951a565
Revises: 89c099d8239c
Create Date: 2026-10-01 18:00:00.000000
"""

import re
import unicodedata
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25e73951a565"
down_revision: str | Sequence[str] | None = "89c099d8239c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"
_SPACES = "\u00a0\u202f\u200b\u200c\u200d\u2060\ufeff"
_TABLE = str.maketrans({**dict.fromkeys(_DASHES, "-"), **dict.fromkeys(_SPACES, " ")})
_MARKS = re.compile("[\u0300-\u036f]")


def _new_fold(name: str) -> str:
    text = _MARKS.sub("", unicodedata.normalize("NFKD", name or ""))
    return " ".join(text.translate(_TABLE).split()).casefold()


def _old_fold(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _refold(fold: Callable[[str], str]) -> None:
    """Give every payee `fold(name)` as its key where that is nobody else's.

    Where several payees of one household fold to one key, exactly one of them
    gets it: the one already holding it if any does, otherwise the one with
    the most transactions, then the lowest id. The rest keep the key they had
    and stay in the merge preview. Leaving the key unheld instead would let the
    next `get_or_create` of a third spelling make a third payee.
    """
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, household_id, name, name_folded FROM payees")).all()
    counts = dict(
        conn.execute(
            sa.text(
                "SELECT payee_id, count(*) FROM transactions"
                " WHERE payee_id IS NOT NULL GROUP BY payee_id"
            )
        ).all()
    )

    wanted: dict[tuple[str, str], list[str]] = {}
    held: dict[tuple[str, str], str] = {}
    current: dict[str, tuple[str, str]] = {}
    for payee_id, household_id, name, stored in rows:
        wanted.setdefault((household_id, fold(name)), []).append(payee_id)
        held[(household_id, stored)] = payee_id
        current[payee_id] = (household_id, stored)

    # Until nothing moves: a key can be freed by a payee later in the order.
    moved = True
    while moved:
        moved = False
        for (household_id, key), ids in sorted(wanted.items()):
            if any(current[one] == (household_id, key) for one in ids):
                continue  # already held by one of its own spellings
            if (household_id, key) in held:
                continue  # held by a payee that has not moved off it (yet)
            payee_id = min(ids, key=lambda one: (-counts.get(one, 0), one))
            conn.execute(
                sa.text("UPDATE payees SET name_folded = :key WHERE id = :id"),
                {"key": key, "id": payee_id},
            )
            del held[current[payee_id]]
            held[(household_id, key)] = payee_id
            current[payee_id] = (household_id, key)
            moved = True


def upgrade() -> None:
    _refold(_new_fold)


def downgrade() -> None:
    _refold(_old_fold)
