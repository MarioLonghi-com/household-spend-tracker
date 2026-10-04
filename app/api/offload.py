"""Where the CPU-heavy part of a request runs: off the event loop, two at a time.

Issue #84. An `async def` route runs on the event loop, and anything it does
without an `await` holds that loop for every other request in the process. A
statement parse, a PDF render or an AVIF encode is hundreds of milliseconds to
seconds of pure CPU, so one slow file used to freeze the whole server -- a GET
for the register waited behind somebody's spreadsheet.

`asyncio.to_thread` alone moves the work off the loop but bounds nothing: its
default executor is sized for I/O, and twenty concurrent receipt uploads were
twenty full-size decodes in memory at once (issue #89). So the work goes to a
pool of its own, **two workers wide**, and that pool *is* the semaphore: a
third upload queues rather than decodes. The pool is shared by every door --
the browser's receipt upload, the agent's three receipt doors, the statement
import and the recogniser -- because the thing being rationed is this host's
memory and cores, not any one route's.

Two ways in, for the two kinds of caller:

* `run_cpu` for an `async def` route. It awaits the pool, so the loop keeps
  answering while the work runs or waits its turn.
* `run_cpu_sync` for a plain `def` route, which FastAPI already runs on a
  threadpool worker. It blocks *that worker* until the pool takes the job --
  the loop is never involved, and the cap still holds.

What goes in must be **pure**: bytes in, a value out, no session. A SQLAlchemy
session is not safe to use from two threads, so the database work stays in
the handler, before and after.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

#: Measured, not guessed: one 25 MP PNG grew the process by ~340 MiB before
#: #89 and ~150 after, a 24 MP phone JPEG ~325 and ~100 -- and this is a small
#: self-hosted box. Two keeps a second upload from waiting behind the first
#: without letting a burst of them decide how much memory the process spends.
CPU_WORKERS = 2

_pool = ThreadPoolExecutor(max_workers=CPU_WORKERS, thread_name_prefix="spendtracker-cpu")


def _bound[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Callable[[], T]:
    # The caller's context rides along, as `asyncio.to_thread` would carry it,
    # so a log line from inside a parse still knows which request it is.
    context = contextvars.copy_context()
    return functools.partial(context.run, fn, *args, **kwargs)


async def run_cpu[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run `fn` on the shared CPU pool and await it, leaving the loop free."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, _bound(fn, *args, **kwargs))


def run_cpu_sync[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """The same cap, from a plain `def` route that is already off the loop."""
    return _pool.submit(_bound(fn, *args, **kwargs)).result()
