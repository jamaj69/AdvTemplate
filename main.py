#!/usr/bin/env python3
"""
main.py
=======
Realistic RSS aggregator pipeline using all three AdvTemplate task types.

Three-phase sequential pipeline
--------------------------------
Phase 1 — RSSFetchTask  (SchedulerAsyncTask)
    Reads every URL from ``rssfeeds.conf`` and fetches them **all in
    parallel** with ``aiohttp``.  Raw XML is saved to ``tmp/raw/``.

Phase 2 — RSSParserTask  (SchedulerProcessTask)
    Receives file paths from Phase 1, parses each RSS / Atom feed inside a
    dedicated subprocess, and writes structured JSON to ``tmp/processed/``.

Phase 3 — APIServerTask  (SchedulerThreadTask)
    Starts a FastAPI / uvicorn HTTP server in a background thread and serves
    the processed data for ``API_SECS`` seconds (configurable below).

    Endpoints::

        GET /              server status
        GET /feeds         list of all parsed feeds
        GET /items         all news items  (?source= &limit=)
        GET /items/{idx}   single item by 0-based global index

Run
---
::

    python main.py
"""

from __future__ import annotations

import asyncio
import logging
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
API_SECS  = 60          # seconds to keep the API server running


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
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Three-phase RSS aggregator pipeline.

    Each phase uses a different execution strategy and a fresh
    :class:`SchedulerManager`.  Phases are sequential because each one
    produces data consumed by the next.

    * The **parallelism** inside Phase 1 comes from ``aiohttp`` running all
      HTTP requests as concurrent coroutines within the isolated asyncio loop
      of the ``SchedulerAsyncTask``.
    * The **CPU isolation** in Phase 2 comes from the ``SchedulerProcessTask``
      running XML parsing in a separate OS process.
    * The **non-blocking I/O** in Phase 3 comes from the ``SchedulerThreadTask``
      keeping the FastAPI / uvicorn server in a background thread while the
      main asyncio loop drives the shutdown timer.
    """

    # ── Phase 1 — Fetch  (SchedulerAsyncTask) ────────────────────────────
    # All HTTP requests run as concurrent coroutines inside a single aiohttp
    # session.  The task exits on its own once every URL has been processed.
    logger.info("════ Phase 1 — Fetch RSS feeds (AsyncTask) ════")

    fetch_task = RSSFetchTask(
        name="rss-fetch",
        feeds_conf=CONF_FILE,
        tmp_dir=TMP_DIR,
        log_level="INFO",
    )
    m1 = SchedulerManager(name="phase1-fetch", concurrent=False)
    m1.add(fetch_task)
    await m1.run_all()

    ok_results = [r for r in fetch_task.results if r.payload.get("ok")]
    logger.info(
        "Phase 1 done — %d/%d feeds saved to %s/raw/",
        len(ok_results), len(fetch_task.results), TMP_DIR,
    )

    # ── Phase 2 — Parse  (SchedulerProcessTask) ──────────────────────────
    # File paths from Phase 1 are pre-fed into the process task before
    # start().  The task runs XML parsing in a dedicated subprocess, then
    # writes structured JSON to tmp/processed/.
    logger.info("════ Phase 2 — Parse feeds (ProcessTask) ════")

    parse_task = RSSParserTask(
        name="rss-parse",
        tmp_dir=TMP_DIR,
        log_level="INFO",
    )
    for result in ok_results:
        parse_task.feed(
            Message.data(
                sender="main",
                payload={
                    "filepath": result.payload["filepath"],
                    "url":      result.payload["url"],
                },
            )
        )
    parse_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))

    m2 = SchedulerManager(name="phase2-parse", concurrent=False)
    m2.add(parse_task)
    await m2.run_all()

    total_items = sum(r.payload.get("items", 0) for r in parse_task.results)
    logger.info(
        "Phase 2 done — %d feeds parsed, %d total items → %s/processed/",
        len(parse_task.results), total_items, TMP_DIR,
    )

    # ── Phase 3 — Serve  (SchedulerThreadTask) ────────────────────────────
    # FastAPI + uvicorn run in a background daemon thread.  A concurrent
    # stopper coroutine sends STOP after API_SECS seconds while the main
    # event loop stays free — proving the thread is fully isolated.
    logger.info("════ Phase 3 — Serve API (ThreadTask, %d s) ════", API_SECS)

    api_task = APIServerTask(
        name="rss-api",
        tmp_dir=TMP_DIR,
        port=API_PORT,
        log_level="INFO",
    )
    m3 = SchedulerManager(name="phase3-api", concurrent=False)
    m3.add(api_task)

    async def _stopper() -> None:
        logger.info(
            "API will serve for %d s — visit http://127.0.0.1:%d",
            API_SECS, API_PORT,
        )
        await asyncio.sleep(API_SECS)
        logger.info("stopping API server …")
        api_task.inbox.put(
            Message.control(sender="main", signal=ControlSignal.STOP)
        )

    await asyncio.gather(m3.run_all(), _stopper())
    logger.info("All phases complete.")

    # -- Results -------------------------------------------------------------
    logger.info("--- async-task results (doubled) ---")
    for msg in async_task.results:
        logger.info("  %s", msg.payload)

    logger.info("--- thread-task results (tripled) ---")
    for msg in thread_task.results:
        logger.info("  %s", msg.payload)

    logger.info("--- process-task results (squared) ---")
    for msg in process_task.results:
        logger.info("  %s", msg.payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())

