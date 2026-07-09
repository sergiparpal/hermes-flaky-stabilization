"""Coalesced + debounced background task runner for the provider's sync path.

Extracted from the provider so the "never block the turn loop" scheduling policy
lives in one place, decoupled from *what* runs. It is a generic background
runner: construct it with a zero-arg ``task`` callable and call :meth:`trigger`
once per turn. Two guards keep work off the hot path:

* **Coalesce** — if the task is already running, return immediately (never
  ``join()`` on the calling/turn thread).
* **Debounce** — skip if the task ran within ``min_interval`` seconds.

The scheduler is intentionally ignorant of ingest, the store, and the prefetch
cache: the provider passes a thunk that ingests *and* (on change) invalidates the
cache, so the provider stays the mediator between those two concerns. Errors in
the task are swallowed and logged — a failed background sync must never surface
to the agent loop.

Stdlib only; no Hermes imports.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Runs *task* on a daemon thread, coalesced and debounced. Never blocks."""

    def __init__(self, task: Callable[[], None], *, min_interval: float,
                 name: str = "sync"):
        self._task = task
        self._min_interval = float(min_interval)
        self._name = name
        self._thread: threading.Thread | None = None
        self._last_at: float = 0.0
        # Serialises the coalesce/debounce check-then-spawn so two near-
        # simultaneous trigger() calls can't both pass the is_alive() guard and
        # start duplicate background runs. The task itself runs outside the lock.
        self._lock = threading.Lock()

    def trigger(self) -> None:
        """Spawn the task in the background if not coalesced/debounced away."""
        with self._lock:
            prev = self._thread
            if prev and prev.is_alive():
                return  # coalesce: one in flight is enough
            now = time.monotonic()
            if self._last_at and (now - self._last_at) < self._min_interval:
                return  # debounce: ran recently
            self._last_at = now

            def _work():
                try:
                    self._task()
                except Exception as e:
                    logger.warning("%s: background task failed: %s", self._name, e)

            self._thread = threading.Thread(target=_work, daemon=True, name=self._name)
            self._thread.start()

    def join(self, timeout: float) -> None:
        """Wait up to *timeout* for any in-flight run to settle (best effort)."""
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread
