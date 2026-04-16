"""
coordination/base.py
====================
Abstract base class shared by all three coordination task strategies.

All concrete coordination tasks **must** extend one of the three derived
base classes (:class:`CoroutineCoordinationTask`,
:class:`ThreadCoordinationTask`, :class:`ProcessCoordinationTask`).
Subclasses are required to implement :meth:`run`.

Mandatory contract
------------------
* :meth:`log`      — structured logging through the standard ``logging`` module.
* :meth:`get_item` — receive the next :class:`~customtypes.Message` from the inbox.
* :meth:`put_item` — place a :class:`~customtypes.Message` on the outbox.
* :meth:`run`      — task body (abstract, must be overridden).
"""

from __future__ import annotations

import abc
import logging
from typing import Any

from customtypes import AnyQueue, Message, MessageKind, TaskConfig


class BaseCoordinationTask(abc.ABC):
    """
    Abstract foundation for all coordination tasks.

    Parameters
    ----------
    config:
        :class:`~customtypes.TaskConfig` instance carrying identity,
        queue handles and optional extra configuration.
    """

    def __init__(self, config: TaskConfig) -> None:
        self._config   : TaskConfig  = config
        self._task_id  : str         = config.task_id
        self._inbox    : AnyQueue    = config.inbox
        self._outbox   : AnyQueue    = config.outbox
        self._logger   : logging.Logger = self._build_logger(config)

    # ------------------------------------------------------------------
    # Logger factory
    # ------------------------------------------------------------------

    @staticmethod
    def _build_logger(config: TaskConfig) -> logging.Logger:
        logger = logging.getLogger(config.task_id)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(config.log_level)
        return logger

    # ------------------------------------------------------------------
    # Public helpers — available to every subclass
    # ------------------------------------------------------------------

    def log(
        self,
        level:   str,
        message: str,
        *,
        extra:   dict[str, Any] | None = None,
    ) -> None:
        """
        Emit a structured log record.

        Parameters
        ----------
        level:
            One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
        message:
            Human-readable log text.
        extra:
            Optional mapping of additional key/value pairs to include.
        """
        log_fn = getattr(self._logger, level.lower(), self._logger.info)
        if extra:
            log_fn("%s | extra=%s", message, extra)
        else:
            log_fn("%s", message)

    # ------------------------------------------------------------------
    # Abstract queue operations (sync signature; subclasses override)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def get_item(self) -> Any:
        """
        Retrieve the next :class:`~customtypes.Message` from the inbox queue.

        Returns
        -------
        Message
            The next item from the inbox.
        """

    @abc.abstractmethod
    def put_item(self, message: Message) -> Any:
        """
        Place a :class:`~customtypes.Message` onto the outbox queue.

        Parameters
        ----------
        message:
            The message to enqueue.
        """

    # ------------------------------------------------------------------
    # Task body (must be implemented by all concrete subclasses)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def run(self) -> Any:
        """
        Main task body.

        Implement the coordination logic here.  The method signature
        varies by strategy:

        * **Coroutine**: ``async def run(self) -> None``
        * **Thread**:    ``async def run(self) -> None``  (runs in executor)
        * **Process**:   ``def run(self) -> None``        (entry point)
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def task_id(self) -> str:
        """Unique identifier for this task instance."""
        return self._task_id

    @property
    def config(self) -> TaskConfig:
        """Read-only access to the task configuration."""
        return self._config

    def _is_stop_signal(self, message: Message) -> bool:
        """Return ``True`` if *message* carries a STOP control signal."""
        if message.kind != MessageKind.CONTROL:
            return False
        payload = message.payload or {}
        return payload.get("signal") == "stop"
