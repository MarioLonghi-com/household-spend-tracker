"""Gaps: deadlines that do not count sleep, and expiry that does (design notes 8.6, 15.1 U7).

Both clocks are fakes the test moves. Suspending a machine stops
`CLOCK_MONOTONIC` while `CLOCK_REALTIME` is put right afterwards, so "the
laptop slept for an hour" is the wall clock jumping an hour ahead of the
monotonic one.
"""

from __future__ import annotations

import pytest

from updater import contract
from updater.clock import Deadline, GapClock, wall_age

START = 1_792_000_000.0
DRILL = 30 * 60


class Clocks:
    def __init__(self) -> None:
        self.real = START
        self.mono = 1000.0

    def run(self, seconds: float, tick: GapClock | None = None) -> None:
        """Time passes with the machine awake, ticking every 5 s."""
        while seconds > 0:
            step = min(5.0, seconds)
            self.real += step
            self.mono += step
            seconds -= step
            if tick:
                tick.tick()

    def sleep(self, seconds: float) -> None:
        """The machine is suspended: only the wall clock moves."""
        self.real += seconds


@pytest.fixture
def clocks():
    c = Clocks()
    return c, GapClock(realtime=lambda: c.real, monotonic=lambda: c.mono)


def test_an_hour_asleep_mid_drill_does_not_expire_the_30_minute_deadline(clocks):
    c, clock = clocks
    drill, health = Deadline(clock, DRILL), Deadline(clock, 120)
    c.run(10 * 60, clock)
    c.sleep(3600)
    c.run(5, clock)
    assert not drill.expired()
    assert drill.elapsed() == pytest.approx(10 * 60 + 5)
    assert drill.remaining() == pytest.approx(DRILL - 605)
    # The outcome records the hour asleep (5.3, 8.6).
    assert clock.asleep() == pytest.approx(3600)
    assert len(clock.gaps) == 1
    # The health deadline started at the same time and has truly run out.
    assert health.expired()
    # And the drill deadline still runs out when the drill really overruns.
    c.run(DRILL - 605, clock)
    assert drill.expired()


def test_a_request_11_wall_clock_minutes_old_across_a_gap_is_stale(clocks):
    c, clock = clocks
    stale_created = clock.now()
    # What a gap-excluding deadline would have said, to show expiry is not one.
    as_a_deadline = Deadline(clock, contract.REQUEST_MAX_AGE_SECONDS)
    c.run(120, clock)
    fresh_created = clock.now()
    c.sleep(9 * 60)
    c.run(5, clock)
    assert not as_a_deadline.expired() and as_a_deadline.elapsed() == pytest.approx(125)
    assert wall_age(stale_created, clock.now()) == pytest.approx(11 * 60 + 5)
    with pytest.raises(contract.Refusal) as e:
        contract.check_created_at(contract.iso(stale_created), clock.now())
    assert e.value.code == "stale"
    # Created two minutes later, the other is just over nine minutes old and still good.
    contract.check_created_at(contract.iso(fresh_created), clock.now())


def test_a_report_expires_by_the_wall_clock_whatever_the_machine_did(clocks):
    c, clock = clocks
    expires = contract.iso(clock.now() + 24 * 3600)
    report = {"from_version": "0.7.1", "to_version": "0.9.0", "expires_at": expires}
    raw = {"kind": "apply", "from_version": "0.7.1", "to_version": "0.9.0"}
    c.run(3600, clock)
    contract.check_report(raw, report, clock.now())
    c.sleep(23 * 3600)
    c.run(5, clock)
    with pytest.raises(contract.Refusal) as e:
        contract.check_report(raw, report, clock.now())
    assert e.value.code == "report_expired"


def test_drift_under_30_seconds_is_not_a_gap(clocks):
    c, clock = clocks
    deadline = Deadline(clock, 120)
    c.run(30, clock)
    c.sleep(29)
    assert clock.tick() == 0.0
    c.sleep(31)
    assert clock.tick() == pytest.approx(31)
    assert [round(g.seconds) for g in clock.gaps] == [31]
    # The 29 s were counted, the 31 s were not.
    assert deadline.elapsed() == pytest.approx(30 + 29)


def test_a_wall_clock_stepped_back_does_not_hand_a_deadline_extra_time(clocks):
    c, clock = clocks
    deadline = Deadline(clock, 120)
    c.run(60, clock)
    c.real -= 3600  # NTP corrects an hour-fast clock
    c.run(5, clock)
    assert deadline.elapsed() == pytest.approx(65)
    assert clock.asleep() == 0.0
    c.run(55, clock)
    assert deadline.expired()


def test_an_engine_restart_is_a_gap_the_clocks_cannot_see(clocks):
    c, clock = clocks
    drill = Deadline(clock, DRILL)
    c.run(120, clock)
    c.run(14 * 60)  # Docker Desktop gone for 14 minutes (S7), ticks or not
    clock.record_gap(14 * 60, "engine")
    assert drill.elapsed() == pytest.approx(120)
    assert [(g.reason, g.seconds) for g in clock.gaps] == [("engine", 14 * 60)]


def test_a_deadline_carried_through_the_journal_resumes_from_what_was_spent(clocks):
    c, clock = clocks
    drill = Deadline(clock, DRILL)
    c.run(400, clock)
    saved = drill.to_dict()
    assert saved == {"seconds": DRILL, "spent": pytest.approx(400)}
    c.sleep(2 * 3600)  # the updater was not running at all
    restarted = GapClock(realtime=lambda: c.real, monotonic=lambda: c.mono)
    again = Deadline.from_dict(restarted, saved)
    c.run(100, restarted)
    assert again.elapsed() == pytest.approx(500)
    assert not again.expired()


def test_health_failures_just_after_a_gap_are_retried_not_counted(clocks):
    c, clock = clocks
    c.run(10, clock)
    assert not clock.settling()
    c.sleep(600)
    c.run(5, clock)
    assert clock.settling()
    c.run(60, clock)
    assert not clock.settling()


def test_the_real_clocks_tick_without_a_gap():
    clock = GapClock()
    assert clock.tick() == 0.0
    assert Deadline(clock, 1).remaining() > 0
