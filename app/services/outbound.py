"""The two rules every outbound GET in this app keeps (#219, #225).

**Nothing leaves the instance unless the owner presses a button, and no
request says anything about the instance.** That is the promise, and it is
the one that stays as the list of requests grows. This app's own requests:
the YNAB client (``one_time_import/ynab_api``), when an owner runs the
one-time import, and the owner's "check for updates" button
(``platform.check_upstream``), which reads the repository's published
releases. Both talk to one fixed host, and neither has any business following
a redirect or waiting forever.

The self-updater, which runs in its own container, makes requests of its own
-- to ghcr.io, api.github.com and Sigstore -- and only while it handles a
request an owner started from the Application screen: *Prepare*, *Update* or
*Update the updater*. Nothing on a timer, and nothing that names this
instance, there either.

**No redirects.** urllib's default handler copies every header onto the next
request -- ``Authorization`` included -- whatever host or scheme ``Location``
names, and will step down from https to http to do it. Neither API redirects,
so anything that answers 3xx is not the host that was asked, and the request
stops there as an ``HTTPError`` carrying the 3xx status.

**A deadline on the whole read.** ``timeout=`` on ``urlopen`` bounds each
``recv``, not the answer: a server dripping one byte a minute holds the worker
indefinitely. :func:`read_within` stops at a wall-clock deadline and at a size
ceiling, whichever comes first.
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request

#: Read at most this much per call, so the deadline is looked at between reads.
CHUNK_BYTES = 1 << 20


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so no header follows one anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def opener(context: ssl.SSLContext) -> urllib.request.OpenerDirector:
    """An opener that verifies TLS with ``context`` and follows no redirect."""
    return urllib.request.build_opener(NoRedirects, urllib.request.HTTPSHandler(context=context))


class TooLarge(Exception):
    """The answer was longer than the ceiling it was read against."""


class TooSlow(TimeoutError):
    """The answer did not finish arriving before the deadline."""


def read_within(answer, *, limit: int, seconds: float) -> bytes:
    """The whole body of ``answer``, or :class:`TooLarge` / :class:`TooSlow`.

    Reads with ``read1`` where the response has it, which returns what has
    arrived rather than waiting for a full chunk -- so the deadline is checked
    after every ``recv``, and the worst case is the deadline plus one socket
    timeout.
    """
    deadline = time.monotonic() + seconds
    read = getattr(answer, "read1", None) or answer.read
    parts: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TooSlow("the answer took longer than it is allowed")
        chunk = read(min(CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
        if total > limit:
            raise TooLarge("the answer was larger than it is allowed")
    return b"".join(parts)
