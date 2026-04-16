"""
examples/example_process.py
============================
Example process coordination task.

Demonstrates the minimal pattern for a derived
:class:`~coordination.process_task.ProcessCoordinationTask`:

* Override :meth:`run` as a **synchronous** method (runs in a subprocess).
* Use ``self.get_item(timeout=…)`` to receive messages.
* Use ``self.put_item(…)`` to send results as JSON strings.
* Honour stop signals.

.. important::
   All attributes of this class (and its child task) must be **picklable**
   because Python's ``multiprocessing`` module uses pickle to transfer objects
   between processes.  Avoid storing sockets, file handles, locks, etc.

This task also spawns a child process task in a
:class:`multiprocessing.Process` to demonstrate the two-level coordination
pattern.
"""

from __future__ import annotations

import multiprocessing

from customtypes import ControlSignal, Message, TaskConfig, TaskKind
from coordination.process_task import ProcessCoordinationTask


# ---------------------------------------------------------------------------
# Child process task (spawned by ExampleProcessTask)
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

class ExampleProcessTask(ProcessCoordinationTask):
    """
    Example top-level process coordination task.

    Spawns a :class:`_ChildProcessTask` in a dedicated
    :class:`multiprocessing.Process` and forwards incoming messages to it.
    Results from the child are placed on this task's outbox.
    """

    def run(self) -> None:
        self.log("info", "ExampleProcessTask started")

        # Internal queues for child communication (multiprocessing-safe)
        child_inbox:  multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]
        child_outbox: multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]

        child_config = TaskConfig(
            task_id=f"{self.task_id}.child",
            kind=TaskKind.PROCESS,
            inbox=child_inbox,
            outbox=child_outbox,
            log_level=self._config.log_level,
        )
        child = _ChildProcessTask(child_config)
        child_proc = multiprocessing.Process(
            target=child.run,
            name=child_config.task_id,
            daemon=False,
        )
        child_proc.start()
        self.log("info", f"spawned child process '{child_config.task_id}' (pid={child_proc.pid})")

        while True:
            try:
                msg = self.get_item(timeout=5.0)
            except Exception:
                break
            if self._is_stop_signal(msg):
                self.log("info", "received stop — propagating to child")
                child_inbox.put(
                    Message.control(sender=self.task_id, signal=ControlSignal.STOP).to_json()
                )
                child_proc.join(timeout=10.0)
                break
            # Forward to child as JSON string
            child_inbox.put(msg.to_json())
            # Collect child result
            try:
                raw = child_outbox.get(timeout=5.0)
                result = Message.from_json(raw) if isinstance(raw, str) else raw
                self.put_item(result)
            except Exception:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleProcessTask finished")
