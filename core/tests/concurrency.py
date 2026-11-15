"""
Helper for concurrency tests: run several callables at the same instant on real threads.

Each thread gets its own PostgreSQL connection (Django connections are per thread),
opens it before the start barrier so connection setup does not stagger the start,
and closes it when done. Tests using this helper must be ``TransactionTestCase``s,
because rows created by the test have to be committed to be visible to other
connections.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import connection

START_TIMEOUT_SECONDS = 15
FINISH_TIMEOUT_SECONDS = 60


@dataclass
class Outcome:
    """What one thread produced: a return value or the exception it raised."""

    result: Any = None
    error: BaseException | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def run_concurrently(tasks: list[Callable[[], Any]]) -> list[Outcome]:
    """
    Start every task at the same moment on its own thread and wait for all of them.

    Returns:
        One ``Outcome`` per task, in the same order as ``tasks``.

    Raises:
        AssertionError: a thread did not finish in time (for example a lock was
            never released). A hang is a failing test, never something to ignore.
    """
    barrier = threading.Barrier(len(tasks))
    outcomes = [Outcome() for _ in tasks]

    def worker(index: int, task: Callable[[], Any]) -> None:
        try:
            connection.ensure_connection()
            barrier.wait(timeout=START_TIMEOUT_SECONDS)
            outcomes[index].result = task()
        except Exception as exc:
            # Recorded, not swallowed: the test asserts on every outcome.
            outcomes[index].error = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(index, task), daemon=True)
        for index, task in enumerate(tasks)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=FINISH_TIMEOUT_SECONDS)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        raise AssertionError(f"Threads did not finish within {FINISH_TIMEOUT_SECONDS}s: {stuck}")
    return outcomes
