"""
coordination/process_task.py
=============================
Base class for coordination tasks that run in a **separate OS process** while
still being orchestrated by asyncio via
``asyncio.get_event_loop().run_in_executor(ProcessPoolExecutor)``.

Messages travel through :class:`multiprocessing.Queue`.  Because a separate
process cannot share memory, all messages must be JSON-serialisable — which is
guaranteed by :class:`~customtypes.Message`.

Usage
-----
Subclass :class:`ProcessCoordinationTask` and override :meth:`run`:

.. code-block:: python

    class MyProcessTask(ProcessCoordinationTask):
        def run(self) -> None:
            while True:
                msg = self.get_item()
                if self._is_stop_signal(msg):
                    break
                # … process msg …
                self.put_item(Message.result(self.task_id, {"ok": True}))

Scheduling from the coordinator::

    executor = ProcessPoolExecutor()
    asyncio.get_event_loop().run_in_executor(executor, task.run)

.. note::
   ``multiprocessing`` requires that task objects be **picklable**.
   Avoid storing non-picklable objects (e.g., locks, sockets) in instance
   attributes before the process is spawned.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from customtypes import Message, ProcessQueue, TaskConfig
from coordination.base import BaseCoordinationTask


class ProcessCoordinationTask(BaseCoordinationTask):
    """
    Coordination task that executes inside a separate OS process.

    The inbox and outbox are :class:`multiprocessing.Queue` instances.

    Parameters
    ----------
    config:
        Task configuration.  ``config.inbox`` and ``config.outbox`` **must**
        be :class:`multiprocessing.Queue` instances.
    """

    def __init__(self, config: TaskConfig) -> None:
        super().__init__(config)
        self._inbox:  ProcessQueue = config.inbox   # type: ignore[assignment]
        self._outbox: ProcessQueue = config.outbox  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Queue operations (synchronous — called inside the spawned process)
    # ------------------------------------------------------------------

    def get_item(self, timeout: float | None = None) -> Message:  # type: ignore[override]
        """
        Retrieve the next message from the inbox.

        Parameters
        ----------
        timeout:
            Seconds to wait.  ``None`` means block indefinitely.

        Returns
        -------
        Message
            The next message from the inbox.

        Raises
        ------
        queue.Empty
            If *timeout* is set and no message arrives in time.
        """
        raw = self._inbox.get(timeout=timeout)
        # Support both Message objects and raw JSON strings (cross-process)
        if isinstance(raw, str):
            message = Message.from_json(raw)
        else:
            message = raw
        self.log("debug", f"get_item ← {message.kind.value}", extra={"sender": message.sender})
        return message

    def put_item(self, message: Message) -> None:  # type: ignore[override]
        """
        Place *message* on the outbox.

        Parameters
        ----------
        message:
            Message to enqueue.  Sent as JSON string to ensure picklability.
        """
        self.log("debug", f"put_item → {message.kind.value}")
        self._outbox.put(message.to_json())

    # ------------------------------------------------------------------
    # Asyncio integration helper
    # ------------------------------------------------------------------

    async def run_in_executor(
        self,
        executor: ProcessPoolExecutor | None = None,
    ) -> Any:
        """
        Schedule :meth:`run` in a process-pool executor and await completion.

        Parameters
        ----------
        executor:
            Optional custom :class:`~concurrent.futures.ProcessPoolExecutor`.
            Defaults to the loop's default executor.
        """
        loop = asyncio.get_running_loop()
        self.log("info", f"scheduling '{self.task_id}' in process executor")
        return await loop.run_in_executor(executor, self.run)

    # ------------------------------------------------------------------
    # Abstract run — must be overridden (synchronous, runs in subprocess)
    # ------------------------------------------------------------------

    def run(self) -> None:  # type: ignore[override]
        """
        Override this method with your coordination logic (synchronous).

        Executes inside the spawned subprocess.  The default implementation
        logs a warning and returns immediately.
        """
        self.log("warning", f"[{self.task_id}] run() not overridden — exiting immediately")
