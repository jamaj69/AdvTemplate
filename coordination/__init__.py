"""
coordination
============
Base classes for the three coordination task execution strategies:

* :class:`~coordination.coroutine_task.CoroutineCoordinationTask` — asyncio coroutines
* :class:`~coordination.thread_task.ThreadCoordinationTask`       — threads driven by asyncio
* :class:`~coordination.process_task.ProcessCoordinationTask`     — subprocesses driven by asyncio

Scheduler
---------
* :class:`~coordination.scheduler.SchedulerTask` — lifecycle manager (start, monitor, control)
"""

from coordination.base import BaseCoordinationTask
from coordination.coroutine_task import CoroutineCoordinationTask
from coordination.thread_task import ThreadCoordinationTask
from coordination.process_task import ProcessCoordinationTask
from coordination.scheduler import SchedulerTask, SchedulerAsyncTask, SchedulerThreadTask, SchedulerProcessTask, SchedulerManager

__all__ = [
    "BaseCoordinationTask",
    "CoroutineCoordinationTask",
    "ThreadCoordinationTask",
    "ProcessCoordinationTask",
    "SchedulerTask",
    "SchedulerAsyncTask",
    "SchedulerThreadTask",
    "SchedulerProcessTask",
    "SchedulerManager",
]
