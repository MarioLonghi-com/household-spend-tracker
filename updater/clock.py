"""Deadlines that do not count the time a laptop slept (design notes, 8.6).

A laptop sleeps in the middle of a drill; Docker Desktop pauses its VM; a
`podman machine` stops. Each is **a gap**, and a 30-minute drill deadline that
counted one would come back to a timed-out drill and a needless rollback.

**Detecting a gap.** The updater ticks every 5 s and compares
`CLOCK_REALTIME` with `CLOCK_MONOTONIC`. While the machine or the VM is
suspended, the monotonic clock stands still and the wall clock is put right
afterwards, so their difference jumps. A difference that grows by more than
30 s between two ticks is a gap. A difference that *shrinks* by as much is the
wall clock being stepped back (NTP correcting it); that is not sleep, but it
is excluded the same way, so a deadline is not handed an hour it never waited.

**Deadlines** are measured in wall-clock time minus the gaps, which is
monotonic time that can be written to the journal and read back after a
restart. **Request and report expiry are the exception**: they count plain
wall-clock time, so an apply prepared yesterday is not run today, however
long the machine slept in between -- see `wall_age`.

Both clocks are injected, so a test can move one against the other.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

#: How often the updater ticks, and the jump between ticks that counts as a gap.
TICK_SECONDS = 5.0
GAP_THRESHOLD_SECONDS = 30.0

#: A health failure this soon after a gap is retried rather than counted:
#: the network inside a resumed VM takes a moment (8.6).
SETTLE_AFTER_GAP_SECONDS = 60.0


def _realtime() -> float:
    return time.clock_gettime(time.CLOCK_REALTIME)


def _monotonic() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


@dataclass(frozen=True)
class Gap:
    """One detected gap: when it was noticed (wall clock) and how long it was."""

    noticed_at: float
    seconds: float
    reason: str = "asleep"


class GapClock:
    """A wall clock that knows how much of its time the machine was away."""

    def __init__(
        self,
        realtime: Callable[[], float] = _realtime,
        monotonic: Callable[[], float] = _monotonic,
        threshold: float = GAP_THRESHOLD_SECONDS,
    ) -> None:
        self._realtime = realtime
        self._monotonic = monotonic
        self._threshold = threshold
        self._offset = realtime() - monotonic()
        #: Seconds of wall-clock time that are not to be counted: gaps, plus
        #: (negatively) any backward step of the wall clock.
        self._excluded = 0.0
        self.gaps: list[Gap] = []

    def now(self) -> float:
        """Wall-clock seconds since the epoch. What a timestamp in a file uses."""
        return self._realtime()

    def tick(self) -> float:
        """Compare the clocks once. Returns the length of a gap found now, else 0."""
        offset = self._realtime() - self._monotonic()
        moved = offset - self._offset
        self._offset = offset
        if abs(moved) <= self._threshold:
            return 0.0
        self._excluded += moved
        if moved > 0:
            self.gaps.append(Gap(noticed_at=self.now(), seconds=moved))
            return moved
        return 0.0

    def record_gap(self, seconds: float, reason: str) -> None:
        """A gap the clocks cannot see: the engine went away, or the updater restarted."""
        if seconds > 0:
            self._excluded += seconds
            self.gaps.append(Gap(noticed_at=self.now(), seconds=seconds, reason=reason))

    @property
    def excluded(self) -> float:
        return self._excluded

    def asleep(self) -> float:
        """Every gap so far, in seconds. What the history records (5.3)."""
        return sum(g.seconds for g in self.gaps)

    def settling(self) -> bool:
        """Whether the last gap was noticed less than a minute ago."""
        self.tick()
        if not self.gaps:
            return False
        return self.now() - self.gaps[-1].noticed_at < SETTLE_AFTER_GAP_SECONDS


class Deadline:
    """A budget of seconds that only counts time the machine was actually running."""

    def __init__(self, clock: GapClock, seconds: float, already_spent: float = 0.0) -> None:
        self.clock = clock
        self.seconds = seconds
        self._spent_before = already_spent
        clock.tick()
        self._start = clock.now()
        self._excluded_at_start = clock.excluded

    def elapsed(self) -> float:
        self.clock.tick()
        ran = (self.clock.now() - self._start) - (self.clock.excluded - self._excluded_at_start)
        return self._spent_before + max(ran, 0.0)

    def remaining(self) -> float:
        return max(self.seconds - self.elapsed(), 0.0)

    def expired(self) -> bool:
        return self.elapsed() >= self.seconds

    def to_dict(self) -> dict:
        """For the journal: what was spent, so a restarted updater carries on from it.

        The time the updater was not running at all is not counted: a fresh
        start of the updater is an engine restart, and that is a gap (8.6).
        """
        return {"seconds": self.seconds, "spent": round(self.elapsed(), 3)}

    @classmethod
    def from_dict(cls, clock: GapClock, data: dict) -> Deadline:
        return cls(clock, float(data["seconds"]), already_spent=float(data.get("spent", 0.0)))


def wall_age(created_at: float, now: float) -> float:
    """Plain wall-clock age, gaps and all. For request and report expiry only."""
    return now - created_at
