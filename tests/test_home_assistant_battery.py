"""Tests for the Home Assistant battery importer."""

from datetime import datetime, timezone

import httpx
import pytest

from energy_optimizer.providers.home_assistant_battery import (
    HomeAssistantBatteryImporter,
)
from energy_optimizer.providers.home_assistant_energy import HomeAssistantError
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_importer_factory,
    home_assistant_state_payload,
)

START = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)

BATTERY_MAPPINGS = {
    "state_of_charge": {
        "entity_id": "sensor.battery_soc",
        "unit": "%",
    },
    "capacity": {
        "entity_id": "sensor.battery_capacity",
        "unit": "kWh",
    },
    "minimum_soc": {
        "entity_id": "sensor.battery_minimum_soc",
        "unit": "kWh",
    },
    "maximum_soc": {
        "entity_id": "sensor.battery_maximum_soc",
        "unit": "kWh",
    },
    "maximum_charge": {
        "entity_id": "sensor.battery_maximum_charge",
        "unit": "W",
    },
    "maximum_discharge": {
        "entity_id": "sensor.battery_maximum_discharge",
        "unit": "kW",
    },
    "charge_efficiency": {
        "entity_id": "sensor.battery_charge_efficiency",
        "unit": "%",
    },
    "discharge_efficiency": {
        "entity_id": "sensor.battery_discharge_efficiency",
        "unit": "ratio",
    },
}


configuration = home_assistant_configuration_factory(battery=BATTERY_MAPPINGS)
payload = home_assistant_state_payload


def standard_payloads() -> dict[str, dict[str, object]]:
    states = {
        "sensor.battery_soc": 50,
        "sensor.battery_capacity": 10,
        "sensor.battery_minimum_soc": 2,
        "sensor.battery_maximum_soc": 10,
        "sensor.battery_maximum_charge": 4000,
        "sensor.battery_maximum_discharge": 4,
        "sensor.battery_charge_efficiency": 95,
        "sensor.battery_discharge_efficiency": 0.9,
    }
    return {entity_id: payload(entity_id, state) for entity_id, state in states.items()}


importer = home_assistant_importer_factory(HomeAssistantBatteryImporter, configuration)


def test_fetch_normalizes_battery_state_and_capabilities() -> None:
    responses = standard_payloads()

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.path.rsplit("/", 1)[-1]
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=responses[entity_id])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(now=START)
    finally:
        client.close()

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.state_of_charge_kwh == (5.0,)
    assert data.capacity_kwh == 10.0
    assert data.minimum_soc_kwh == 2.0
    assert data.maximum_soc_kwh == 10.0
    assert data.initial_soc_kwh == 5.0
    assert data.maximum_charge_kw == 4.0
    assert data.maximum_discharge_kw == 4.0
    assert data.charge_efficiency == 0.95
    assert data.discharge_efficiency == 0.9
    assert data.source.entity_id == "battery"
    assert data.retrieved_at == START
    assert data.latest_observation_at == datetime(2026, 1, 1, 5, tzinfo=timezone.utc)


def test_fetch_reads_values_from_attributes_and_reuses_one_state_request() -> None:
    mapping = {
        "state_of_charge": {
            "entity_id": "sensor.battery",
            "unit": "%",
        },
        "capacity": {
            "entity_id": "sensor.battery",
            "unit": "kWh",
            "attribute": "capacity_kwh",
        },
        "minimum_soc": {
            "entity_id": "sensor.battery",
            "unit": "kWh",
            "attribute": "minimum_soc_kwh",
        },
        "maximum_soc": {
            "entity_id": "sensor.battery",
            "unit": "kWh",
            "attribute": "maximum_soc_kwh",
        },
        "maximum_charge": {
            "entity_id": "sensor.battery",
            "unit": "kW",
            "attribute": "maximum_charge_kw",
        },
        "maximum_discharge": {
            "entity_id": "sensor.battery",
            "unit": "kW",
            "attribute": "maximum_discharge_kw",
        },
        "charge_efficiency": {
            "entity_id": "sensor.battery",
            "unit": "ratio",
            "attribute": "charge_efficiency",
        },
        "discharge_efficiency": {
            "entity_id": "sensor.battery",
            "unit": "ratio",
            "attribute": "discharge_efficiency",
        },
    }
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                **payload("sensor.battery", 50),
                "attributes": {
                    "capacity_kwh": 10,
                    "minimum_soc_kwh": 2,
                    "maximum_soc_kwh": 10,
                    "maximum_charge_kw": 4,
                    "maximum_discharge_kw": 4,
                    "charge_efficiency": 0.95,
                    "discharge_efficiency": 0.9,
                },
            },
        )

    provider, client = importer(httpx.MockTransport(handler), battery=mapping)
    try:
        data = provider.fetch(now=START)
    finally:
        client.close()

    assert requests == 1
    assert data.capacity_kwh == 10
    assert data.state_of_charge_kwh == (5,)


