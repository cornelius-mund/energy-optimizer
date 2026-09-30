"""Tests for shared provider HTTP helpers."""

from collections.abc import Callable

import httpx
import pytest

from energy_optimizer.providers.http import JsonHttpClient, home_assistant_headers


def request_home_assistant_json(
    handler: Callable[[httpx.Request], httpx.Response],
    authentication_error_factory: Callable[[str], Exception] | None = None,
) -> object:
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return JsonHttpClient(client).get_home_assistant_json(
            "http://homeassistant.local/api/test",
            token="test-token",
            timeout_seconds=5,
            error_factory=RuntimeError,
            authentication_error_factory=authentication_error_factory,
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

    assert request_home_assistant_json(handler) == {"state": "ok"}


def test_home_assistant_headers_are_built_consistently() -> None:
    assert home_assistant_headers("test-token") == {
        "Authorization": "Bearer test-token",
        "Accept": "application/json",
    }


class AuthenticationRejected(RuntimeError):
    """A distinguishable error for a rejected Home Assistant token."""


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationRejected),
        (403, AuthenticationRejected),
        (404, RuntimeError),
        (503, RuntimeError),
    ],
)
def test_authentication_statuses_use_the_authentication_error_factory(
    status: int, error_type: type[RuntimeError]
) -> None:
    with pytest.raises(RuntimeError) as failure:
        request_home_assistant_json(
            lambda _: httpx.Response(status), AuthenticationRejected
        )

    # Only an authentication rejection may use the distinguishable error type.
    assert type(failure.value) is error_type


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
    with pytest.raises(RuntimeError, match=message):
        request_home_assistant_json(lambda _: httpx.Response(status))
