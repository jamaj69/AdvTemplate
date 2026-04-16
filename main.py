#!/usr/bin/env python3
"""
main.py
=======
Entry point for the AdvTemplate async coordination system.

This module creates and schedules coordination tasks in three modes:

* **Coroutine** — pure asyncio coroutines sharing the event loop.
* **Thread**    — blocking work delegated to a ``ThreadPoolExecutor``.
* **Process**   — CPU-bound work delegated to a ``ProcessPoolExecutor``.

Each mode is demonstrated with the example tasks found in the ``examples/``
package.  Replace those with your own derived classes to build a real system.

Run
---
::

    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from customtypes import (
    ControlSignal,
    Message,
    TaskConfig,
    TaskKind,
)
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
# Coroutine mode scheduler
# ---------------------------------------------------------------------------

async def run_coroutine_demo() -> None:
    """Schedule and run example coroutine coordination tasks."""
    logger.info("=== Coroutine mode ===")

    inbox:  asyncio.Queue[Message] = asyncio.Queue()
    outbox: asyncio.Queue[Message] = asyncio.Queue()

    config = TaskConfig(
        task_id="coroutine-task-1",
        kind=TaskKind.COROUTINE,
        inbox=inbox,
        outbox=outbox,
        log_level="DEBUG",
    )

    task = ExampleCoroutineTask(config)
    asyncio_task = asyncio.create_task(task.run(), name=config.task_id)

    # Feed a few messages then send a stop signal
    for i in range(3):
        await inbox.put(Message.data(sender="main", payload={"index": i}))

    await inbox.put(Message.control(sender="main", signal=ControlSignal.STOP))
    await asyncio_task

    # Drain outbox
    while not outbox.empty():
        reply = outbox.get_nowait()
        logger.info("coroutine result: %s", reply.payload)


# ---------------------------------------------------------------------------
# Thread mode scheduler
# ---------------------------------------------------------------------------

async def run_thread_demo() -> None:
    """Schedule and run example thread coordination tasks."""
    logger.info("=== Thread mode ===")

    inbox:  queue.Queue[Message] = queue.Queue()
    outbox: queue.Queue[Message] = queue.Queue()

    config = TaskConfig(
        task_id="thread-task-1",
        kind=TaskKind.THREAD,
        inbox=inbox,
        outbox=outbox,
        log_level="DEBUG",
    )

    task = ExampleThreadTask(config)

    # Feed messages before starting (queue is thread-safe)
    for i in range(3):
        inbox.put(Message.data(sender="main", payload={"index": i}))
    inbox.put(Message.control(sender="main", signal=ControlSignal.STOP))

    with ThreadPoolExecutor(max_workers=1) as executor:
        await asyncio.get_running_loop().run_in_executor(executor, task.run)

    # Drain outbox
    while not outbox.empty():
        reply = outbox.get_nowait()
        logger.info("thread result: %s", reply.payload)


# ---------------------------------------------------------------------------
# Process mode scheduler
# ---------------------------------------------------------------------------

async def run_process_demo() -> None:
    """Schedule and run example process coordination tasks."""
    logger.info("=== Process mode ===")

    with multiprocessing.Manager() as manager:
        inbox  = manager.Queue()
        outbox = manager.Queue()

        config = TaskConfig(
            task_id="process-task-1",
            kind=TaskKind.PROCESS,
            inbox=inbox,
            outbox=outbox,
            log_level="DEBUG",
        )

        task = ExampleProcessTask(config)

        # Feed messages before starting
        for i in range(3):
            inbox.put(Message.data(sender="main", payload={"index": i}).to_json())
        inbox.put(Message.control(sender="main", signal=ControlSignal.STOP).to_json())

        with ProcessPoolExecutor(max_workers=1) as executor:
            await asyncio.get_running_loop().run_in_executor(executor, task.run)

        # Drain outbox
        while not outbox.empty():
            raw = outbox.get_nowait()
            reply = Message.from_json(raw) if isinstance(raw, str) else raw
            logger.info("process result: %s", reply.payload)


# ---------------------------------------------------------------------------
# Isolated-loop runner
# ---------------------------------------------------------------------------

async def _run_in_isolated_loop(coro_fn: Callable[[], Awaitable[None]]) -> None:
    """
    Run *coro_fn* in a brand-new ``asyncio`` event loop inside a dedicated
    thread, keeping it fully isolated from the main loop.

    The main loop awaits completion via ``run_in_executor`` so it remains
    responsive while the worker thread runs.
    """
    exc: BaseException | None = None

    def _thread() -> None:
        nonlocal exc
        try:
            asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001
            exc = e

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    await asyncio.get_running_loop().run_in_executor(None, t.join)
    if exc is not None:
        raise exc


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Top-level async coordinator.

    Each demo runs in its own thread with its own ``asyncio`` event loop,
    fully isolated from the main loop.  In a real application you would
    replace this with your own scheduling logic — mixing modes as needed and
    wiring up queues between coordination layers.
    """
    await _run_in_isolated_loop(run_coroutine_demo)
    await _run_in_isolated_loop(run_thread_demo)
    await _run_in_isolated_loop(run_process_demo)
    logger.info("All coordination tasks completed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
