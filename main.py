#!/usr/bin/env python3
"""
main.py
=======
Entry point for the AdvTemplate async coordination system.

Demonstrates all three execution strategies by instantiating the example tasks,
pre-loading their inboxes, registering them with a :class:`SchedulerManager`,
and running them sequentially.

Each task encapsulates its own queue creation, executor setup, and child-task
lifecycle.  ``main()`` only needs to supply messages and read results.

Run
---
::

    python main.py
"""

from __future__ import annotations

import asyncio
import logging

from coordination.scheduler import SchedulerManager
from customtypes import ControlSignal, Message
from examples.example_coroutine import ExampleCoroutineTask
from examples.example_thread import ExampleThreadTask
from examples.example_process import ExampleProcessTask


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Top-level async coordinator.

    Instantiates one task of each strategy, pre-feeds messages into their
    inboxes, registers them with a :class:`~coordination.scheduler.SchedulerManager`,
    and awaits sequential execution.  Results are read from ``task.results``
    after the manager completes.
    """
    manager = SchedulerManager(name="demo-manager")

    # -- Coroutine task ------------------------------------------------------
    logger.info("=== Coroutine mode ===")
    coroutine_task = ExampleCoroutineTask(name="coroutine-task-1", log_level="DEBUG")
    for i in range(3):
        coroutine_task.inbox.put_nowait(Message.data(sender="main", payload={"index": i}))
    coroutine_task.inbox.put_nowait(
        Message.control(sender="main", signal=ControlSignal.STOP)
    )
    manager.add(coroutine_task)

    # -- Thread task ---------------------------------------------------------
    logger.info("=== Thread mode ===")
    thread_task = ExampleThreadTask(name="thread-task-1", log_level="DEBUG")
    for i in range(3):
        thread_task.inbox.put(Message.data(sender="main", payload={"index": i}))
    thread_task.inbox.put(Message.control(sender="main", signal=ControlSignal.STOP))
    manager.add(thread_task)

    # -- Process task --------------------------------------------------------
    logger.info("=== Process mode ===")
    process_task = ExampleProcessTask(name="process-task-1", log_level="DEBUG")
    for i in range(3):
        process_task.feed(Message.data(sender="main", payload={"index": i}))
    process_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))
    manager.add(process_task)

    # -- Run all -------------------------------------------------------------
    await manager.run_all()

    for task in manager.tasks:
        for msg in task.results:
            logger.info("%s result: %s", task.name, msg.payload)
    logger.info("Manager '%s' status: %s", manager.name, manager.status)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