def test_fetch_uses_constants_without_requesting_static_entities() -> None:
    mapping = {
        "state_of_charge": {
            "entity_id": "sensor.battery_soc",
            "unit": "%",
        },
        "capacity": {"value": 10, "unit": "kWh"},
        "minimum_soc": {"value": 2, "unit": "kWh"},
        "maximum_soc": {"value": 10, "unit": "kWh"},
        "maximum_charge": {"value": 4, "unit": "kW"},
        "maximum_discharge": {"value": 4, "unit": "kW"},
        "charge_efficiency": {"value": 95, "unit": "%"},
        "discharge_efficiency": {"value": 0.9, "unit": "ratio"},
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.path.rsplit("/", 1)[-1]
        requests.append(entity_id)
        return httpx.Response(200, json=payload(entity_id, 50))

    provider, client = importer(httpx.MockTransport(handler), battery=mapping)
    try:
        data = provider.fetch(now=START)
    finally:
        client.close()

    assert requests == ["sensor.battery_soc"]
    assert data.capacity_kwh == 10
    assert data.minimum_soc_kwh == 2
    assert data.maximum_soc_kwh == 10
    assert data.maximum_charge_kw == 4
    assert data.maximum_discharge_kw == 4
    assert data.charge_efficiency == 0.95
    assert data.discharge_efficiency == 0.9
    assert data.latest_observation_at == datetime(2026, 1, 1, 5, tzinfo=timezone.utc)


def test_fetch_allows_unavailable_entity_state_when_all_values_use_attributes() -> None:
    mapping = {
        name: {
            "entity_id": "sensor.battery",
            "unit": "kWh"
            if name in {"capacity", "minimum_soc", "maximum_soc"}
            else ("kW" if name in {"maximum_charge", "maximum_discharge"} else "ratio"),
            "attribute": name,
        }
        for name in (
            "capacity",
            "minimum_soc",
            "maximum_soc",
            "maximum_charge",
            "maximum_discharge",
            "charge_efficiency",
            "discharge_efficiency",
        )
    }
    mapping["state_of_charge"] = {
        "entity_id": "sensor.battery",
        "unit": "%",
        "attribute": "state_of_charge",
    }

    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    **payload("sensor.battery", "unavailable"),
                    "attributes": {
                        "state_of_charge": 50,
                        "capacity": 10,
                        "minimum_soc": 2,
                        "maximum_soc": 10,
                        "maximum_charge": 4,
                        "maximum_discharge": 4,
                        "charge_efficiency": 0.95,
                        "discharge_efficiency": 0.9,
                    },
                },
            )
        ),
        battery=mapping,
    )
    try:
        data = provider.fetch(now=START)
    finally:
        client.close()

    assert data.state_of_charge_kwh == (5,)


@pytest.mark.parametrize(
    ("entity_id", "state", "message"),
    [
        ("sensor.battery_soc", "unavailable", "unavailable"),
        ("sensor.battery_capacity", "not-a-number", "non-numeric"),
        ("sensor.battery_maximum_charge", "nan", "non-finite"),
    ],
)
def test_fetch_rejects_unsafe_values(
    entity_id: str, state: object, message: str
) -> None:
    responses = standard_payloads()
    responses[entity_id] = payload(entity_id, state)

    def handler(request: httpx.Request) -> httpx.Response:
        requested_entity = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=responses[requested_entity])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(now=START)
    finally:
        client.close()


def test_fetch_rejects_inconsistent_soc_limits() -> None:
    responses = standard_payloads()
    responses["sensor.battery_maximum_soc"] = payload("sensor.battery_maximum_soc", 11)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path.rsplit("/", 1)[-1]])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="exceeds capacity"):
            provider.fetch(now=START)
    finally:
        client.close()


def test_fetch_reports_authentication_failures_without_exposing_token() -> None:
    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(401)))
    try:
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            provider.fetch(now=START)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404), "was not found"),
        (httpx.Response(200, content=b"not-json"), "malformed JSON"),
    ],
)
def test_fetch_reports_missing_entities_and_malformed_responses(
    response: httpx.Response, message: str
) -> None:
    provider, client = importer(httpx.MockTransport(lambda _: response))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(now=START)
    finally:
        client.close()


def test_fetch_reports_missing_mapped_attribute() -> None:
    mapping = {
        **BATTERY_MAPPINGS,
        "capacity": {
            "entity_id": "sensor.battery_capacity",
            "attribute": "capacity_kwh",
            "unit": "kWh",
        },
    }
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=standard_payloads()[request.url.path.rsplit("/", 1)[-1]],
            )
        ),
        battery=mapping,
    )
    try:
        with pytest.raises(HomeAssistantError, match="missing.*attribute"):
            provider.fetch(now=START)
    finally:
        client.close()


def test_fetch_rejects_naive_observation_timestamp() -> None:
    responses = standard_payloads()
    responses["sensor.battery_soc"] = payload(
        "sensor.battery_soc", 50, "2026-01-01T05:00:00"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path.rsplit("/", 1)[-1]])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="must include a timezone"):
            provider.fetch(now=START)
    finally:
        client.close()


def test_fetch_does_not_return_partial_data_when_one_entity_fails() -> None:
    calls = 0
    responses = standard_payloads()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            entity_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=responses[entity_id])
        return httpx.Response(503)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            provider.fetch(now=START)
    finally:
        client.close()

    assert calls == 2


def test_freshness_uses_the_oldest_mapped_observation() -> None:
    responses = standard_payloads()
    responses["sensor.battery_capacity"] = payload(
        "sensor.battery_capacity", 10, "2026-01-01T03:00:00+00:00"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path.rsplit("/", 1)[-1]])

    provider, client = importer(httpx.MockTransport(handler), max_data_age_seconds=7200)
    try:
        data = provider.fetch(now=START)
    finally:
        client.close()

    assert data.latest_observation_at == datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
    assert not provider.is_fresh(data, now=START)


def test_fetch_requires_timezone_aware_retrieval_time() -> None:
    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        with pytest.raises(HomeAssistantError, match="timezone"):
            provider.fetch(now=datetime(2026, 1, 1, 5, 30))
    finally:
        client.close()
