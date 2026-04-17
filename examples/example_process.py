"""
examples/example_process.py
============================
Example process coordination task.

Demonstrates the pattern for a :class:`~coordination.scheduler.SchedulerProcessTask`:

* Subclass :class:`SchedulerProcessTask` and override ``def run()`` (synchronous).
* Use ``self.get_item(timeout=…)`` / ``self.put_item(msg)`` for message I/O.
* ``self.inbox`` and ``self.outbox`` are ``multiprocessing.Manager().Queue()``
  proxies, set by :meth:`~coordination.scheduler.SchedulerProcessTask.start`
  before the process is spawned.
* All instance attributes must be **picklable**.

Child tasks still use :class:`~coordination.process_task.ProcessCoordinationTask`
as they are internal implementation details of the parent.  Their queues are
plain ``multiprocessing.Queue`` objects shared via fork.
"""

from __future__ import annotations

import multiprocessing

from customtypes import ControlSignal, Message, TaskConfig, TaskKind
from coordination.process_task import ProcessCoordinationTask
from coordination.scheduler import SchedulerProcessTask


# ---------------------------------------------------------------------------
# Child process task (internal — spawned by ExampleProcessTask)
# ---------------------------------------------------------------------------

class _ChildProcessTask(ProcessCoordinationTask):
    """
    Private child task executed in a dedicated subprocess.

    Receives DATA messages, squares the ``index`` value and places
    a RESULT on its outbox as a JSON string.
    """

    def run(self) -> None:
        self.log("info", "child process task started")
        while True:
            try:
                msg = self.get_item(timeout=5.0)
            except Exception:
                break
            if self._is_stop_signal(msg):
                self.log("info", "child received stop signal — exiting")
                break
            index: int = (msg.payload or {}).get("index", 0)
            result = Message.result(
                sender=self.task_id,
                payload={"index": index, "squared": index ** 2},
                correlation_id=msg.correlation_id,
            )
            self.put_item(result)
        self.log("info", "child process task finished")


# ---------------------------------------------------------------------------
# Public example task
# ---------------------------------------------------------------------------

class ExampleProcessTask(SchedulerProcessTask):
    """
    Example process coordination task.

    Spawns a :class:`_ChildProcessTask` via :class:`multiprocessing.Process`,
    forwards every incoming DATA message to it, collects results, and places
    them on its own outbox.  Stops cleanly when it receives a CONTROL/STOP signal.
    """

    def run(self) -> None:
        self.log("info", "ExampleProcessTask started")

        # Plain multiprocessing.Queue for child↔parent IPC (shared via fork)
        child_inbox:  multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]
        child_outbox: multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]

        child_config = TaskConfig(
            task_id=f"{self.name}.child",
            kind=TaskKind.PROCESS,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self.log_level,
        )
        child = _ChildProcessTask(child_config)
        child_proc = multiprocessing.Process(
            target=child.run,
            name=child_config.task_id,
            daemon=False,
        )
        child_proc.start()
        self.log("info", "spawned child process '%s' (pid=%d)",
                 child_config.task_id, child_proc.pid)

        while True:
            try:
                msg = self.get_item(timeout=5.0)
            except Exception:
                break
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP).to_json()
                )
                child_proc.join(timeout=10.0)
                break
            child_inbox.put(msg.to_json())
            try:
                raw = child_outbox.get(timeout=5.0)
                result = Message.from_json(raw) if isinstance(raw, str) else raw
                self.put_item(result)
            except Exception:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleProcessTask finished")
