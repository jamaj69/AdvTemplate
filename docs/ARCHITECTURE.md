# Architecture — AdvTemplate Async Coordination System

## Overview

AdvTemplate is a Python skeleton for building **multi-layer async coordination
systems**. The top-level program (`main.py`) schedules coordination tasks across
four execution strategies via `SchedulerManager`: coroutine, thread, process,
and persistent process pool. Each coordination task can, in turn, spawn its own
child tasks — creating a two-level (or deeper) coordination hierarchy.

The reference implementation in `main.py` is a realistic **live RSS aggregator
pipeline** that demonstrates the coroutine, process, and thread strategies
running together:

```
RSSFetchTask  (SchedulerAsyncTask)    — concurrent HTTP fetching via aiohttp
     │ process-safe Message queue
     ▼
RSSParserTask (SchedulerProcessTask)  — XML parsing in a subprocess

APIServerTask (SchedulerThreadTask)   — FastAPI/uvicorn HTTP API in a thread
```

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
├── _is_stop_signal(msg) -> bool      (staticmethod)
├── _is_shutdown_signal(msg) -> bool  (staticmethod)
├── _is_pause_signal(msg) -> bool     (staticmethod)
├── _is_resume_signal(msg) -> bool    (staticmethod)
├── start() -> None                   (abstract, async)
└── run() -> None                     (abstract)
    │
    ├── SchedulerAsyncTask
    │   ├── inbox:     asyncio.Queue[Message]
    │   ├── outbox:    asyncio.Queue[Message]
    │   ├── log_queue: asyncio.Queue[Message] | None
    │   ├── get_item() -> Message              (async)
    │   ├── put_item(msg) -> None              (async)
    │   ├── _wait_for_resume() -> None         (async)
    │   ├── start() -> None                   (async, concrete)
    │   └── run() -> None                     (abstract, async)
    │
    ├── SchedulerThreadTask
    │   ├── inbox:     queue.Queue[Message]
    │   ├── outbox:    queue.Queue[Message]
    │   ├── log_queue: queue.Queue[Message] | None
    │   ├── _stop_event: threading.Event
    │   ├── get_item(timeout=None) -> Message | None   (sync)
    │   ├── put_item(msg) -> None                      (sync)
    │   ├── _wait_for_resume() -> bool                 (sync)
    │   ├── start() -> None                            (async, concrete)
    │   └── run() -> None                              (abstract, sync)
    │
    └── SchedulerProcessTask
        ├── inbox:     Manager().Queue proxy   (set by start())
        ├── outbox:    Manager().Queue proxy   (set by start())
        ├── log_queue: Manager().Queue proxy | None
        ├── _pre_inbox: list[str]
        ├── __getstate__() -> dict
        ├── __setstate__(state) -> None
        ├── feed(msg) -> None
        ├── get_item(timeout=None) -> Message   (sync)
        ├── put_item(msg) -> None               (sync)
        ├── _wait_for_resume() -> bool          (sync)
        ├── start() -> None                     (async, concrete)
        └── run() -> None                       (abstract, sync)

    SchedulerProcessPoolTask                  coordination/scheduler.py
        ├── num_workers: int                  (default 4)
        ├── inbox:     Manager().Queue proxy   (pool controller inbox, set by start())
        ├── outbox:    Manager().Queue proxy   (shared result queue,  set by start())
        ├── _pre_inbox: list[str]
        ├── __getstate__() -> dict
        ├── __setstate__(state) -> None
        ├── feed(msg) -> None
        ├── get_item(timeout=None) -> Message   (sync, called by workers)
        ├── put_item(msg) -> None               (sync, called by workers)
        ├── _wait_for_resume() -> bool          (sync, called by workers)
        ├── _run_controller(pool_inbox, work_q) (routes lifecycle signals)
        ├── start() -> None                     (async, concrete)
        └── run() -> None                       (abstract, sync — runs in each worker)

SchedulerManager                          coordination/scheduler.py
├── name: str
├── concurrent: bool   (default True — asyncio.gather)
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

| Strategy     | Scheduler class               | Queue type                          | Executor              |
|--------------|-------------------------------|-------------------------------------|-----------------------|
| Coroutine    | `SchedulerAsyncTask`          | `asyncio.Queue`                     | isolated thread+loop  |
| Thread       | `SchedulerThreadTask`         | `queue.Queue`                       | `ThreadPoolExecutor`  |
| Process      | `SchedulerProcessTask`        | `multiprocessing.Manager().Queue()` | `ProcessPoolExecutor` |
| Process pool | `SchedulerProcessPoolTask`    | `multiprocessing.Manager().Queue()` | N `Process` objects   |

