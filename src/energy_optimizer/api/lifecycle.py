"""Application startup and shutdown lifecycle for the Energy Optimizer API."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from energy_optimizer.config import ConfigurationError, load_configuration
from energy_optimizer.logging_config import configure_logging
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.storage import ProviderDataStore

logger = logging.getLogger("energy_optimizer.api")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load configuration before accepting requests."""
    try:
        log_level = configure_logging()
    except ConfigurationError:
        logger.critical(
            "event=service_startup_failed component=logging operation=configure "
            "error_type=ConfigurationError"
        )
        raise
    path = Path(os.environ.get("ENERGY_OPTIMIZER_CONFIG", "config.yaml"))
    logger.info(
        "event=service_starting component=api operation=startup "
        "log_level=%s configuration_path=%s",
        logging.getLevelName(log_level),
        path,
    )
    service_started = False
    try:
        try:
            configuration = load_configuration(path)
            application.state.configuration = configuration
            application.state.provider_data_store = (
                ProviderDataStore(configuration.persistence.directory)
                if configuration.persistence is not None
                else None
            )
            application.state.orchestrator = build_configured_orchestrator(
                configuration,
                application.state.provider_data_store,
            )
            stop_event = asyncio.Event()
            application.state.orchestration_stop_event = stop_event
            application.state.orchestration_task = None
            if application.state.orchestrator is not None:
                application.state.orchestration_task = asyncio.create_task(
                    application.state.orchestrator.run_forever(stop_event)
                )
            logger.info(
                "event=service_started component=api operation=startup "
                "persistence_enabled=%s orchestration_enabled=%s "
                "home_assistant_enabled=%s",
                configuration.persistence is not None,
                configuration.orchestration is not None
                and configuration.orchestration.enabled,
                configuration.home_assistant is not None,
            )
            service_started = True
        except ConfigurationError as error:
            logger.critical(
                "event=service_startup_failed component=api operation=startup "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        except Exception as error:
            logger.critical(
                "event=service_startup_failed component=api operation=startup "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        yield
    finally:
        if service_started:
            logger.info("event=service_stopping component=api operation=shutdown")
            configured_stop_event = getattr(
                application.state, "orchestration_stop_event", None
            )
            task = getattr(application.state, "orchestration_task", None)
            if configured_stop_event is not None:
                configured_stop_event.set()
            if task is not None:
                try:
                    await task
                except Exception:
                    logger.critical(
                        "event=service_shutdown_failed component=orchestration "
                        "operation=stop",
                        exc_info=True,
                    )
                    raise
            logger.info("event=service_stopped component=api operation=shutdown")
