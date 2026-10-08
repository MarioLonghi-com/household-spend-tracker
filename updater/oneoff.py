"""Running a one-off container and reading what became of it (4.1, 5.6).

The restricted client has no `wait` and no `attach`: a one-off is created,
started, and then **inspected until it has exited**, against a deadline that
does not count a gap (8.6). Its exit code comes from that inspection, its
output from `logs` -- allowed for the updater's own one-offs only -- and then
it is removed (8.7).

A one-off is named after the request it serves and its role, so the same one
is found again after a crash: `<app>-<role>-<first 8 of the request id>`. A
leftover of that name is removed before a new one is created, except the
drill, which is waited for (5.6) and never started twice.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from updater import engine as eng
from updater import survey
from updater.clock import Deadline, GapClock

POLL_SECONDS = 1.0


class TimedOut(Exception):
    """A one-off ran past its deadline. It has been stopped and removed."""


@dataclass(frozen=True)
class Result:
    exit_code: int
    output: str


def name_for(app_name: str, role: str, request_id: str) -> str:
    return f"{app_name}-{role}-{request_id[:8]}"


class Runner:
    def __init__(
        self,
        client: eng.EngineClient,
        clock: GapClock,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = POLL_SECONDS,
    ) -> None:
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.poll = poll

    def find(self, name: str) -> dict | None:
        c = survey.find(self.client, name)
        return c if c is not None and survey.is_oneoff(c) else None

    def state(self, name: str) -> dict | None:
        """The one-off's `State`, or None when it is not there."""
        if self.find(name) is None:
            return None
        seen = self.client.inspect(name)
        return seen.get("State") if isinstance(seen.get("State"), dict) else {}

    def discard(self, name: str) -> None:
        if self.find(name) is not None:
            self.client.remove(name, force=True)

    def launch(self, name: str, body: dict) -> str:
        self.discard(name)
        cid = self.client.create(name, body)
        self.client.start(cid)
        return cid

    def wait(self, name: str, deadline: Deadline) -> int:
        """Until the one-off has exited; its exit code. `TimedOut` past the deadline."""
        while True:
            state = self.state(name)
            if state is None:
                raise TimedOut(f"{name} is gone")
            if state.get("Status") in ("exited", "dead") or (
                state.get("Running") is False and state.get("Status") not in ("created", "restarting")
            ):
                code = state.get("ExitCode")
                return code if isinstance(code, int) else -1
            if deadline.expired():
                self.discard(name)
                raise TimedOut(f"{name} ran past {int(deadline.seconds)} s")
            self.sleep(self.poll)

    def output(self, name: str, stderr: bool = False) -> str:
        return self.client.logs(name, stderr=stderr)

    def run(self, name: str, body: dict, seconds: float, stderr: bool = False) -> Result:
        """Create, start, wait, read and remove. The one-off is gone afterwards, whatever happened."""
        self.launch(name, body)
        try:
            code = self.wait(name, Deadline(self.clock, seconds))
            return Result(code, self.output(name, stderr=stderr))
        finally:
            with contextlib.suppress(eng.EngineError, eng.NotAllowed):
                self.discard(name)
