"""
coordination/coroutine_task.py
==============================
Base class for coordination tasks that run as **asyncio coroutines**.

Usage
-----
Subclass :class:`CoroutineCoordinationTask` and override :meth:`run`:

.. code-block:: python

    class MyCoroutineTask(CoroutineCoordinationTask):
        async def run(self) -> None:
            while True:
                msg = await self.get_item()
                if self._is_stop_signal(msg):
                    break
                # … process msg …
                await self.put_item(Message.result(self.task_id, {"ok": True}))

The task is scheduled by :mod:`main` via ``asyncio.create_task(task.run())``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from customtypes import AsyncQueue, Message, TaskConfig
from coordination.base import BaseCoordinationTask


class CoroutineCoordinationTask(BaseCoordinationTask):
    """
    Coordination task that executes as an ``asyncio`` coroutine.

    The inbox and outbox are :class:`asyncio.Queue` instances and all
    queue operations are ``await``-able.

    Parameters
    ----------
    config:
        Task configuration.  ``config.inbox`` and ``config.outbox`` **must**
        be :class:`asyncio.Queue` instances.
    """

    def __init__(self, config: TaskConfig) -> None:
        super().__init__(config)
        self._inbox:  AsyncQueue = config.inbox   # type: ignore[assignment]
        self._outbox: AsyncQueue = config.outbox  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    async def get_item(self) -> Message:  # type: ignore[override]
        """
        Asynchronously retrieve the next message from the inbox.

        Suspends the coroutine until a message is available.
        """
        message: Message = await self._inbox.get()
        self.log("debug", f"get_item ← {message.kind.value}", extra={"sender": message.sender})
        self._inbox.task_done()
        return message

    async def put_item(self, message: Message) -> None:  # type: ignore[override]
        """
        Asynchronously place *message* on the outbox.

        Suspends the coroutine if the queue is full.
        """
        self.log("debug", f"put_item → {message.kind.value}", extra={"receiver": "outbox"})
        await self._outbox.put(message)

    # ------------------------------------------------------------------
    # Child-task helpers
    # ------------------------------------------------------------------

    async def spawn(
        self,
        task: "CoroutineCoordinationTask",
    ) -> asyncio.Task[Any]:
        """
        Schedule a child coroutine task and return the asyncio Task handle.

        Parameters
        ----------
        task:
            An already-constructed :class:`CoroutineCoordinationTask`
            instance whose :meth:`run` will be scheduled.
        """
        self.log("info", f"spawning child coroutine task '{task.task_id}'")
        return asyncio.create_task(task.run(), name=task.task_id)

    # ------------------------------------------------------------------
    # Abstract run — must be overridden
    # ------------------------------------------------------------------

    async def run(self) -> None:  # type: ignore[override]
        """
        Override this method with your coordination logic.

        The default implementation logs a warning and returns immediately.
        """
        self.log("warning", f"[{self.task_id}] run() not overridden — exiting immediately")
