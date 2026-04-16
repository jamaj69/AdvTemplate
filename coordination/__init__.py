"""
coordination
============
Base classes for the three coordination task execution strategies:

* :class:`~coordination.coroutine_task.CoroutineCoordinationTask` — asyncio coroutines
* :class:`~coordination.thread_task.ThreadCoordinationTask`       — threads driven by asyncio
* :class:`~coordination.process_task.ProcessCoordinationTask`     — subprocesses driven by asyncio
"""

from coordination.base import BaseCoordinationTask
from coordination.coroutine_task import CoroutineCoordinationTask
from coordination.thread_task import ThreadCoordinationTask
from coordination.process_task import ProcessCoordinationTask

__all__ = [
    "BaseCoordinationTask",
    "CoroutineCoordinationTask",
    "ThreadCoordinationTask",
    "ProcessCoordinationTask",
]
