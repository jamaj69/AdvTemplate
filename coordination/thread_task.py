"""
coordination/thread_task.py
============================
Base class for coordination tasks that run in a **thread pool** while still
being orchestrated by asyncio.

The heavy-lifting runs in a ``ThreadPoolExecutor`` via
``asyncio.get_event_loop().run_in_executor()``, so the asyncio event loop
remains responsive.  Queue I/O uses :class:`queue.Queue` with short-timeout
polls to avoid blocking the thread indefinitely.

Usage
-----
Subclass :class:`ThreadCoordinationTask` and override :meth:`run`:

.. code-block:: python

    class MyThreadTask(ThreadCoordinationTask):
        def run(self) -> None:
            while not self._stop_event.is_set():
                msg = self.get_item(timeout=1.0)
                if msg is None:
                    continue
                if self._is_stop_signal(msg):
                    break
                # … process msg …
                self.put_item(Message.result(self.task_id, {"ok": True}))

Scheduling from the coordinator::

    asyncio.get_event_loop().run_in_executor(None, task.run)
"""

from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from customtypes import Message, TaskConfig, ThreadQueue
from coordination.base import BaseCoordinationTask


class ThreadCoordinationTask(BaseCoordinationTask):
    """
    Coordination task that executes inside a worker thread.

    The inbox and outbox are :class:`queue.Queue` instances.
    A :class:`threading.Event` (``_stop_event``) is available so that the
    thread body can respond to external stop requests without busy-waiting.

    Parameters
    ----------
    config:
        Task configuration.  ``config.inbox`` and ``config.outbox`` **must**
        be :class:`queue.Queue` instances.
    """

    def __init__(self, config: TaskConfig) -> None:
        super().__init__(config)
        self._inbox:      ThreadQueue    = config.inbox   # type: ignore[assignment]
        self._outbox:     ThreadQueue    = config.outbox  # type: ignore[assignment]
        self._stop_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Queue operations (synchronous — called from the worker thread)
    # ------------------------------------------------------------------

    def get_item(self, timeout: float = 1.0) -> Message | None:  # type: ignore[override]
        """
        Retrieve the next message from the inbox with a timeout.

        Parameters
        ----------
        timeout:
            Seconds to wait before returning ``None``.

        Returns
        -------
        Message or None
            The next message, or ``None`` if the timeout expired.
        """
        try:
            message: Message = self._inbox.get(timeout=timeout)
            self.log("debug", f"get_item ← {message.kind.value}", extra={"sender": message.sender})
            return message
        except queue.Empty:
            return None

    def put_item(self, message: Message) -> None:  # type: ignore[override]
        """
        Place *message* on the outbox (blocks if the queue is full).

        Parameters
        ----------
        message:
            Message to enqueue.
        """
        self.log("debug", f"put_item → {message.kind.value}")
        self._outbox.put(message)

    # ------------------------------------------------------------------
    # Asyncio integration helper
    # ------------------------------------------------------------------

    async def run_in_executor(
        self,
        executor: ThreadPoolExecutor | None = None,
    ) -> Any:
        """
        Schedule :meth:`run` in a thread-pool executor and await completion.

        Parameters
        ----------
        executor:
            Optional custom :class:`~concurrent.futures.ThreadPoolExecutor`.
            Defaults to the loop's default executor.
        """
        loop = asyncio.get_running_loop()
        self.log("info", f"scheduling '{self.task_id}' in thread executor")
        return await loop.run_in_executor(executor, self.run)

    # ------------------------------------------------------------------
    # Stop signal
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the worker thread to stop at the next opportunity."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Abstract run — must be overridden (synchronous)
    # ------------------------------------------------------------------

    def run(self) -> None:  # type: ignore[override]
        """
        Override this method with your coordination logic (synchronous).

        The default implementation logs a warning and returns immediately.
        """
        self.log("warning", f"[{self.task_id}] run() not overridden — exiting immediately")
