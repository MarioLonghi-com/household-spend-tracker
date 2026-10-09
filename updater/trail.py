"""What the updater is doing, one line at a time, on its container's stdout (#278).

In normal operation the updater's log was empty: everything it did went into
`status.json` and `history/`, in a volume an owner of a container without a
shell does not read, and the only trace of a failure was a traceback. So
`docker compose logs updater` now answers "what was it doing": one INFO line
per request taken, per step started and ended (prepare, apply, rollback), per
handover step and its outcome, and per request finished. Each carries the
request id, the step, the sentence the updater wrote for the owner, and the
time since the request was taken.

**Never a secret.** What reaches a line is a sentence written for the owner's
screen, which holds no token and no recovery-code hash -- and `scrub` still
cuts any long run of hash-like characters out of it, because a line in a
container log is copied into issues.

Standard library only, like the rest of the package.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

LOGGER = logging.getLogger("spend-tracker-updater")

#: A recovery-code hash, whole (`contract.RECOVERY_HASH`'s format).
_SCRYPT = re.compile(r"scrypt\$\S*")
#: A digest, a container id or a token: 32 or more characters of hex, or 40
#: or more of a URL-safe alphabet with no `/` -- so a path, whose segments
#: are short, is left whole. Cut to its first eight, which names it without
#: carrying it.
_HASHLIKE = re.compile(r"\b[0-9a-f]{32,}\b|[A-Za-z0-9_-]{40,}")


def configure(stream: TextIO | None = None) -> logging.Handler:
    """Send the lines to `stream` (stdout), once. The entry point calls it."""
    for handler in LOGGER.handlers:
        if getattr(handler, "_trail", False):
            return handler
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s updater: %(message)s"))
    handler._trail = True  # type: ignore[attr-defined]
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    return handler


def scrub(text: object) -> str:
    """One line, with nothing in it that looks like a hash or a token."""
    one = _SCRYPT.sub("scrypt$…", " ".join(str(text).split()))
    return _HASHLIKE.sub(lambda m: m.group(0)[:8] + "…", one)


def line(
    request_id: str | None,
    step: str | None,
    what: str,
    sentence: str | None = None,
    *,
    elapsed: float | None = None,
) -> None:
    """`request <id> step <step> <what> after <n> s: <sentence>`."""
    parts = [f"request {request_id or '-'}"]
    if step:
        parts.append(f"step {step}")
    parts.append(scrub(what))
    if elapsed is not None:
        parts.append(f"after {max(elapsed, 0.0):.1f} s")
    said = " ".join(parts)
    if sentence:
        said += f": {scrub(sentence)}"
    LOGGER.info("%s", said)
