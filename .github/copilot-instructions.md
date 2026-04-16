# Copilot Instructions — AdvTemplate

## Context Restore Checklist

Read these files at the start of every session to restore full context:

1. `README.md` — project overview and quick-start
2. `docs/ARCHITECTURE.md` — execution strategies, isolated loop model, message protocol, design principles
3. `main.py` — entry point, `_run_in_isolated_loop()`, three demo schedulers
4. `customtypes.py` — `Message`, `TaskConfig`, `TaskKind`, type aliases
5. `coordination/base.py` — `BaseCoordinationTask`, `_build_logger` (note: `propagate = False`)
6. `coordination/coroutine_task.py` / `thread_task.py` / `process_task.py` — strategy base classes
7. `examples/example_*.py` — concrete task examples (coroutine, thread, process)

---

## Project Summary

**AdvTemplate** is a Python (3.11+, stdlib only) template for hierarchical
async coordination systems. Tasks communicate exclusively through typed,
JSON-serialisable `Message` objects. Three execution strategies share a
uniform `get_item()` / `put_item()` / `log()` API.

---

## Key Architecture Decisions

### 1. Isolated event loop per coordinator
`_run_in_isolated_loop(coro_fn)` in `main.py` spins a `threading.Thread`
and calls `asyncio.run(coro_fn())` inside it, giving each coordinator its own
isolated loop. The main loop awaits via `run_in_executor(None, thread.join)`.

```python
async def _run_in_isolated_loop(coro_fn) -> None:
    t = threading.Thread(target=lambda: asyncio.run(coro_fn()), daemon=True)
    t.start()
    await asyncio.get_running_loop().run_in_executor(None, t.join)
```

### 2. Process-mode queues use `multiprocessing.Manager().Queue()`
Plain `multiprocessing.Queue` cannot be pickled and fails when passed to a
`ProcessPoolExecutor` worker. The manager proxy is picklable.

```python
with multiprocessing.Manager() as manager:
    inbox  = manager.Queue()
    outbox = manager.Queue()
    ...
    with ProcessPoolExecutor(max_workers=1) as executor:
        await loop.run_in_executor(executor, task.run)
```

### 3. Task loggers disable propagation
`coordination/base.py` sets `logger.propagate = False` on every task logger
to prevent duplicate log output (each logger has its own `StreamHandler`).

### 4. Shebang
`main.py` starts with `#!/usr/bin/env python3` so it can be executed directly.

---

## Execution Strategy Reference

| Strategy  | Base class                   | Queue type                        | Scheduled via                              |
|-----------|------------------------------|-----------------------------------|--------------------------------------------|
| Coroutine | `CoroutineCoordinationTask`  | `asyncio.Queue`                   | `asyncio.create_task(task.run())`          |
| Thread    | `ThreadCoordinationTask`     | `queue.Queue`                     | `loop.run_in_executor(ThreadPoolExecutor)` |
| Process   | `ProcessCoordinationTask`    | `multiprocessing.Manager().Queue()` | `loop.run_in_executor(ProcessPoolExecutor)` |

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

1. Subclass the appropriate base class and override `run()`.
2. Create `TaskConfig` with matching queue types.
3. Schedule via `_run_in_isolated_loop()` (or directly inside an existing demo).
4. Send `Message.control(..., signal=ControlSignal.STOP)` to terminate.

---

## Running

```bash
python3 main.py
# or
./main.py
```
