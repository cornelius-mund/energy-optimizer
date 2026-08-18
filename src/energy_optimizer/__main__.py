"""Run the Energy Optimizer service with logging configured first."""

from __future__ import annotations

import os

from energy_optimizer.logging_config import bootstrap_logging


def main() -> None:
    """Configure logging before starting Uvicorn."""
    bootstrap_logging()

    import uvicorn

    uvicorn.run(
        "energy_optimizer.api:app",
        host=os.environ.get("ENERGY_OPTIMIZER_HOST", "0.0.0.0"),
        port=int(os.environ.get("ENERGY_OPTIMIZER_PORT", "8000")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
