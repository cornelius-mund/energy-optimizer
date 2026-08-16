"""Tests for the production Docker image configuration."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"


def test_healthcheck_runs_every_minute_with_existing_probe_settings() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    match = re.search(
        r"HEALTHCHECK (?P<options>.+?) \\\n+\s+CMD (?P<command>.+)",
        dockerfile,
    )

    assert match is not None
    assert match.group("options") == (
        "--interval=60s --timeout=3s --start-period=5s --retries=3"
    )
    assert match.group("command") == (
        'python -c "import urllib.request; '
        "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)\""
    )


def test_dockerfile_copies_the_dashboard_assets() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY frontend ./frontend" in dockerfile
    assert "ENERGY_OPTIMIZER_FRONTEND_DIRECTORY=/app/frontend" in dockerfile
