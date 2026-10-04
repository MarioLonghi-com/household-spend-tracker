# statements

Reading bank and credit-card statements into rows.

No database, no web framework, no configuration. Bytes in, transactions out.
It is a plain package inside this repo today, with no import that points back at
`app/` — which is what would let it move to its own repository as a straight
`git mv` if that ever earns its keep.

## Use it as a library

```python
from statements import sniff, parse

sniffed = sniff(raw)              # what is this file?
rows = parse(raw, sniffed.format) # what is in it?
```

`sniff` decides the shape and reports its reasoning — which column is the date,
which one holds money and which way it points, the decimal separator, how many
preamble rows to skip — in `Sniffed.format` and `Sniffed.warnings`, so a caller
can show its work before writing anything.

`parse` returns one `ParsedRow` per line, including the ones it could not read:
those carry a `problem` string rather than raising. One bad date costs one row,
not the file. `row.details` keeps every non-empty cell under the bank's own
column names, so a transaction can still be explained when the file is gone.

Amounts are `Decimal`. Rounding to minor units needs a currency, and a statement
does not reliably state one — that is the caller's job. When it does state one —
a currency column, or an OFX `CURDEF` — `row.currency` carries it as an
upper-case ISO code, for the caller to hold against the account it is filing
into. It is `None` when the file does not say.

The only exception raised on purpose is `UnreadableStatement`: the file itself
cannot be used. Catch that one class and you have caught every deliberate
refusal. That includes a file far larger than any statement: every reader has a
ceiling, because the file is somebody else's and may have been built to be slow.

## Use it from a terminal

```
python -m statements statement.csv
python -m statements statement.pdf --rows 40
python -m statements export.ofx --json | jq '.rows[] | select(.problem)'
```

Which is the fastest way to answer "did the bank change their export?" — the
verdict, the warnings, and the rows, without an app around it.

## What it reads

| | |
|---|---|
| Delimited text | `,` `;` tab `\|`, encoding sniffed strictly (a mis-guess raises rather than filling with `U+FFFD`) |
| Separate debit/credit columns | and single signed-amount columns, either way round |
| OFX | 1.x SGML with unclosed tags and 2.x XML, `FITID` carried through, `CORRECTFITID` corrections applied, `HOLD` marked as not-yet-happened |
| `.xls` | the first sheet only, unpacked to rows via xlrd; refused past a million cells |
| PDF | word positions measured against the header row; a named layout may claim a document it recognises. Refused past 50 pages, and past the unpacking and interpreting limits in `pdf_limits.py` -- pdfminer has none of its own |

`camt.053` and MT940 are understood and deliberately not implemented: no real
sample file exists here to test against, and a format implemented against the
spec alone is a format that has never been read.

## The rule that keeps it honest

A named PDF layout (`pdf_layouts.py`) must reconcile against a figure the
document itself states — a closing balance, a bill total. If the rows do not add
up to it, the layout raises `LayoutMismatch` instead of falling back to the
generic reader. Handing over numbers that are known to be wrong is worse than
refusing, and this check has already caught one real extraction error.

## Tests

`tests/test_statement_formats.py`, against synthetic fixtures in
`tests/statement_files/` modelled on real exports from several banks. They are
synthetic on purpose — real statements are not committable — which means a real
file should be run through the CLI whenever one arrives.
