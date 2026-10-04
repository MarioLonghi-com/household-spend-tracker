"""The boundary around `statements`, and the command line over it.

The library is worth having as a library only for as long as it stays one. The
first test here is the whole point: it fails the moment somebody reaches back
into `app` from inside it, which is exactly how a clean seam quietly stops being
one.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import statements
from statements import cli

LIBRARY = Path(statements.__file__).resolve().parent
FIXTURES = Path(__file__).resolve().parent / "statement_files"


def _imported_names(source: str) -> set[str]:
    """Every module named by an import in this file, absolute and relative."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which can only be a sibling here.
            names.add(("." * node.level) + (node.module or ""))
    return names


@pytest.mark.parametrize("path", sorted(LIBRARY.glob("*.py")), ids=lambda p: p.name)
def test_the_library_never_imports_the_app(path: Path):
    """No `app` import, and no relative import that could climb out of it."""
    for name in _imported_names(path.read_text()):
        assert not name.startswith("app"), f"{path.name} imports {name}"
        assert not name.startswith(".."), f"{path.name} imports {name} from outside the library"


def test_the_library_imports_with_the_app_absent(monkeypatch):
    """Importable in a process that has never heard of the app.

    A plain `import statements` in a suite that has already imported `app`
    proves nothing -- the module is in `sys.modules` either way.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import statements; assert 'app' not in __import__('sys').modules"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr


def test_every_deliberate_refusal_is_one_catchable_class():
    """One `except` covers the library's whole refusal surface."""
    for name in ("UnreadablePdf", "UnreadableSpreadsheet", "LayoutMismatch"):
        assert issubclass(getattr(statements, name), statements.UnreadableStatement)


def _run(capsys, *args: str) -> tuple[int, str, str]:
    """Call the CLI in this process, so coverage can see it."""
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "statements", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )


def test_the_module_runs_as_a_command(capsys):
    """`python -m statements` really is an entry point, not just a function."""
    result = _cli(str(FIXTURES / "bank_sgml.ofx"))
    assert result.returncode == 0, result.stderr
    assert "2 transactions, 0 lines refused" in result.stdout


def test_the_command_line_reports_what_it_read(capsys):
    code, out, _ = _run(capsys, str(FIXTURES / "bank_sgml.ofx"))
    assert code == 0
    assert "2 transactions, 0 lines refused" in out
    assert "net 137.66" in out
    # The verdict, not just the rows: what it decided this file was.
    assert "ofx" in out
    assert "2025-09-08" in out and "150.00" in out


def test_a_table_file_reports_the_columns_it_chose(capsys):
    """The whole point of the CLI: seeing what the sniffer decided."""
    code, out, _ = _run(capsys, str(FIXTURES / "signed_with_state.csv"))
    assert code == 0
    assert "date_column" in out and "amount_column" in out
    assert "delimiter" in out


def test_several_files_are_reported_one_after_another(capsys):
    code, out, _ = _run(
        capsys, str(FIXTURES / "bank_sgml.ofx"), str(FIXTURES / "signed_with_state.csv")
    )
    assert code == 0
    assert "bank_sgml.ofx" in out and "signed_with_state.csv" in out


def test_the_command_line_speaks_json(capsys):
    code, out, _ = _run(capsys, str(FIXTURES / "bank_sgml.ofx"), "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["format"]["kind"] == "ofx"
    assert [row["amount"] for row in payload["rows"]] == ["150.00", "-12.34"]
    assert payload["rows"][1]["payee"] == "A SHOP"
    # The provenance the app stores is present here too.
    assert len(payload["sha256"]) == 64


def test_the_command_line_refuses_a_file_it_cannot_read(capsys, tmp_path: Path):
    unreadable = tmp_path / "scan.pdf"
    unreadable.write_bytes(b"%PDF-1.4\n" + b"0" * 200)
    code, out, err = _run(capsys, str(unreadable))
    assert code == 1
    assert out == ""
    assert "scan.pdf:" in err


def test_the_command_line_says_which_file_is_missing(capsys, tmp_path: Path):
    code, _, err = _run(capsys, str(tmp_path / "nothing.csv"))
    assert code == 1
    assert "no such file" in err


def test_one_bad_file_does_not_stop_the_good_one(capsys, tmp_path: Path):
    """Same rule as a bad row: it costs you that file, not the run."""
    code, out, err = _run(
        capsys, str(tmp_path / "nothing.csv"), str(FIXTURES / "bank_sgml.ofx")
    )
    assert code == 1                       # the run reports failure...
    assert "no such file" in err
    assert "2 transactions" in out         # ...and still read the other one


def test_a_row_limit_keeps_the_rest_countable(capsys):
    code, out, _ = _run(capsys, str(FIXTURES / "bank_sgml.ofx"), "--rows", "1")
    assert code == 0
    assert "… 1 more (use --rows)" in out
