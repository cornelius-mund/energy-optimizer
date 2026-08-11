"""Logging configuration for the Energy Optimizer service."""

from __future__ import annotations

import logging
import os
import sys
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


def configure_logging(value: str | None = None) -> int:
    """Configure process logging and return the selected numeric level.

    Application logs use the root stream handler so they follow the same
    container-friendly output path as Uvicorn's error logs. Request completion
    is logged by the application middleware, so Uvicorn's separate access
    records are disabled to avoid duplicate records.
    """
    level = resolve_log_level(value)
    root = logging.getLogger()
    root.setLevel(level)

    handler = next(
        (
            candidate
            for candidate in root.handlers
            if getattr(candidate, _HANDLER_MARKER, False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    application_logger = logging.getLogger("energy_optimizer")
    application_logger.setLevel(logging.NOTSET)
    application_logger.propagate = True

    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.handlers.clear()
    uvicorn_error.setLevel(logging.NOTSET)
    uvicorn_error.propagate = True

    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True
    uvicorn_access.propagate = False

    return level
