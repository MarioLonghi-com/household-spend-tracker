"""`desktop/scripts/upload-mas.sh` keeps asking altool until Apple has a verdict.

0.16.0's upload succeeded and the job still failed: altool answered non-zero
once, seconds after the upload, and `set -e` ended the script on the failing
command substitution before any retry. A fake `xcrun` stands in for altool
here, counting its calls in a file, so the test asserts how many times the
script asked, not only what it exited with.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "desktop" / "scripts" / "upload-mas.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="a /bin/sh script")


ALTOOL_26_HELP = 'case "$*" in *--help*) echo "altool --build-status {--delivery-id <id>}"; exit 0;; esac\n'


def _fake_altool(tmp_path: pathlib.Path, script: str, help_text: str = ALTOOL_26_HELP) -> dict[str, str]:
    """A `xcrun` on PATH that runs `script` with the call count in $CALLS.

    `help_text` answers `altool --help` before anything is counted: by default
    an altool that has `--build-status`, as Xcode 26's does.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    counter.write_text("0")
    xcrun = bin_dir / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n" + help_text + f"n=$(($(cat {counter}) + 1)); echo $n > {counter}; CALLS=$n\n" + script + "\n"
    )
    xcrun.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ASC_KEY_ID": "KEY",
        "ASC_ISSUER_ID": "ISSUER",
        "ASC_WAIT_SECONDS": "0",
        "ASC_WAIT_TRIES": "5",
    }


def _calls(tmp_path: pathlib.Path) -> int:
    return int((tmp_path / "calls").read_text())


def test_a_failing_status_call_is_asked_again_until_valid(tmp_path: pathlib.Path) -> None:
    env = _fake_altool(
        tmp_path,
        'if [ "$CALLS" -le 2 ]; then echo "*** Error: delivery not found" >&2; exit 1; fi\n'
        'echo "= IMPORT-STATUS: VALID"',
    )
    done = subprocess.run(["sh", str(SCRIPT), "--status", "D1"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert _calls(tmp_path) == 3
    assert "delivery D1: VALID" in done.stdout


def test_a_refusal_stops_at_once(tmp_path: pathlib.Path) -> None:
    env = _fake_altool(tmp_path, 'echo "PROCESSING-ERRORS: bad icon"; echo "= IMPORT-STATUS: INVALID"')
    done = subprocess.run(["sh", str(SCRIPT), "--status", "D2"], env=env, capture_output=True, text=True)
    assert done.returncode == 1
    assert _calls(tmp_path) == 1
    assert "Apple refused the build" in done.stderr


def test_running_out_of_patience_says_so_with_the_last_answer(tmp_path: pathlib.Path) -> None:
    env = _fake_altool(tmp_path, 'echo "= IMPORT-STATUS: PROCESSING"')
    done = subprocess.run(["sh", str(SCRIPT), "--status", "D3"], env=env, capture_output=True, text=True)
    assert done.returncode == 1
    assert _calls(tmp_path) == 5
    assert "still being processed" in done.stderr
    assert "IMPORT-STATUS: PROCESSING" in done.stderr


def test_altool_26s_processing_state_is_read(tmp_path: pathlib.Path) -> None:
    env = _fake_altool(
        tmp_path,
        'if [ "$CALLS" -le 1 ]; then echo "DeliveryID \'D4\' Processing State: PROCESSING"; exit 0; fi\n'
        'echo "    processingState: VALID"',
    )
    done = subprocess.run(["sh", str(SCRIPT), "--status", "D4"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert _calls(tmp_path) == 2
    assert "delivery D4: VALID" in done.stdout
    # The first answer is in the log whole, so its format is never a guess again.
    assert "Processing State: PROCESSING" in done.stdout


def test_altool_26s_refusal_stops_at_once(tmp_path: pathlib.Path) -> None:
    env = _fake_altool(tmp_path, 'echo "DeliveryID \'D5\' Processing State: FAILED"')
    done = subprocess.run(["sh", str(SCRIPT), "--status", "D5"], env=env, capture_output=True, text=True)
    assert done.returncode == 1
    assert _calls(tmp_path) == 1
    assert "Apple refused the build" in done.stderr


def test_an_altool_without_build_status_stops_before_asking(tmp_path: pathlib.Path) -> None:
    # Xcode 16.4, the macos-15 runner's default: its usage text has no
    # --build-status, and 0.16.0 and 0.16.1 each waited 30 minutes on it.
    env = _fake_altool(
        tmp_path,
        'echo "usage: altool --upload-app -f <file> -t <platform>"',
        help_text='case "$*" in *--help*) echo "altool --upload-app -f <file>"; exit 0;; '
        '*--find*) echo /Applications/Xcode_16.4.app/altool; exit 0;; esac\n',
    )
    package = tmp_path / "app.pkg"
    package.write_bytes(b"")
    done = subprocess.run(["sh", str(SCRIPT), str(package)], env=env, capture_output=True, text=True)
    assert done.returncode == 2
    assert _calls(tmp_path) == 0
    assert "has no --build-status" in done.stderr
