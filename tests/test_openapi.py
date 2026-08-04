"""Verify checked-in OpenAPI documentation stays in sync with the API."""

from pathlib import Path

import yaml

from energy_optimizer.api import app

OPENAPI_DOCUMENT_PATH = (
    Path(__file__).parents[1] / "src" / "energy_optimizer" / "openapi-docs.yml"
)


def test_static_openapi_document_matches_generated_schema() -> None:
    static_document = yaml.safe_load(OPENAPI_DOCUMENT_PATH.read_text())

    assert static_document == app.openapi()
