# The catalogs

`messages.po` per locale, written by `npm run extract` from the source and
never edited by hand except for `msgstr` and its `#, fuzzy` flag. Only English
is served; the others hold drafts until #58. `GLOSSARY.md` is the term base:
one agreed word per concept in each language.

## Translator notes (#228)

A translator sees only the English. "Balance", "Split", "Clear", "Expected" or
"Match" each mean more than one thing, and a button reads differently from a
heading. So the catalogs carry the disambiguation themselves, through Lingui's
own fields, and it reaches every language's file and every translation tool.

- **`comment`** -- one line of English for the translator, written at the
  source:

  ```tsx
  t({ message: "Balance", comment: "Column heading on the Accounts screen: the amount an account holds" })
  <Trans comment="Button on the Register screen: verb, divide one transaction into parts">Split</Trans>
  ```

  `npm run extract` writes it into **every** locale's `.po` as a `#.` line.

- **`context`** (`msgctxt`) -- only when the same English needs *different
  translations* in different places: "Transfer" the noun and the verb, "New"
  for an account and for a category (Swedish *nytt* and *ny*). It makes them
  separate messages. A context is not a note; give it a `comment` as well.

**Which messages.** Where the English alone is ambiguous:

- single words and short labels -- every message of one or two words needs a
  note, and `catalogs.test.ts` fails on one without (the few that never need
  one, like "OK", are on its reviewed `SELF_EXPLANATORY` list);
- words with more than one meaning in finance: Balance, Split, Clear, Cleared,
  Expected, Match, Settle, Reconcile, Statement, Transfer;
- whether it is a button, a heading, a column, a menu item or a state;
- what a placeholder will hold, when its name does not say;
- a length limit, where a narrow screen has one.

**How to write one.** One line, English, under 120 characters (the test holds
it to that), saying what the text *is*, not how to translate it:
*"Column heading on the Payees screen"*, *"State of the authenticator: it was
removed and has to be set up again"*. A domain term points at `GLOSSARY.md`
("See GLOSSARY.md") rather than repeating its row.

Not every message needs one: a whole sentence usually says what it is.

## The tooling (#270)

- **micromatch is replaced.** `@lingui/cli` depends on micromatch, which
  depends on `braces`, and no release of braces fixes GHSA-vfj7-8cjw-p6xm.
  Lingui only calls micromatch's `capture` and `any`, so `client/vendor/micromatch`
  is those two on picomatch, put in place by `overrides` in
  `client/package.json`. `src/lib/micromatch-shim.test.ts` holds it to what
  micromatch answered. Drop both once a Lingui release stops needing braces.
- **`npm ci` warns `EBADENGINE` for `pseudolocale` on Node 22.** Lingui 6.8
  requires pseudolocale 3.1, which declares Node 24 or later. Lingui only
  calls its accenting function, which runs on Node 22: CI extracts on Node 22
  and fails if the en-XA catalog differs from the committed one. So the
  warning is harmless, and is left alone rather than pinned away to an older
  pseudolocale. Node 24 or 26 silences it.
