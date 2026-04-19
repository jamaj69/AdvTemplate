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
        Owns and concurrently runs a collection of SchedulerTask instances.

Pipeline usage::

    q_in  = asyncio.Queue()
    q_mid = asyncio.Queue()
    q_out = asyncio.Queue()
    log_q = asyncio.Queue()

    manager = SchedulerManager(name="pipeline")
    manager.add(StageOneTask(name="s1", inbox=q_in,  outbox=q_mid, log_queue=log_q))
    manager.add(StageTwoTask(name="s2", inbox=q_mid, outbox=q_out, log_queue=log_q))

    await manager.run_all()          # all tasks run concurrently

Lifecycle signals consumed by every task's run() loop::

    ControlSignal.STOP     — exit the run loop gracefully
    ControlSignal.SHUTDOWN — exit immediately (no drain)
    ControlSignal.PAUSE    — suspend DATA processing until RESUME arrives
    ControlSignal.RESUME   — resume after a PAUSE
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


Status = Literal["idle", "running", "paused", "done", "error"]


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
    inbox:
        Optional input queue.  If *None* a new queue is created internally.
        Pass an external queue to wire tasks into a pipeline.
    outbox:
        Optional output queue.  If *None* a new queue is created internally.
        Pass an external queue to wire tasks into a pipeline.
    log_queue:
        Optional shared log queue.  When provided, every :meth:`log` call also
        emits a ``Message(kind=LOG)`` onto this queue for centralised log
        collection.  The queue type must match the task's execution strategy
        (``asyncio.Queue`` for async, ``queue.Queue`` for thread/process).
    """

    def __init__(
        self,
        name: str,
        log_level: str = "INFO",
        *,
        inbox=None,
        outbox=None,
        log_queue=None,
    ) -> None:
        self.name:      str     = name
        self.log_level: str     = log_level
        self._logger            = _make_logger(name, log_level)
        self._status: Status    = "idle"
        self._exc: BaseException | None = None
        self.results: list[Message] = []
        # Queues are assigned by subclasses (or injected here as a sentinel)
        self._injected_inbox    = inbox
        self._injected_outbox   = outbox
        self._injected_log_queue = log_queue

    # ------------------------------------------------------------------
    # Properties — uniform interface consumed by SchedulerManager
    # ------------------------------------------------------------------

    @property
    def status(self) -> Status:
        """Current lifecycle status."""
        return self._status

    @property
    def is_running(self) -> bool:
        """``True`` while the worker is executing."""
        return self._status == "running"

    @property
    def exception(self) -> BaseException | None:
        """The exception raised by the task body, or ``None`` on success."""
        return self._exc

    # ------------------------------------------------------------------
    # Shared helpers — available to every subclass
    # ------------------------------------------------------------------

    def log(self, level: str, message: str, *args: object) -> None:
        """
        Emit a log record through the task's private logger and, if a
        *log_queue* was provided, also place a LOG :class:`~customtypes.Message`
        on that queue (subclasses implement :meth:`_emit_log_message`).
        """
        getattr(self._logger, level.lower())(message, *args)
        self._emit_log_message(level, message % args if args else message)

    def _emit_log_message(self, level: str, text: str) -> None:
        """
        Place a ``Message(kind=LOG)`` on the log queue if one was configured.

        Override in each concrete subclass to match the queue's put semantics
        (async ``put_nowait`` vs sync ``put_nowait``).  Default is a no-op.
        """

    # ------------------------------------------------------------------
    # Control signal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stop_signal(msg: Message) -> bool:
        """Return ``True`` if *msg* is a CONTROL/STOP message."""
        return (
            msg.kind == MessageKind.CONTROL
            and (msg.payload or {}).get("signal") == ControlSignal.STOP.value
        )

    @staticmethod
    def _is_shutdown_signal(msg: Message) -> bool:
        """Return ``True`` if *msg* is a CONTROL/SHUTDOWN message."""
        return (
            msg.kind == MessageKind.CONTROL
            and (msg.payload or {}).get("signal") == ControlSignal.SHUTDOWN.value
        )

    @staticmethod
    def _is_pause_signal(msg: Message) -> bool:
        """Return ``True`` if *msg* is a CONTROL/PAUSE message."""
        return (
            msg.kind == MessageKind.CONTROL
            and (msg.payload or {}).get("signal") == ControlSignal.PAUSE.value
        )

    @staticmethod
    def _is_resume_signal(msg: Message) -> bool:
        """Return ``True`` if *msg* is a CONTROL/RESUME message."""
        return (
            msg.kind == MessageKind.CONTROL
            and (msg.payload or {}).get("signal") == ControlSignal.RESUME.value
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

        All queue I/O and lifecycle signals are handled via the base-class API:

        * ``await self.get_item()`` / ``self.get_item(timeout)`` — receive next message
        * ``await self.put_item(msg)`` / ``self.put_item(msg)``  — send a message
        * ``self.log(level, …)``                                — structured logging
        * ``self._is_stop_signal(msg)``                        — STOP detection
        * ``self._is_shutdown_signal(msg)``                    — SHUTDOWN detection
        * ``self._is_pause_signal(msg)``                       — PAUSE detection
        * ``self._is_resume_signal(msg)``                      — RESUME detection
        * ``await self._wait_for_resume()`` / ``self._wait_for_resume()`` — block until RESUME

        The recommended run() loop pattern::

            async def run(self) -> None:
                while True:
                    msg = await self.get_item()
                    if self._is_shutdown_signal(msg):
                        break
                    if self._is_stop_signal(msg):
                        break
                    if self._is_pause_signal(msg):
                        await self._wait_for_resume()
                        continue
                    # … process msg.payload …
                    await self.put_item(Message.result(sender=self.name, payload={…}))
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
        ``asyncio.Queue`` — source of incoming :class:`~customtypes.Message` objects.
        Injected at construction or created internally.
    outbox:
        ``asyncio.Queue`` — destination for outgoing messages.
        Injected at construction or created internally.
        Drained into :attr:`results` automatically after :meth:`run` returns.
    log_queue:
        Optional ``asyncio.Queue`` for centralised log collection.
    """

    def __init__(
        self,
        name: str,
        log_level: str = "INFO",
        *,
        inbox: asyncio.Queue[Message] | None = None,
        outbox: asyncio.Queue[Message] | None = None,
        log_queue: asyncio.Queue[Message] | None = None,
    ) -> None:
        super().__init__(name, log_level, inbox=inbox, outbox=outbox,
                         log_queue=log_queue)
        self.inbox:     asyncio.Queue[Message] = inbox     or asyncio.Queue()
        self.outbox:    asyncio.Queue[Message] = outbox    or asyncio.Queue()
        self.log_queue: asyncio.Queue[Message] | None = log_queue

    # -- Log queue emission --------------------------------------------------

    def _emit_log_message(self, level: str, text: str) -> None:
        if self.log_queue is not None:
            try:
                self.log_queue.put_nowait(
                    Message(
                        kind=MessageKind.LOG,
                        sender=self.name,
                        payload={"level": level.upper(), "message": text},
                    )
                )
            except asyncio.QueueFull:
                pass  # drop silently rather than block the logger

    # -- Queue helpers -------------------------------------------------------

    async def get_item(self) -> Message:
        """Await the next message from the inbox and log it."""
        msg = await self.inbox.get()
        self.log("debug", "get_item ← %s | sender=%s", msg.kind.value, msg.sender)
        return msg

    async def put_item(self, msg: Message) -> None:
        """Place *msg* on the outbox and log it."""
        self.log("debug", "put_item → %s", msg.kind.value)
        await self.outbox.put(msg)

    # -- Lifecycle control helpers -------------------------------------------

    async def _wait_for_resume(self) -> None:
        """
        Block the task loop until a CONTROL/RESUME message arrives on the inbox.

        All messages received while paused are discarded except RESUME and
        SHUTDOWN (which exits immediately).
        """
        self._status = "paused"
        self.log("info", "task paused — waiting for RESUME")
        while True:
            msg = await self.inbox.get()
            if self._is_resume_signal(msg):
                self._status = "running"
                self.log("info", "task resumed")
                return
            if self._is_shutdown_signal(msg):
                self._status = "running"
                self.log("info", "SHUTDOWN received while paused — raising")
                raise asyncio.CancelledError("SHUTDOWN while paused")

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

        # Only drain outbox if it was created internally (not shared pipeline queue)
        if self._injected_outbox is None:
            while not self.outbox.empty():
                self.results.append(self.outbox.get_nowait())

        self._status = "done"

    @abstractmethod
    async def run(self) -> None:  # type: ignore[override]
        """Override with the coroutine coordination logic."""
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
        ``queue.Queue`` — source of incoming messages.
        Injected at construction or created internally.
    outbox:
        ``queue.Queue`` — destination for outgoing messages.
        Injected at construction or created internally.
        Drained into :attr:`results` automatically after :meth:`run` returns.
    log_queue:
        Optional ``queue.Queue`` for centralised log collection.
    """

    def __init__(
        self,
        name: str,
        log_level: str = "INFO",
        *,
        inbox: queue.Queue[Message] | None = None,
        outbox: queue.Queue[Message] | None = None,
        log_queue: queue.Queue[Message] | None = None,
    ) -> None:
        super().__init__(name, log_level, inbox=inbox, outbox=outbox,
                         log_queue=log_queue)
        self.inbox:     queue.Queue[Message] = inbox     or queue.Queue()
        self.outbox:    queue.Queue[Message] = outbox    or queue.Queue()
        self.log_queue: queue.Queue[Message] | None = log_queue
        self._stop_event: threading.Event = threading.Event()

    # -- Log queue emission --------------------------------------------------

    def _emit_log_message(self, level: str, text: str) -> None:
        if self.log_queue is not None:
            try:
                self.log_queue.put_nowait(
                    Message(
                        kind=MessageKind.LOG,
                        sender=self.name,
                        payload={"level": level.upper(), "message": text},
                    )
                )
            except Exception:
                pass

    # -- Queue helpers -------------------------------------------------------

    def get_item(self, timeout: float | None = None) -> Message | None:
        """
        Retrieve the next message from the inbox.

        Returns ``None`` on timeout so the caller can poll :attr:`_stop_event`.
        """
        try:
            msg = self.inbox.get(block=True, timeout=timeout)
            self.log("debug", "get_item ← %s | sender=%s", msg.kind.value, msg.sender)
            return msg
        except queue.Empty:
            return None

    def put_item(self, msg: Message) -> None:
        """Place *msg* on the outbox and log it."""
        self.log("debug", "put_item → %s", msg.kind.value)
        self.outbox.put(msg)

    # -- Lifecycle control helpers -------------------------------------------

    def _wait_for_resume(self) -> bool:
        """
        Block until a CONTROL/RESUME message arrives on the inbox.

        Returns ``True`` when resumed, ``False`` when a SHUTDOWN was received
        (caller should exit the run loop).  All other messages are discarded.
        """
        self._status = "paused"
        self.log("info", "task paused — waiting for RESUME")
        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue
            if self._is_resume_signal(msg):
                self._status = "running"
                self.log("info", "task resumed")
                return True
            if self._is_shutdown_signal(msg):
                self._status = "running"
                self.log("info", "SHUTDOWN received while paused")
                return False
        return False

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

        if self._injected_outbox is None:
            while not self.outbox.empty():
                self.results.append(self.outbox.get_nowait())

        self._status = "done"

    @abstractmethod
    def run(self) -> None:
        """Override with the synchronous coordination logic."""
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

    .. note::
        When injecting external queues for pipeline wiring they **must** be
        ``multiprocessing.Manager().Queue()`` proxies created before constructing
        the tasks, with the manager context kept alive for the full run duration.
    """

    def __init__(
        self,
        name: str,
        log_level: str = "INFO",
        *,
        inbox=None,
        outbox=None,
        log_queue=None,
    ) -> None:
        super().__init__(name, log_level, inbox=inbox, outbox=outbox,
                         log_queue=log_queue)
        self._pre_inbox: list[str] = []
        # If injected, used as-is; otherwise assigned by start()
        self.inbox    = inbox   # multiprocessing.Manager().Queue proxy or None
        self.outbox   = outbox  # multiprocessing.Manager().Queue proxy or None
        self.log_queue = log_queue

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

    # -- Log queue emission --------------------------------------------------

    def _emit_log_message(self, level: str, text: str) -> None:
        if self.log_queue is not None:
            try:
                self.log_queue.put_nowait(
                    Message(
                        kind=MessageKind.LOG,
                        sender=self.name,
                        payload={"level": level.upper(), "message": text},
                    ).to_json()
                )
            except Exception:
                pass

    # -- Pre-feed ------------------------------------------------------------

    def feed(self, msg: Message) -> None:
        """
        Buffer *msg* for delivery into the inbox when :meth:`start` is called.

        Use this instead of writing directly to :attr:`inbox` because the
        Manager queue does not exist until :meth:`start` creates it (unless
        an external inbox was injected at construction time).
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
        self.log("debug", "get_item ← %s | sender=%s", msg.kind.value, msg.sender)
        return msg

    def put_item(self, msg: Message) -> None:
        """Serialise and place *msg* on the outbox."""
        self.log("debug", "put_item → %s", msg.kind.value)
        self.outbox.put(msg.to_json())

    # -- Lifecycle control helpers -------------------------------------------

    def _wait_for_resume(self) -> bool:
        """
        Block until CONTROL/RESUME arrives on the inbox.

        Returns ``True`` when resumed, ``False`` on SHUTDOWN.
        All other messages are discarded while paused.
        """
        self._status = "paused"
        self.log("info", "task paused — waiting for RESUME")
        while True:
            try:
                msg = self.get_item(timeout=1.0)
            except Exception:
                continue
            if self._is_resume_signal(msg):
                self._status = "running"
                self.log("info", "task resumed")
                return True
            if self._is_shutdown_signal(msg):
                self._status = "running"
                self.log("info", "SHUTDOWN received while paused")
                return False
        return False  # unreachable

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """
        Create Manager queues (if not injected), feed buffered messages,
        run :meth:`run` in a ProcessPoolExecutor, then drain outbox into
        :attr:`results`.
        """
        self._status = "running"
        self._exc    = None
        self.results = []

        loop = asyncio.get_running_loop()

        # Use injected queues if provided, else create them inside a Manager ctx
        if self._injected_inbox is not None and self._injected_outbox is not None:
            # Queues already exist — run without creating a new Manager context
            for raw in self._pre_inbox:
                self.inbox.put(raw)
            with ProcessPoolExecutor(max_workers=1) as executor:
                try:
                    await loop.run_in_executor(executor, self.run)
                except BaseException as exc:  # noqa: BLE001
                    self._exc    = exc
                    self._status = "error"
                    raise
            # Injected outbox — do not drain into results (shared)
            self._status = "done"
            return

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
        """Override with the synchronous coordination logic (runs in a subprocess)."""
        ...


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class SchedulerManager:
    """
    Registry and runner for a collection of :class:`SchedulerTask` objects.

    By default :meth:`run_all` runs all tasks **concurrently** via
    ``asyncio.gather``, so tasks can form a pipeline without deadlocking on
    each other's queues.  Pass ``concurrent=False`` to restore the original
    sequential behaviour.

    Parameters
    ----------
    name:
        Human-readable label for this manager instance.
    concurrent:
        If ``True`` (default) all tasks are started simultaneously.
        If ``False`` tasks are started one by one in registration order.
    """

    def __init__(self, name: str = "scheduler-manager", *, concurrent: bool = True) -> None:
        self.name: str = name
        self.concurrent: bool = concurrent
        self._tasks: list[SchedulerTask] = []
        self._status: Status = "idle"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> Status:
        """Aggregate lifecycle status."""
        return self._status

    @property
    def tasks(self) -> list[SchedulerTask]:
        """Ordered list of registered :class:`SchedulerTask` instances."""
        return list(self._tasks)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add(self, task: SchedulerTask) -> None:
        """Register a task with this manager."""
        self._tasks.append(task)

    def get(self, name: str) -> SchedulerTask | None:
        """Look up a registered task by name.  Returns ``None`` if not found."""
        return next((t for t in self._tasks if t.name == name), None)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_all(self) -> None:
        """
        Run all registered tasks.

        When *concurrent* is ``True`` (default) all tasks start simultaneously
        via ``asyncio.gather`` — required for pipeline topologies where tasks
        block on each other's queues.

        When *concurrent* is ``False`` tasks run sequentially in registration
        order; the first exception stops execution immediately.

        Raises
        ------
        BaseException
            Re-raises the first exception that escaped from any task.
        """
        self._status = "running"
        if self.concurrent:
            try:
                await asyncio.gather(*(task.start() for task in self._tasks))
            except BaseException as exc:  # noqa: BLE001
                self._status = "error"
                raise
        else:
            for task in self._tasks:
                await task.start()
                if task.status == "error":
                    self._status = "error"
                    raise task.exception  # type: ignore[misc]
        self._status = "done"


