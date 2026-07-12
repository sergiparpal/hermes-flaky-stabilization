"""Bounded, timeout-degrading cache for the provider's ``prefetch()`` path.

Extracted from the provider so the caching policy — FIFO eviction, the bounded
worker pool, the "return fast or degrade to empty" timeout, and "never cache an
empty result" — lives in one cohesive, independently-testable component instead
of being interleaved with the legacy plugin's lifecycle plumbing.

The cache is deliberately ignorant of *what* it builds: it is constructed with a
``builder`` callable ``(query) -> str`` (the provider supplies its redaction-aware
prefetch builder) and only handles memoisation, concurrency, and timeouts. This
keeps redaction/egress concerns in the provider and makes this module reusable.

Stdlib only; no Hermes imports.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 64
DEFAULT_WORKERS = 4  # bound on concurrent prefetch builder threads


class PrefetchCache:
    """Memoising, timeout-bounded wrapper around a prefetch *builder*."""

    def __init__(self, builder: Callable[[str], str], *,
                 max_size: int = DEFAULT_MAX_SIZE, workers: int = DEFAULT_WORKERS,
                 name: str = "prefetch"):
        self._builder = builder
        self._max_size = max_size
        self._workers = workers
        self._name = name
        self._cache: dict[str, str] = {}
        self._inflight: dict[str, Future[str]] = {}
        self._generation = 0
        self._closed = False
        self._max_pending = max(workers, workers * 2)
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()

    @staticmethod
    def _key(query: str) -> str:
        return query.strip().lower()

    def get(self, query: str, *, timeout: float) -> str:
        """Return the cached/built prefetch block, or ``""`` on miss/timeout/error.

        Serves from cache when warm. Otherwise builds on the bounded pool and
        waits only up to *timeout*; on timeout we degrade to ``""`` but the task
        keeps running and warms the cache for the next turn. Never raises.
        """
        if not query:
            return ""
        key = self._key(query)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            fut = self._submit(key, query)
            if fut is None:
                return ""
            return fut.result(timeout=timeout) or ""
        except FuturesTimeout:
            return ""  # degrade; the result still warms the cache when it lands
        except Exception as e:
            logger.debug("%s: prefetch build failed: %s", self._name, e)
            return ""

    def queue(self, query: str) -> None:
        """Warm the cache for the next turn in the background. Never raises."""
        if not query:
            return
        key = self._key(query)
        try:
            self._submit(key, query)
        except Exception as e:
            logger.debug("%s: queue failed: %s", self._name, e)

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._cache.clear()

    def contains(self, query: str) -> bool:
        """Thread-safe membership test (used by callers/tests to observe warming)."""
        with self._lock:
            return self._key(query) in self._cache

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            self._cache.clear()
            self._inflight.clear()
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        self._pool = None

    # -- internals -----------------------------------------------------------

    def _submit(self, key: str, query: str) -> Future[str] | None:
        pool = self._get_pool()
        with self._lock:
            if self._closed:
                return None
            existing = self._inflight.get(key)
            if existing is not None:
                return existing
            if len(self._inflight) >= self._max_pending:
                return None
            generation = self._generation
            future = pool.submit(self._build_and_cache, key, query, generation)
            self._inflight[key] = future
        # A completed Future invokes callbacks synchronously. Register outside
        # the cache lock so a very fast builder cannot deadlock in _forget().
        future.add_done_callback(lambda done: self._forget(key, done))
        return future

    def _build_and_cache(self, key: str, query: str, generation: int) -> str:
        value = self._builder(query)
        if value:
            # Don't cache empty results: the index may simply be warming up,
            # and a cached "" would shadow matches once ingest populates it.
            self._put(key, value, generation)
        return value

    def _forget(self, key: str, future: Future[str]) -> None:
        with self._lock:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)

    def _put(self, key: str, value: str, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                return
            if key not in self._cache and len(self._cache) >= self._max_size:
                # Evict the oldest entry (FIFO — dicts preserve insertion order).
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = value

    def _get_pool(self) -> ThreadPoolExecutor:
        pool = self._pool
        if pool is not None:
            return pool
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("prefetch cache is shut down")
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix=self._name)
            return self._pool
