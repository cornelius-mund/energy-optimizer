"""Logging configuration for the Energy Optimizer service."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Final

from energy_optimizer.config import ConfigurationError

LOG_LEVELS: Final[dict[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "NOTSET": logging.NOTSET,
}
LOG_LEVEL_NAMES: Final[tuple[str, ...]] = tuple(LOG_LEVELS)
_HANDLER_MARKER = "_energy_optimizer_handler"
_LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_EVENT_PREFIX = re.compile(r"^event=(\S+)")
_NORMALIZED_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    "httpx",
    "httpcore",
)


class ConsistentFormatter(logging.Formatter):
    """Render every physical log line with the same level and timestamp."""

    def __init__(self) -> None:
        super().__init__(fmt="%(message)s", datefmt=_LOG_DATE_FORMAT)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Render timestamps in UTC so container output is deterministic."""
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return timestamp.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        lines = message.splitlines() or [""]
        event_match = _EVENT_PREFIX.match(lines[0])
        if event_match:
            lines[0] = f"{event_match.group(1)} | {lines[0]}"
        prefix = f"{record.levelname} {self.formatTime(record, self.datefmt)} "
        return "\n".join(prefix + line for line in lines)


def resolve_log_level(value: str | None = None) -> int:
    """Return the configured standard-library logging level.

    ``value`` is primarily useful for tests and callers that already read their
    environment. Runtime callers use ``ENERGY_OPTIMIZER_LOG_LEVEL`` directly.
    """
    configured = (
        (
            value
            if value is not None
            else os.environ.get("ENERGY_OPTIMIZER_LOG_LEVEL", "INFO")
        )
        .strip()
        .upper()
    )
    if configured not in LOG_LEVELS:
        supported = ", ".join(LOG_LEVEL_NAMES)
        raise ConfigurationError(
            "Invalid ENERGY_OPTIMIZER_LOG_LEVEL "
            f"{configured!r}; expected one of: {supported}"
        )
    return LOG_LEVELS[configured]


def _configure_logging(level: int) -> int:
    root = logging.getLogger()
    root.setLevel(level)

    handlers = [
        candidate
        for candidate in root.handlers
        if getattr(candidate, _HANDLER_MARKER, False)
    ]
    handler = handlers[0] if handlers else None
    for duplicate in handlers[1:]:
        root.removeHandler(duplicate)
        duplicate.close()

    external_handlers = [
        candidate
        for candidate in root.handlers
        if not getattr(candidate, _HANDLER_MARKER, False)
        and _is_external_handler(candidate)
    ]
    if external_handlers:
        if handler is not None:
            root.removeHandler(handler)
            handler.close()
        for external_handler in external_handlers:
            external_handler.setFormatter(ConsistentFormatter())
    else:
        if (
            handler is None
            or not isinstance(handler, logging.StreamHandler)
            or handler.stream is not sys.stdout
        ):
            if handler is not None:
                root.removeHandler(handler)
                handler.close()
            handler = logging.StreamHandler(sys.stdout)
            setattr(handler, _HANDLER_MARKER, True)
            root.addHandler(handler)
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(ConsistentFormatter())

    application_logger = logging.getLogger("energy_optimizer")
    application_logger.setLevel(logging.NOTSET)
    application_logger.propagate = True

    for logger_name in _NORMALIZED_LOGGERS:
        normalized_logger = logging.getLogger(logger_name)
        normalized_logger.handlers.clear()
        normalized_logger.setLevel(logging.NOTSET)
        normalized_logger.disabled = False
        normalized_logger.propagate = True

    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
    uvicorn_access.propagate = False

    return level


def _is_external_handler(handler: logging.Handler) -> bool:
    """Return whether a root handler is an application output destination.

    Pytest installs capture handlers on the root logger while running tests.
    They observe records but are not service destinations, so treating them as
    an external logging configuration would disable the normal stdout fallback.
    """
    return not isinstance(handler, logging.NullHandler) and not (
        handler.__class__.__module__ == "_pytest.logging"
    )


def bootstrap_logging() -> int:
    """Install the default formatter before the ASGI server starts."""
    return _configure_logging(logging.INFO)


def configure_logging(value: str | None = None) -> int:
    """Configure process logging and return the selected numeric level.

    Application and supported third-party logs use one root stream handler.
    Request completion is logged by the application middleware, so Uvicorn's
    separate access records are disabled to avoid duplicate records.
    """
    return _configure_logging(resolve_log_level(value))
