"""HTTP API for the Energy Optimizer service."""

from fastapi import FastAPI

from energy_optimizer import __version__

app = FastAPI(title="Energy Optimizer", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}
