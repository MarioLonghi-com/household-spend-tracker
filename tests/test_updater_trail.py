"""The updater's lines on stdout (#278): their shape, and what never reaches them.

What each request, step and handover says is pinned beside the code that
says it (`test_updater_apply.py`, `test_updater_handover.py`); this is the
line itself.
"""

from __future__ import annotations

import io
import logging

import pytest

from updater import trail

RECOVERY_HASH = "scrypt$ln=15,r=8,p=1$" + "Q" * 22 + "$" + "Zm9v" * 10 + "abc"
DIGESTS = ("sha256:" + "3f2a" * 16, "sha256:" + "77b0" * 16)
TOKENS = ("x" * 43, "a_B-" * 11)


@pytest.fixture()
def stream():
    out = io.StringIO()
    handler = trail.configure(out)
    try:
        yield out
    finally:
        trail.LOGGER.removeHandler(handler)


def test_a_line_names_the_request_the_step_the_time_and_the_sentence(stream):
    trail.line("3c1e7a52-0000-4000-8000-000000000001", "5", "apply started", "Backing up.", elapsed=12.34)
    trail.line("3c1e7a52-0000-4000-8000-000000000002", None, "prepare taken", "To 0.9.1.")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(
        "INFO updater: request 3c1e7a52-0000-4000-8000-000000000001 step 5 apply started "
        "after 12.3 s: Backing up."
    )
    assert lines[1].endswith(
        "INFO updater: request 3c1e7a52-0000-4000-8000-000000000002 prepare taken: To 0.9.1."
    )


def test_configured_twice_it_still_says_each_line_once(stream):
    trail.configure(stream)
    trail.line("r1", "H1", "handover")
    assert stream.getvalue().count("request r1 step H1 handover") == 1


@pytest.mark.parametrize("secret", [RECOVERY_HASH, *DIGESTS, *TOKENS])
def test_no_hash_digest_or_token_reaches_a_line_whole(stream, secret):
    trail.line("r2", "1", "apply", f"something mentioned {secret} in passing\nand broke the line")
    said = stream.getvalue()
    assert secret not in said
    assert said.count("\n") == 1, "one line, whatever the sentence held"
    assert "in passing and broke the line" in said


@pytest.mark.parametrize(
    "kept",
    [
        "/var/lib/spend-tracker/backups/20261008-211120",
        "3c1e7a52-0000-4000-8000-000000000001",
    ],
)
def test_a_path_or_an_id_is_left_whole(kept):
    assert trail.scrub(f"Kept {kept}.") == f"Kept {kept}."


def test_unconfigured_nothing_is_printed_but_the_records_are_there(caplog, capsys):
    with caplog.at_level(logging.INFO, logger="spend-tracker-updater"):
        trail.line("r3", "R1", "apply started", "Removing the new version.")
    assert caplog.messages == ["request r3 step R1 apply started: Removing the new version."]
    assert capsys.readouterr().out == ""
