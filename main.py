#!/usr/bin/env python3
"""
main.py
=======
Entry point for the AdvTemplate async coordination system.

Demonstrates all three execution strategies wired as **independent tasks**
(not a cross-strategy pipeline — each strategy demo is self-contained) but
running with the new lifecycle-aware pattern: PAUSE, RESUME, STOP, SHUTDOWN.

Each task accepts its queues from outside, making it trivial to wire multiple
tasks of the same strategy into a pipeline.  ``main()`` only needs to supply
messages and read results.

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

    Each demo section shows how to wire a task's queues externally (for
    pipeline use) and how PAUSE/RESUME/STOP signals are delivered.

    All three tasks are registered with a single :class:`SchedulerManager`
    that runs them **sequentially** (each demo waits for the previous to
    finish).  Within each demo the task itself runs concurrently with the
    main coroutine feeding its inbox.
    """

    # -----------------------------------------------------------------------
    # Coroutine task demo (with PAUSE / RESUME)
    # -----------------------------------------------------------------------
    logger.info("=== Coroutine mode ===")

    coro_inbox:  asyncio.Queue[Message] = asyncio.Queue()
    coro_outbox: asyncio.Queue[Message] = asyncio.Queue()
    coro_log_q:  asyncio.Queue[Message] = asyncio.Queue()

    coroutine_task = ExampleCoroutineTask(
        name="coroutine-task-1",
        log_level="DEBUG",
        inbox=coro_inbox,
        outbox=coro_outbox,
        log_queue=coro_log_q,
    )

    # Pre-feed inbox before starting — SchedulerAsyncTask runs in an
    # isolated event loop (separate thread), so messages must be queued
    # before run_all() is called.
    for i in range(3):
        coro_inbox.put_nowait(Message.data(sender="main", payload={"index": i}))
    coro_inbox.put_nowait(Message.control(sender="main", signal=ControlSignal.STOP))

    coro_manager = SchedulerManager(name="coro-demo", concurrent=False)
    coro_manager.add(coroutine_task)
    await coro_manager.run_all()

    logger.info("Coroutine task results:")
    while not coro_outbox.empty():
        msg = coro_outbox.get_nowait()
        logger.info("  %s", msg.payload)

    # Log messages collected from log_queue
    while not coro_log_q.empty():
        lmsg = coro_log_q.get_nowait()
        logger.debug("  [LOG] %s", lmsg.payload)

    # -----------------------------------------------------------------------
    # Thread task demo (with PAUSE / RESUME)
    # -----------------------------------------------------------------------
    logger.info("=== Thread mode ===")

    import queue as _queue
    thread_inbox  = _queue.Queue()
    thread_outbox = _queue.Queue()
    thread_log_q  = _queue.Queue()

    thread_task = ExampleThreadTask(
        name="thread-task-1",
        log_level="DEBUG",
        inbox=thread_inbox,
        outbox=thread_outbox,
        log_queue=thread_log_q,
    )

    # Pre-feed: DATA, PAUSE, RESUME, DATA, STOP
    for i in range(2):
        thread_inbox.put(Message.data(sender="main", payload={"index": i}))
    thread_inbox.put(Message.control(sender="main", signal=ControlSignal.PAUSE))
    thread_inbox.put(Message.control(sender="main", signal=ControlSignal.RESUME))
    thread_inbox.put(Message.data(sender="main", payload={"index": 99}))
    thread_inbox.put(Message.control(sender="main", signal=ControlSignal.STOP))

    thread_manager = SchedulerManager(name="thread-demo", concurrent=False)
    thread_manager.add(thread_task)
    await thread_manager.run_all()

    logger.info("Thread task results:")
    while not thread_outbox.empty():
        msg = thread_outbox.get_nowait()
        logger.info("  %s", msg.payload)

    # -----------------------------------------------------------------------
    # Process task demo (standard — no injected queues; uses feed() + drain)
    # -----------------------------------------------------------------------
    logger.info("=== Process mode ===")

    process_task = ExampleProcessTask(name="process-task-1", log_level="DEBUG")
    for i in range(3):
        process_task.feed(Message.data(sender="main", payload={"index": i}))
    process_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))

    proc_manager = SchedulerManager(name="proc-demo", concurrent=False)
    proc_manager.add(process_task)
    await proc_manager.run_all()

    logger.info("Process task results:")
    for msg in process_task.results:
        logger.info("  %s", msg.payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())

