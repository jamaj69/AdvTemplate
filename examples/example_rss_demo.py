#!/usr/bin/env python3
"""
examples/example_rss_demo.py
==============================
Realistic RSS aggregator pipeline using all three scheduler task types:

* :class:`RSSFetchTask`  (SchedulerAsyncTask)    — concurrent aiohttp fetching
* :class:`RSSParserTask` (SchedulerProcessTask)  — subprocess XML parsing
* :class:`APIServerTask` (SchedulerThreadTask)   — FastAPI / uvicorn HTTP API

Data flow
---------
Phase 1  ``RSSFetchTask`` reads every URL from ``rssfeeds.conf``, fetches them
         **all in parallel** with ``aiohttp``, and saves raw content to
         ``<tmp_dir>/raw/<hash>.xml``.  It can run once or repeat forever every
         ``interval_seconds``.  One RESULT message per URL flows into
         ``task.results`` or, when a downstream queue is provided, streams
         immediately to the next task.

Phase 2  ``RSSParserTask`` runs in a dedicated subprocess.  It receives the
         file paths pre-fed from Phase 1, parses each RSS 2.0 or Atom feed,
         and writes structured JSON to ``<tmp_dir>/processed/<hash>.json``.
         Extracted fields: title, description, language, link, format,
         fetched_at, source_url, and per-item: title, description, link,
         pub_date, guid, source.

Phase 3  ``APIServerTask`` starts a FastAPI + uvicorn HTTP server in a
         background daemon thread and serves the processed data until a STOP
         signal arrives.

         Endpoints::

             GET /              — server status (feed count, total items)
             GET /feeds         — list all feeds (metadata only)
             GET /items         — all items, filterable by ?source= and ?limit=
             GET /items/{idx}   — single item by 0-based global index
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from contextlib import suppress
from typing import Any

import aiohttp

from coordination.scheduler import (
    SchedulerAsyncTask,
    SchedulerManager,
    SchedulerProcessTask,
    SchedulerThreadTask,
)
from customtypes import ControlSignal, Message, MessageKind


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — RSSFetchTask  (SchedulerAsyncTask)
# ─────────────────────────────────────────────────────────────────────────────

class RSSFetchTask(SchedulerAsyncTask):
    """
    Reads all feed URLs from *feeds_conf* (a JSON array with ``"url"`` keys)
    and fetches them **concurrently** with a single ``aiohttp.ClientSession``.

    Each response is written to ``<tmp_dir>/raw/<sha256_16chars>.xml``.
    One RESULT message per URL is emitted to the outbox::

        {"filepath": "/…/raw/abc123.xml", "url": "https://…", "ok": True}

    Failed fetches produce ``{"ok": False, "error": "…"}`` so they can be
    skipped in Phase 2 without crashing the pipeline.
    """

    def __init__(
        self,
        name: str,
        feeds_conf: str | Path,
        tmp_dir: str | Path,
        log_level: str = "INFO",
        *,
        downstream_queue: Any | None = None,
        emit_stop: bool = False,
        interval_seconds: float | None = None,
    ) -> None:
        super().__init__(name, log_level)
        self._feeds_conf = Path(feeds_conf)
        self._raw_dir    = Path(tmp_dir) / "raw"
        self._downstream_queue = downstream_queue
        self._emit_stop = emit_stop
        self._interval_seconds = interval_seconds

    async def run(self) -> None:
        self.log("info", "RSSFetchTask started")
        self._raw_dir.mkdir(parents=True, exist_ok=True)

        with self._feeds_conf.open(encoding="utf-8") as fh:
            feeds = json.load(fh)

        urls = [f["url"] for f in feeds if f.get("url")]
        self.log("info", "loaded %d feed URLs", len(urls))

        try:
            while True:
                await self._fetch_cycle(urls)
                if self._interval_seconds is None:
                    break

                self.log("info", "next fetch cycle in %.1f s",
                         self._interval_seconds)
                signal = await self._wait_for_control_or_timeout(
                    self._interval_seconds
                )
                if signal in {ControlSignal.STOP, ControlSignal.SHUTDOWN}:
                    self.log("info", "%s received — stopping fetch loop",
                             signal.value.upper())
                    break
                if signal == ControlSignal.PAUSE:
                    self.log("info", "PAUSE received")
                    with suppress(asyncio.CancelledError):
                        await self._wait_for_resume()
        finally:
            if self._emit_stop:
                self._emit_downstream(
                    Message.control(sender=self.name, signal=ControlSignal.STOP)
                )

        self.log("info", "RSSFetchTask finished")

    async def _fetch_cycle(self, urls: list[str]) -> None:
        self.log("info", "fetching %d feeds concurrently …", len(urls))
        connector = aiohttp.TCPConnector(limit=10)
        timeout   = aiohttp.ClientTimeout(total=30)
        headers   = {"User-Agent": "Mozilla/5.0 (compatible; RSSBot/1.0)"}

        ok = failed = 0
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        ) as session:
            tasks = [asyncio.create_task(self._fetch_result(session, url))
                     for url in urls]
            for completed in asyncio.as_completed(tasks):
                result = await completed
                result_msg = Message.result(sender=self.name, payload=result)
                await self.put_item(result_msg)

                if result.get("ok"):
                    ok += 1
                    self._emit_downstream(
                        Message.data(
                            sender=self.name,
                            payload={
                                "filepath": result["filepath"],
                                "url":      result["url"],
                            },
                        )
                    )
                else:
                    failed += 1
                    self.log("warning", "FAIL  %s — %s",
                             result.get("url"), result.get("error", ""))

        self.log("info", "fetch complete — ok=%d  failed=%d", ok, failed)

    async def _wait_for_control_or_timeout(
        self,
        timeout_seconds: float,
    ) -> ControlSignal | None:
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.get_item(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                return None

            if msg.kind != MessageKind.CONTROL:
                continue
            try:
                return ControlSignal((msg.payload or {}).get("signal"))
            except ValueError:
                continue

    async def _fetch_result(self, session: aiohttp.ClientSession, url: str) -> dict:
        try:
            result = await self._fetch_one(session, url)
            result["ok"] = True
            return result
        except Exception as exc:
            return {"filepath": None, "url": url, "ok": False, "error": str(exc)}

    def _emit_downstream(self, msg: Message) -> None:
        if self._downstream_queue is None:
            return
        payload = msg.to_json()
        put = getattr(self._downstream_queue, "put")
        put(payload)

    async def _fetch_one(self, session: aiohttp.ClientSession, url: str) -> dict:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        slug = hashlib.sha256(f"{url}{ts}".encode()).hexdigest()[:16]
        fp   = self._raw_dir / f"{slug}.xml"

        async with session.get(url) as resp:
            resp.raise_for_status()
            content = await resp.text(errors="replace")

        fp.write_text(content, encoding="utf-8")
        self.log("debug", "saved  %-55s → %s", url, fp.name)
        return {"filepath": str(fp), "url": url, "ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — RSSParserTask  (SchedulerProcessTask)
# ─────────────────────────────────────────────────────────────────────────────

class RSSParserTask(SchedulerProcessTask):
    """
    Runs in a dedicated subprocess (CPU-bound XML parsing is off the main loop).

    Receives ``Message.data`` items via inbox (pre-fed before ``start()``) with
    payload ``{"filepath": "…", "url": "…"}``, parses each file, and writes
    structured JSON to ``<tmp_dir>/processed/<same_stem>.json``.

    Supports RSS 2.0 and Atom 1.0 feeds.  All parsing helpers are defined as
    local nested functions inside :meth:`run` so the method is fully
    self-contained and picklable across process boundaries.

    Emits one RESULT per successfully parsed file::

        {"source": "https://…", "title": "BBC News", "items": 42,
         "output": "/…/processed/abc123.json"}
    """

    def __init__(
        self,
        name: str,
        tmp_dir: str | Path,
        log_level: str = "INFO",
        *,
        inbox=None,
        outbox=None,
        log_queue=None,
    ) -> None:
        super().__init__(name, log_level, inbox=inbox, outbox=outbox,
                         log_queue=log_queue)
        self._tmp_dir = str(tmp_dir)   # plain str — trivially picklable

    # ------------------------------------------------------------------ #
    # run() is called inside a subprocess — all imports are local so     #
    # the method is fully self-contained and does not rely on the parent  #
    # process having the same sys.path.                                   #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        from pathlib import Path as _P
        import json as _json
        import xml.etree.ElementTree as _ET
        from datetime import datetime as _dt, timezone as _tz
        import os

        processed_dir = _P(self._tmp_dir) / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        self.log("info", "RSSParserTask started (pid=%d)", os.getpid())

        # ── helpers ──────────────────────────────────────────────────── #

        def _text(el, tag: str, default: str = "") -> str:
            child = el.find(tag)
            return (child.text or "").strip() if child is not None else default

        def _parse_rss2(channel, source_url: str) -> dict:
            dc_ns = "http://purl.org/dc/elements/1.1/"
            feed: dict = {
                "format":      "rss2",
                "source_url":  source_url,
                "title":       _text(channel, "title"),
                "description": _text(channel, "description"),
                "link":        _text(channel, "link"),
                "language":    _text(channel, "language"),
                "fetched_at":  _dt.now(_tz.utc).isoformat(),
                "items":       [],
            }
            for item in channel.findall("item"):
                pub = _text(item, "pubDate") or \
                      item.findtext(f"{{{dc_ns}}}date", "")
                feed["items"].append({
                    "title":       _text(item, "title"),
                    "description": _text(item, "description"),
                    "link":        _text(item, "link"),
                    "pub_date":    pub,
                    "guid":        _text(item, "guid"),
                    "source":      feed["title"] or source_url,
                })
            return feed

        def _parse_atom(root, source_url: str, ns: str) -> dict:
            def t(n: str) -> str: return f"{{{ns}}}{n}"
            xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
            feed: dict = {
                "format":      "atom",
                "source_url":  source_url,
                "title":       _text(root, t("title")),
                "description": _text(root, t("subtitle")),
                "link":        source_url,
                "language":    root.get(xml_lang, ""),
                "fetched_at":  _dt.now(_tz.utc).isoformat(),
                "items":       [],
            }
            for entry in root.findall(t("entry")):
                link_el = entry.find(t("link"))
                href    = link_el.get("href", "") if link_el is not None else ""
                feed["items"].append({
                    "title":       _text(entry, t("title")),
                    "description": (_text(entry, t("summary"))
                                    or _text(entry, t("content"))),
                    "link":        href,
                    "pub_date":    (_text(entry, t("updated"))
                                    or _text(entry, t("published"))),
                    "guid":        _text(entry, t("id")),
                    "source":      feed["title"] or source_url,
                })
            return feed

        def _parse(filepath: str, source_url: str) -> dict:
            tree = _ET.parse(filepath)
            root = tree.getroot()
            atom_ns = "http://www.w3.org/2005/Atom"
            # Detect Atom by namespace in root tag or plain tag name
            if atom_ns in root.tag or root.tag == "feed":
                return _parse_atom(root, source_url, atom_ns)
            channel = root.find("channel")
            if channel is None:
                raise ValueError("no <channel> element found in RSS file")
            return _parse_rss2(channel, source_url)

        # ── main processing loop ─────────────────────────────────────── #

        while True:
            msg = self.get_item()

            if self._is_shutdown_signal(msg):
                self.log("info", "SHUTDOWN received")
                break
            if self._is_stop_signal(msg):
                self.log("info", "STOP received")
                break

            payload    = msg.payload or {}
            filepath   = payload.get("filepath")
            source_url = payload.get("url", "unknown")

            if not filepath or not _P(filepath).exists():
                self.log("warning", "skipping missing file: %s", filepath)
                continue

            try:
                parsed = _parse(filepath, source_url)
            except Exception as exc:
                self.log("warning", "parse error [%s]: %s",
                         _P(filepath).name, exc)
                continue

            out = processed_dir / (_P(filepath).stem + ".json")
            out.write_text(
                _json.dumps(parsed, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            n = len(parsed.get("items", []))
            self.put_item(
                Message.result(
                    sender=self.name,
                    payload={
                        "source": source_url,
                        "title":  parsed.get("title", ""),
                        "items":  n,
                        "output": str(out),
                    },
                )
            )
            self.log("info", "%-55s  %d items", source_url, n)

        self.log("info", "RSSParserTask finished")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — APIServerTask  (SchedulerThreadTask)
# ─────────────────────────────────────────────────────────────────────────────

class APIServerTask(SchedulerThreadTask):
    """
    Starts a **FastAPI + uvicorn** HTTP server in a background daemon thread,
    keeping the ``SchedulerThreadTask`` run-loop free to handle STOP signals.

    The server reads ``<tmp_dir>/processed/*.json`` on every request (no cache)
    so new files written by ``RSSParserTask`` appear immediately.

    Endpoints
    ---------
    ``GET /``
        Server status: feed count and total item count.
    ``GET /feeds``
        Metadata for every parsed feed (title, language, format, item_count, …).
    ``GET /items``
        All news items across every feed.  Optional query parameters:

        * ``source`` — filter by a substring of the feed's source URL
        * ``limit``  — max items to return (default 50)
    ``GET /items/{idx}``
        Single item by 0-based global index.

    The ``run()`` loop blocks until a STOP (or SHUTDOWN) signal arrives, sets
    ``server.should_exit = True``, and waits for uvicorn to drain connections.
    """

    def __init__(
        self,
        name: str,
        tmp_dir: str | Path,
        port: int = 8000,
        log_level: str = "INFO",
    ) -> None:
        super().__init__(name, log_level)
        self._tmp_dir = Path(tmp_dir)
        self._port    = port

    def run(self) -> None:
        import uvicorn as _uvicorn
        from fastapi import FastAPI as _FastAPI, HTTPException as _HTTPEx
        from pathlib import Path as _P
        import json as _json

        processed_dir = _P(self._tmp_dir) / "processed"

        # ── helpers ──────────────────────────────────────────────────── #

        def _load_all() -> list[dict]:
            if not processed_dir.exists():
                return []
            feeds = []
            for fp in sorted(processed_dir.glob("*.json")):
                try:
                    feeds.append(_json.loads(fp.read_text(encoding="utf-8")))
                except Exception:
                    pass
            return feeds

        # ── FastAPI app ───────────────────────────────────────────────── #

        app = _FastAPI(
            title="RSS Aggregator API",
            description="Serves feeds parsed by RSSParserTask.",
            version="1.0.0",
        )

        @app.get("/", summary="Server status")
        def status() -> dict:
            feeds = _load_all()
            return {
                "status":      "running",
                "feeds":       len(feeds),
                "total_items": sum(len(f.get("items", [])) for f in feeds),
            }

        @app.get("/feeds", summary="All parsed feeds (metadata)")
        def list_feeds() -> list:
            return [
                {
                    "title":      f.get("title", ""),
                    "source_url": f.get("source_url", ""),
                    "language":   f.get("language", ""),
                    "format":     f.get("format", ""),
                    "fetched_at": f.get("fetched_at", ""),
                    "item_count": len(f.get("items", [])),
                }
                for f in _load_all()
            ]

        @app.get("/items", summary="All news items — ?source= &limit=")
        def list_items(source: str = "", limit: int = 50) -> list:
            items: list[dict] = []
            for f in _load_all():
                if source and source.lower() not in f.get("source_url", "").lower():
                    continue
                for item in f.get("items", []):
                    items.append({
                        **item,
                        "_feed":     f.get("title", ""),
                        "_feed_url": f.get("source_url", ""),
                    })
            return items[:limit]

        @app.get("/items/{idx}", summary="Single item by global index")
        def get_item_by_idx(idx: int) -> dict:
            all_items = [
                {**item,
                 "_feed":     f.get("title", ""),
                 "_feed_url": f.get("source_url", "")}
                for f in _load_all()
                for item in f.get("items", [])
            ]
            if not 0 <= idx < len(all_items):
                raise _HTTPEx(status_code=404,
                              detail=f"index {idx} out of range "
                                     f"(total {len(all_items)})")
            return all_items[idx]

        # ── start uvicorn in a daemon thread ─────────────────────────── #
        #
        # server.run() calls asyncio.run(server.serve()) internally, which
        # creates an isolated event loop — fully independent of the main
        # asyncio loop.  Signal-handler installation is disabled because
        # add_signal_handler() is only allowed in the main OS thread.

        config = _uvicorn.Config(
            app, host="0.0.0.0", port=self._port, log_level="warning"
        )
        server = _uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # not the main thread

        srv_thread = threading.Thread(
            target=server.run, daemon=True, name=f"{self.name}-uvicorn"
        )
        srv_thread.start()
        self.log("info", "API server ready → http://127.0.0.1:%d", self._port)
        self.log("info", "  GET /              server status")
        self.log("info", "  GET /feeds         feed list")
        self.log("info", "  GET /items         all items  (?source= &limit=)")
        self.log("info", "  GET /items/0       first item")

        # ── wait for STOP / SHUTDOWN signal ──────────────────────────── #

        while not self._stop_event.is_set():
            msg = self.get_item(timeout=0.5)
            if msg is None:
                continue
            if self._is_stop_signal(msg) or self._is_shutdown_signal(msg):
                self.log("info", "stop signal received — shutting down uvicorn")
                break

        server.should_exit = True
        srv_thread.join(timeout=5.0)
        self.log("info", "APIServerTask finished")


# ─────────────────────────────────────────────────────────────────────────────
# Stand-alone entry point (for running this file directly)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from examples.example_rss_demo import RSSFetchTask, RSSParserTask, APIServerTask  # noqa: F811

    _ROOT      = Path(__file__).resolve().parents[1]
    _CONF_FILE = _ROOT / "rssfeeds.conf"
    _TMP_DIR   = _ROOT / "tmp"
    _API_PORT  = 8000
    _API_SECS  = 60

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    _logger = logging.getLogger("example_rss_demo")

    async def _main() -> None:
        # ── Phase 1 ─────────────────────────────────────────────────── #
        _logger.info("════ Phase 1 — Fetch RSS feeds (AsyncTask) ════")
        fetch_task = RSSFetchTask("rss-fetch", _CONF_FILE, _TMP_DIR)
        m1 = SchedulerManager("phase1-fetch", concurrent=False)
        m1.add(fetch_task)
        await m1.run_all()
        ok = [r for r in fetch_task.results if r.payload.get("ok")]
        _logger.info("Phase 1 done — %d/%d feeds saved to %s/raw/",
                     len(ok), len(fetch_task.results), _TMP_DIR)

        # ── Phase 2 ─────────────────────────────────────────────────── #
        _logger.info("════ Phase 2 — Parse feeds (ProcessTask) ════")
        parse_task = RSSParserTask("rss-parse", _TMP_DIR)
        for r in ok:
            parse_task.feed(Message.data(
                sender="main",
                payload={"filepath": r.payload["filepath"],
                         "url":      r.payload["url"]},
            ))
        parse_task.feed(Message.control(sender="main", signal=ControlSignal.STOP))
        m2 = SchedulerManager("phase2-parse", concurrent=False)
        m2.add(parse_task)
        await m2.run_all()
        total = sum(r.payload.get("items", 0) for r in parse_task.results)
        _logger.info("Phase 2 done — %d feeds, %d total items → %s/processed/",
                     len(parse_task.results), total, _TMP_DIR)

        # ── Phase 3 ─────────────────────────────────────────────────── #
        _logger.info("════ Phase 3 — Serve API (ThreadTask, %d s) ════", _API_SECS)
        api_task = APIServerTask("rss-api", _TMP_DIR, port=_API_PORT)
        m3 = SchedulerManager("phase3-api", concurrent=False)
        m3.add(api_task)

        async def _stopper() -> None:
            _logger.info("API will serve for %d s — http://127.0.0.1:%d",
                         _API_SECS, _API_PORT)
            await asyncio.sleep(_API_SECS)
            _logger.info("stopping API server …")
            api_task.inbox.put(Message.control(sender="main",
                                               signal=ControlSignal.STOP))

        await asyncio.gather(m3.run_all(), _stopper())
        _logger.info("all phases complete.")

    asyncio.run(_main())
