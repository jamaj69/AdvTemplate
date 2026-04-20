"""
examples/example_process.py
============================
Example process coordination task demonstrating the pipeline + lifecycle pattern.

Key behaviours
--------------
* Subclass :class:`SchedulerProcessTask` and override ``def run()`` (synchronous).
* Queues (``inbox``, ``outbox``, ``log_queue``) are injected via the
  constructor — the base class exposes them; ``run()`` never touches raw
  queues directly.
* ``get_item(timeout)`` returns any :class:`~customtypes.Message`.
  The run loop dispatches:

  - ``STOP``     → drain child, exit gracefully.
  - ``SHUTDOWN`` → terminate child process, exit immediately.
  - ``PAUSE``    → call ``self._wait_for_resume()``.
  - ``DATA``     → forward to child, collect result.

* All instance attributes must be **picklable** (the task object is serialised
  to cross the process boundary via ``ProcessPoolExecutor``).

Child tasks use :class:`~coordination.process_task.ProcessCoordinationTask`
with plain ``multiprocessing.Queue`` objects shared via fork.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
        Input Manager queue proxy.  Created internally by :meth:`start` if not
        provided.
    outbox:
        Output Manager queue proxy.  Created internally by :meth:`start` if not
        provided.
    log_queue:
        Optional shared log queue (Manager proxy) for centralised log collection.
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

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received — terminating child immediately")
                child_proc.terminate()
                child_proc.join(timeout=5.0)
                break

            if self._is_stop_signal(msg):
                self.log("info", "STOP received — propagating to child")
                child_inbox.put(
                    Message.control(sender=self.name, signal=ControlSignal.STOP).to_json()
                )
                child_proc.join(timeout=10.0)
                break

            if self._is_pause_signal(msg):
                self.log("info", "PAUSE received")
                if not self._wait_for_resume():
                    # SHUTDOWN arrived while paused
                    child_proc.terminate()
                    child_proc.join(timeout=5.0)
                    break
                continue

            # DATA — forward to child and collect result
            child_inbox.put(msg.to_json())
            try:
                raw = child_outbox.get(timeout=5.0)
                result = Message.from_json(raw) if isinstance(raw, str) else raw
                self.put_item(result)
            except Exception:
                self.log("warning", "timeout waiting for child result")

        self.log("info", "ExampleProcessTask finished")

