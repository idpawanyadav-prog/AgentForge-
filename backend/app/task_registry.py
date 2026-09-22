"""Single authoritative background-task registry.

Every detached coroutine the server spawns on its own (PO resume at startup,
scheduler loops, SSE pollers, deferred work) should be created through this
module instead of a bare ``asyncio.create_task``. Doing so guarantees two
things the plan calls for:

* a background failure is logged rather than silently swallowed, and
* application shutdown can cancel every outstanding task from one place.

``create`` schedules on the captured main loop (thread-safe, so it also works
from worker threads) or, when none is captured yet, on the caller's running
loop. The returned future is tracked until it finishes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional

logger = logging.getLogger(__name__)


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the main event loop so tasks can be spawned onto it from
        worker threads (see :meth:`create`)."""
        self._loop = loop

    def _label(self, fut: Any) -> str:
        return getattr(fut, "get_name", lambda: "?")() or "?"

    def _on_done(self, fut: Any) -> None:
        self._tasks.discard(fut)
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("Background task failed: %s", self._label(fut), exc_info=exc)

    def track(self, fut: Any) -> Any:
        """Register an already-created task/future so failures are logged and
        it participates in :meth:`cancel_all`."""
        self._tasks.add(fut)
        fut.add_done_callback(self._on_done)
        return fut

    def create(self, coro: Coroutine, *, name: Optional[str] = None) -> Any:
        """Schedule ``coro`` on the main loop (thread-safe) or the current
        running loop and track the resulting future."""
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            fut = asyncio.get_running_loop().create_task(coro, name=name)
        return self.track(fut)

    def cancel_all(self) -> int:
        """Request cancellation of every tracked task. Returns how many were
        still in flight. Called on application shutdown."""
        pending = list(self._tasks)
        for fut in pending:
            try:
                fut.cancel()
            except Exception:  # a future already finishing must not break shutdown
                logger.debug("cancel_all: failed to cancel %s", self._label(fut),
                             exc_info=True)
        self._tasks.clear()
        return len(pending)

    @property
    def count(self) -> int:
        return len(self._tasks)


background_tasks = BackgroundTaskRegistry()
