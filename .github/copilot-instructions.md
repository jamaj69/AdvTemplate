# Copilot Instructions — AdvTemplate

## Context Restore Checklist

Read these files at the start of every session to restore full context:

1. `README.md` — project overview and quick-start
2. `docs/ARCHITECTURE.md` — full class hierarchy, isolated loop model, data flow, design principles
3. `main.py` — entry point; shows how SchedulerManager + tasks are wired
4. `customtypes.py` — `Message`, `TaskConfig`, `TaskKind`, type aliases
5. `coordination/scheduler.py` — `SchedulerTask` ABC, three subclasses, `SchedulerManager`
6. `coordination/base.py` — `BaseCoordinationTask` (used by internal child tasks)
7. `examples/example_*.py` — concrete task implementations

---

## Project Summary

**AdvTemplate** is a Python (3.11+, stdlib only) template for hierarchical
async coordination systems. Tasks communicate exclusively through typed,
JSON-serialisable `Message` objects. Three execution strategies share a
uniform `get_item()` / `put_item()` / `log()` API.

---

## Scheduler Class Hierarchy

```
SchedulerTask (ABC)                 coordination/scheduler.py
├── start()      ← called by SchedulerManager
├── run()        ← abstract; overridden by user
├── log(), _is_stop_signal()
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
└── SchedulerProcessTask
    ├── feed(msg)  — buffer before start()
    ├── get_item() / put_item()  — sync, JSON-serialised
    ├── __getstate__ / __setstate__  — pickle-safe
    └── start() — Manager queues + ProcessPoolExecutor

SchedulerManager
├── add(task), get(name), tasks, status
└── run_all()  — calls task.start() sequentially
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

| Strategy  | Scheduler class          | Queue type                          | Executor             |
|-----------|--------------------------|-------------------------------------|----------------------|
| Coroutine | `SchedulerAsyncTask`     | `asyncio.Queue`                     | isolated thread+loop |
| Thread    | `SchedulerThreadTask`    | `queue.Queue`                       | ThreadPoolExecutor   |
| Process   | `SchedulerProcessTask`   | `multiprocessing.Manager().Queue()` | ProcessPoolExecutor  |

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
