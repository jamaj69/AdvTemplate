"""
examples/example_thread.py
===========================
Example thread coordination task demonstrating the pipeline + lifecycle pattern.

Key behaviours
--------------
* Subclass :class:`SchedulerThreadTask` and override ``def run()`` (synchronous).
* Queues (``inbox``, ``outbox``, ``log_queue``) are injected via the
  constructor — the base class exposes them; ``run()`` never touches raw
  queues directly.
* ``get_item(timeout)`` returns any :class:`~customtypes.Message` or ``None``
  on timeout (allowing ``_stop_event`` polling).  The run loop dispatches:

  - ``STOP``     → drain child, exit gracefully.
  - ``SHUTDOWN`` → exit immediately.
  - ``PAUSE``    → call ``self._wait_for_resume()``.
  - ``DATA``     → forward to child, collect result.

Child tasks use :class:`~coordination.thread_task.ThreadCoordinationTask`.
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
        Input queue (``queue.Queue``).  Created internally if not provided.
    outbox:
        Output queue (``queue.Queue``).  Created internally if not provided.
    log_queue:
        Optional shared log queue for centralised log collection.
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

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — stopping immediately")
                child._stop_event.set()
                child_thread.join(timeout=2.0)
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — propagating to child")
                child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP)
                )
                child_thread.join(timeout=5.0)
                break

            if self._is_pause_signal(msg):
                self.log("info", "PAUSE received")
                if not self._wait_for_resume():
                    break   # SHUTDOWN arrived while paused
                continue

            # DATA — forward to child and collect result
            child_inbox.put(msg)
            try:
                result = child_outbox.get(timeout=5.0)
                self.put_item(result)
            except queue.Empty:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleThreadTask finished")


