"""How the service delivers the dashboard files."""

import pytest
from fastapi.testclient import TestClient

from energy_optimizer.api import app


@pytest.mark.parametrize(
    "path", ["/dashboard/", "/dashboard/app.js", "/dashboard/styles.css"]
)
def test_dashboard_files_are_revalidated_on_every_load(path: str) -> None:
    response = TestClient(app).get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_an_unchanged_dashboard_file_is_answered_without_a_body() -> None:
    client = TestClient(app)
    etag = client.get("/dashboard/app.js").headers["etag"]

    response = client.get("/dashboard/app.js", headers={"If-None-Match": etag})

    assert response.status_code == 304
