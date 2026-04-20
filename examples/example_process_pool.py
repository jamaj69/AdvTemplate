#!/usr/bin/env python3
"""
examples/example_process_pool.py
==================================
Demonstrates :class:`~coordination.scheduler.SchedulerProcessPoolTask` — a
pool of N **persistent** worker processes that share a common work queue.

Difference from SchedulerProcessTask
--------------------------------------
``SchedulerProcessTask`` spawns a new OS process on every ``start()`` call
and tears it down when ``run()`` returns.  For workloads with many small tasks
this spawn-overhead (≈ 100–300 ms per process) dominates runtime.

``SchedulerProcessPoolTask`` pre-spawns *num_workers* processes once.  They
all block on ``self.get_item()`` and compete for work items as they arrive —
zero per-task spawn cost.  Workers exit only when the pool receives a STOP or
SHUTDOWN signal.

Pool controller
---------------
A lightweight controller thread reads from the external pool inbox and::

    DATA      → forwarded to the shared worker work queue
    PAUSE     → incoming messages buffered; workers untouched
    RESUME    → buffer flushed to work queue; forwarding resumes
    STOP      → controller injects *num_workers* STOP poison pills → workers exit
    SHUTDOWN  → controller injects *num_workers* SHUTDOWN pills   → workers exit

So ``main.py`` / ``feed()`` only needs to send **one** STOP regardless of
how many workers the pool has.

This example
------------
Creates a pool of 4 workers that each square the ``value`` field of a DATA
message.  20 work items are pre-fed; workers process them in parallel.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import logging

from coordination.scheduler import SchedulerManager, SchedulerProcessPoolTask
from customtypes import ControlSignal, Message


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("example_process_pool")


# ---------------------------------------------------------------------------
# Worker implementation
# ---------------------------------------------------------------------------

class SquarePoolWorker(SchedulerProcessPoolTask):
    """
    Pool worker that squares the ``value`` field of every DATA message.

    Each worker process runs this ``run()`` loop independently, pulling items
    from the shared work queue.  All instance attributes must be picklable
    because ``run()`` is executed in a subprocess.
    """

    def run(self) -> None:
        import os
        self.log("info", "worker started (pid=%d)", os.getpid())
        while True:
            try:
                msg = self.get_item(timeout=5.0)
            except Exception:
                self.log("warning", "inbox timeout — exiting (pid=%d)", os.getpid())
                break

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received (pid=%d)", os.getpid())
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received (pid=%d)", os.getpid())
                break

            if self._is_pause_signal(msg):
                if not self._wait_for_resume():
                    break
                continue

            value: int = (msg.payload or {}).get("value", 0)
            self.put_item(
                Message.result(
                    sender=self.name,
                    payload={"value": value, "squared": value ** 2},
                    correlation_id=msg.correlation_id,
                )
            )

        self.log("info", "worker finished (pid=%d)", os.getpid())


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Create a pool of 4 workers, feed 20 numbers, collect results.

    Note: only ONE STOP signal is needed — the pool controller broadcasts
    a poison pill to every worker automatically.
    """
    NUM_WORKERS = 4
    NUM_ITEMS   = 20

    pool = SquarePoolWorker(
        name="square-pool",
        num_workers=NUM_WORKERS,
        log_level="INFO",
    )

    # Pre-feed work items via feed() — the Manager queue does not exist until
    # start() creates it, so direct inbox writes are not possible yet.
    for i in range(1, NUM_ITEMS + 1):
        pool.feed(Message.data(sender="main", payload={"value": i}))

    # One STOP is sufficient — the pool controller broadcasts NUM_WORKERS pills.
    pool.feed(Message.control(sender="main", signal=ControlSignal.STOP))

    manager = SchedulerManager(name="pool-demo", concurrent=False)
    manager.add(pool)

    logger.info(
        "Starting pool: %d workers, %d work items …",
        NUM_WORKERS, NUM_ITEMS,
    )
    await manager.run_all()
    logger.info(
        "Pool finished — %d/%d results received",
        len(pool.results), NUM_ITEMS,
    )

    # Results arrive in completion order (not submission order) —
    # sort by value for a deterministic display.
    for msg in sorted(pool.results, key=lambda m: m.payload["value"]):
        logger.info("  %s", msg.payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
