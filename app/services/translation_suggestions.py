"""Suggested wordings for the draft languages, from review mode (#272).

The household reviews the drafts in the app. An owner picks a message and
writes a better wording; the wording is stored here, exported as a `.po` patch
or as JSON, and applied to the catalogs on a branch by
`scripts/apply_translation_suggestions.py`. Nothing here touches a catalog:
the server never sees one.

Writes happen inside the caller's batch, through loaded objects, like every
other audited table.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.batch import BATCH_KEY
from ..errors import NotFound
from ..models import SuggestionStatus, TranslationSuggestion, User

#: The languages with drafts. A suggestion for any other is refused by the
#: schema before it gets here.
DRAFT_LOCALES = ("pt-BR", "es-ES", "sv-SE")


def suggest(
    session: Session,
    household_id: str,
    *,
    locale: str,
    message: str,
    suggested: str,
    context: str = "",
    note: str | None = None,
) -> TranslationSuggestion:
    """Keep one suggested wording. Two for the same message are both kept:
    the export lists them oldest first, so the newest is the one applied."""
    batch_row = session.info.get(BATCH_KEY)
    note = (note or "").strip() or None
    row = TranslationSuggestion(
        household_id=household_id,
        locale=locale,
        context=context,
        message=message,
        suggested=suggested,
        note=note,
        suggested_by_id=batch_row.actor_id if batch_row is not None else None,
        status=SuggestionStatus.open,
    )
    session.add(row)
    session.flush()
    return row


def listed(
    session: Session, household_id: str, *, status: SuggestionStatus | None = None
) -> list[TranslationSuggestion]:
    """The household's suggestions, oldest first."""
    query = select(TranslationSuggestion).where(TranslationSuggestion.household_id == household_id)
    if status is not None:
        query = query.where(TranslationSuggestion.status == status)
    return list(
        session.execute(
            query.order_by(TranslationSuggestion.created_at, TranslationSuggestion.id)
        ).scalars()
    )


def get(session: Session, household_id: str, suggestion_id: str) -> TranslationSuggestion:
    row = session.execute(
        select(TranslationSuggestion).where(
            TranslationSuggestion.id == suggestion_id,
            TranslationSuggestion.household_id == household_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("no such suggested wording", code="translation.suggestion_not_found")
    return row


def mark(rows: Iterable[TranslationSuggestion], status: SuggestionStatus) -> int:
    """Move each row to ``status``; returns how many changed."""
    changed = 0
    for row in rows:
        if row.status is not status:
            row.status = status
            changed += 1
    return changed


def remove(session: Session, row: TranslationSuggestion) -> None:
    session.delete(row)
    session.flush()


def _names(session: Session, rows: list[TranslationSuggestion]) -> dict[str, str]:
    ids = {row.suggested_by_id for row in rows if row.suggested_by_id}
    if not ids:
        return {}
    return dict(session.execute(select(User.id, User.display_name).where(User.id.in_(ids))).all())


def as_json(session: Session, rows: list[TranslationSuggestion]) -> str:
    """Every row, as the apply script reads it. Oldest first."""
    names = _names(session, rows)
    return json.dumps(
        {
            "suggestions": [
                {
                    "locale": row.locale,
                    "context": row.context,
                    "message": row.message,
                    "suggested": row.suggested,
                    "note": row.note,
                    "by": names.get(row.suggested_by_id or ""),
                    "at": row.created_at.isoformat(timespec="seconds") + "Z",
                }
                for row in rows
            ]
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _po_string(text: str) -> str:
    """A PO string literal: JSON's escaping is PO's for every character a
    message holds -- quotes, backslashes, newlines, tabs."""
    return json.dumps(text, ensure_ascii=False)


def as_po(session: Session, rows: list[TranslationSuggestion], locale: str) -> str:
    """One language's suggestions as a `.po` patch: a header naming the
    language, then one entry per suggestion, oldest first, each with who
    suggested it, when, and their note as translator comments."""
    names = _names(session, rows)
    out = [
        f"# Suggested wordings for {locale}, from Spend Tracker's review mode.",
        "# Apply with: scripts/apply_translation_suggestions.py <this file>",
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=utf-8\\n"',
        f'"Language: {locale}\\n"',
        "",
    ]
    for row in rows:
        if row.locale != locale:
            continue
        who = names.get(row.suggested_by_id or "", "someone")
        out.append(f"# {who}, {row.created_at.date().isoformat()}")
        for line in (row.note or "").splitlines():
            out.append(f"# {line}")
        if row.context:
            out.append(f"msgctxt {_po_string(row.context)}")
        out.append(f"msgid {_po_string(row.message)}")
        out.append(f"msgstr {_po_string(row.suggested)}")
        out.append("")
    return "\n".join(out)
