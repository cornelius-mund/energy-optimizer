"""Application startup and shutdown lifecycle for the Energy Optimizer API."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from energy_optimizer.background import BackgroundLoops, SupervisedLoop
from energy_optimizer.config import (
    Configuration,
    ConfigurationError,
    load_configuration,
)
from energy_optimizer.control.ev_controller import EvController
from energy_optimizer.control.plug_in_reminder import PlugInReminder
from energy_optimizer.logging_config import configure_logging
from energy_optimizer.optimization.plan_generation import PlanGenerator
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.storage import ProviderDataStore

logger = logging.getLogger("energy_optimizer.api")


def _start_ev_control(
    configuration: Configuration,
    store: ProviderDataStore | None,
    loops: BackgroundLoops,
) -> None:
    ev = configuration.home_assistant.ev if configuration.home_assistant else None
    if ev is None or ev.control is None or not ev.control.enabled or store is None:
        return
    assert configuration.home_assistant is not None
    controller = EvController(configuration.home_assistant, store)
    logger.info("event=service_ev_control_starting component=control operation=startup")
    loops.start(
        SupervisedLoop(
            "ev_control", controller.apply_due, ev.control.poll_interval_seconds
        )
    )


def _start_plug_in_reminder(
    configuration: Configuration,
    store: ProviderDataStore | None,
    loops: BackgroundLoops,
) -> None:
    charging = configuration.ev_charging
    home_assistant = configuration.home_assistant
    if (
        charging is None
        or charging.plug_in_reminder is None
        or charging.home_hours is None
        or home_assistant is None
        or home_assistant.ev is None
        or store is None
    ):
        return
    reminder = PlugInReminder(
        home_assistant,
        charging.home_hours,
        charging.plug_in_reminder,
        ZoneInfo(configuration.timezone),
        store,
    )
    logger.info(
        "event=service_plug_in_reminder_starting component=control operation=startup"
    )
    loops.start(
        SupervisedLoop(
            "plug_in_reminder",
            reminder.remind_due,
            charging.plug_in_reminder.poll_interval_seconds,
        )
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load configuration before accepting requests."""
    startup_started_at = perf_counter()
    logger.info("event=service_lifespan_entered component=api operation=startup")
    logging_started_at = perf_counter()
    try:
        log_level = configure_logging()
    except ConfigurationError:
        logger.critical(
            "event=service_startup_failed component=logging operation=configure "
            "error_type=ConfigurationError"
        )
        raise
    logger.info(
        "event=service_logging_configured component=logging operation=startup "
        "duration_seconds=%.3f log_level=%s",
        perf_counter() - logging_started_at,
        logging.getLevelName(log_level),
    )
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
            configuration_started_at = perf_counter()
            logger.info(
                "event=service_configuration_load_started component=configuration "
                "operation=startup path=%s",
                path.name,
            )
            configuration = load_configuration(path)
            logger.info(
                "event=service_configuration_loaded component=configuration "
                "operation=startup duration_seconds=%.3f",
                perf_counter() - configuration_started_at,
            )
            application.state.configuration = configuration
            persistence_enabled = configuration.persistence is not None
            logger.info(
                "event=service_persistence_store_initializing component=storage "
                "operation=startup enabled=%s",
                persistence_enabled,
            )
            application.state.provider_data_store = (
                ProviderDataStore(configuration.persistence.directory)
                if configuration.persistence is not None
                else None
            )
            logger.info(
                "event=service_persistence_store_initialized component=storage "
                "operation=startup enabled=%s",
                persistence_enabled,
            )
            orchestrator_started_at = perf_counter()
            logger.info(
                "event=service_orchestrator_build_started component=orchestration "
                "operation=startup enabled=%s",
                configuration.orchestration is not None
                and configuration.orchestration.enabled,
            )
            store = application.state.provider_data_store
            application.state.orchestrator = build_configured_orchestrator(
                configuration,
                store,
                PlanGenerator(configuration, store) if store is not None else None,
            )
            logger.info(
                "event=service_orchestrator_built component=orchestration "
                "operation=startup duration_seconds=%.3f enabled=%s",
                perf_counter() - orchestrator_started_at,
                application.state.orchestrator is not None,
            )
            loops = BackgroundLoops()
            application.state.background_loops = loops
            orchestrator = application.state.orchestrator
            if orchestrator is not None:
                logger.info(
                    "event=service_orchestration_task_starting "
                    "component=orchestration operation=startup"
                )
                loops.start(
                    SupervisedLoop(
                        "orchestration",
                        orchestrator.run_due,
                        orchestrator.poll_interval_seconds,
                    )
                )
                logger.info(
                    "event=service_orchestration_task_started "
                    "component=orchestration operation=startup"
                )
            _start_ev_control(configuration, store, loops)
            _start_plug_in_reminder(configuration, store, loops)
            logger.info(
                "event=service_started component=api operation=startup "
                "persistence_enabled=%s orchestration_enabled=%s "
                "home_assistant_enabled=%s duration_seconds=%.3f",
                configuration.persistence is not None,
                configuration.orchestration is not None
                and configuration.orchestration.enabled,
                configuration.home_assistant is not None,
                perf_counter() - startup_started_at,
            )
            service_started = True
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
            await application.state.background_loops.stop()
            logger.info("event=service_stopped component=api operation=shutdown")
