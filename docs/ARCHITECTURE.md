# Architecture — AdvTemplate Async Coordination System

## Overview

AdvTemplate is a Python skeleton for building **multi-layer async coordination
systems**. The top-level program (`main.py`) schedules coordination tasks in
three execution strategies via `SchedulerManager`. Each coordination task can,
in turn, spawn its own child tasks — creating a two-level (or deeper)
coordination hierarchy.

---

## Class Hierarchy

```
SchedulerTask (ABC)                       coordination/scheduler.py
├── name: str
├── log_level: str
├── results: list[Message]
├── status: Status          (property)
├── is_running: bool        (property)
├── exception: BaseException | None  (property)
├── log(level, message, *args) -> None
├── _is_stop_signal(msg) -> bool     (staticmethod)
├── start() -> None                  (abstract, async)
└── run() -> None                    (abstract)
    │
    ├── SchedulerAsyncTask
    │   ├── inbox:  asyncio.Queue[Message]
    │   ├── outbox: asyncio.Queue[Message]
    │   ├── get_item() -> Message           (async)
    │   ├── put_item(msg) -> None           (async)
    │   ├── start() -> None                (async, concrete)
    │   └── run() -> None                  (abstract, async)
    │
    ├── SchedulerThreadTask
    │   ├── inbox:  queue.Queue[Message]
    │   ├── outbox: queue.Queue[Message]
    │   ├── _stop_event: threading.Event
    │   ├── get_item(timeout=None) -> Message | None   (sync)
    │   ├── put_item(msg) -> None                      (sync)
    │   ├── start() -> None                            (async, concrete)
    │   └── run() -> None                              (abstract, sync)
    │
    └── SchedulerProcessTask
        ├── inbox:  Manager().Queue proxy   (set by start())
        ├── outbox: Manager().Queue proxy   (set by start())
        ├── _pre_inbox: list[str]
        ├── __getstate__() -> dict
        ├── __setstate__(state) -> None
        ├── feed(msg) -> None
        ├── get_item(timeout=None) -> Message   (sync)
        ├── put_item(msg) -> None               (sync)
        ├── start() -> None                     (async, concrete)
        └── run() -> None                       (abstract, sync)

SchedulerManager                          coordination/scheduler.py
├── name: str
├── status: Status      (property)
├── tasks: list[SchedulerTask]  (property)
├── add(task) -> None
├── get(name) -> SchedulerTask | None
└── run_all() -> None   (async)
```

Child tasks (spawned internally by coordinator `run()` methods) use:

```
BaseCoordinationTask (ABC)                coordination/base.py
├── _task_id: str
├── _inbox: AnyQueue
├── _outbox: AnyQueue
├── task_id: str            (property)
├── config: TaskConfig      (property)
├── _build_logger(config) -> Logger   (staticmethod)
├── log(level, message, *, extra) -> None
├── get_item() -> Any       (abstract)
├── put_item(msg) -> Any    (abstract)
├── run() -> Any            (abstract)
└── _is_stop_signal(msg) -> bool
    │
    ├── CoroutineCoordinationTask   coordination/coroutine_task.py
    ├── ThreadCoordinationTask      coordination/thread_task.py
    └── ProcessCoordinationTask     coordination/process_task.py
```

---

## Execution Strategies

| Strategy  | Scheduler class          | Queue type                          | Executor             |
|-----------|--------------------------|-------------------------------------|----------------------|
| Coroutine | `SchedulerAsyncTask`     | `asyncio.Queue`                     | isolated thread+loop |
| Thread    | `SchedulerThreadTask`    | `queue.Queue`                       | `ThreadPoolExecutor` |
| Process   | `SchedulerProcessTask`   | `multiprocessing.Manager().Queue()` | `ProcessPoolExecutor`|

All three are driven by `SchedulerManager.run_all()` which calls `task.start()`
sequentially, awaiting each to completion before starting the next.

