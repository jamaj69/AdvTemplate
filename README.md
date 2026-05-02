# AdvTemplate — Async Multi-Strategy Coordination System

A Python project template for building **hierarchical async coordination
systems** where a top-level `main.py` feeds messages into typed coordination
tasks, managed by a `SchedulerManager`, using a consistent message-passing
protocol.

---

## Features

- **Four execution strategies**, each as a concrete `SchedulerTask` subclass:
  - `SchedulerAsyncTask` — pure `asyncio` coroutines in an isolated loop
  - `SchedulerThreadTask` — synchronous body in a `ThreadPoolExecutor`
  - `SchedulerProcessTask` — synchronous body in a `ProcessPoolExecutor`
  - `SchedulerProcessPoolTask` — N persistent worker processes; zero per-task spawn cost
- **Uniform API** — every task exposes `start()`, `run()`, `log()`,
  `get_item()`, and `put_item()` regardless of strategy.
- **Full lifecycle signals** — STOP, SHUTDOWN, PAUSE, RESUME handled uniformly.
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
- Standard-library-only core (no third-party deps required)
- Optional for the RSS demo: `aiohttp`, `fastapi`, `uvicorn`
  ```bash
  pip install aiohttp fastapi uvicorn
  ```

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/<your-username>/AdvTemplate.git
cd AdvTemplate

# (Optional) install deps for the RSS demo
pip install aiohttp fastapi uvicorn

# Run the live RSS aggregator pipeline
python main.py
```

Set `RSS_FETCH_INTERVAL_SECS` to control how often the fetch cycle repeats:

```bash
RSS_FETCH_INTERVAL_SECS=300 python main.py
```

The service runs until it receives `CTRL+C`, `SIGINT`, or `SIGTERM`. Shutdown is
graceful: the fetcher finishes its current cycle, sends STOP to the parser, the
parser drains queued items, and the API stops on its control message.

The demo starts all three RSS tasks together and connects them with queues:

| Task | Strategy | What it does |
|---|---|---|
| `RSSFetchTask` | `SchedulerAsyncTask` | Every `RSS_FETCH_INTERVAL_SECS`, fetches all feeds from `rssfeeds.conf` concurrently with `aiohttp`; streams successful file paths to the parser queue |
| `RSSParserTask` | `SchedulerProcessTask` | Blocks on a process-safe queue, parses RSS 2.0 / Atom feeds in a subprocess, and writes structured JSON to `tmp/processed/` |
| `APIServerTask` | `SchedulerThreadTask` | Serves `tmp/processed/*.json` via FastAPI on port 8000 while fetch and parse are still running |

**API endpoints** (while Phase 3 is running):

```
GET /              → server status  (feed count, total_items)
GET /feeds         → list of all parsed feeds
GET /items         → all news items  (?source=bbc &limit=20)
GET /items/{idx}   → single item by 0-based global index
```

---

## Project Structure

```
AdvTemplate/
├── main.py                        # Entry point — live concurrent RSS pipeline
├── customtypes.py                 # Shared types & message protocol
├── rssfeeds.conf                  # JSON array of RSS feed URLs
├── coordination/
│   ├── base.py                    # BaseCoordinationTask (for internal child tasks)
│   ├── coroutine_task.py          # CoroutineCoordinationTask (child tasks)
│   ├── thread_task.py             # ThreadCoordinationTask   (child tasks)
│   ├── process_task.py            # ProcessCoordinationTask  (child tasks)
│   └── scheduler.py               # SchedulerTask ABC + subclasses + SchedulerManager
├── examples/
│   ├── example_coroutine.py       # ExampleCoroutineTask  ← SchedulerAsyncTask
│   ├── example_thread.py          # ExampleThreadTask     ← SchedulerThreadTask
│   ├── example_process.py         # ExampleProcessTask    ← SchedulerProcessTask
│   ├── example_all_tasks.py       # Async/thread/process concurrent demo
│   ├── example_process_pool.py    # Persistent-worker pool demo
│   └── example_rss_demo.py        # RSSFetchTask, RSSParserTask, APIServerTask
└── docs/
    ├── ARCHITECTURE.md            # Full architecture documentation
    └── CONTEXT_RESTORE.md         # Fast workspace context restore checklist
```

---

## Running the Examples

```bash
# All-strategies concurrent demo (no external deps)
python examples/example_all_tasks.py

# Persistent process-pool demo (no external deps)
python examples/example_process_pool.py

# RSS aggregator examples (requires aiohttp + fastapi + uvicorn)
python examples/example_rss_demo.py
# or simply:
python main.py
```

All examples resolve their own imports and can be run from any directory.

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
                if self._is_pause_signal(msg):
                    await self._wait_for_resume()
                    continue
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

> For `SchedulerProcessTask` and `SchedulerProcessPoolTask`, use `task.feed(msg)`
> instead of `task.inbox.put_nowait(msg)` — the Manager queue does not exist until
> `start()` creates it.  For a pool, only **one** STOP signal is needed — the pool
> controller broadcasts a poison pill to every worker automatically.

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

### Lifecycle control signals

| Signal     | Meaning                                         |
|------------|-------------------------------------------------|
| `STOP`     | Finish current work, then exit cleanly          |
| `SHUTDOWN` | Exit immediately without draining               |
| `PAUSE`    | Suspend DATA processing until RESUME arrives    |
| `RESUME`   | Resume after a PAUSE                            |

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full class hierarchy,
isolated event loop model, data flow diagrams, and extension guidelines.

For future sessions, start with [docs/CONTEXT_RESTORE.md](docs/CONTEXT_RESTORE.md).
It is the short, project-specific context restoration file for this workspace.

---

## License

MIT
