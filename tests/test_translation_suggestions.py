"""Suggested wordings from review mode, and applying them to the catalogs (#272).

Two households: ours, with its owner (signed in) and a member; theirs, with an
owner of its own who is not in ours. The routes are an owner's, and a
household's own -- everybody else gets the 404 an unknown household gets.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.models import BatchKind, Change, Role, SuggestionStatus, TranslationSuggestion
from scripts import apply_translation_suggestions as apply_script
from tests.conftest import HEADERS, _setup_owner
from tests.test_invitations import _accept, _invite

BASE = "/api/households/{}/translation-suggestions"

#: Invented messages, the shapes the catalogs hold: a plain sentence, one with
#: a placeholder and a tag, and one with a context.
SAVE = "Save"
MOVED = "Moved <0>{name}</0> to {count} rows."


def _world(client) -> dict:
    """Ours (owner signed in, plus a member) and theirs (another owner's)."""
    owner = _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    import app.db as db
    from app.services import households as household_service
    from app.services import translation_suggestions as service
    from tests.conftest import _bootstrap_user

    with db.SessionLocal() as session:
        other = _bootstrap_user(session, email="other.owner@example.com", name="Other", role=Role.owner)
        with batch(session, kind=BatchKind.admin, actor_id=other.id):
            theirs = household_service.create_household(session, name="Theirs", creator=other)
        with batch(session, kind=BatchKind.manual, actor_id=other.id, household_id=theirs.id):
            kept = service.suggest(session, theirs.id, locale="sv-SE", message=SAVE, suggested="Spara nu")
        session.commit()
        theirs_id, their_suggestion = theirs.id, kept.id
    return {"owner": owner, "ours": ours["id"], "theirs": theirs_id, "their_suggestion": their_suggestion}


def _rows() -> list[TranslationSuggestion]:
    import app.db as db

    with db.SessionLocal() as session:
        return list(session.execute(select(TranslationSuggestion).order_by(TranslationSuggestion.created_at)).scalars())


def test_an_owner_suggests_a_wording_and_it_is_stored_as_said(client):
    world = _world(client)
    made = client.post(
        BASE.format(world["ours"]),
        json={"locale": "pt-BR", "message": MOVED, "suggested": "Movi <0>{name}</0> para {count} linhas.",
              "note": "  'Movi' reads better here  "},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text

    ours = [row for row in _rows() if row.household_id == world["ours"]]
    assert [(r.locale, r.context, r.message, r.suggested, r.note, r.status, r.suggested_by_id) for r in ours] == [
        ("pt-BR", "", MOVED, "Movi <0>{name}</0> para {count} linhas.", "'Movi' reads better here",
         SuggestionStatus.open, world["owner"]["user"]["id"]),
    ]
    listed = client.get(BASE.format(world["ours"]), headers=HEADERS).json()
    assert [one["suggested"] for one in listed] == ["Movi <0>{name}</0> para {count} linhas."]

    # Audited: the act is in History, as a suggested wording.
    import app.db as db

    with db.SessionLocal() as session:
        logged = session.execute(
            select(func.count()).select_from(Change).where(Change.table_name == "translation_suggestions")
        ).scalar()
    assert logged == 2, "one for theirs, made in the fixture, and one for ours"


@pytest.mark.parametrize(
    "body",
    [
        {"locale": "en", "message": SAVE, "suggested": "Keep"},
        {"locale": "de-DE", "message": SAVE, "suggested": "Speichern"},
        {"locale": "sv-SE", "message": SAVE, "suggested": "   "},
        {"locale": "sv-SE", "message": "", "suggested": "Spara"},
    ],
)
def test_a_suggestion_for_no_draft_language_or_with_no_words_is_refused(client, body):
    world = _world(client)
    answer = client.post(BASE.format(world["ours"]), json=body, headers=HEADERS)
    assert answer.status_code == 422
    assert [row.household_id for row in _rows()] == [world["theirs"]], "nothing was stored for ours"


def test_the_export_carries_the_open_suggestions_as_the_apply_script_reads_them(client):
    world = _world(client)
    base = BASE.format(world["ours"])
    for body in (
        {"locale": "sv-SE", "message": SAVE, "suggested": "Spara", "note": "Imperative, as the glossary says"},
        {"locale": "sv-SE", "context": "button", "message": SAVE, "suggested": "Spara knapp"},
        {"locale": "pt-BR", "message": MOVED, "suggested": "Movi <0>{name}</0> para {count} linhas."},
    ):
        assert client.post(base, json=body, headers=HEADERS).status_code == 201

    exported = client.get(f"{base}/export.json", headers=HEADERS)
    assert exported.status_code == 200
    assert 'filename="suggestions.json"' in exported.headers["content-disposition"]
    rows = exported.json()["suggestions"]
    assert [(r["locale"], r["context"], r["message"], r["suggested"], r["note"], r["by"]) for r in rows] == [
        ("sv-SE", "", SAVE, "Spara", "Imperative, as the glossary says", "Jane"),
        ("sv-SE", "button", SAVE, "Spara knapp", None, "Jane"),
        ("pt-BR", "", MOVED, "Movi <0>{name}</0> para {count} linhas.", None, "Jane"),
    ], "ours only, oldest first -- theirs is not in it"

    patch = client.get(f"{base}/export.po", params={"locale": "sv-SE"}, headers=HEADERS)
    assert patch.status_code == 200
    assert patch.headers["content-type"].startswith("text/x-gettext-translation")
    assert apply_script.read_patch(patch.text) == [
        apply_script.Suggestion("sv-SE", "", SAVE, "Spara"),
        apply_script.Suggestion("sv-SE", "button", SAVE, "Spara knapp"),
    ]
    assert "# Imperative, as the glossary says" in patch.text

    # Marked applied, they leave the export and stay on record.
    ids = [one["id"] for one in client.get(base, headers=HEADERS).json() if one["locale"] == "sv-SE"]
    marked = client.post(f"{base}/status", json={"ids": ids, "status": "applied"}, headers=HEADERS)
    assert [one["status"] for one in marked.json()] == ["applied", "applied"]
    assert [r["locale"] for r in client.get(f"{base}/export.json", headers=HEADERS).json()["suggestions"]] == ["pt-BR"]
    assert sorted(row.status.value for row in _rows() if row.household_id == world["ours"]) == ["applied", "applied", "open"]


def test_a_withdrawn_suggestion_is_deleted(client):
    world = _world(client)
    base = BASE.format(world["ours"])
    made = client.post(base, json={"locale": "es-ES", "message": SAVE, "suggested": "Guardar"}, headers=HEADERS).json()
    assert client.delete(f"{base}/{made['id']}", headers=HEADERS).status_code == 204
    assert [row.household_id for row in _rows()] == [world["theirs"]]


def test_another_households_suggestion_is_not_found_through_ours(client):
    world = _world(client)
    base = BASE.format(world["ours"])
    gone = client.delete(f"{base}/{world['their_suggestion']}", headers=HEADERS)
    assert gone.status_code == 404
    assert gone.json()["code"] == "translation.suggestion_not_found"
    marked = client.post(f"{base}/status", json={"ids": [world["their_suggestion"]], "status": "applied"}, headers=HEADERS)
    assert marked.status_code == 404
    assert [(row.household_id, row.status) for row in _rows()] == [(world["theirs"], SuggestionStatus.open)]


def _routes(household: str) -> list[tuple[str, str, dict | None]]:
    base = BASE.format(household)
    return [
        ("get", base, None),
        ("post", base, {"locale": "sv-SE", "message": SAVE, "suggested": "Spara"}),
        ("post", f"{base}/status", {"ids": ["f" * 32], "status": "applied"}),
        ("delete", f"{base}/{'f' * 32}", None),
        ("get", f"{base}/export.json", None),
        ("get", f"{base}/export.po?locale=sv-SE", None),
    ]


def _call(client, method: str, path: str, body: dict | None):
    if body is None:
        return getattr(client, method)(path, headers=HEADERS)
    return getattr(client, method)(path, json=body, headers=HEADERS)


def test_another_households_owner_gets_the_404_an_unknown_household_gets(client):
    world = _world(client)
    for (method, path, body), (_, fake, _) in zip(_routes(world["theirs"]), _routes("f" * 32), strict=True):
        real, made_up = _call(client, method, path, body), _call(client, method, fake, body)
        assert real.status_code == made_up.status_code == 404, f"{method} {path}: {real.status_code}"
        assert real.json() == made_up.json()
    assert [(row.household_id, row.suggested) for row in _rows()] == [(world["theirs"], "Spara nu")]


def test_a_member_gets_the_404_a_stranger_gets(client):
    world = _world(client)
    created = _invite(client, role="member", households=[world["ours"]])
    client.cookies.clear()
    _accept(client, created["token"], email="member@example.com", name="Member")
    assert world["ours"] in [one["id"] for one in client.get("/api/households", headers=HEADERS).json()]

    for (method, path, body), (_, fake, _) in zip(_routes(world["ours"]), _routes("f" * 32), strict=True):
        real, made_up = _call(client, method, path, body), _call(client, method, fake, body)
        assert real.status_code == made_up.status_code == 404, f"{method} {path}: {real.status_code}"
        assert real.json() == made_up.json()
    assert [row.household_id for row in _rows()] == [world["theirs"]], "the member stored nothing"


# --------------------------------------------------------------------------- #
# The apply script
# --------------------------------------------------------------------------- #

#: Two languages, each with a plain message, a placeholder message, a message
#: with a context and one nobody suggests for -- all drafts.
ENGLISH = [("", SAVE), ("", MOVED), ("button", SAVE), ("", "Untouched")]
DRAFTS = {
    "pt-BR": ["Salvar", "Movido <0>{name}</0> para {count} linhas.", "Salvar", "Intocado"],
    "sv-SE": ["Spara", "Flyttade <0>{name}</0> till {count} rader.", "Spara", "Orörd"],
    "es-ES": ["Guardar", "Movido <0>{name}</0> a {count} filas.", "Guardar", "Intacto"],
}


def _catalogs(root: pathlib.Path) -> pathlib.Path:
    for locale, drafts in DRAFTS.items():
        blocks = [f'msgid ""\nmsgstr ""\n"Language: {locale}\\n"']
        for (context, message), draft in zip(ENGLISH, drafts, strict=True):
            lines = ["#. A translator note", "#: src/screens/Example.tsx", "#, fuzzy"]
            if context:
                lines.append(f"msgctxt {json.dumps(context)}")
            lines += [f"msgid {json.dumps(message)}", f"msgstr {json.dumps(draft, ensure_ascii=False)}"]
            blocks.append("\n".join(lines))
        (root / locale).mkdir(parents=True)
        (root / locale / "messages.po").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return root


def _entries(root: pathlib.Path, locale: str) -> list[tuple[str, str, str, bool]]:
    text = (root / locale / "messages.po").read_text(encoding="utf-8")
    entries = [apply_script.parse_block(block) for block in text.split("\n\n")]
    return [(e.context, e.message, e.translation, e.fuzzy) for e in entries if e and e.message]


def test_applying_writes_the_words_and_marks_only_those_entries_reviewed(tmp_path):
    root = _catalogs(tmp_path)
    before = (root / "pt-BR" / "messages.po").read_text(encoding="utf-8")
    outcome = apply_script.apply(
        [
            apply_script.Suggestion("sv-SE", "", SAVE, "Spara det"),
            apply_script.Suggestion("sv-SE", "", SAVE, "Spara nu"),  # newer: wins
            apply_script.Suggestion("sv-SE", "button", SAVE, "Spara knappen"),
            apply_script.Suggestion("sv-SE", "", MOVED, "Flyttade {name} till {count} rader."),  # lost its tag
            apply_script.Suggestion("sv-SE", "", "Not in the catalog", "Inte här"),
            apply_script.Suggestion("en", "", SAVE, "Save it"),
        ],
        root,
    )
    assert len(outcome.applied) == 3
    assert [why for _, why in outcome.skipped] == [
        "en is not a draft language",
        "it does not keep the English's placeholders and tags",
        "the message is not in the catalog; its English may have changed",
    ]
    assert _entries(root, "sv-SE") == [
        ("", SAVE, "Spara nu", False),
        ("", MOVED, "Flyttade <0>{name}</0> till {count} rader.", True),
        ("button", SAVE, "Spara knappen", False),
        ("", "Untouched", "Orörd", True),
    ]
    assert (root / "pt-BR" / "messages.po").read_text(encoding="utf-8") == before, "another language is untouched"
    # The note and the reference stay; only the flag goes.
    text = (root / "sv-SE" / "messages.po").read_text(encoding="utf-8")
    assert text.count("#. A translator note") == 4 and text.count("#, fuzzy") == 2

    assert apply_script.reviewed(root) == {"pt-BR": (0, 4), "es-ES": (0, 4), "sv-SE": (2, 4)}
    assert apply_script.percent(2, 4) == "50.0%"


def test_a_downloaded_export_applies_end_to_end(client, tmp_path):
    world = _world(client)
    base = BASE.format(world["ours"])
    for body in (
        {"locale": "pt-BR", "message": MOVED, "suggested": "Movi <0>{name}</0> para {count} linhas."},
        {"locale": "es-ES", "context": "button", "message": SAVE, "suggested": "Guardar ya"},
    ):
        client.post(base, json=body, headers=HEADERS)
    downloaded = tmp_path / "suggestions.json"
    downloaded.write_text(client.get(f"{base}/export.json", headers=HEADERS).text, encoding="utf-8")
    patch = tmp_path / "suggestions-pt-BR.po"
    patch.write_text(client.get(f"{base}/export.po?locale=pt-BR", headers=HEADERS).text, encoding="utf-8")
    root = _catalogs(tmp_path / "locales")

    assert apply_script.main([str(downloaded), str(patch), "--catalogs", str(root)]) == 0
    assert _entries(root, "pt-BR")[1] == ("", MOVED, "Movi <0>{name}</0> para {count} linhas.", False)
    assert _entries(root, "es-ES")[2] == ("button", SAVE, "Guardar ya", False)
    assert apply_script.reviewed(root) == {"pt-BR": (1, 4), "es-ES": (1, 4), "sv-SE": (0, 4)}


@pytest.mark.repo_wide
def test_the_report_reads_the_real_catalogs():
    """Every draft language is counted against every message it holds."""
    report = apply_script.reviewed()
    assert set(report) == {"pt-BR", "es-ES", "sv-SE"}
    totals = {total for _, total in report.values()}
    assert len(totals) == 1 and totals.pop() > 1000
    assert all(0 <= done <= total for done, total in report.values())
