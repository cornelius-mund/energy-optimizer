"""Run the Energy Optimizer service with logging configured first."""

from __future__ import annotations

import logging

from energy_optimizer.logging_config import bootstrap_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Configure logging before starting Uvicorn."""
    bootstrap_logging()
    logger.info(
        "event=process_logging_bootstrapped component=process operation=startup"
    )

    import uvicorn

    uvicorn.run(
        "energy_optimizer.api:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
