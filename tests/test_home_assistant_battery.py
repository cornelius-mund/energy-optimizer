"""Tests for the Home Assistant battery importer."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant_battery import (
    HomeAssistantBatteryImporter,
)
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.interfaces import (
    BatteryData,
    BatteryEfficiencyData,
    SourceMetadata,
)
from home_assistant_fixtures import (
    aggregate_settings,
    home_assistant_configuration_factory,
    home_assistant_importer_factory,
    home_assistant_state_payload,
)

START = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)

BATTERY_MAPPINGS = {
    "state_of_charge": {"entity_id": "sensor.battery_soc", "unit": "%"},
    "capacity": {"entity_id": "sensor.battery_capacity", "unit": "kWh"},
    "minimum_soc": {"entity_id": "sensor.battery_minimum_soc", "unit": "kWh"},
    "maximum_soc": {"entity_id": "sensor.battery_maximum_soc", "unit": "kWh"},
    "maximum_charge": {"entity_id": "sensor.battery_maximum_charge", "unit": "W"},
    "maximum_discharge": {
        "entity_id": "sensor.battery_maximum_discharge",
        "unit": "kW",
    },
    "battery_efficiency": {"entity_id": "sensor.battery_efficiency", "unit": "ratio"},
}
ATTRIBUTE_UNITS = {
    "state_of_charge": "%",
    "capacity": "kWh",
    "minimum_soc": "kWh",
    "maximum_soc": "kWh",
    "maximum_charge": "kW",
    "maximum_discharge": "kW",
    "battery_efficiency": "ratio",
}


configuration = home_assistant_configuration_factory(battery=BATTERY_MAPPINGS)
payload = home_assistant_state_payload
importer = home_assistant_importer_factory(HomeAssistantBatteryImporter, configuration)


def standard_payloads(*replacements: dict[str, object]) -> dict[str, dict[str, object]]:
    """Serve a healthy battery; ``replacements`` replace the payloads of entities."""
    states = {
        "sensor.battery_soc": 50,
        "sensor.battery_capacity": 10,
        "sensor.battery_minimum_soc": 2,
        "sensor.battery_maximum_soc": 10,
        "sensor.battery_maximum_charge": 4000,
        "sensor.battery_maximum_discharge": 4,
        "sensor.battery_efficiency": 0.85,
    }
    payloads = {
        entity_id: payload(entity_id, state) for entity_id, state in states.items()
    }
    payloads.update((str(item["entity_id"]), item) for item in replacements)
    return payloads


def serve(responses: Mapping[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer every state request with the response of the requested entity."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=responses[request.url.path.rsplit("/", 1)[-1]])

    return handler


def fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    now: datetime = START,
    **overrides: Any,
) -> BatteryData:
    provider, client = importer(httpx.MockTransport(handler), **overrides)
    with client:
        return provider.fetch(now=now)


def attribute_mapping(attributes: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Read each battery value from an attribute of the one ``sensor.battery``."""
    return {
        name: {
            "entity_id": "sensor.battery",
            "unit": ATTRIBUTE_UNITS[name],
            "attribute": attribute,
        }
        for name, attribute in attributes.items()
    }


def test_fetch_normalizes_battery_state_and_capabilities() -> None:
    data = fetch(serve(standard_payloads()))

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.state_of_charge_kwh == (5.0,)
    assert data.capacity_kwh == 10.0
    assert data.minimum_soc_kwh == 2.0
    assert data.maximum_soc_kwh == 10.0
    assert data.initial_soc_kwh == 5.0
    assert data.maximum_charge_kw == 4.0
    assert data.maximum_discharge_kw == 4.0
    assert data.battery_efficiency == 0.85
    assert data.source.entity_id == "battery"
    assert data.retrieved_at == START
    assert data.latest_observation_at == OBSERVED_AT


