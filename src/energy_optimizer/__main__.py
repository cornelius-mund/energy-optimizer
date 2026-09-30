"""Run the Energy Optimizer service with logging configured first."""

import logging
import os

from energy_optimizer.logging_config import bootstrap_logging

# An explicit name keeps the record under the application logger; ``__name__`` is
# "__main__" when the service starts through ``python -m energy_optimizer``.
logger = logging.getLogger("energy_optimizer.process")


def main() -> None:
    """Configure logging before starting Uvicorn."""
    bootstrap_logging()
    logger.info(
        "event=process_logging_bootstrapped component=process operation=startup"
    )

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
