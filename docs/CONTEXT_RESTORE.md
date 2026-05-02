# Context Restore — AdvTemplate

Read this file first whenever opening this workspace in a new session. It is a
compact map of the project intent, current architecture, and known sharp edges.

## What This Project Is

AdvTemplate is a Python 3.11+ template for hierarchical coordination systems.
Top-level scheduler tasks receive and emit typed `Message` objects, while each
task can also spawn child coordination tasks internally.

The important design constraint is consistency: coroutine, thread, process, and
process-pool strategies expose the same conceptual API:

```python
start()
run()
log(...)
get_item(...)
put_item(...)
```

All task-to-task payloads use `customtypes.Message`, which is JSON-serialisable
so messages can cross thread/process boundaries safely.

## Read Order

Use this order to regain full context without guessing:

1. `README.md` — public overview, commands, examples, message protocol.
2. `docs/ARCHITECTURE.md` — class hierarchy, lifecycle, data flow.
3. `customtypes.py` — `Message`, enums, `TaskConfig`, queue aliases.
4. `coordination/scheduler.py` — top-level scheduler task implementations.
5. `coordination/base.py` — base class for internal child tasks.
6. `coordination/coroutine_task.py`, `thread_task.py`, `process_task.py` — child task adapters.
7. `examples/example_*.py` — runnable patterns and concrete task bodies.
8. `main.py` — RSS pipeline entry point and current integration state.

## Core Files

- `coordination/scheduler.py`: `SchedulerTask`, `SchedulerAsyncTask`,
  `SchedulerThreadTask`, `SchedulerProcessTask`, `SchedulerProcessPoolTask`,
  and `SchedulerManager`.
- `customtypes.py`: `MessageKind`, `ControlSignal`, `TaskKind`, `Message`,
  `TaskConfig`, queue aliases.
- `examples/example_rss_demo.py`: reference RSS pipeline tasks:
  `RSSFetchTask`, `RSSParserTask`, `APIServerTask`.
- `main.py`: live concurrent RSS pipeline using the RSS demo task classes.

## Execution Strategies

| Strategy | Class | Queue | Runtime |
|---|---|---|---|
| Coroutine | `SchedulerAsyncTask` | `asyncio.Queue` | isolated thread + event loop |
| Thread | `SchedulerThreadTask` | `queue.Queue` | `ThreadPoolExecutor` |
| Process | `SchedulerProcessTask` | `multiprocessing.Manager().Queue()` | `ProcessPoolExecutor` |
| Process pool | `SchedulerProcessPoolTask` | `multiprocessing.Manager().Queue()` | persistent `multiprocessing.Process` workers |

`SchedulerManager.run_all()` runs tasks concurrently by default. Use
`SchedulerManager(..., concurrent=False)` only for deliberately batch-style
examples.

## Message Lifecycle

Use `Message.data(...)` for work items and `Message.result(...)` for outputs.
Use `Message.control(..., ControlSignal.STOP)` to request graceful exit.

Supported lifecycle signals:

- `STOP`: finish cleanly and exit.
- `SHUTDOWN`: exit immediately.
- `PAUSE`: suspend data processing.
- `RESUME`: continue after pause.

For process and process-pool tasks, feed messages with `task.feed(msg)` before
`start()` / `manager.run_all()`, because Manager queues are created during
startup unless externally injected.

For `SchedulerProcessPoolTask`, send one `STOP`; the pool controller broadcasts
one poison pill per worker.

## RSS Demo

The `main.py` RSS pipeline is:

1. `RSSFetchTask` fetches URLs from `rssfeeds.conf` with `aiohttp` and writes
   raw XML to `tmp/raw/` every `RSS_FETCH_INTERVAL_SECS` seconds, streaming each
   successful file path to a process-safe queue as soon as the fetch completes.
2. `RSSParserTask` runs at the same time, blocks on that queue, parses raw XML
   in a subprocess, and writes JSON to `tmp/processed/`.
3. `APIServerTask` starts with the pipeline and serves parsed data with
   FastAPI/uvicorn while new processed files appear.

The service runs until `CTRL+C`, `SIGINT`, or `SIGTERM`. Shutdown sends STOP to
the fetcher and API; the fetcher emits STOP downstream so the parser drains its
queue and exits cleanly.

Endpoints while the API task is running:

```text
GET /              status
GET /feeds         parsed feed metadata
GET /items         flattened items, with source and limit query params
GET /items/{idx}   one item by global index
```

## Known Current Edges

- `coordination/__init__.py` does not currently re-export
  `SchedulerProcessPoolTask`, even though the class exists in
  `coordination/scheduler.py`.
- `customtypes.TaskKind` lists coroutine/thread/process only. The process-pool
  scheduler exists, but there is no `TaskKind.PROCESS_POOL` enum value yet.
- `items.json`, `feeds.json`, `status.json`, `test_output.log`, and `tmp/` are
  runtime artifacts. `.gitignore` excludes `tmp/`, logs, and JSON outputs, but
  some generated files are already present in the workspace.
- Sandboxed environments may block `multiprocessing.Manager()` with
  `PermissionError: Operation not permitted`; process examples may need a normal
  local shell to run fully.

## Useful Commands

```bash
python3 -m py_compile main.py customtypes.py coordination/*.py examples/*.py
python3 examples/example_all_tasks.py
python3 examples/example_process_pool.py
python3 examples/example_rss_demo.py
```

The RSS demo requires optional dependencies:

```bash
pip install aiohttp fastapi uvicorn
```