`SchedulerManager.run_all()` runs all tasks **concurrently** by default
(`concurrent=True`, backed by `asyncio.gather`).  Pass `concurrent=False` for
sequential execution.

> **Process queue note:** Plain `multiprocessing.Queue` cannot be pickled and
> therefore cannot be passed across the process boundary.
> `multiprocessing.Manager().Queue()` returns a picklable proxy backed by a
> manager server process, solving the cross-process boundary problem.

> **Spawn cost note:** `SchedulerProcessTask` spawns one OS process per
> `start()` call (≈ 100–300 ms overhead).  `SchedulerProcessPoolTask`
> pre-spawns *num_workers* processes once; they live until a STOP / SHUTDOWN
> signal is received, eliminating per-task spawn cost for high-throughput
> workloads.

---

## Package Structure

```
AdvTemplate/
│
├── main.py                        # Async entry point — live concurrent RSS pipeline
├── customtypes.py                 # Message, TaskConfig, TaskKind, MessageKind, ControlSignal
├── rssfeeds.conf                  # JSON array of RSS feed URLs
│
├── coordination/
│   ├── __init__.py                # Re-exports public classes
│   ├── scheduler.py               # SchedulerTask ABC, strategy subclasses, SchedulerManager
│   ├── base.py                    # BaseCoordinationTask (used by child tasks)
│   ├── coroutine_task.py          # CoroutineCoordinationTask
│   ├── thread_task.py             # ThreadCoordinationTask
│   └── process_task.py            # ProcessCoordinationTask
│
├── examples/
│   ├── __init__.py
│   ├── example_coroutine.py       # ExampleCoroutineTask (+ _ChildCoroutineTask)
│   ├── example_thread.py          # ExampleThreadTask    (+ _ChildThreadTask)
│   ├── example_process.py         # ExampleProcessTask   (+ _ChildProcessTask)
│   ├── example_all_tasks.py       # Async/thread/process in a single concurrent demo
│   ├── example_process_pool.py    # Persistent-worker pool demo (SchedulerProcessPoolTask)
│   └── example_rss_demo.py        # RSSFetchTask, RSSParserTask, APIServerTask
│
└── docs/
    └── ARCHITECTURE.md            # This document
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

| Signal     | Meaning                                         |
|------------|-------------------------------------------------|
| `STOP`     | Finish current work, then exit cleanly          |
| `SHUTDOWN` | Exit immediately without draining               |
| `PAUSE`    | Suspend DATA processing until RESUME arrives    |
| `RESUME`   | Resume after a PAUSE                            |

Detection helpers (all `staticmethod` on `SchedulerTask`):
`_is_stop_signal`, `_is_shutdown_signal`, `_is_pause_signal`, `_is_resume_signal`.

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
automatically (unless an external outbox was injected). `main.py` only reads
`task.results` — no manual queue draining.

---

## Concurrent Manager — Pipeline Topology

When `SchedulerManager(concurrent=True)` (the default), `run_all()` uses
`asyncio.gather` so all tasks start simultaneously:

```
await asyncio.gather(task_a.start(), task_b.start(), task_c.start())
```

This is required whenever tasks are wired into a pipeline and would otherwise
deadlock waiting on each other's queues.

To wire tasks into a pipeline, inject shared queues at construction:

```python
q_in  = asyncio.Queue()   # or queue.Queue for thread tasks
q_out = asyncio.Queue()

task_a = ProducerTask(name="producer", outbox=q_in)
task_b = ConsumerTask(name="consumer", inbox=q_in, outbox=q_out)
```

---

## Process Pool — Persistent Worker Pattern

`SchedulerProcessPoolTask` is the right choice when spawn overhead dominates
or when a queue of many small jobs must be processed by parallel workers.

```
external inbox (pool controller)
     ┌────────────────┐
     │ pool controller │  ← PAUSE/RESUME buffer
     └────────────────┘
             │   STOP/SHUTDOWN → broadcast N pills
             ▼   DATA → forward
        work_queue (shared)
     ┌───────▼───────┐
     │               │
   worker-0  worker-1  …  worker-N (each runs self.run())
     │               │
     └─────▼──────┘
         result_queue
             │
         task.results  (drained by start())