def test_fetch_reads_values_from_attributes_and_reuses_one_state_request() -> None:
    mapping = {
        "state_of_charge": {"entity_id": "sensor.battery", "unit": "%"},
        **attribute_mapping(
            {
                "capacity": "capacity_kwh",
                "minimum_soc": "minimum_soc_kwh",
                "maximum_soc": "maximum_soc_kwh",
                "maximum_charge": "maximum_charge_kw",
                "maximum_discharge": "maximum_discharge_kw",
                "battery_efficiency": "battery_efficiency",
            }
        ),
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
                    "battery_efficiency": 0.85,
                },
            },
        )

    data = fetch(handler, battery=mapping)

    assert requests == 1
    assert data.capacity_kwh == 10
    assert data.state_of_charge_kwh == (5,)


def test_fetch_uses_constants_without_requesting_static_entities() -> None:
    mapping = {
        "state_of_charge": {"entity_id": "sensor.battery_soc", "unit": "%"},
        "capacity": {"value": 10, "unit": "kWh"},
        "minimum_soc": {"value": 2, "unit": "kWh"},
        "maximum_soc": {"value": 10, "unit": "kWh"},
        "maximum_charge": {"value": 4, "unit": "kW"},
        "maximum_discharge": {"value": 4, "unit": "kW"},
        "battery_efficiency": {"value": 0.85, "unit": "ratio"},
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.path.rsplit("/", 1)[-1]
        requests.append(entity_id)
        return httpx.Response(200, json=payload(entity_id, 50))

    data = fetch(handler, battery=mapping)

    assert requests == ["sensor.battery_soc"]
    assert data.capacity_kwh == 10
    assert data.minimum_soc_kwh == 2
    assert data.maximum_soc_kwh == 10
    assert data.maximum_charge_kw == 4
    assert data.maximum_discharge_kw == 4
    assert data.battery_efficiency == 0.85
    assert data.latest_observation_at == OBSERVED_AT


def test_fetch_allows_unavailable_entity_state_when_all_values_use_attributes() -> None:
    attributes = {
        "state_of_charge": 50,
        "capacity": 10,
        "minimum_soc": 2,
        "maximum_soc": 10,
        "maximum_charge": 4,
        "maximum_discharge": 4,
        "battery_efficiency": 0.85,
    }

    data = fetch(
        lambda _: httpx.Response(
            200,
            json={**payload("sensor.battery", "unavailable"), "attributes": attributes},
        ),
        battery=attribute_mapping({name: name for name in ATTRIBUTE_UNITS}),
    )

    assert data.state_of_charge_kwh == (5,)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        pytest.param(
            payload("sensor.battery_soc", "unavailable"),
            "unavailable",
            id="unavailable-state",
        ),
        pytest.param(
            payload("sensor.battery_capacity", "not-a-number"),
            "non-numeric",
            id="non-numeric-state",
        ),
        pytest.param(
            payload("sensor.battery_maximum_charge", "nan"),
            "non-finite",
            id="non-finite-state",
        ),
        pytest.param(
            payload("sensor.battery_maximum_soc", 11),
            "exceeds capacity",
            id="soc-limit-above-capacity",
        ),
        pytest.param(
            payload("sensor.battery_soc", 50, "2026-01-01T05:00:00"),
            "must include a timezone",
            id="naive-observation-timestamp",
        ),
    ],
)
def test_fetch_rejects_invalid_observations(
    replacement: dict[str, object], message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        fetch(serve(standard_payloads(replacement)))


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param(
            httpx.Response(401), "authentication failed", id="authentication-failure"
        ),
        pytest.param(httpx.Response(404), "was not found", id="missing-entity"),
        pytest.param(
            httpx.Response(200, content=b"not-json"),
            "malformed JSON",
            id="malformed-json",
        ),
    ],
)
def test_fetch_reports_failed_and_malformed_responses(
    response: httpx.Response, message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        fetch(lambda _: response)


def test_fetch_reports_missing_mapped_attribute() -> None:
    mapping = {
        **BATTERY_MAPPINGS,
        "capacity": {
            "entity_id": "sensor.battery_capacity",
            "attribute": "capacity_kwh",
            "unit": "kWh",
        },
    }

    with pytest.raises(HomeAssistantError, match="missing.*attribute"):
        fetch(serve(standard_payloads()), battery=mapping)


def test_fetch_does_not_return_partial_data_when_one_entity_fails() -> None:
    calls = 0
    served = serve(standard_payloads())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return served(request) if calls == 1 else httpx.Response(503)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        fetch(handler)

    assert calls == 2


def test_freshness_uses_the_oldest_mapped_observation() -> None:
    responses = standard_payloads(
        payload("sensor.battery_capacity", 10, "2026-01-01T03:00:00+00:00")
    )
    provider, client = importer(
        httpx.MockTransport(serve(responses)), max_data_age_seconds=7200
    )
    with client:
        data = provider.fetch(now=START)

    assert data.latest_observation_at == datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
    assert not provider.is_fresh(data, now=START)


def test_fetch_requires_timezone_aware_retrieval_time() -> None:
    with pytest.raises(HomeAssistantError, match="timezone"):
        fetch(lambda _: httpx.Response(200), now=datetime(2026, 1, 1, 5, 30))


def _calculated_mode_configuration() -> HomeAssistantConfiguration:
    def leg(name: str) -> dict[str, object]:
        entity = {
            "entity_id": f"sensor.{name}",
            "state_class": "total_increasing",
            "unit": "kWh",
        }
        return {
            "energy_in": aggregate_settings(add=[entity]),
            "energy_out": aggregate_settings(add=[entity]),
        }

    mapping: dict[str, object] = {
        **BATTERY_MAPPINGS,
        "efficiency_calculation": {
            "state_of_charge": {"entity_id": "sensor.battery_soc", "unit": "%"},
            "battery": leg("battery"),
            "inverter_charge": leg("charge"),
            "inverter_discharge": leg("discharge"),
        },
    }
    del mapping["battery_efficiency"]
    return configuration(battery=mapping)


INSUFFICIENT_EFFICIENCY = BatteryEfficiencyData(
    schema_version="1",
    status="insufficient_data",
    inverter_charge_efficiency=None,
    inverter_discharge_efficiency=None,
    battery_efficiency=None,
    round_trip_efficiency=None,
    history_start=START,
    history_end=START,
    battery_throughput_kwh=0,
    charge_throughput_kwh=0,
    discharge_throughput_kwh=0,
    complete_cycle_count=0,
    unit="ratio",
    source=SourceMetadata(provider="home-assistant", entity_id="battery_efficiency"),
    retrieved_at=START,
    latest_observation_at=START,
)
COMPLETED_EFFICIENCY = replace(
    INSUFFICIENT_EFFICIENCY,
    status="ok",
    inverter_charge_efficiency=0.9,
    inverter_discharge_efficiency=0.85,
    battery_efficiency=0.8,
    round_trip_efficiency=0.612,
    battery_throughput_kwh=10,
    charge_throughput_kwh=10,
    discharge_throughput_kwh=10,
    complete_cycle_count=1,
)


@pytest.mark.parametrize(
    ("efficiency_data", "expected"),
    [
        pytest.param(None, 0.95, id="before-the-first-complete-cycle"),
        pytest.param(INSUFFICIENT_EFFICIENCY, 0.95, id="insufficient-calculation"),
        pytest.param(COMPLETED_EFFICIENCY, 0.8, id="completed-calculation"),
    ],
)
def test_fetch_uses_the_calculated_battery_efficiency_or_the_95_percent_default(
    efficiency_data: BatteryEfficiencyData | None, expected: float
) -> None:
    responses = standard_payloads()
    del responses["sensor.battery_efficiency"]

    with httpx.Client(transport=httpx.MockTransport(serve(responses))) as client:
        provider = HomeAssistantBatteryImporter(
            _calculated_mode_configuration(), client
        )
        data = provider.fetch(now=START, efficiency_data=efficiency_data)

    assert data.battery_efficiency == expected
