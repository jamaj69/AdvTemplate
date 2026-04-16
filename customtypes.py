"""
customtypes.py
==============
Shared custom types, dataclasses, type aliases and JSON helpers used across
all coordination task kinds (coroutine, thread, process).

All inter-task messages travel as instances of :class:`Message` and are
serialised to / deserialised from JSON so they can safely cross any boundary
(in-process queue, thread queue or multiprocessing queue).
"""

from __future__ import annotations

import asyncio
import json
import queue
import multiprocessing
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeAlias, Union


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TaskKind(str, Enum):
    """The three supported coordination task execution strategies."""
    COROUTINE = "coroutine"
    THREAD    = "thread"
    PROCESS   = "process"


class MessageKind(str, Enum):
    """Semantic category of a :class:`Message`."""
    DATA    = "data"      # normal payload
    CONTROL = "control"   # lifecycle signals (start, stop, pause …)
    LOG     = "log"       # log record forwarded through the queue
    ERROR   = "error"     # error payload
    RESULT  = "result"    # computation result sent back to coordinator


class ControlSignal(str, Enum):
    """Control signals carried by a :class:`Message` of kind CONTROL."""
    START    = "start"
    STOP     = "stop"
    PAUSE    = "pause"
    RESUME   = "resume"
    SHUTDOWN = "shutdown"


# ---------------------------------------------------------------------------
# Core message dataclass
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """
    Universal message envelope exchanged between coordination tasks.

    Attributes
    ----------
    kind:
        Semantic category — see :class:`MessageKind`.
    sender:
        Identifier of the task / component that created the message.
    payload:
        Arbitrary JSON-serialisable data.
    correlation_id:
        Optional identifier linking a request to its response.
    timestamp:
        ISO-8601 UTC creation time (auto-filled).
    """
    kind:           MessageKind
    sender:         str
    payload:        Any                   = field(default=None)
    correlation_id: str | None            = field(default=None)
    timestamp:      str                   = field(
                        default_factory=lambda: datetime.now(timezone.utc).isoformat()
                    )

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialise this message to a JSON string."""
        d = asdict(self)
        d["kind"] = self.kind.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> "Message":
        """Deserialise a JSON string produced by :meth:`to_json`."""
        d: dict[str, Any] = json.loads(raw)
        d["kind"] = MessageKind(d["kind"])
        return cls(**d)

    @classmethod
    def data(cls, sender: str, payload: Any, correlation_id: str | None = None) -> "Message":
        """Convenience constructor for a DATA message."""
        return cls(kind=MessageKind.DATA, sender=sender, payload=payload,
                   correlation_id=correlation_id)

    @classmethod
    def control(cls, sender: str, signal: ControlSignal) -> "Message":
        """Convenience constructor for a CONTROL message."""
        return cls(kind=MessageKind.CONTROL, sender=sender,
                   payload={"signal": signal.value})

    @classmethod
    def result(cls, sender: str, payload: Any, correlation_id: str | None = None) -> "Message":
        """Convenience constructor for a RESULT message."""
        return cls(kind=MessageKind.RESULT, sender=sender, payload=payload,
                   correlation_id=correlation_id)

    @classmethod
    def error(cls, sender: str, error: str, correlation_id: str | None = None) -> "Message":
        """Convenience constructor for an ERROR message."""
        return cls(kind=MessageKind.ERROR, sender=sender,
                   payload={"error": error}, correlation_id=correlation_id)


# ---------------------------------------------------------------------------
# Queue type aliases
# ---------------------------------------------------------------------------

#: Async queue for coroutine-based tasks (single-process, single-thread).
AsyncQueue: TypeAlias = asyncio.Queue[Message]

#: Thread-safe queue for thread-based tasks.
ThreadQueue: TypeAlias = queue.Queue[Message]

#: Process-safe queue for process-based tasks.
ProcessQueue: TypeAlias = multiprocessing.Queue  # type: ignore[type-arg]

#: Union of all supported queue flavours.
AnyQueue: TypeAlias = Union[AsyncQueue, ThreadQueue, ProcessQueue]


# ---------------------------------------------------------------------------
# Task configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class TaskConfig:
    """
    Configuration passed to every coordination task at construction time.

    Attributes
    ----------
    task_id:
        Unique identifier for this task instance.
    kind:
        Execution strategy — see :class:`TaskKind`.
    inbox:
        Queue from which the task reads incoming :class:`Message` objects.
    outbox:
        Queue to which the task writes outgoing :class:`Message` objects.
    log_level:
        Python logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    extra:
        Arbitrary JSON-serialisable extra configuration.
    """
    task_id:   str
    kind:      TaskKind
    inbox:     AnyQueue
    outbox:    AnyQueue
    log_level: str       = field(default="INFO")
    extra:     dict[str, Any] = field(default_factory=dict)
