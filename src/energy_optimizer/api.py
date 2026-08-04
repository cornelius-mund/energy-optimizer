"""HTTP API for the Energy Optimizer service."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from energy_optimizer import __version__
from energy_optimizer.config import load_configuration


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load configuration before accepting requests."""
    path = Path(os.environ.get("ENERGY_OPTIMIZER_CONFIG", "config.yaml"))
    application.state.configuration = load_configuration(path)
    yield


app = FastAPI(title="Energy Optimizer", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}
