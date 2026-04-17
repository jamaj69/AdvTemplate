"""
coordination/scheduler.py
==========================
Abstract base and three concrete scheduler task types, plus the manager.

Hierarchy::

    SchedulerTask (ABC)
    ├── SchedulerAsyncTask    — coroutine body runs in an isolated asyncio loop
    ├── SchedulerThreadTask   — synchronous body runs in a ThreadPoolExecutor
    └── SchedulerProcessTask  — synchronous body runs in a ProcessPoolExecutor

    SchedulerManager
        Owns and sequentially runs a collection of SchedulerTask instances.

Typical usage::

    manager = SchedulerManager(name="demo-manager")

    coroutine_task = ExampleCoroutineTask(name="coroutine-task-1", log_level="DEBUG")
    coroutine_task.inbox.put_nowait(Message.data(sender="main", payload={"x": 1}))
    coroutine_task.inbox.put_nowait(Message.control(sender="main", signal=ControlSignal.STOP))
    manager.add(coroutine_task)

    await manager.run_all()
    print(coroutine_task.results)
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Literal

from customtypes import ControlSignal, Message, MessageKind


Status = Literal["idle", "running", "done", "error"]


# ---------------------------------------------------------------------------
# Logger factory (shared by all task types)
# ---------------------------------------------------------------------------

def _make_logger(name: str, log_level: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(log_level)
    return logger


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class SchedulerTask(ABC):
    """
    Abstract base class for all scheduler task types.

    Subclass one of :class:`SchedulerAsyncTask`, :class:`SchedulerThreadTask`, or
    :class:`SchedulerProcessTask` and override :meth:`run` with the task body.
    The :class:`SchedulerManager` interacts exclusively through this interface.

    Parameters
    ----------
    name:
        Unique identifier used as the logger name and thread/process name.
    log_level:
        Standard Python logging level string (``DEBUG``, ``INFO``, …).
    """

    def __init__(self, name: str, log_level: str = "INFO") -> None:
        self.name:      str     = name
        self.log_level: str     = log_level
        self._logger            = _make_logger(name, log_level)
        self._status: Status    = "idle"
        self._exc: BaseException | None = None
        self.results: list[Message] = []

    # ------------------------------------------------------------------
    # Properties — uniform interface consumed by SchedulerManager
    # ------------------------------------------------------------------

    @property
    def status(self) -> Status:
        """Current lifecycle status: ``idle``, ``running``, ``done``, or ``error``."""
        return self._status

    @property
    def is_running(self) -> bool:
        """``True`` while the worker thread is executing."""
        return self._status == "running"

    @property
    def exception(self) -> BaseException | None:
        """The exception raised by the task body, or ``None`` on success."""
        return self._exc

    # ------------------------------------------------------------------
    # Shared helpers — available to every subclass
    # ------------------------------------------------------------------

    def log(self, level: str, message: str, *args: object) -> None:
        """Emit a log record at *level* through this task's private logger."""
        getattr(self._logger, level.lower())(message, *args)

    @staticmethod
    def _is_stop_signal(msg: Message) -> bool:
        """Return ``True`` if *msg* is a CONTROL/STOP message."""
        return (
            msg.kind == MessageKind.CONTROL
            and (msg.payload or {}).get("signal") == ControlSignal.STOP.value
        )

    # ------------------------------------------------------------------
    # Lifecycle entry-point — called by SchedulerManager
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """
        Set up all infrastructure (queues, executor) and run the task body.

        Must set :attr:`status` to ``"done"`` on success or ``"error"`` on
        failure and populate :attr:`results` before returning.
        """
        ...

    # ------------------------------------------------------------------
    # Task body — overridden by user subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self) -> None:  # type: ignore[return]  # async override allowed
        """
        Task body.  Override this in your subclass with the coordination logic.

        * For :class:`SchedulerAsyncTask` subclasses, declare as ``async def run``.
        * For :class:`SchedulerThreadTask` and :class:`SchedulerProcessTask`,
          declare as a plain ``def run``.
        """
        ...


# ---------------------------------------------------------------------------
# Coroutine strategy
# ---------------------------------------------------------------------------

