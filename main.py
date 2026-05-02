#!/usr/bin/env python3
"""
main.py
=======
Live RSS aggregator pipeline using all three AdvTemplate task types at once.

The intent of the project is a concurrent coordination graph, not a sequence of
batch phases.  This entry point starts the fetcher, parser, and API server in
one ``SchedulerManager(concurrent=True)`` run.  The tasks communicate through
typed ``Message`` objects carried by queues:

* ``RSSFetchTask`` (``SchedulerAsyncTask``) performs I/O-bound HTTP fetches
  concurrently with ``aiohttp`` and streams each successful file path to a
  process-safe queue as soon as that feed finishes.
* ``RSSParserTask`` (``SchedulerProcessTask``) runs in a subprocess and blocks
  on that queue, parsing files while the fetcher is still fetching the rest.
* ``APIServerTask`` (``SchedulerThreadTask``) serves ``tmp/processed/*.json``
  while the pipeline is running and for a short grace period after parsing
  completes.

Run:

    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import queue
import signal
from pathlib import Path

from coordination.scheduler import SchedulerManager
from customtypes import ControlSignal, Message
from examples.example_rss_demo import APIServerTask, RSSFetchTask, RSSParserTask


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT      = Path(__file__).parent
CONF_FILE = ROOT / "rssfeeds.conf"
TMP_DIR   = ROOT / "tmp"
API_PORT  = 8000
FETCH_INTERVAL_SECS = float(os.getenv("RSS_FETCH_INTERVAL_SECS", "300"))


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_message_queue(shared_queue) -> list[Message]:
    messages: list[Message] = []
    while True:
        try:
            raw = shared_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            break
        messages.append(Message.from_json(raw) if isinstance(raw, str) else raw)
    return messages


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Run the RSS pipeline as concurrent tasks connected by queues.

    A ``multiprocessing.Manager().Queue()`` is used between the async fetcher
    and process parser because it is safe to pickle into the subprocess.  The
    fetch task streams DATA messages to it and sends one STOP when all fetches
    have completed, allowing the parser to terminate naturally after draining
    all queued work.
    """

    with multiprocessing.Manager() as mp_manager:
        parse_inbox = mp_manager.Queue()
        parse_outbox = mp_manager.Queue()

        fetch_task = RSSFetchTask(
            name="rss-fetch",
            feeds_conf=CONF_FILE,
            tmp_dir=TMP_DIR,
            log_level="INFO",
            downstream_queue=parse_inbox,
            emit_stop=True,
            interval_seconds=FETCH_INTERVAL_SECS,
        )
        parse_task = RSSParserTask(
            name="rss-parse",
            tmp_dir=TMP_DIR,
            log_level="INFO",
            inbox=parse_inbox,
            outbox=parse_outbox,
        )
        api_task = APIServerTask(
            name="rss-api",
            tmp_dir=TMP_DIR,
            port=API_PORT,
            log_level="INFO",
        )

        manager = SchedulerManager(name="rss-live-pipeline", concurrent=True)
        manager.add(fetch_task)
        manager.add(parse_task)
        manager.add(api_task)

        loop = asyncio.get_running_loop()
        stop_requested = asyncio.Event()

        def _request_shutdown(signum: int | None = None) -> None:
            if stop_requested.is_set():
                return
            label = signal.Signals(signum).name if signum is not None else "manual"
            logger.info("shutdown requested by %s; signalling tasks", label)
            stop_requested.set()
            fetch_task.feed(
                Message.control(sender="main", signal=ControlSignal.STOP)
            )
            api_task.inbox.put(
                Message.control(sender="main", signal=ControlSignal.STOP)
            )

        registered_signals: list[int] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, _request_shutdown, signum)
                registered_signals.append(signum)
            except (NotImplementedError, RuntimeError):
                pass

        logger.info(
            "pipeline running; API at http://127.0.0.1:%d; fetch interval %.1f s",
            API_PORT,
            FETCH_INTERVAL_SECS,
        )

        try:
            await manager.run_all()
        except asyncio.CancelledError:
            _request_shutdown()
            raise
        finally:
            for signum in registered_signals:
                loop.remove_signal_handler(signum)

        fetch_ok = [r for r in fetch_task.results if (r.payload or {}).get("ok")]
        fetch_failed = len(fetch_task.results) - len(fetch_ok)
        parse_results = _drain_message_queue(parse_outbox)
        total_items = sum(r.payload.get("items", 0) for r in parse_results)

        logger.info(
            "Fetch complete: %d ok, %d failed",
            len(fetch_ok),
            fetch_failed,
        )
        logger.info(
            "Parse complete: %d feeds parsed, %d total items -> %s/processed/",
            len(parse_results),
            total_items,
            TMP_DIR,
        )
        logger.info("All live pipeline tasks complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
