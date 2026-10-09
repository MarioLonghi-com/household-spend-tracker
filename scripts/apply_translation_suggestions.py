"""Apply the wordings a household suggested in review mode to the catalogs (#272).

    python -m scripts.apply_translation_suggestions suggestions.json
    python -m scripts.apply_translation_suggestions suggestions-pt-BR.po suggestions-sv-SE.po
    python -m scripts.apply_translation_suggestions            # the report alone

Each file is what Application management's *Download suggestions* gave: JSON
for every language, or a `.po` patch for one. Run it on a branch, from the
repository's root. For each suggestion it finds the message in
`client/src/locales/<locale>/messages.po` -- by its English source and its
context, as the catalogs name it -- writes the suggested words as the
translation and takes the `fuzzy` flag off: the entry is **reviewed**. Then
`npm run extract` (in `client/`) to let Lingui write the files its own way,
and a pull request.

A suggestion is skipped, and named, when:

- its message is not in the catalog -- the English changed since the review;
- it does not keep the English's placeholders and tags exactly (`{0}`,
  `{name}`, `<0>`): a translation that drops one shows `{0}` or loses a link.

When one message has several suggestions, the newest wins: the export lists
them oldest first and they are applied in that order.

Last, for every draft language, it reports how much of the catalog is
reviewed: entries with a translation and no `fuzzy` flag, out of every
message. That is the number #58 ships a language on.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOGS = ROOT / "client" / "src" / "locales"
DRAFT_LOCALES = ("pt-BR", "es-ES", "sv-SE")


@dataclass(frozen=True)
class Suggestion:
    locale: str
    context: str
    message: str
    suggested: str


# --------------------------------------------------------------------------- #
# Reading PO text, the little of it this needs
# --------------------------------------------------------------------------- #


def _unquote(line: str) -> str:
    """A PO string literal's value. PO escapes like JSON for everything a
    message holds -- quotes, backslashes, newlines, tabs."""
    return json.loads(line[line.index('"') :])


@dataclass
class Entry:
    """One block of a PO file, with where its parts are."""

    lines: list[str]
    context: str
    message: str
    translation: str
    fuzzy: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.context, self.message)


def parse_block(block: str) -> Entry | None:
    lines = block.split("\n")
    context = message = translation = ""
    field: str | None = None
    fuzzy = False
    found = False
    for line in lines:
        if line.startswith("#,"):
            fuzzy = fuzzy or "fuzzy" in line
        elif line.startswith("msgctxt "):
            field, context = "ctx", _unquote(line)
        elif line.startswith("msgid "):
            field, message, found = "id", _unquote(line), True
        elif line.startswith("msgstr "):
            field, translation = "str", _unquote(line)
        elif line.startswith('"'):
            if field == "ctx":
                context += _unquote(line)
            elif field == "id":
                message += _unquote(line)
            elif field == "str":
                translation += _unquote(line)
    if not found:
        return None
    return Entry(lines=lines, context=context, message=message, translation=translation, fuzzy=fuzzy)


def read_patch(text: str) -> list[Suggestion]:
    """A `.po` patch: its header names the language, every other entry is one
    suggestion."""
    blocks = [parse_block(block) for block in re.split(r"\n\s*\n", text)]
    entries = [entry for entry in blocks if entry is not None]
    header = next((entry for entry in entries if entry.message == ""), None)
    locale = None
    if header is not None:
        match = re.search(r"^Language:\s*(\S+)\s*$", header.translation, re.M)
        locale = match.group(1) if match else None
    if locale is None:
        raise ValueError("the .po patch has no Language: line in its header")
    return [
        Suggestion(locale, entry.context, entry.message, entry.translation)
        for entry in entries
        if entry.message and entry.translation
    ]


def read_json(text: str) -> list[Suggestion]:
    data = json.loads(text)
    return [
        Suggestion(one["locale"], one.get("context") or "", one["message"], one["suggested"])
        for one in data["suggestions"]
    ]


def read_suggestions(path: pathlib.Path) -> list[Suggestion]:
    text = path.read_text(encoding="utf-8")
    return read_json(text) if text.lstrip().startswith("{") else read_patch(text)


# --------------------------------------------------------------------------- #
# Placeholders and tags: what a translation has to keep
# --------------------------------------------------------------------------- #


def placeholders(text: str) -> tuple[list[str], list[str]]:
    """The placeholder names and the tags, as `catalogs.test.ts` compares them."""
    names = sorted(set(re.findall(r"\{(\w+)[,}]", text)))
    tags = sorted(re.findall(r"</?\d+>", text))
    return names, tags


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #


def _reviewed(entry_lines: list[str], translation: str) -> list[str]:
    """The block with `translation` as its msgstr and no `fuzzy` flag."""
    out: list[str] = []
    skipping = False
    for line in entry_lines:
        if skipping:
            if line.startswith('"'):
                continue
            skipping = False
        if line.startswith("#,"):
            flags = [flag.strip() for flag in line[2:].split(",") if flag.strip() and flag.strip() != "fuzzy"]
            if flags:
                out.append("#, " + ", ".join(flags))
            continue
        if line.startswith("msgstr "):
            out.append("msgstr " + json.dumps(translation, ensure_ascii=False))
            skipping = True
            continue
        out.append(line)
    return out


@dataclass
class Outcome:
    applied: list[Suggestion]
    skipped: list[tuple[Suggestion, str]]


def apply(suggestions: list[Suggestion], catalogs: pathlib.Path = CATALOGS) -> Outcome:
    outcome = Outcome(applied=[], skipped=[])
    by_locale: dict[str, list[Suggestion]] = {}
    for one in suggestions:
        if one.locale not in DRAFT_LOCALES:
            outcome.skipped.append((one, f"{one.locale} is not a draft language"))
            continue
        by_locale.setdefault(one.locale, []).append(one)

    for locale, wanted in by_locale.items():
        path = catalogs / locale / "messages.po"
        blocks = path.read_text(encoding="utf-8").split("\n\n")
        index: dict[tuple[str, str], int] = {}
        for at, block in enumerate(blocks):
            entry = parse_block(block)
            if entry is not None and entry.message:
                index[entry.key] = at
        for one in wanted:
            at = index.get((one.context, one.message))
            if at is None:
                outcome.skipped.append((one, "the message is not in the catalog; its English may have changed"))
                continue
            if placeholders(one.suggested) != placeholders(one.message):
                outcome.skipped.append((one, "it does not keep the English's placeholders and tags"))
                continue
            entry = parse_block(blocks[at])
            assert entry is not None
            blocks[at] = "\n".join(_reviewed(entry.lines, one.suggested))
            outcome.applied.append(one)
        path.write_text("\n\n".join(blocks), encoding="utf-8")
    return outcome


def reviewed(catalogs: pathlib.Path = CATALOGS) -> dict[str, tuple[int, int]]:
    """Per draft language: (reviewed entries, every entry), the header left out."""
    report: dict[str, tuple[int, int]] = {}
    for locale in DRAFT_LOCALES:
        text = (catalogs / locale / "messages.po").read_text(encoding="utf-8")
        entries = [entry for entry in map(parse_block, re.split(r"\n\s*\n", text)) if entry and entry.message]
        done = sum(1 for entry in entries if entry.translation and not entry.fuzzy)
        report[locale] = (done, len(entries))
    return report


def percent(done: int, total: int) -> str:
    return f"{(100 * done / total) if total else 0:.1f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("files", nargs="*", type=pathlib.Path, help="downloaded .json or .po files")
    parser.add_argument("--catalogs", type=pathlib.Path, default=CATALOGS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    suggestions: list[Suggestion] = []
    for path in args.files:
        suggestions += read_suggestions(path)
    if suggestions:
        outcome = apply(suggestions, args.catalogs)
        print(f"Applied {len(outcome.applied)} of {len(suggestions)} suggestions.")
        for one, why in outcome.skipped:
            print(f"  skipped ({one.locale}) {one.message[:70]!r}: {why}")
        if outcome.applied:
            print("Now run `npm run extract` in client/, and open a pull request.")

    print("Reviewed, per language:")
    for locale, (done, total) in reviewed(args.catalogs).items():
        print(f"  {locale}: {done} of {total} ({percent(done, total)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