class SchedulerAsyncTask(SchedulerTask):
    """
    Scheduler task whose body (:meth:`run`) is an ``async`` coroutine.

    The coroutine runs inside a dedicated OS thread that owns its own
    ``asyncio`` event loop, fully isolated from the caller's loop.

    Attributes
    ----------
    inbox:
        ``asyncio.Queue`` — deposit :class:`~customtypes.Message` objects here
        *before* calling :meth:`start`.  Use ``put_nowait`` from synchronous
        context or ``await put`` from async context.
    outbox:
        ``asyncio.Queue`` populated by the task body.  Drained into
        :attr:`results` automatically after :meth:`start` returns.
    """

    def __init__(self, name: str, log_level: str = "INFO") -> None:
        super().__init__(name, log_level)
        self.inbox:  asyncio.Queue[Message] = asyncio.Queue()
        self.outbox: asyncio.Queue[Message] = asyncio.Queue()

    # -- Queue helpers -------------------------------------------------------

    async def get_item(self) -> Message:
        """Await the next message from the inbox and log it."""
        msg = await self.inbox.get()
        self.log("debug", "get_item ← %s | extra=%r",
                 msg.kind.value, {"sender": msg.sender})
        return msg

    async def put_item(self, msg: Message) -> None:
        """Place *msg* on the outbox and log it."""
        self.log("debug", "put_item → %s | extra=%r",
                 msg.kind.value, {"receiver": "outbox"})
        await self.outbox.put(msg)

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Run :meth:`run` in an isolated thread+loop; drain outbox on completion."""
        self._status = "running"
        self._exc    = None
        self.results = []

        _exc_holder: list[BaseException] = []

        def _thread_target() -> None:
            try:
                asyncio.run(self.run())  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001
                _exc_holder.append(exc)

        t = threading.Thread(target=_thread_target, name=self.name, daemon=True)
        t.start()
        await asyncio.get_running_loop().run_in_executor(None, t.join)

        if _exc_holder:
            self._exc    = _exc_holder[0]
            self._status = "error"
            raise self._exc

        while not self.outbox.empty():
            self.results.append(self.outbox.get_nowait())

        self._status = "done"

    @abstractmethod
    async def run(self) -> None:  # type: ignore[override]
        """
        Override with the coroutine coordination logic.

        ``self.inbox``, ``self.outbox``, ``self.log()``, and
        ``self._is_stop_signal()`` are all available.
        """
        ...


# ---------------------------------------------------------------------------
# Thread strategy
# ---------------------------------------------------------------------------

class SchedulerThreadTask(SchedulerTask):
    """
    Scheduler task whose body (:meth:`run`) is a synchronous callable executed
    inside a :class:`~concurrent.futures.ThreadPoolExecutor` worker thread.

    Attributes
    ----------
    inbox:
        ``queue.Queue`` — deposit :class:`~customtypes.Message` objects here
        before calling :meth:`start`.
    outbox:
        ``queue.Queue`` populated by the task body.  Drained into
        :attr:`results` automatically after :meth:`start` returns.
    """

    def __init__(self, name: str, log_level: str = "INFO") -> None:
        super().__init__(name, log_level)
        self.inbox:  queue.Queue[Message] = queue.Queue()
        self.outbox: queue.Queue[Message] = queue.Queue()
        self._stop_event: threading.Event = threading.Event()

    # -- Queue helpers -------------------------------------------------------

    def get_item(self, timeout: float | None = None) -> Message | None:
        """
        Retrieve the next message from the inbox.

        Returns ``None`` on timeout so the caller can poll :attr:`_stop_event`.
        """
        try:
            msg = self.inbox.get(block=True, timeout=timeout)
            self.log("debug", "get_item ← %s | extra=%r",
                     msg.kind.value, {"sender": msg.sender})
            return msg
        except queue.Empty:
            return None

    def put_item(self, msg: Message) -> None:
        """Place *msg* on the outbox and log it."""
        self.log("debug", "put_item → %s", msg.kind.value)
        self.outbox.put(msg)

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Run :meth:`run` in a ThreadPoolExecutor; drain outbox on completion."""
        self._status = "running"
        self._exc    = None
        self.results = []
        self._stop_event.clear()

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                await loop.run_in_executor(executor, self.run)
            except BaseException as exc:  # noqa: BLE001
                self._exc    = exc
                self._status = "error"
                raise

        while not self.outbox.empty():
            self.results.append(self.outbox.get_nowait())

        self._status = "done"

    @abstractmethod
    def run(self) -> None:
        """
        Override with the synchronous coordination logic.

        ``self.inbox``, ``self.outbox``, ``self._stop_event``, ``self.log()``,
        and ``self._is_stop_signal()`` are all available.
        """
        ...


# ---------------------------------------------------------------------------
# Process strategy
# ---------------------------------------------------------------------------