```

| Concern | Behaviour |
|---|---|
| Spawn | Workers spawned **once** at `start()`; no re-spawn between jobs |
| Load balancing | OS-level — workers compete on the shared work queue |
| Shutdown | Send **one** `STOP` / `SHUTDOWN` to the pool; controller broadcasts N pills |
| PAUSE/RESUME | Controller buffers messages; workers are not notified |
| Pickling | `run()` executes in subprocess — all attributes must be picklable; logger excluded via `__getstate__` / `__setstate__` |
| Pre-feed | Use `pool.feed(msg)` (same as `SchedulerProcessTask`) |

---

## Lifecycle / PAUSE-RESUME Pattern

The recommended `run()` loop for scheduler task types:

```python
# Async
async def run(self) -> None:
    while True:
        msg = await self.get_item()
        if self._is_shutdown_signal(msg): break
        if self._is_stop_signal(msg):     break
        if self._is_pause_signal(msg):
            await self._wait_for_resume()
            continue
        # … process msg …

# Thread / Process
def run(self) -> None:
    while not self._stop_event.is_set():   # thread only
        msg = self.get_item(timeout=0.5)
        if msg is None: continue           # timeout — poll stop_event
        if self._is_shutdown_signal(msg): break
        if self._is_stop_signal(msg):     break
        if self._is_pause_signal(msg):
            self._wait_for_resume()
            continue
        # … process msg …
```

`_wait_for_resume()` blocks the task's own thread/process until RESUME (or
SHUTDOWN) arrives — the main event loop and all other tasks remain unaffected.

---

## RSS Pipeline — Reference Implementation

`main.py` demonstrates a realistic live pipeline. Fetch, parse, and API tasks
are started by one concurrent `SchedulerManager`; the fetcher streams successful
files to the parser through a process-safe queue, and the API serves the
processed directory while both stages are still active.

The process is resident. `RSSFetchTask` repeats every
`RSS_FETCH_INTERVAL_SECS` seconds, while `RSSParserTask` blocks on its input
queue and `APIServerTask` blocks on its control queue. `CTRL+C`, `SIGINT`, and
`SIGTERM` are translated into STOP control messages so every task can exit
gracefully.

```
rssfeeds.conf
        │
        ▼
╔══════════════════════════════════════════╗
║  Phase 1 — RSSFetchTask                 ║  SchedulerAsyncTask
║  aiohttp: all URLs fetched concurrently  ║  isolated thread + loop
║  → tmp/raw/<hash>.xml                    ║
╚══════════════════════════════════════════╝
        │  Manager.Queue[Message{filepath, url}]
        ▼
╔══════════════════════════════════════════╗
║  Phase 2 — RSSParserTask                ║  SchedulerProcessTask
║  XML parsing in a subprocess (CPU-bound) ║  ProcessPoolExecutor
║  RSS 2.0 + Atom 1.0 supported           ║
║  → tmp/processed/<hash>.json            ║
╚══════════════════════════════════════════╝
╔══════════════════════════════════════════╗
║  Phase 3 — APIServerTask                ║  SchedulerThreadTask
║  FastAPI + uvicorn in a daemon thread    ║  ThreadPoolExecutor
║  GET /  /feeds  /items  /items/{idx}    ║
║  Reads tmp/processed while parse runs    ║
╚══════════════════════════════════════════╝
```

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
  │  (skipped if outbox was externally injected — pipeline mode)
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
            if self._is_shutdown_signal(msg): break
            if self._is_stop_signal(msg):     break
            if self._is_pause_signal(msg):
                await self._wait_for_resume()
                continue
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

# Process pool: use feed(); one STOP broadcasts to all workers automatically
pool_task = MyPoolTask(name="pool-task", num_workers=4, log_level="DEBUG")
for i in range(20):
    pool_task.feed(Message.data(sender="main", payload={"x": i}))
pool_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))
manager.add(pool_task)
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

* **Consistency** — identical public API (`get_item`, `put_item`, `log`) across all scheduler execution strategies.
* **Type safety** — type aliases and annotated dataclasses in `customtypes.py`.
* **Serialisability** — all messages travel as JSON; no strategy requires shared memory.
* **Separation of concerns** — `main.py` only schedules; tasks handle their own children.
* **Loop isolation** — `SchedulerAsyncTask` runs in a dedicated thread+loop, fully isolated from the caller's loop.
* **No log leakage** — task loggers set `propagate = False` to prevent duplicate log output across loop/thread boundaries.
* **Automatic result collection** — `start()` drains the outbox into `task.results`; callers never touch queues directly.
* **Process-safe queuing** — `SchedulerProcessTask` and `SchedulerProcessPoolTask` use `multiprocessing.Manager().Queue()` proxies and exclude the logger from pickling via `__getstate__`/`__setstate__`.
* **Zero spawn overhead option** — `SchedulerProcessPoolTask` pre-spawns *N* persistent workers; only one STOP signal is needed regardless of pool size.
* **Runnable from any directory** — all examples insert the project root into `sys.path` via `Path(__file__).resolve().parents[1]`.
