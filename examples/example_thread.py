"""
examples/example_thread.py
===========================
Example thread coordination task.

Demonstrates the minimal pattern for a derived
:class:`~coordination.thread_task.ThreadCoordinationTask`:

* Override :meth:`run` as a **synchronous** method (runs in a thread).
* Use ``self.get_item(timeout=…)`` to receive messages (returns ``None``
  on timeout, allowing the stop event to be checked).
* Use ``self.put_item(…)`` to send results.
* Honour stop signals and ``self._stop_event``.

This task also spawns a child thread task in a :class:`~threading.Thread`
to demonstrate the two-level coordination pattern.
"""

from __future__ import annotations

import queue
import threading

from customtypes import ControlSignal, Message, TaskConfig, TaskKind
from coordination.thread_task import ThreadCoordinationTask


# ---------------------------------------------------------------------------
# Child thread task (spawned by ExampleThreadTask)
# ---------------------------------------------------------------------------

class _ChildThreadTask(ThreadCoordinationTask):
    """
    Private child task run in a worker thread by :class:`ExampleThreadTask`.

    Receives DATA messages, triples the ``index`` value and places
    a RESULT on its outbox.
    """

    def run(self) -> None:
        self.log("info", "child thread task started")
        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue
            if self._is_stop_signal(msg):
                self.log("info", "child received stop signal — exiting")
                break
            index: int = (msg.payload or {}).get("index", 0)
            result = Message.result(
                sender=self.task_id,
                payload={"index": index, "tripled": index * 3},
                correlation_id=msg.correlation_id,
            )
            self.put_item(result)
        self.log("info", "child thread task finished")


# ---------------------------------------------------------------------------
# Public example task
# ---------------------------------------------------------------------------

class ExampleThreadTask(ThreadCoordinationTask):
    """
    Example top-level thread coordination task.

    Spawns a :class:`_ChildThreadTask` in a dedicated :class:`threading.Thread`
    and forwards incoming messages to it.  Results from the child are placed
    on this task's outbox.
    """

    def run(self) -> None:
        self.log("info", "ExampleThreadTask started")

        # Internal queues for child communication
        child_inbox:  queue.Queue[Message] = queue.Queue()
        child_outbox: queue.Queue[Message] = queue.Queue()

        child_config = TaskConfig(
            task_id=f"{self.task_id}.child",
            kind=TaskKind.THREAD,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self._config.log_level,
        )
        child = _ChildThreadTask(child_config)
        child_thread = threading.Thread(target=child.run, name=child_config.task_id, daemon=True)
        child_thread.start()
        self.log("info", f"spawned child thread '{child_config.task_id}'")

        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                child_inbox.put(Message.control(sender=self.task_id, signal=ControlSignal.STOP))
                child_thread.join(timeout=5.0)
                break
            # Forward to child
            child_inbox.put(msg)
            # Collect child result (blocking wait)
            try:
                result = child_outbox.get(timeout=5.0)
                self.put_item(result)
            except queue.Empty:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleThreadTask finished")
