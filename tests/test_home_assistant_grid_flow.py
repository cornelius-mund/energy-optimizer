"""Tests for the Home Assistant grid-flow importer."""

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.providers.home_assistant_energy import HomeAssistantError
from energy_optimizer.providers.home_assistant_grid_flow import (
    HomeAssistantGridFlowImporter,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
    home_assistant_importer_factory,
)

ENTITY_ID = "sensor.grid_import"
EXPORT_ENTITY_ID = "sensor.grid_export"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)


configuration = home_assistant_configuration_factory(
    grid_import_entities=[
        {
            "entity_id": ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    ],
    grid_export_entities=[
        {
            "entity_id": EXPORT_ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    ],
)


history_payload = home_assistant_history_payload


def standard_payload(entity_id: str) -> list[list[dict[str, Any]]]:
    return history_payload(
        entity_id,
        [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "1"),
            ("2026-01-01T02:00:00+00:00", "3"),
            ("2026-01-01T03:00:00+00:00", "6"),
            ("2026-01-01T04:00:00+00:00", "10"),
        ],
    )


importer = home_assistant_importer_factory(HomeAssistantGridFlowImporter, configuration)


def test_fetch_normalizes_import_and_export_entities() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "1.5"),
                ("2026-01-01T03:00:00+00:00", "3"),
                ("2026-01-01T04:00:00+00:00", "5"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=responses[entity_id])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.start_time == START
    assert data.import_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.export_kw == (0.5, 1.0, 1.5, 2.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == "grid_flow"
    assert data.retrieved_at == NOW
    assert data.latest_observation_at == datetime(2026, 1, 1, 4, tzinfo=timezone.utc)


def test_fetch_supports_multiple_signed_entities_per_channel() -> None:
    second_import = "sensor.grid_import_submeter"
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        second_import: history_payload(
            second_import,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.25"),
                ("2026-01-01T02:00:00+00:00", "0.75"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
                ("2026-01-01T04:00:00+00:00", "2.5"),
            ],
        ),
        EXPORT_ENTITY_ID: standard_payload(EXPORT_ENTITY_ID),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        grid_import_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": second_import,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.import_kw == (0.75, 1.5, 2.25, 3.0)
    assert data.export_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_aligns_channels_to_the_latest_available_start() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:30:00+00:00", "0"),
                ("2026-01-01T01:30:00+00:00", "1"),
                ("2026-01-01T02:30:00+00:00", "3"),
                ("2026-01-01T03:30:00+00:00", "6"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.start_time == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert data.import_kw == (2.0, 3.0, 4.0)
    assert data.export_kw == (1.0, 2.0, 3.0)


def test_fetch_fails_without_returning_partial_data_when_one_channel_fails() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=standard_payload(ENTITY_ID))
        return httpx.Response(503)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert calls == 2


def test_freshness_uses_the_oldest_channel_observation() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "1"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
                ("2026-01-01T03:30:00+00:00", "2"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(httpx.MockTransport(handler), max_data_age_seconds=90)
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.latest_observation_at == datetime(
        2026, 1, 1, 3, 30, tzinfo=timezone.utc
    )
    assert not provider.is_fresh(data, now=NOW)


def test_fetch_rejects_instantaneous_power_channel() -> None:
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=history_payload(
                    request.url.params["filter_entity_id"],
                    [("2026-01-01T00:00:00+00:00", "500")],
                    unit="W",
                ),
            )
        )
    )
    try:
        with pytest.raises(HomeAssistantError, match="instantaneous power"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()
