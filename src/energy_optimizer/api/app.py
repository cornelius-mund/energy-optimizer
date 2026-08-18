"""HTTP API application construction for the Energy Optimizer service."""

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from energy_optimizer import __version__
from energy_optimizer.api import schemas
from energy_optimizer.api.lifecycle import lifespan
from energy_optimizer.api.middleware import log_requests
from energy_optimizer.api.routers import core as core_routes
from energy_optimizer.api.routers import dashboard as dashboard_routes
from energy_optimizer.api.routers import provider as provider_routes

app = FastAPI(title="Energy Optimizer", version=__version__, lifespan=lifespan)
DEFAULT_FRONTEND_DIRECTORY = Path(__file__).parents[3] / "frontend"
MAX_HORIZON_HOURS = schemas.MAX_HORIZON_HOURS


def configured_frontend_directory() -> Path:
    """Return the dashboard directory for source and installed deployments."""
    return Path(
        os.environ.get(
            "ENERGY_OPTIMIZER_FRONTEND_DIRECTORY",
            str(DEFAULT_FRONTEND_DIRECTORY),
        )
    )


FRONTEND_DIRECTORY = configured_frontend_directory()

app.middleware("http")(log_requests)
app.include_router(core_routes.router)
app.include_router(provider_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(provider_routes.tail_router)

if FRONTEND_DIRECTORY.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=FRONTEND_DIRECTORY, html=True),
        name="dashboard",
    )


def __getattr__(name: str) -> Any:
    """Preserve the former app-module route and schema exports."""
    for module in (core_routes, provider_routes, dashboard_routes, schemas):
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
