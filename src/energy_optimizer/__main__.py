"""Run the Energy Optimizer service with logging configured first."""

from __future__ import annotations

from energy_optimizer.logging_config import bootstrap_logging


def main() -> None:
    """Configure logging before starting Uvicorn."""
    bootstrap_logging()

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
