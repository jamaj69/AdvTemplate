"""
examples/example_coroutine.py
==============================
Example coroutine coordination task.

Demonstrates the minimal pattern for a derived
:class:`~coordination.coroutine_task.CoroutineCoordinationTask`:

* Override :meth:`run` as an ``async def``.
* Use ``await self.get_item()`` to receive messages.
* Use ``await self.put_item(...)`` to send results.
* Honour stop signals via :meth:`~coordination.base.BaseCoordinationTask._is_stop_signal`.

This task also spawns a child coroutine task to show the two-level
coordination pattern.
"""

from __future__ import annotations

import asyncio

from customtypes import Message, TaskConfig, TaskKind
from coordination.coroutine_task import CoroutineCoordinationTask


# ---------------------------------------------------------------------------
# Child coroutine task (spawned by ExampleCoroutineTask)
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

class ExampleCoroutineTask(CoroutineCoordinationTask):
    """
    Example top-level coroutine coordination task.

    Spawns a :class:`_ChildCoroutineTask` and forwards incoming messages to
    it.  Results from the child are placed on this task's outbox.
    """

    async def run(self) -> None:
        self.log("info", "ExampleCoroutineTask started")

        # Build internal queues to communicate with the child
        child_inbox:  asyncio.Queue[Message] = asyncio.Queue()
        child_outbox: asyncio.Queue[Message] = asyncio.Queue()

        child_config = TaskConfig(
            task_id=f"{self.task_id}.child",
            kind=TaskKind.COROUTINE,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self._config.log_level,
        )
        child = _ChildCoroutineTask(child_config)
        child_task = await self.spawn(child)

        while True:
            msg = await self.get_item()
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                await child_inbox.put(Message.control(sender=self.task_id, signal=__import__("customtypes").ControlSignal.STOP))
                await child_task
                break
            # Forward message to child
            await child_inbox.put(msg)
            # Collect child result and forward to our outbox
            result = await child_outbox.get()
            child_outbox.task_done()
            await self.put_item(result)

        self.log("info", "ExampleCoroutineTask finished")
