"""Tests for the shared Home Assistant energy aggregator's negative handling."""

from datetime import datetime, timezone

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.providers.home_assistant_energy import (
    HomeAssistantEnergyAggregator,
    HomeAssistantError,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
)

ADD_ENTITY_ID = "sensor.charging_battery_energy"
SUBTRACT_ENTITY_ID = "sensor.pv_yield"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

configuration = home_assistant_configuration_factory()


def entity(entity_id: str, operation: str) -> HomeAssistantEnergyEntityConfiguration:
    return HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": entity_id,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": operation,
        }
    )


def _aggregator(
    handler: httpx.MockTransport,
) -> tuple[HomeAssistantEnergyAggregator, httpx.Client]:
    client = httpx.Client(transport=handler)
    return HomeAssistantEnergyAggregator(configuration(), client), client


def _pv_surplus_responses() -> dict[str, list[list[dict[str, object]]]]:
    return {
        # Battery charged 0.738 kWh from all sources this hour.
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.738"),
            ],
        ),
        # Combined PV yield produced 1.54 kWh, exceeding the battery charge;
        # the surplus was exported rather than stored.
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1.54"),
            ],
        ),
    }


def test_allow_negative_clamps_a_directional_net_to_zero() -> None:
    responses = _pv_surplus_responses()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    aggregator, client = _aggregator(httpx.MockTransport(handler))
    try:
        result = aggregator.aggregate(
            [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
            START,
            END,
            label="battery efficiency inverter_charge output",
            allow_negative=True,
        )
    finally:
        client.close()

    assert result.values_kw == (0.0,)


def test_default_behavior_still_rejects_a_negative_combined_value() -> None:
    responses = _pv_surplus_responses()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    aggregator, client = _aggregator(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="negative"):
            aggregator.aggregate(
                [
                    entity(ADD_ENTITY_ID, "add"),
                    entity(SUBTRACT_ENTITY_ID, "subtract"),
                ],
                START,
                END,
                label="household-load",
            )
    finally:
        client.close()


def test_allow_negative_does_not_change_an_ordinary_positive_result() -> None:
    responses = {
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    aggregator, client = _aggregator(httpx.MockTransport(handler))
    try:
        result = aggregator.aggregate(
            [entity(ADD_ENTITY_ID, "add")],
            START,
            END,
            label="battery efficiency battery input",
            allow_negative=True,
        )
    finally:
        client.close()

    assert result.values_kw == (1.0,)
