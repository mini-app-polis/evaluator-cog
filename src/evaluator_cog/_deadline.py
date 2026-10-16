"""Stop a run before the Lambda function's timeout kills it.

Lambda kills a timed-out invocation outright: no ``finally``, no report,
and the handler returns nothing, so the message comes back with nobody
told why. :func:`deadline` raises :class:`RunOutOfTime` a margin early,
inside the run, so it travels the ordinary failure path instead — the
flow reports it and re-raises, and the handler names the message for
redelivery.

The same function also sets the shared library's run deadline
(:func:`mini_app_polis.deadline.run_deadline`), earlier than the stop by
:data:`REQUEST_HEADROOM_SECONDS`. Inside it, the library's Google retry
loops start no attempt after it and skip a backoff that would cross it:
they raise :class:`~mini_app_polis.deadline.RunDeadlineReached`, or
re-raise the error that prompted the retry, so the run fails the ordinary
way before the stop, naming what failed rather than being cut off
mid-backoff. The API client is not deadline-aware, so the run's report
still posts after it.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC
from mini_app_polis.deadline import run_deadline

#: How long before the function's timeout a run is stopped, so that it can
#: still send its report and the handler can still return. The report is
#: one POST with the API client's own timeout; this leaves room for it.
DEADLINE_MARGIN_SECONDS = 30

#: The longest single external request the run makes through the shared
#: library: how much earlier than the stop the shared run deadline is set.
#: The library checks that deadline between attempts, never during one, so
#: an attempt that starts just before it can run this long; set this much
#: earlier, it still ends before the stop. That request is a Drive or Sheets
#: call, whose timeout is the one googleapiclient's ``build()`` gives the
#: library's clients when no socket default timeout is set.
#: evaluator-cog makes no Google request through the library today, so
#: the shared deadline has nothing to act on yet; it is set the same way
#: as in every cog so a Drive or Sheets call added later is covered.
REQUEST_HEADROOM_SECONDS: float = DEFAULT_HTTP_TIMEOUT_SEC


class RunOutOfTime(Exception):  # noqa: N818 — named for what happened
    """The run was about to be killed by the function's timeout.

    Lambda kills a timed-out invocation outright: no ``finally``, no
    report, and the handler returns nothing, so the message comes back
    with nobody told why. Raised a margin early instead, inside the run,
    it travels the ordinary failure path — the flow reports it and
    re-raises, and the handler names the message for redelivery.
    """


@contextmanager
def deadline(context: Any) -> Iterator[None]:
    """Raise :class:`RunOutOfTime` a margin before the invocation's timeout.

    SIGALRM, because the run is blocking I/O on the main thread — which is
    where Lambda runs the handler and where the signal is delivered. A
    context without ``get_remaining_time_in_millis`` (a test, a local
    call) gets no deadline.

    Inside, the shared run deadline is set :data:`REQUEST_HEADROOM_SECONDS`
    before that stop. It is a context variable: work handed to
    ``asyncio.to_thread`` or an ``asyncio`` task inherits it, a plain
    thread or ``ThreadPoolExecutor.submit`` does not. Clamped at zero: an
    invocation with less than the margin and the headroom left cannot fit
    one more request, so the library starts none of its Google requests
    and the run fails, and is handed back, like any other.
    """
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if remaining is None:
        yield
        return
    seconds = remaining() / 1000 - DEADLINE_MARGIN_SECONDS
    if seconds <= 0:
        raise RunOutOfTime("no time left in this invocation to start a run")

    def _expire(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise RunOutOfTime(
            f"stopped {DEADLINE_MARGIN_SECONDS}s before the function timeout"
        )

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        with run_deadline(max(0.0, seconds - REQUEST_HEADROOM_SECONDS)):
            yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
