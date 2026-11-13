"""The help articles, for the agent API (#460 §9).

The articles are written once, as Markdown in ``client/src/help/<locale>/``,
and the client's build compiles them (``client/help-articles.ts``). The build
also writes each language as ``help/<locale>.json`` beside the rest of the
client in ``app/static/dist`` -- which the container image copies in whole and
the desktop bundle carries as data -- and that file is what this module reads:
one source, the same text the panel shows.

A checkout whose client has never been built (the test suite, ``make api``)
has no such file, so the Markdown itself is read instead: front matter and
body, split here exactly as ``client/src/help/compile.ts`` splits it. Nothing
here renders Markdown; an agent is handed the body as written.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
#: The articles as written, in a checkout.
SOURCE = _APP.parent / "client" / "src" / "help"
#: The source language: what a reader of a language with no articles gets.
ENGLISH = "en"


def built_dir() -> Path:
    """Where the build put its copy: the client's own output, moved as the app moves it."""
    dist = os.environ.get("SPENDTRACKER_CLIENT_DIST") or _APP / "static" / "dist"
    return Path(dist) / "help"


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    screens: tuple[str, ...]
    also: tuple[str, ...]
    requires: str | None
    markdown: str


class HelpFormatError(ValueError):
    """An article file this module cannot read. The client's build refuses it too."""


_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def parse(text: str, where: str = "article") -> Article:
    """One Markdown file: its front matter, and its body as written."""
    match = _FRONT.match(text.replace("\r\n", "\n"))
    if not match:
        raise HelpFormatError(f"{where}: no front matter between --- lines")
    fields: dict[str, str | list[str]] = {}
    listing: str | None = None
    for raw in match.group(1).split("\n"):
        if not raw.strip():
            continue
        item = re.match(r"^\s+-\s+(.+)$", raw)
        if item and listing:
            field = fields[listing]
            assert isinstance(field, list)
            field.append(item.group(1).strip())
            continue
        pair = re.match(r"^([a-z]+):\s*(.*)$", raw)
        if not pair:
            raise HelpFormatError(f"{where}: cannot read front matter line {raw!r}")
        key, value = pair.groups()
        if value == "":
            fields[key], listing = [], key
        elif value.startswith("["):
            fields[key] = [one.strip() for one in value.strip()[1:-1].split(",") if one.strip()]
            listing = None
        else:
            fields[key], listing = value.strip(), None

    def one(key: str) -> str | None:
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise HelpFormatError(f"{where}: {key} is one value, not a list")
        return value

    def many(key: str) -> tuple[str, ...]:
        value = fields.get(key, [])
        return (value,) if isinstance(value, str) else tuple(value)

    ident, title = one("id"), one("title")
    if not ident or not title:
        raise HelpFormatError(f"{where}: an article needs an id and a title")
    return Article(
        id=ident,
        title=title,
        screens=many("screens"),
        also=many("also"),
        requires=one("requires"),
        markdown=match.group(2).strip(),
    )


def _from_build(path: Path) -> list[Article]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Article(
            id=one["id"],
            title=one["title"],
            screens=tuple(one.get("screens", [])),
            also=tuple(one.get("also", [])),
            requires=one.get("requires"),
            markdown=one["markdown"],
        )
        for one in data["articles"]
    ]


def _from_source(folder: Path) -> list[Article]:
    return [parse(path.read_text(encoding="utf-8"), str(path)) for path in sorted(folder.glob("*.md"))]


@lru_cache(maxsize=16)
def _articles(built: Path, source: Path, locale: str) -> tuple[Article, ...]:
    shipped = built / f"{locale}.json"
    if shipped.is_file():
        return tuple(_from_build(shipped))
    if (source / locale).is_dir():
        return tuple(_from_source(source / locale))
    return ()


def locales(built: Path | None = None, source: Path = SOURCE) -> list[str]:
    """The languages there are articles in."""
    built = built or built_dir()
    found = {path.stem for path in built.glob("*.json")} if built.is_dir() else set()
    if not found and source.is_dir():
        found = {path.name for path in source.iterdir() if path.is_dir() and any(path.glob("*.md"))}
    return sorted(found)


def negotiate(asked: list[str], available: list[str]) -> str:
    """The best language for what was asked: exact, then the language alone, then English.

    The rule the client and the seeded words follow (`lib/i18n.ts`,
    `seed_words.resolve`): ``pt-PT`` reads ``pt-BR``.
    """
    for wanted in asked:
        for one in available:
            if one.lower() == wanted.lower():
                return one
    for wanted in asked:
        language = wanted.split("-")[0].lower()
        for one in available:
            if one.split("-")[0].lower() == language:
                return one
    return ENGLISH


def accepted(header: str | None) -> list[str]:
    """An Accept-Language header's languages, most wanted first."""
    if not header:
        return []
    ranked: list[tuple[float, int, str]] = []
    for at, part in enumerate(header.split(",")):
        tag, _, rest = part.strip().partition(";")
        weight = 1.0
        if rest.strip().startswith("q="):
            try:
                weight = float(rest.strip()[2:])
            except ValueError:
                weight = 0.0
        if tag and tag != "*" and weight > 0:
            ranked.append((-weight, at, tag))
    return [tag for _, _, tag in sorted(ranked)]


def articles(locale: str, *, built: Path | None = None, source: Path = SOURCE) -> list[Article]:
    """Every article in one language, in the order of their files."""
    return list(_articles(built or built_dir(), source, locale))
