# Copilot Instructions — AdvTemplate

## ⚠️ MANDATORY: Context Restore on Session Start

**Before answering ANY project question**, read `docs/CONTEXT_RESTORE.md` first.
Then read the files below as needed for the requested change:

1. `README.md` — project overview and quick-start
2. `docs/ARCHITECTURE.md` — full class hierarchy, isolated loop model, data flow, design principles
3. `main.py` — RSS entry point and current integration state
4. `customtypes.py` — `Message`, `TaskConfig`, `TaskKind`, type aliases
5. `coordination/scheduler.py` — scheduler strategies and `SchedulerManager`
6. `coordination/base.py` — `BaseCoordinationTask` for internal child tasks
7. `examples/example_*.py` — concrete task implementations

> These files are the single source of truth for architecture, patterns, and conventions.
> Do **not** guess or infer behaviour that can be verified by reading them.

---

## Project Summary

**AdvTemplate** is a Python (3.11+, stdlib-only core) template for hierarchical
async coordination systems. Tasks communicate exclusively through typed,
JSON-serialisable `Message` objects. Four scheduler strategies share a uniform
`get_item()` / `put_item()` / `log()` API: coroutine, thread, process, and
persistent process pool.

---

## Scheduler Class Hierarchy

```
SchedulerTask (ABC)                 coordination/scheduler.py
├── name, log_level, results: list[Message]
├── status, is_running, exception  (properties)
├── start()      ← called by SchedulerManager (abstract, async)
├── run()        ← abstract; overridden by user
├── log(level, message, *args)
└── _is_stop_signal(msg) -> bool   (staticmethod)
    │
    ├── SchedulerAsyncTask
    │   ├── inbox / outbox : asyncio.Queue
    │   ├── get_item() / put_item()  — async
    │   └── start() — isolated thread + asyncio.run(self.run())
    │
    ├── SchedulerThreadTask
    │   ├── inbox / outbox : queue.Queue
    │   ├── _stop_event : threading.Event
    │   ├── get_item(timeout) / put_item()  — sync
    │   └── start() — ThreadPoolExecutor
    │
    ├── SchedulerProcessTask
        ├── _pre_inbox: list[str]
        ├── inbox / outbox : Manager().Queue proxy  (set by start())
        ├── feed(msg)  — buffer before start()
        ├── get_item(timeout) / put_item()  — sync, JSON-serialised
        ├── __getstate__ / __setstate__  — pickle-safe
        └── start() — Manager queues + ProcessPoolExecutor
    │
    └── SchedulerProcessPoolTask
        ├── num_workers: int
        ├── feed(msg)  — buffer before start()
        ├── controller inbox / worker work queue / shared outbox
        └── start() — persistent multiprocessing.Process workers

SchedulerManager
├── name, status (property), tasks (property)
├── add(task), get(name)
└── run_all()  — concurrent by default; pass concurrent=False for sequential phases
```

---

## Key Architecture Decisions

### 1. Isolated event loop per async coordinator
`SchedulerAsyncTask.start()` spins a `threading.Thread` and calls
`asyncio.run(self.run())` inside it, giving the task its own isolated loop.
The main loop awaits via `run_in_executor(None, thread.join)`.

### 2. Process-mode queues use `multiprocessing.Manager().Queue()`
Plain `multiprocessing.Queue` cannot be pickled and fails when passed to a
`ProcessPoolExecutor` worker. `SchedulerProcessTask.start()` creates the
manager context, assigns `self.inbox`/`self.outbox`, then pickles `self`
into the worker. The logger is excluded via `__getstate__`/`__setstate__`.

### 3. Pre-feed pattern for process tasks
Because the Manager queue doesn't exist until `start()` runs, messages must
be buffered with `task.feed(msg)` before calling `manager.run_all()`.

### 4. Task loggers disable propagation
`coordination/base.py` and `coordination/scheduler.py` set `logger.propagate = False`
on every task logger to prevent duplicate log output.

### 5. Results collected automatically
`start()` drains the outbox into `task.results: list[Message]` after `run()`
returns. `main.py` only reads `task.results` — no manual queue draining.

### 6. Shebang
`main.py` starts with `#!/usr/bin/env python3` so it can be executed directly.

---

## Execution Strategy Reference

| Strategy     | Scheduler class              | Queue type                          | Runtime              |
|--------------|------------------------------|-------------------------------------|----------------------|
| Coroutine    | `SchedulerAsyncTask`         | `asyncio.Queue`                     | isolated thread+loop |
| Thread       | `SchedulerThreadTask`        | `queue.Queue`                       | ThreadPoolExecutor   |
| Process      | `SchedulerProcessTask`       | `multiprocessing.Manager().Queue()` | ProcessPoolExecutor  |
| Process pool | `SchedulerProcessPoolTask`   | `multiprocessing.Manager().Queue()` | persistent processes |

---

## Message Kinds

| Kind      | Purpose                                      |
|-----------|----------------------------------------------|
| `DATA`    | Normal payload from producer to consumer     |
| `CONTROL` | Lifecycle signals (`start`, `stop`, …)       |
| `LOG`     | Log records forwarded through queues         |
| `ERROR`   | Error envelopes                              |
| `RESULT`  | Computation results returned to coordinator  |

---

## Adding a New Task (pattern)

```python
# 1. Subclass the right scheduler class and override run()
from coordination.scheduler import SchedulerAsyncTask
from customtypes import ControlSignal, Message

class MyTask(SchedulerAsyncTask):
    async def run(self) -> None:
        while True:
            msg = await self.get_item()
            if self._is_stop_signal(msg):
                break
            await self.put_item(Message.result(sender=self.name, payload={"ok": True}))

# 2. Instantiate, pre-feed, register
task = MyTask(name="my-task", log_level="DEBUG")
task.inbox.put_nowait(Message.data(sender="main", payload={"x": 1}))
task.inbox.put_nowait(Message.control(sender="main", signal=ControlSignal.STOP))
manager.add(task)

# 3. Run and read results
await manager.run_all()
for msg in task.results:
    print(msg.payload)
```

---

## Running

```bash
python3 main.py
# or
./main.py
```

Current note: `main.py` contains a stale result-printing block after the RSS
pipeline. For the clean RSS demo path, use:

```bash
python3 examples/example_rss_demo.py
```
