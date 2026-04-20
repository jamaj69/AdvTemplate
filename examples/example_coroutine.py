"""
examples/example_coroutine.py
==============================
Example async coordination task demonstrating the pipeline + lifecycle pattern.

Key behaviours
--------------
* Subclass :class:`SchedulerAsyncTask` and override ``async def run()``.
* Queues (``inbox``, ``outbox``, ``log_queue``) are injected via the
  constructor — the base class exposes them; ``run()`` never touches raw
  queues directly.
* ``get_item()`` returns any :class:`~customtypes.Message`, including
  CONTROL signals.  The run loop dispatches on signal type:

  - ``STOP``     → drain child, exit gracefully.
  - ``SHUTDOWN`` → exit immediately.
  - ``PAUSE``    → call ``await self._wait_for_resume()``.
  - ``DATA``     → forward to child, collect result.

Child tasks use :class:`~coordination.coroutine_task.CoroutineCoordinationTask`
and are internal implementation details of the parent coordinator.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from customtypes import ControlSignal, Message, TaskConfig, TaskKind
from coordination.coroutine_task import CoroutineCoordinationTask
from coordination.scheduler import SchedulerAsyncTask


# ---------------------------------------------------------------------------
# Child coroutine task (internal — spawned by ExampleCoroutineTask)
# ---------------------------------------------------------------------------

class _ChildCoroutineTask(CoroutineCoordinationTask):
    """
    Private child task spawned by :class:`ExampleCoroutineTask`.

    Receives DATA messages, doubles the ``index`` value and places
    a RESULT on its outbox.
    """

    async def run(self) -> None:
        self.log("info", "child coroutine task started")
        while True:
            msg = await self.get_item()
            if self._is_stop_signal(msg):
                self.log("info", "child received stop signal — exiting")
                break
            index: int = (msg.payload or {}).get("index", 0)
            result = Message.result(
                sender=self.task_id,
                payload={"index": index, "doubled": index * 2},
                correlation_id=msg.correlation_id,
            )
            await self.put_item(result)
        self.log("info", "child coroutine task finished")


# ---------------------------------------------------------------------------
# Public example task
# ---------------------------------------------------------------------------

class ExampleCoroutineTask(SchedulerAsyncTask):
    """
    Example coroutine coordination task.

    Accepts injected ``inbox``, ``outbox`` and ``log_queue`` for pipeline
    wiring.  Handles the full lifecycle: DATA processing, PAUSE/RESUME,
    STOP and SHUTDOWN.

    Parameters
    ----------
    name:
        Task identifier.
    log_level:
        Logging verbosity.
    inbox:
        Input queue (``asyncio.Queue``).  Created internally if not provided.
    outbox:
        Output queue (``asyncio.Queue``).  Created internally if not provided.
    log_queue:
        Optional shared log queue for centralised log collection.
    """

    async def run(self) -> None:
        self.log("info", "ExampleCoroutineTask started")

        child_inbox:  asyncio.Queue[Message] = asyncio.Queue()
        child_outbox: asyncio.Queue[Message] = asyncio.Queue()

        child_config = TaskConfig(
            task_id=f"{self.name}.child",
            kind=TaskKind.COROUTINE,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self.log_level,
        )
        child = _ChildCoroutineTask(child_config)
        child_task = asyncio.create_task(child.run(), name=child_config.task_id)
        self.log("info", "spawning child coroutine task '%s'", child_config.task_id)

        while True:
            msg = await self.get_item()

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — stopping immediately")
                child_task.cancel()
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — propagating to child")
                await child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP)
                )
                await child_task
                break

            if self._is_pause_signal(msg):
                self.log("info", "PAUSE received")
                await self._wait_for_resume()
                continue

            # DATA — forward to child and collect result
            await child_inbox.put(msg)
            result = await child_outbox.get()
            child_outbox.task_done()
            await self.put_item(result)

        self.log("info", "ExampleCoroutineTask finished")

