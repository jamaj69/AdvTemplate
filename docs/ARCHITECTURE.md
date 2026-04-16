# Architecture — AdvTemplate Async Coordination System

## Overview

AdvTemplate is a Python skeleton for building **multi-layer async coordination
systems**. The top-level program (`main.py`) schedules coordination tasks in
three execution strategies. Each coordination task can, in turn, spawn its own
child tasks — creating a two-level (or deeper) coordination hierarchy.

---

## Execution Strategies

| Strategy   | Base class                    | Queue type               | Scheduled via                              |
|------------|-------------------------------|--------------------------|--------------------------------------------|
| Coroutine  | `CoroutineCoordinationTask`   | `asyncio.Queue`          | `asyncio.create_task(task.run())`          |
| Thread     | `ThreadCoordinationTask`      | `queue.Queue`            | `loop.run_in_executor(ThreadPoolExecutor)` |
| Process    | `ProcessCoordinationTask`     | `multiprocessing.Queue`  | `loop.run_in_executor(ProcessPoolExecutor)`|

All three share the same public contract (see `BaseCoordinationTask`) and the
same message envelope (`Message`).

---

## Package Structure

```
AdvTemplate/
│
├── main.py                       # Async entry point; schedules top-level tasks
├── customtypes.py                # Shared types, dataclasses, JSON helpers
│
├── coordination/
│   ├── __init__.py               # Re-exports all public base classes
│   ├── base.py                   # AbstractBaseCoordinationTask
│   ├── coroutine_task.py         # CoroutineCoordinationTask
│   ├── thread_task.py            # ThreadCoordinationTask
│   └── process_task.py           # ProcessCoordinationTask
│
├── examples/
│   ├── __init__.py
│   ├── example_coroutine.py      # ExampleCoroutineTask (+ child)
│   ├── example_thread.py         # ExampleThreadTask    (+ child)
│   └── example_process.py        # ExampleProcessTask   (+ child)
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
│ sender          │ str (task_id)              │
│ payload         │ Any (JSON-serialisable)    │
│ correlation_id  │ str | None                 │
│ timestamp       │ ISO-8601 UTC str           │
└─────────────────┴───────────────────────────┘
```

Messages are serialised to/from JSON (`Message.to_json()` / `Message.from_json()`)
so they can traverse any queue type — including cross-process `multiprocessing.Queue`.

### Message kinds

| Kind      | Purpose                                      |
|-----------|----------------------------------------------|
| `DATA`    | Normal payload from producer to consumer     |
| `CONTROL` | Lifecycle signals (`start`, `stop`, …)       |
| `LOG`     | Log records forwarded through queues         |
| `ERROR`   | Error envelopes                              |
| `RESULT`  | Computation results returned to coordinator  |

---

## Base Class Contract

Every concrete task must inherit from one of the three strategy base classes
and override `run()`. The following three methods are provided by
`BaseCoordinationTask` and must not be overridden unless extending them:

| Method          | Description                                        |
|-----------------|----------------------------------------------------|
| `log(level, …)` | Structured logging via Python `logging`            |
| `get_item()`    | Retrieve next `Message` from the inbox queue       |
| `put_item(msg)` | Place a `Message` on the outbox queue              |

`run()` is the task body:
- For **coroutine** tasks: `async def run(self) -> None`
- For **thread** and **process** tasks: `def run(self) -> None`

---

## Two-Level Coordination Pattern

```
main.py  ──── asyncio.create_task ────► ExampleCoroutineTask (coordinator)
                                              │
                                              ├─ asyncio.Queue (inbox)
                                              ├─ asyncio.Queue (outbox)
                                              │
                                              └─── spawn() ──────► _ChildCoroutineTask
                                                                        │
                                                                        ├─ asyncio.Queue
                                                                        └─ asyncio.Queue
```

The same pattern applies to thread and process strategies — the coordinator
task spawns children using `threading.Thread` or `multiprocessing.Process`
respectively, wiring internal queues between them.

---

## Data Flow

```
main.py
  │
  │  puts Message.data(…)  on inbox
  ▼
CoordinationTask.inbox
  │
  │  get_item()
  ▼
CoordinationTask.run()  ──►  (optional) child task
  │
  │  put_item(Message.result(…))
  ▼
CoordinationTask.outbox
  │
  │  drained by main.py
  ▼
main.py  (logs / further processing)
```

---

## Extending the System

1. **Create a new coordination task** by subclassing the appropriate base:

    ```python
    from coordination.coroutine_task import CoroutineCoordinationTask
    from customtypes import Message

    class MyTask(CoroutineCoordinationTask):
        async def run(self) -> None:
            while True:
                msg = await self.get_item()
                if self._is_stop_signal(msg):
                    break
                await self.put_item(Message.result(self.task_id, {"done": True}))
    ```

2. **Wire up queues** in the coordinator (or `main.py`):

    ```python
    inbox  = asyncio.Queue()
    outbox = asyncio.Queue()
    config = TaskConfig(task_id="my-task", kind=TaskKind.COROUTINE,
                        inbox=inbox, outbox=outbox)
    task = MyTask(config)
    asyncio.create_task(task.run())
    ```

3. **Send messages**:

    ```python
    await inbox.put(Message.data(sender="main", payload={"value": 42}))
    ```

---

## Design Principles

* **Consistency** — identical public API across all three execution strategies.
* **Type safety** — type aliases and annotated dataclasses in `customtypes.py`.
* **Serialisability** — all messages travel as JSON; no strategy requires shared memory.
* **Separation of concerns** — `main.py` only schedules; tasks handle their own children.
* **Extensibility** — add new strategies by subclassing `BaseCoordinationTask`.
