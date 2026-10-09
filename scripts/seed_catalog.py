"""Write `app/seed_catalog.json` from the client's catalogs (#268).

The server seeds a new household's categories and its "Opening balance" payee
in the language asked for, and cannot read the `.po` files at run time: the
image carries the built client, not its sources. This copies the one slice it
needs -- the ``seed.*`` messages -- out of each translated catalog.

**Reviewed translations only.** An entry marked ``#, fuzzy`` is a draft, and a
draft is never seeded, exactly as the client never serves one. Until #58 has
reviewed a language its slice is empty, and a household asking for it is
seeded in English.

    python -m scripts.seed_catalog          write it
    python -m scripts.seed_catalog --check  exit 1 if it is out of date

`tests/test_seed_words.py` runs the check, so a reviewed seed word that has not
reached the JSON fails the suite rather than quietly seeding English.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "client" / "src" / "locales"
OUT = ROOT / "app" / "seed_catalog.json"

#: Not translations: the source, and the pseudo-locale for CI.
NOT_TRANSLATED = {"en", "en-XA"}
PREFIX = "seed."


def read_po(text: str) -> list[tuple[str, str, bool]]:
    """``(msgid, msgstr, fuzzy)`` per entry. Enough of a PO reader for this."""
    entries = []
    for block in text.split("\n\n"):
        msgid = msgstr = ""
        field = None
        fuzzy = False
        for line in block.splitlines():
            if line.startswith("#,"):
                fuzzy = fuzzy or "fuzzy" in line
            elif line.startswith("msgid "):
                field, msgid = "id", json.loads(line[6:])
            elif line.startswith("msgstr "):
                field, msgstr = "str", json.loads(line[7:])
            elif line.startswith('"') and field == "id":
                msgid += json.loads(line)
            elif line.startswith('"') and field == "str":
                msgstr += json.loads(line)
        if msgid:
            entries.append((msgid, msgstr, fuzzy))
    return entries


def build(locales_dir: Path = LOCALES) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for po in sorted(locales_dir.glob("*/messages.po")):
        locale = po.parent.name
        if locale in NOT_TRANSLATED:
            continue
        out[locale] = {
            msgid: msgstr
            for msgid, msgstr, fuzzy in read_po(po.read_text(encoding="utf-8"))
            if msgid.startswith(PREFIX) and msgstr and not fuzzy
        }
    return {locale: dict(sorted(words.items())) for locale, words in sorted(out.items())}


def render(catalog: dict[str, dict[str, str]]) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    text = render(build())
    if "--check" in argv:
        if OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run python -m scripts.seed_catalog")
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
