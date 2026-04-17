"""
examples/example_coroutine.py
==============================
Example async coordination task.

Demonstrates the pattern for a :class:`~coordination.scheduler.SchedulerAsyncTask`:

* Subclass :class:`SchedulerAsyncTask` and override ``async def run()``.
* Use ``await self.get_item()`` / ``await self.put_item(msg)`` for message I/O.
* ``self.inbox`` and ``self.outbox`` are ``asyncio.Queue`` instances.
* Honour stop signals via ``self._is_stop_signal(msg)``.

Child tasks still use :class:`~coordination.coroutine_task.CoroutineCoordinationTask`
as they are internal implementation details of the parent.
"""

from __future__ import annotations

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

    Spawns a :class:`_ChildCoroutineTask`, forwards every incoming DATA
    message to it, collects results, and places them on its own outbox.
    Stops cleanly when it receives a CONTROL/STOP signal.
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
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                await child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP)
                )
                await child_task
                break
            await child_inbox.put(msg)
            result = await child_outbox.get()
            child_outbox.task_done()
            await self.put_item(result)

        self.log("info", "ExampleCoroutineTask finished")
