"""Tests for the Home Assistant household-load importer."""

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant import (
    HomeAssistantError,
    HomeAssistantLoadImporter,
)

ENTITY_ID = "sensor.household_load"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc)


def configuration(**overrides: Any) -> HomeAssistantConfiguration:
    values: dict[str, Any] = {
        "base_url": "http://homeassistant.test:8123",
        "token": "test-token",
        "household_load_entity_id": ENTITY_ID,
        "timeout_seconds": 5,
        "polling_interval_seconds": 300,
        "max_data_age_seconds": 7200,
    }
    values.update(overrides)
    return HomeAssistantConfiguration.model_validate(values)


def history_payload(unit: str = "W") -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "entity_id": ENTITY_ID,
                "state": "500" if unit == "W" else "0.5",
                "last_updated": "2026-01-01T00:00:00+00:00",
                "attributes": {"unit_of_measurement": unit},
            },
            {
                "state": "1000" if unit == "W" else "1.0",
                "last_changed": "2026-01-01T01:00:00+00:00",
            },
            {
                "state": "2000" if unit == "W" else "2.0",
                "last_changed": "2026-01-01T02:00:00+00:00",
            },
            {
                "state": "3000" if unit == "W" else "3.0",
                "last_changed": "2026-01-01T03:00:00+00:00",
            },
        ]
    ]


def importer(
    handler: httpx.MockTransport | httpx.BaseTransport,
) -> tuple[HomeAssistantLoadImporter, httpx.Client]:
    client = httpx.Client(transport=handler)
    return HomeAssistantLoadImporter(configuration(), client), client


def test_fetch_normalizes_watts_and_carries_forward_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/period/2025-12-31T23:00:00+00:00"
        assert request.url.params["filter_entity_id"] == ENTITY_ID
        assert "minimal_response" in request.url.params
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=history_payload())

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.load_kw == (0.5, 1.0, 2.0, 3.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == ENTITY_ID
    assert data.retrieved_at == NOW
    assert data.expires_at == datetime(2026, 1, 1, 5, tzinfo=timezone.utc)


def test_fetch_accepts_values_already_reported_in_kw() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=history_payload("kW"))

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.5, 1.0, 2.0, 3.0)


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "authentication failed"),
        (404, "entity ID"),
        (500, "HTTP 500"),
    ],
)
def test_fetch_reports_http_failures(status_code: int, message: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


def test_fetch_reports_timeout() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="timed out"):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


def test_fetch_reports_malformed_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="malformed JSON"):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "no household-load history"),
        (
            [[{"state": "unavailable", "last_changed": "2026-01-01T00:00:00+00:00"}]],
            "unavailable",
        ),
        (
            [[{"state": "not-a-number", "last_changed": "2026-01-01T00:00:00+00:00"}]],
            "non-numeric",
        ),
        (
            [
                [
                    {
                        "state": "1",
                        "last_changed": "2026-01-01T00:00:00+00:00",
                        "attributes": {"unit_of_measurement": "A"},
                    }
                ]
            ],
            "Unsupported.*unit",
        ),
    ],
)
def test_fetch_reports_invalid_history(payload: Any, message: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


def test_fetch_reports_stale_data() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=history_payload())

    provider, client = importer(
        httpx.MockTransport(handler),
    )
    provider.configuration = configuration(max_data_age_seconds=60)
    try:
        with pytest.raises(HomeAssistantError, match="stale"):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


def test_fetch_requires_more_history_when_first_hour_has_no_value() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [
                    {
                        "state": "1",
                        "last_changed": "2026-01-01T01:30:00+00:00",
                        "attributes": {"unit_of_measurement": "kW"},
                    }
                ]
            ],
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="increase history lookback"):
            provider.fetch(START, END, 3600, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("start_time", "end_time", "lookback", "message"),
    [
        (END, START, 3600, "end_time"),
        (
            datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
            END,
            3600,
            "whole hourly",
        ),
        (START, END, -1, "non-negative"),
    ],
)
def test_fetch_validates_requested_period(
    start_time: datetime,
    end_time: datetime,
    lookback: float,
    message: str,
) -> None:
    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(start_time, end_time, lookback, now=NOW)
    finally:
        client.close()