> **Process queue note:** Plain `multiprocessing.Queue` cannot be pickled and
> therefore cannot be passed to a `ProcessPoolExecutor` worker.
> `multiprocessing.Manager().Queue()` returns a picklable proxy backed by a
> manager server process, solving the cross-process boundary problem.

---

## Package Structure

```
AdvTemplate/
│
├── main.py                       # Async entry point; wires tasks + SchedulerManager
├── customtypes.py                # Message, TaskConfig, TaskKind, MessageKind, ControlSignal
│
├── coordination/
│   ├── __init__.py               # Re-exports public classes
│   ├── scheduler.py              # SchedulerTask ABC, three subclasses, SchedulerManager
│   ├── base.py                   # BaseCoordinationTask (used by child tasks)
│   ├── coroutine_task.py         # CoroutineCoordinationTask
│   ├── thread_task.py            # ThreadCoordinationTask
│   └── process_task.py           # ProcessCoordinationTask
│
├── examples/
│   ├── __init__.py
│   ├── example_coroutine.py      # ExampleCoroutineTask (+ _ChildCoroutineTask)
│   ├── example_thread.py         # ExampleThreadTask    (+ _ChildThreadTask)
│   └── example_process.py        # ExampleProcessTask   (+ _ChildProcessTask)
│
└── docs/
    └── ARCHITECTURE.md           # This document
```

---

## Message Protocol

All tasks communicate exclusively through `Message` objects defined in
`customtypes.py`.

```
┌─────────────────────────────────────────────┐
│                  Message                    │
├─────────────────┬───────────────────────────┤
│ kind            │ MessageKind enum           │
│ sender          │ str                        │
│ payload         │ Any (JSON-serialisable)    │
│ correlation_id  │ str | None                 │
│ timestamp       │ ISO-8601 UTC str           │
└─────────────────┴───────────────────────────┘
```

Convenience constructors: `Message.data()`, `Message.control()`,
`Message.result()`, `Message.error()`.

Serialisation: `msg.to_json()` / `Message.from_json(raw)` — used automatically
by `SchedulerProcessTask.get_item()` / `put_item()` to cross the process boundary.

### Message kinds

| Kind      | Purpose                                      |
|-----------|----------------------------------------------|
| `DATA`    | Normal payload from producer to consumer     |
| `CONTROL` | Lifecycle signals (`start`, `stop`, …)       |
| `LOG`     | Log records forwarded through queues         |
| `ERROR`   | Error envelopes                              |
| `RESULT`  | Computation results returned to coordinator  |

### Control signals (`ControlSignal` enum)

`START`, `STOP`, `PAUSE`, `RESUME`, `SHUTDOWN`

Stop detection: `SchedulerTask._is_stop_signal(msg)` returns `True` when
`msg.kind == CONTROL` and `msg.payload["signal"] == "stop"`.

---

## Isolated Event Loop per Async Task

`SchedulerAsyncTask.start()` spins a daemon `threading.Thread` and calls
`asyncio.run(self.run())` inside it, giving the task its own fully isolated
event loop. The caller awaits it via `run_in_executor(None, thread.join)`.

```
┌─────────────────────────────────────────────────────────────────┐
│ Main thread — main asyncio loop                                 │
│                                                                 │
│  await manager.run_all()                                        │
│        │                                                        │
│        └─── await task.start()                                  │
│                   │                                             │
│                   └─ run_in_executor(None, thread.join) ◄─────┐ │
│                                                               │ │
│  ┌────────────────────────────────────────────────────────┐  │ │
│  │ Worker thread — isolated asyncio loop                  │  │ │
│  │  asyncio.run(self.run())                               │──┘ │
│  └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

After the thread joins, `start()` drains `outbox` into `task.results`
automatically. `main.py` only reads `task.results` — no manual queue draining.

---

## Two-Level Coordination Pattern

```
main.py
  │  task.inbox.put_nowait(msg)   (or task.feed(msg) for process)
  │  manager.add(task)
  │  await manager.run_all()
  ▼
