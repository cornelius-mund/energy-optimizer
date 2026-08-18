"""Tests for shared provider HTTP helpers."""

import httpx
import pytest

from energy_optimizer.providers.http import JsonHttpClient, home_assistant_headers


def request_home_assistant_json(client: httpx.Client) -> object:
    return JsonHttpClient(client).get_home_assistant_json(
        "http://homeassistant.local/api/test",
        token="test-token",
        timeout_seconds=5,
        error_factory=RuntimeError,
        not_found_message="history was not found",
        status_message=lambda status: f"Home Assistant returned HTTP {status}",
        timeout_message="request timed out",
        transport_message="transport failed",
        malformed_message="malformed JSON",
        log_event="home_assistant_test",
        component="home_assistant",
        operation="test",
    )


def test_home_assistant_request_adds_auth_headers_and_decodes_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        return httpx.Response(200, json={"state": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert request_home_assistant_json(client) == {"state": "ok"}
    finally:
        client.close()


def test_home_assistant_headers_are_built_consistently() -> None:
    assert home_assistant_headers("test-token") == {
        "Authorization": "Bearer test-token",
        "Accept": "application/json",
    }


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "authentication failed"),
        (403, "authentication failed"),
        (404, "history was not found"),
        (503, "Home Assistant returned HTTP 503"),
    ],
)
def test_home_assistant_request_translates_status_failures(
    status: int, message: str
) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    )
    try:
        with pytest.raises(RuntimeError, match=message):
            request_home_assistant_json(client)
    finally:
        client.close()
