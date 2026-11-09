"""What the register's search box means, read into terms (#367).

The box used to be one substring. It is a small query language now, parsed
here and turned into SQL by ``transactions.filtered`` -- on the server, so the
register and the agent's reads share one meaning of ``search``:

* **Words** are each a term, and every term must match (AND). Each may match a
  different field: ``lidl birthday`` finds the Lidl row whose memo says
  birthday. A row that matched the old phrase reading still matches, since it
  holds every word of the phrase, so a search somebody already relied on finds
  what it found before, and possibly more.
* ``OR`` -- in capitals, as a word of its own -- or ``|`` between two terms
  means either. It binds tighter than the gaps between words, so
  ``coffee lidl OR aldi`` is coffee and (lidl or aldi). ``AND`` in capitals is
  accepted and changes nothing.
* **Quotes** keep a phrase together as one term: ``"100% cotton"``. Inside
  every form ``%`` and ``_`` are the characters themselves (#239).
* A leading ``-`` excludes: ``coffee -decaf``, ``-"standing order"``.

Nothing typed is refused. What would be malformed has one defined reading
instead, because a search box that answers a typo with an error is a box
people stop trusting:

* an ``OR``, ``AND`` or ``|`` without a term on both sides is the word itself
  (``OR`` alone looks for "OR");
* an unclosed quote runs to the end of the text;
* a lone ``-`` is the text "-", and ``""`` is no term at all.

The language is the parser's alone; it imports nothing from the app, so it
can be read -- and tested -- without a database.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    """One thing a row must, or must not, mention."""

    text: str
    excluded: bool = False


#: The query as read: every group must match, and a group matches when any of
#: its terms does. ``a b OR c -d`` is ``[[a], [b, c], [-d]]``.
Query = list[list[Term]]

_OR, _AND = "OR", "AND"

#: The language in one sentence, for the agent manifest -- written here, beside
#: the parser, so the two cannot drift apart.
SYNTAX = (
    "search: every word must match, each in the payee, the memo or the bank's own text; "
    'OR or | between two terms is either; "quotes" keep a phrase; -word excludes; '
    "% and _ are literal."
)


@dataclass(frozen=True)
class _Token:
    """A term, or an operator as typed (``word`` is what it reads as when it
    turns out to be a word after all)."""

    term: Term | None = None
    operator: str | None = None
    word: str = ""


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    at = 0
    end = len(text)
    while at < end:
        char = text[at]
        if char.isspace():
            at += 1
            continue
        if char == "|":
            tokens.append(_Token(operator=_OR, word="|"))
            at += 1
            continue
        excluded = False
        if char == "-" and at + 1 < end and not text[at + 1].isspace() and text[at + 1] != "|":
            excluded = True
            at += 1
        if text[at] == '"':
            close = text.find('"', at + 1)
            if close == -1:
                close = end
            phrase = text[at + 1 : close]
            at = close + 1
            if phrase.strip():
                tokens.append(_Token(term=Term(phrase, excluded)))
            continue
        stop = at
        while stop < end and not text[stop].isspace() and text[stop] not in '|"':
            stop += 1
        word = text[at:stop]
        at = stop
        if not excluded and word in (_OR, _AND):
            tokens.append(_Token(operator=word, word=word))
            continue
        tokens.append(_Token(term=Term(word, excluded)))
    return tokens


def parse(text: str | None) -> Query:
    """The search box's text as groups of terms. Empty means no filter."""
    if not text or not text.strip():
        return []
    tokens = _tokens(text)

    def is_term(index: int) -> bool:
        return 0 <= index < len(tokens) and tokens[index].term is not None

    # An operator is one only between two terms; otherwise it is a word.
    terms: list[tuple[Term, bool]] = []  # (term, joined to the one before by OR)
    joined = False
    for index, token in enumerate(tokens):
        if token.operator is not None:
            between = bool(terms) and is_term(index + 1)
            if between:
                joined = joined or token.operator == _OR
                continue
            token = _Token(term=Term(token.word))
        terms.append((token.term, joined))  # type: ignore[arg-type]
        joined = False

    groups: Query = []
    for term, with_previous in terms:
        if with_previous and groups:
            groups[-1].append(term)
        else:
            groups.append([term])
    return groups
