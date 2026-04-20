#!/usr/bin/env python3
"""
examples/example_all_tasks.py
==============================
Demonstrates a single :class:`~coordination.scheduler.SchedulerManager`
running one task of each execution strategy simultaneously:

* :class:`SimpleAsyncTask`   — coroutine body, ``asyncio.Queue``
* :class:`SimpleThreadTask`  — synchronous body, ``queue.Queue``
* :class:`SimpleProcessTask` — synchronous body, ``multiprocessing.Manager().Queue()``

All three tasks are registered with the same manager and started
**concurrently** (the default ``concurrent=True`` behaviour).  Each task
receives a small batch of DATA messages followed by a STOP signal, processes
every message independently, and places RESULT messages on its outbox.

After :meth:`~coordination.scheduler.SchedulerManager.run_all` returns,
results are available in ``task.results`` for every task.

Run
---
::

    python examples/example_all_tasks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import logging

from coordination.scheduler import (
    SchedulerAsyncTask,
    SchedulerManager,
    SchedulerProcessTask,
    SchedulerThreadTask,
)
from customtypes import ControlSignal, Message


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("example_all_tasks")


# ---------------------------------------------------------------------------
# Task implementations
# ---------------------------------------------------------------------------

class SimpleAsyncTask(SchedulerAsyncTask):
    """
    Coroutine task that doubles the ``value`` field of every DATA message.

    Exits cleanly on STOP; also honours SHUTDOWN and PAUSE/RESUME via the
    base-class helpers.
    """

    async def run(self) -> None:
        self.log("info", "SimpleAsyncTask started")
        while True:
            msg = await self.get_item()

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — exiting immediately")
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — exiting gracefully")
                break

            if self._is_pause_signal(msg):
                await self._wait_for_resume()
                continue

            value: int = (msg.payload or {}).get("value", 0)
            await self.put_item(
                Message.result(
                    sender=self.name,
                    payload={"value": value, "doubled": value * 2},
                    correlation_id=msg.correlation_id,
                )
            )

        self.log("info", "SimpleAsyncTask finished")


class SimpleThreadTask(SchedulerThreadTask):
    """
    Thread task that triples the ``value`` field of every DATA message.

    Exits cleanly on STOP or when ``_stop_event`` is set; also honours
    SHUTDOWN and PAUSE/RESUME via the base-class helpers.
    """

    def run(self) -> None:
        self.log("info", "SimpleThreadTask started")
        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — exiting immediately")
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — exiting gracefully")
                break

            if self._is_pause_signal(msg):
                if not self._wait_for_resume():
                    break  # SHUTDOWN arrived while paused
                continue

            value: int = (msg.payload or {}).get("value", 0)
            self.put_item(
                Message.result(
                    sender=self.name,
                    payload={"value": value, "tripled": value * 3},
                    correlation_id=msg.correlation_id,
                )
            )

        self.log("info", "SimpleThreadTask finished")


class SimpleProcessTask(SchedulerProcessTask):
    """
    Process task that squares the ``value`` field of every DATA message.

    Runs in a dedicated subprocess.  All attributes must remain picklable.
    Exits cleanly on STOP; also honours SHUTDOWN and PAUSE/RESUME via the
    base-class helpers.
    """

    def run(self) -> None:
        self.log("info", "SimpleProcessTask started")
        while True:
            try:
                msg = self.get_item(timeout=5.0)
            except Exception:
                break

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — exiting immediately")
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — exiting gracefully")
                break

            if self._is_pause_signal(msg):
                if not self._wait_for_resume():
                    break  # SHUTDOWN arrived while paused
                continue

            value: int = (msg.payload or {}).get("value", 0)
            self.put_item(
                Message.result(
                    sender=self.name,
                    payload={"value": value, "squared": value ** 2},
                    correlation_id=msg.correlation_id,
                )
            )

        self.log("info", "SimpleProcessTask finished")


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Create one task of each execution strategy, register them with a single
    :class:`SchedulerManager`, and run them all concurrently.
    """

    # -- Async task ----------------------------------------------------------
    # Pre-feed the inbox before run_all(): the task runs in an isolated thread
    # with its own event loop, so messages must already be enqueued.
    async_task = SimpleAsyncTask(name="async-task", log_level="DEBUG")
    for i in range(1, 4):
        async_task.inbox.put_nowait(
            Message.data(sender="main", payload={"value": i})
        )
    async_task.inbox.put_nowait(
        Message.control(sender="main", signal=ControlSignal.STOP)
    )

    # -- Thread task ---------------------------------------------------------
    thread_task = SimpleThreadTask(name="thread-task", log_level="DEBUG")
    for i in range(1, 4):
        thread_task.inbox.put(
            Message.data(sender="main", payload={"value": i})
        )
    thread_task.inbox.put(
        Message.control(sender="main", signal=ControlSignal.STOP)
    )

    # -- Process task --------------------------------------------------------
    # Use feed() instead of writing directly to inbox: the Manager queue does
    # not exist until start() creates it.
    process_task = SimpleProcessTask(name="process-task", log_level="DEBUG")
    for i in range(1, 4):
        process_task.feed(Message.data(sender="main", payload={"value": i}))
    process_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))

    # -- Manager -------------------------------------------------------------
    # concurrent=True (default): all three tasks start simultaneously.
    manager = SchedulerManager(name="combined-demo")
    manager.add(async_task)
    manager.add(thread_task)
    manager.add(process_task)

    logger.info("Starting all tasks concurrently …")
    await manager.run_all()
    logger.info("All tasks finished (manager status: %s)", manager.status)

    # -- Results -------------------------------------------------------------
    logger.info("--- async-task results (doubled) ---")
    for msg in async_task.results:
        logger.info("  %s", msg.payload)

    logger.info("--- thread-task results (tripled) ---")
    for msg in thread_task.results:
        logger.info("  %s", msg.payload)

    logger.info("--- process-task results (squared) ---")
    for msg in process_task.results:
        logger.info("  %s", msg.payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
