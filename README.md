# AdvTemplate — Async Multi-Strategy Coordination System

A Python project template for building **hierarchical async coordination
systems** where a top-level `main.py` schedules coordination tasks that can
themselves spawn and coordinate sub-tasks — all using a consistent, typed
message-passing protocol.

---

## Features

- **Three execution strategies** for coordination tasks:
  - Pure `asyncio` coroutines
  - Worker threads (`ThreadPoolExecutor`)
  - Separate OS processes (`ProcessPoolExecutor`)
- **Uniform API** — every task exposes the same `log()`, `get_item()`, and
  `put_item()` interface regardless of strategy.
- **Typed messages** — all communication uses `Message` dataclasses serialised
  to/from JSON, ensuring compatibility across all boundaries.
- **Two-level coordination** — each top-level task can spawn and coordinate its
  own child tasks.
- **Isolated event loops** — each coordinator runs in a dedicated thread with
  its own `asyncio` loop, fully isolated from the main loop.
- **Extensible** — add new tasks by subclassing the appropriate base class and
  overriding `run()`.

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
2026-04-16T12:00:00 [INFO] main: coroutine result: {'index': 0, 'doubled': 0}
...
2026-04-16T12:00:00 [INFO] main: === Thread mode ===
...
2026-04-16T12:00:00 [INFO] main: thread result: {'index': 0, 'tripled': 0}
...
2026-04-16T12:00:00 [INFO] main: === Process mode ===
...
2026-04-16T12:00:00 [INFO] main: process result: {'index': 0, 'squared': 0}
2026-04-16T12:00:00 [INFO] main: All coordination tasks completed.
```

---

## Project Structure

```
AdvTemplate/
├── main.py                       # Async entry point
├── customtypes.py                # Shared types & message protocol
├── coordination/
│   ├── base.py                   # Abstract base class
│   ├── coroutine_task.py         # Coroutine strategy base
│   ├── thread_task.py            # Thread strategy base
│   └── process_task.py           # Process strategy base
├── examples/
│   ├── example_coroutine.py      # Coroutine task example
│   ├── example_thread.py         # Thread task example
│   └── example_process.py        # Process task example
└── docs/
    └── ARCHITECTURE.md           # Full architecture documentation
```

---

## How to Add a Custom Task

1. Choose a strategy and subclass the corresponding base:

    ```python
    # my_task.py
    from coordination.coroutine_task import CoroutineCoordinationTask
    from customtypes import Message

    class MyTask(CoroutineCoordinationTask):
        async def run(self) -> None:
            while True:
                msg = await self.get_item()
                if self._is_stop_signal(msg):
                    break
                # process msg.payload …
                await self.put_item(
                    Message.result(self.task_id, {"status": "ok"})
                )
    ```

2. Wire up queues and a `TaskConfig` in `main.py` or your own coordinator:

    ```python
    import asyncio
    from customtypes import TaskConfig, TaskKind

    inbox  = asyncio.Queue()
    outbox = asyncio.Queue()
    config = TaskConfig(
        task_id="my-task",
        kind=TaskKind.COROUTINE,
        inbox=inbox,
        outbox=outbox,
    )
    task = MyTask(config)
    asyncio.create_task(task.run())
    ```

3. Send messages to the task:

    ```python
    from customtypes import Message
    await inbox.put(Message.data(sender="main", payload={"value": 42}))
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

| Field            | Type          | Description                         |
|------------------|---------------|-------------------------------------|
| `kind`           | `MessageKind` | DATA / CONTROL / LOG / ERROR / RESULT |
| `sender`         | `str`         | Originating task ID                 |
| `payload`        | `Any`         | JSON-serialisable content           |
| `correlation_id` | `str \| None` | Links requests to responses         |
| `timestamp`      | `str`         | ISO-8601 UTC creation time          |

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a detailed description of
the design, isolated event loop model, data flow diagrams, and extension guidelines.

---

## License

MIT
