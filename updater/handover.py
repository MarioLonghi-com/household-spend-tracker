"""The handover to a successor updater, as the orchestration sees it (6.6, C1).

The orchestration calls a handover at two points of an apply:

- **step 2a, updater first (C1):** after preflight and before the app is
  stopped, when the prepared updater is newer than this one and its protocol
  window includes the running app's protocol (`goes_first`). If the handover
  completes, the successor owns the journal and carries on from step 3; if it
  does not, this updater keeps the request and carries on from step 3 itself,
  and the handover is tried again at step 10;
- **step 10:** after a successful apply, unless 2a already handed over or this
  updater is already newer than the target release's (never downgrade).

The mechanics, H1-H7 -- creating the successor, its self-check, the standby
window, taking back over -- are #162's. Until it lands, `NotAvailable` is the
handover: it reports "not available", which is 4.2's failure branch of 2a
exactly (nothing stopped, the old updater carries on), and at step 10 leaves
the app update standing with the updater behind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from updater import contract
from updater.detect import PROTOCOL_LABEL
from updater.journal import Owner

#: The label on an updater image naming the protocols it accepts (C4): `1-N`.
#: Built from the app's label, which is built from the ghcr owner (R28).
PROTOCOLS_LABEL = PROTOCOL_LABEL + "s"


@dataclass(frozen=True)
class Outcome:
    done: bool
    sentence: str
    #: The updater that owns the request afterwards; None when unchanged.
    owner: Owner | None = None


class Handover(Protocol):
    def first(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        """Step 2a: hand over before the app stops. `done` means the successor owns the request."""
        ...

    def after(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        """Step 10: hand over after the app update settled."""
        ...


class NotAvailable:
    """No handover yet (#162): every attempt reports that, and nothing is touched."""

    SENTENCE = "Handing over to a newer updater is not available in this updater yet."

    def first(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        return Outcome(False, self.SENTENCE)

    def after(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        return Outcome(False, self.SENTENCE)


def protocol_window(labels: object) -> tuple[int, int] | None:
    """An updater image's `…updater-protocols` window, or None when it carries none."""
    value = labels.get(PROTOCOLS_LABEL) if isinstance(labels, dict) else None
    m = re.fullmatch(r"([0-9]{1,4})-([0-9]{1,4})", value) if isinstance(value, str) else None
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return (lo, hi) if lo <= hi else None


def goes_first(me: Owner, successor: Owner, window: tuple[int, int] | None, app_protocol: int) -> bool:
    """C1: the successor is newer than this updater and speaks the running app's protocol."""
    if window is None:
        return False
    return successor.newer_than(me) and window[0] <= app_protocol <= window[1]


def stays_newer(me: Owner, target_version: str) -> bool:
    """Never downgrade an updater (6.6): this one is already newer than the target release's."""
    try:
        return contract.parse_version(me.version) > contract.parse_version(target_version)
    except ValueError:
        return False
