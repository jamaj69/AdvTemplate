"""
examples/example_thread.py
===========================
Example thread coordination task.

Demonstrates the pattern for a :class:`~coordination.scheduler.SchedulerThreadTask`:

* Subclass :class:`SchedulerThreadTask` and override ``def run()`` (synchronous).
* Use ``self.get_item(timeout=…)`` / ``self.put_item(msg)`` for message I/O.
* ``self.inbox`` and ``self.outbox`` are ``queue.Queue`` instances.
* Poll ``self._stop_event`` to support cooperative cancellation.

Child tasks still use :class:`~coordination.thread_task.ThreadCoordinationTask`
as they are internal implementation details of the parent.
"""

from __future__ import annotations

import queue
import threading

from customtypes import ControlSignal, Message, TaskConfig, TaskKind
from coordination.thread_task import ThreadCoordinationTask
from coordination.scheduler import SchedulerThreadTask


# ---------------------------------------------------------------------------
# Child thread task (internal — spawned by ExampleThreadTask)
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

class ExampleThreadTask(SchedulerThreadTask):
    """
    Example thread coordination task.

    Spawns a :class:`_ChildThreadTask` in a dedicated :class:`threading.Thread`,
    forwards every incoming DATA message to it, collects results, and places
    them on its own outbox.  Stops cleanly when it receives a CONTROL/STOP signal.
    """

    def run(self) -> None:
        self.log("info", "ExampleThreadTask started")

        child_inbox:  queue.Queue[Message] = queue.Queue()
        child_outbox: queue.Queue[Message] = queue.Queue()

        child_config = TaskConfig(
            task_id=f"{self.name}.child",
            kind=TaskKind.THREAD,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self.log_level,
        )
        child = _ChildThreadTask(child_config)
        child_thread = threading.Thread(
            target=child.run, name=child_config.task_id, daemon=True
        )
        child_thread.start()
        self.log("info", "spawned child thread '%s'", child_config.task_id)

        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP)
                )
                child_thread.join(timeout=5.0)
                break
            child_inbox.put(msg)
            try:
                result = child_outbox.get(timeout=5.0)
                self.put_item(result)
            except queue.Empty:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleThreadTask finished")
