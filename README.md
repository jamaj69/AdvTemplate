# AdvTemplate — Async Multi-Strategy Coordination System

A Python project template for building **hierarchical async coordination
systems** where a top-level `main.py` feeds messages into typed coordination
tasks, managed by a `SchedulerManager`, using a consistent message-passing
protocol.

---

## Features

- **Three execution strategies**, each as a concrete `SchedulerTask` subclass:
  - `SchedulerAsyncTask` — pure `asyncio` coroutines in an isolated loop
  - `SchedulerThreadTask` — synchronous body in a `ThreadPoolExecutor`
  - `SchedulerProcessTask` — synchronous body in a `ProcessPoolExecutor`
- **Uniform API** — every task exposes `start()`, `run()`, `log()`,
  `get_item()`, and `put_item()` regardless of strategy.
- **Self-contained tasks** — each task owns its queues, executor, and
  serialisation; `main.py` only feeds messages and reads `task.results`.
- **Typed messages** — all communication uses `Message` dataclasses serialised
  to/from JSON, ensuring compatibility across all boundaries.
- **Two-level coordination** — each top-level task can spawn and coordinate its
  own child tasks internally.
- **Isolated event loops** — `SchedulerAsyncTask` runs in a dedicated thread
  with its own `asyncio` loop, fully isolated from the main loop.
- **Extensible** — add new tasks by subclassing the appropriate scheduler class
  and overriding `run()`.

---

## Requirements

- Python 3.11+
- No third-party dependencies (standard library only)

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/<your-username>/AdvTemplate.git
cd AdvTemplate

# Run the demo
python main.py
```

Expected output:

```
2026-04-16T12:00:00 [INFO] main: === Coroutine mode ===
...
2026-04-16T12:00:00 [INFO] main: coroutine-task-1 result: {'index': 0, 'doubled': 0}
...
2026-04-16T12:00:00 [INFO] main: === Thread mode ===
...
2026-04-16T12:00:00 [INFO] main: thread-task-1 result: {'index': 0, 'tripled': 0}
...
2026-04-16T12:00:00 [INFO] main: === Process mode ===
...
2026-04-16T12:00:00 [INFO] main: process-task-1 result: {'index': 0, 'squared': 0}
2026-04-16T12:00:00 [INFO] main: Manager 'demo-manager' status: done
```

---

## Project Structure

```
AdvTemplate/
├── main.py                       # Entry point; builds SchedulerManager, feeds messages
├── customtypes.py                # Shared types & message protocol
├── coordination/
│   ├── base.py                   # BaseCoordinationTask (for internal child tasks)
│   ├── coroutine_task.py         # CoroutineCoordinationTask (child tasks)
│   ├── thread_task.py            # ThreadCoordinationTask   (child tasks)
│   ├── process_task.py           # ProcessCoordinationTask  (child tasks)
│   └── scheduler.py             # SchedulerTask ABC + subclasses + SchedulerManager
├── examples/
│   ├── example_coroutine.py      # ExampleCoroutineTask  ← SchedulerAsyncTask
│   ├── example_thread.py         # ExampleThreadTask     ← SchedulerThreadTask
│   └── example_process.py        # ExampleProcessTask    ← SchedulerProcessTask
└── docs/
    └── ARCHITECTURE.md           # Full architecture documentation
```

---

## How to Add a Custom Task

1. Choose a strategy and subclass the corresponding scheduler class:

    ```python
    # my_task.py
    from coordination.scheduler import SchedulerAsyncTask
    from customtypes import Message

    class MyTask(SchedulerAsyncTask):
        async def run(self) -> None:
            while True:
                msg = await self.get_item()
                if self._is_stop_signal(msg):
                    break
                await self.put_item(
                    Message.result(sender=self.name, payload={"status": "ok"})
                )
    ```

2. Instantiate, pre-feed, and register with the manager:

    ```python
    from coordination.scheduler import SchedulerManager
    from customtypes import ControlSignal, Message

    task = MyTask(name="my-task", log_level="DEBUG")
    task.inbox.put_nowait(Message.data(sender="main", payload={"value": 42}))
    task.inbox.put_nowait(Message.control(sender="main", signal=ControlSignal.STOP))

    manager = SchedulerManager(name="my-manager")
    manager.add(task)
    await manager.run_all()
    ```

3. Read results:

    ```python
    for msg in task.results:
        print(msg.payload)
    ```

---

## Message Protocol

All messages are instances of the `Message` dataclass and can be serialised
to/from JSON:

```python
msg = Message.data(sender="producer", payload={"key": "value"})
json_str = msg.to_json()
msg2 = Message.from_json(json_str)
```

| Field            | Type          | Description                           |
|------------------|---------------|---------------------------------------|
| `kind`           | `MessageKind` | DATA / CONTROL / LOG / ERROR / RESULT |
| `sender`         | `str`         | Originating task name                 |
| `payload`        | `Any`         | JSON-serialisable content             |
| `correlation_id` | `str \| None` | Links requests to responses           |
| `timestamp`      | `str`         | ISO-8601 UTC creation time            |

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full class hierarchy,
isolated event loop model, data flow diagrams, and extension guidelines.

---

## License

MIT

