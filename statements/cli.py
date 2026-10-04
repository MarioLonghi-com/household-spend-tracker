"""Reading a statement from a terminal, with no app around it.

The point of this is to make a format question answerable in one command. When
a bank changes its export and rows start coming out wrong, you want to see what
the sniffer decided and what it made of each line — without a database, a
session, or an account to import into:

    python -m statements statement.csv
    python -m statements statement.pdf --rows 40
    python -m statements *.ofx --json | jq '.rows[] | select(.problem)'

Nothing here writes anything.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from decimal import Decimal
from pathlib import Path

from .errors import UnreadableStatement
from .parsing import file_digest, parse
from .sniffing import sniff


def _column_widths(rows: list[list[str]]) -> list[int]:
    widths = [0] * max((len(row) for row in rows), default=0)
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return widths


def _table(header: list[str], body: list[list[str]], *, right: set[int] = frozenset()) -> str:
    widths = _column_widths([header, *body])
    def line(cells: list[str]) -> str:
        padded = [
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()
    rule = "  ".join("-" * width for width in widths)
    return "\n".join([line(header), rule, *(line(row) for row in body)])


def _report(path: Path, *, limit: int) -> str:
    raw = path.read_bytes()
    sniffed = sniff(raw)
    rows = parse(raw, sniffed.format)
    fmt = sniffed.format

    out = [f"{path.name}  ({len(raw):,} bytes, sha256 {file_digest(raw)[:12]}…)", ""]
    told = [(key, value) for key, value in fmt.describe().items() if value not in (None, "", False)]
    out.append(_table(["read as", ""], [[key, str(value)] for key, value in told]))
    if sniffed.warnings:
        out += ["", "warnings:"] + [f"  - {text}" for text in sniffed.warnings]

    readable = [row for row in rows if row.problem is None]
    refused = [row for row in rows if row.problem is not None]
    total = sum((row.amount or Decimal(0)) for row in readable)
    out += [
        "",
        f"{len(readable)} transactions, {len(refused)} lines refused, "
        f"net {total}",
        "",
    ]

    shown = rows[:limit]
    out.append(
        _table(
            ["line", "date", "amount", "payee", "memo / problem"],
            [
                [
                    str(row.line_no),
                    row.when.isoformat() if row.when else "",
                    f"{row.amount}" if row.amount is not None else "",
                    (row.payee or "")[:40],
                    (row.problem or row.memo or "")[:50],
                ]
                for row in shown
            ],
            right={0, 2},
        )
    )
    if len(rows) > len(shown):
        out.append(f"… {len(rows) - len(shown)} more (use --rows)")
    return "\n".join(out)


def _as_json(path: Path) -> dict:
    raw = path.read_bytes()
    sniffed = sniff(raw)
    rows = parse(raw, sniffed.format)
    return {
        "file": str(path),
        "sha256": file_digest(raw),
        "format": sniffed.format.describe(),
        "warnings": sniffed.warnings,
        "rows": [
            {
                **dataclasses.asdict(row),
                "when": row.when.isoformat() if row.when else None,
                "amount": str(row.amount) if row.amount is not None else None,
            }
            for row in rows
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m statements",
        description="Read a bank or credit-card statement and show what is in it.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="statement files to read")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--rows", type=int, default=20, metavar="N", help="how many lines to print (default 20)"
    )
    args = parser.parse_args(argv)

    failed = False
    for index, path in enumerate(args.files):
        try:
            if args.json:
                print(json.dumps(_as_json(path), indent=2, ensure_ascii=False))
            else:
                if index:
                    print()
                print(_report(path, limit=args.rows))
        except FileNotFoundError:
            print(f"{path}: no such file", file=sys.stderr)
            failed = True
        except UnreadableStatement as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0