class SchedulerProcessTask(SchedulerTask):
    """
    Scheduler task whose body (:meth:`run`) is a synchronous callable executed
    inside a :class:`~concurrent.futures.ProcessPoolExecutor` worker process.

    Because the task object is pickled to cross the process boundary all
    instance attributes must be picklable.  :attr:`inbox` and :attr:`outbox`
    are ``multiprocessing.Manager().Queue()`` proxies, which *are* picklable.
    The logger is excluded from pickling and rebuilt in the worker via
    :meth:`__getstate__` / :meth:`__setstate__`.

    Pre-feed messages with :meth:`feed` before calling :meth:`start`; they are
    loaded into the Manager inbox queue when :meth:`start` runs.
    """

    def __init__(self, name: str, log_level: str = "INFO") -> None:
        super().__init__(name, log_level)
        self._pre_inbox: list[str] = []
        # Assigned by start() before pickling:
        self.inbox  = None  # multiprocessing.Manager().Queue proxy
        self.outbox = None  # multiprocessing.Manager().Queue proxy

    # -- Pickling support ----------------------------------------------------

    def __getstate__(self) -> dict:
        """Exclude the logger (contains an RLock) from the pickle payload."""
        state = self.__dict__.copy()
        state.pop("_logger", None)
        return state

    def __setstate__(self, state: dict) -> None:
        """Rebuild the logger after unpickling in the worker process."""
        self.__dict__.update(state)
        self._logger = _make_logger(self.name, self.log_level)

    # -- Pre-feed ------------------------------------------------------------

    def feed(self, msg: Message) -> None:
        """
        Buffer *msg* for delivery into the inbox when :meth:`start` is called.

        Use this instead of writing directly to :attr:`inbox` because the
        Manager queue does not exist until :meth:`start` creates it.
        """
        self._pre_inbox.append(msg.to_json())

    # -- Queue helpers -------------------------------------------------------

    def get_item(self, timeout: float | None = None) -> Message:
        """Retrieve and deserialise the next message from the inbox."""
        raw = (
            self.inbox.get(timeout=timeout)
            if timeout is not None
            else self.inbox.get()
        )
        msg = Message.from_json(raw) if isinstance(raw, str) else raw
        self.log("debug", "get_item ← %s | extra=%r",
                 msg.kind.value, {"sender": msg.sender})
        return msg

    def put_item(self, msg: Message) -> None:
        """Serialise and place *msg* on the outbox."""
        self.log("debug", "put_item → %s", msg.kind.value)
        self.outbox.put(msg.to_json())

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """
        Create Manager queues, feed buffered messages, run :meth:`run` in a
        ProcessPoolExecutor, then drain outbox into :attr:`results`.
        """
        self._status = "running"
        self._exc    = None
        self.results = []

        loop = asyncio.get_running_loop()
        with multiprocessing.Manager() as mgr:
            self.inbox  = mgr.Queue()
            self.outbox = mgr.Queue()

            for raw in self._pre_inbox:
                self.inbox.put(raw)

            with ProcessPoolExecutor(max_workers=1) as executor:
                try:
                    await loop.run_in_executor(executor, self.run)
                except BaseException as exc:  # noqa: BLE001
                    self._exc    = exc
                    self._status = "error"
                    raise

            while not self.outbox.empty():
                raw = self.outbox.get_nowait()
                self.results.append(
                    Message.from_json(raw) if isinstance(raw, str) else raw
                )

        self._status = "done"

    @abstractmethod
    def run(self) -> None:
        """
        Override with the synchronous coordination logic (runs in a subprocess).

        ``self.inbox`` and ``self.outbox`` are Manager queue proxies; use
        :meth:`get_item` and :meth:`put_item` for typed message I/O.
        ``self.log()`` and ``self._is_stop_signal()`` are also available.
        """
        ...


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class SchedulerManager:
    """
    Registry and sequential runner for a collection of :class:`SchedulerTask` objects.

    Parameters
    ----------
    name:
        Human-readable label for this manager instance.

    Example
    -------
    ::

        manager = SchedulerManager(name="demo-manager")
        manager.add(ExampleCoroutineTask(name="coroutine-task-1", log_level="DEBUG"))
        manager.add(ExampleThreadTask(name="thread-task-1",    log_level="DEBUG"))
        await manager.run_all()
        print(manager.status)   # "done" or "error"
    """

    def __init__(self, name: str = "scheduler-manager") -> None:
        self.name: str = name
        self._tasks: list[SchedulerTask] = []
        self._status: Status = "idle"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> Status:
        """
        Aggregate status.

        * ``idle``    — no tasks have been started yet.
        * ``running`` — at least one task is currently executing.
        * ``error``   — a task raised an exception (run_all stopped early).
        * ``done``    — all tasks completed successfully.
        """
        return self._status

    @property
    def tasks(self) -> list[SchedulerTask]:
        """Ordered list of registered :class:`SchedulerTask` instances."""
        return list(self._tasks)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add(self, task: SchedulerTask) -> None:
        """
        Register a :class:`SchedulerTask` with this manager.

        Parameters
        ----------
        task:
            Task to append to the execution queue.
        """
        self._tasks.append(task)

    def get(self, name: str) -> SchedulerTask | None:
        """
        Look up a registered task by name.

        Returns ``None`` if no task with that name exists.
        """
        return next((t for t in self._tasks if t.name == name), None)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_all(self) -> None:
        """
        Run all registered tasks **sequentially**, in registration order.

        Each task's :meth:`~SchedulerTask.start` method handles its own
        infrastructure (isolated loop, executor, queues).  Execution stops
        immediately if any task raises an exception; :attr:`status` is set to
        ``"error"`` and the exception is re-raised.

        Raises
        ------
        BaseException
            Re-raises the first exception that escaped from any task.
        """
        self._status = "running"
        for task in self._tasks:
            await task.start()
            if task.status == "error":
                self._status = "error"
                raise task.exception  # type: ignore[misc]
        self._status = "done"
