"""SyncScheduler: coalescing + debounce + lock (audit finding 5)."""

import threading

from jira_incidents.sync import SyncScheduler


def test_coalesces_concurrent_triggers():
    """Many simultaneous triggers must start exactly one background run."""
    started = []
    gate = threading.Event()

    def task():
        started.append(1)
        gate.wait(1.0)  # hold the run "in flight" while the other triggers fire

    s = SyncScheduler(task, min_interval=0.0, name="t")
    threads = [threading.Thread(target=s.trigger) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gate.set()
    s.join(1.0)
    assert sum(started) == 1


def test_debounce_skips_quick_resync():
    runs = []
    s = SyncScheduler(lambda: runs.append(1), min_interval=60.0, name="t")
    s.trigger()
    s.join(1.0)
    s.trigger()  # within min_interval -> debounced away
    s.join(1.0)
    assert sum(runs) == 1


def test_task_error_is_swallowed():
    def boom():
        raise RuntimeError("background boom")

    s = SyncScheduler(boom, min_interval=0.0, name="t")
    s.trigger()       # must not raise on the calling thread
    s.join(1.0)
    assert not s.is_running()