SchedulerAsyncTask.start()
  │  spawns isolated thread+loop
  ▼
ExampleCoroutineTask.run()         (user-defined coordinator body)
  │
  ├─ asyncio.Queue (child_inbox)
  ├─ asyncio.Queue (child_outbox)
  │
  └─── asyncio.create_task ────► _ChildCoroutineTask.run()
                                        │
                                        └─ puts Message.result(…) on child_outbox
  │
  │  collects child results → puts Message.result(…) on self.outbox
  ▼
SchedulerAsyncTask.start()  — drains outbox → task.results
  ▼
main.py  reads task.results
```

The same two-level pattern applies to thread (`threading.Thread` +
`queue.Queue`) and process (`multiprocessing.Process` + Manager queues)
strategies.

---

## Data Flow

```
main.py
  │
  │  task.inbox.put_nowait(Message.data(…))   ← coroutine/thread
  │  task.feed(Message.data(…))               ← process only
  ▼
SchedulerTask.inbox
  │
  │  self.get_item()
  ▼
SchedulerTask.run()  ──►  (optional) child task via BaseCoordinationTask
  │
  │  self.put_item(Message.result(…))
  ▼
SchedulerTask.outbox
  │
  │  drained automatically by start() into task.results
  ▼
main.py  reads task.results
```

---

## Extending the System

### 1. Subclass the right scheduler class and override `run()`

```python
from coordination.scheduler import SchedulerAsyncTask
from customtypes import ControlSignal, Message

class MyTask(SchedulerAsyncTask):
    async def run(self) -> None:
        while True:
            msg = await self.get_item()
            if self._is_stop_signal(msg):
                break
            await self.put_item(
                Message.result(sender=self.name, payload={"ok": True})
            )
```

For thread tasks use `SchedulerThreadTask` with `def run(self)` (sync),
for process tasks use `SchedulerProcessTask` with `def run(self)` (sync).

### 2. Pre-feed messages and register

```python
manager = SchedulerManager(name="my-manager")

# Coroutine / Thread: write directly to inbox before run_all()
task = MyTask(name="my-task", log_level="DEBUG")
task.inbox.put_nowait(Message.data(sender="main", payload={"x": 1}))
task.inbox.put_nowait(Message.control(sender="main", signal=ControlSignal.STOP))
manager.add(task)

# Process only: use feed() because Manager queue doesn't exist until start()
proc_task = MyProcessTask(name="proc-task", log_level="DEBUG")
proc_task.feed(Message.data(sender="main", payload={"x": 1}))
proc_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))
manager.add(proc_task)
```

### 3. Run and read results

```python
await manager.run_all()

for msg in task.results:
    print(msg.payload)
```

### 4. Child tasks (optional, internal to `run()`)

Spawn children inside `run()` using `BaseCoordinationTask` subclasses
(`CoroutineCoordinationTask`, `ThreadCoordinationTask`,
`ProcessCoordinationTask`) — see `examples/` for reference implementations.

---

## Design Principles

* **Consistency** — identical public API (`get_item`, `put_item`, `log`) across all three execution strategies.
* **Type safety** — type aliases and annotated dataclasses in `customtypes.py`.
* **Serialisability** — all messages travel as JSON; no strategy requires shared memory.
* **Separation of concerns** — `main.py` only schedules; tasks handle their own children.
* **Loop isolation** — `SchedulerAsyncTask` runs in a dedicated thread+loop, fully isolated from the caller's loop.
* **No log leakage** — task loggers set `propagate = False` to prevent duplicate log output across loop/thread boundaries.
* **Automatic result collection** — `start()` drains the outbox into `task.results`; callers never touch queues directly.
* **Process-safe queuing** — `SchedulerProcessTask` uses `multiprocessing.Manager().Queue()` proxies and excludes the logger from pickling via `__getstate__`/`__setstate__`.
