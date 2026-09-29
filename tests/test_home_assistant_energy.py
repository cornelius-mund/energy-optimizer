"""Tests for the shared Home Assistant energy aggregate's negative handling."""

import logging
import math
import re
from datetime import datetime, timezone

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.providers import home_assistant_energy
from energy_optimizer.providers.home_assistant_energy import HomeAssistantEnergySeries
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.interfaces import IntervalQuality
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
    home_assistant_suspect_negative_hour_readings,
    import_and_aggregate,
)

ADD_ENTITY_ID = "sensor.charging_battery_energy"
SUBTRACT_ENTITY_ID = "sensor.pv_yield"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
THREE_HOURS_END = datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
CLAMP_EVENT = "event=home_assistant_negative_hour_clamped"

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


def _aggregate(
    handler: httpx.MockTransport,
    entities: list[HomeAssistantEnergyEntityConfiguration],
    end_time: datetime,
    *,
    label: str,
    allow_negative: bool = False,
) -> HomeAssistantEnergySeries:
    client = httpx.Client(transport=handler)
    try:
        return import_and_aggregate(
            configuration(),
            client,
            entities,
            START,
            end_time,
            label=label,
            allow_negative=allow_negative,
        )
    finally:
        client.close()


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

    result = _aggregate(
        httpx.MockTransport(handler),
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        END,
        label="battery efficiency inverter_charge output",
        allow_negative=True,
    )

    assert result.values_kw == (0.0,)


def test_default_behavior_still_rejects_a_negative_combined_value() -> None:
    responses = _pv_surplus_responses()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    with pytest.raises(
        HomeAssistantError,
        match=re.escape("negative value at 2026-01-01T00:00:00+00:00"),
    ):
        _aggregate(
            httpx.MockTransport(handler),
            [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
            END,
            label="household-load",
        )


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

    result = _aggregate(
        httpx.MockTransport(handler),
        [entity(ADD_ENTITY_ID, "add")],
        END,
        label="battery efficiency battery input",
        allow_negative=True,
    )

    assert result.values_kw == (1.0,)


def _suspect_negative_hour_responses(
    *, add_side_valid: bool = False
) -> dict[str, list[list[dict[str, object]]]]:
    add_readings, subtract_readings = home_assistant_suspect_negative_hour_readings(
        add_side_valid=add_side_valid
    )
    return {
        ADD_ENTITY_ID: home_assistant_history_payload(ADD_ENTITY_ID, add_readings),
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID, subtract_readings
        ),
    }


def _aggregate_three_hours(
    responses: dict[str, list[list[dict[str, object]]]],
    *,
    allow_negative: bool = False,
) -> HomeAssistantEnergySeries:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    return _aggregate(
        httpx.MockTransport(handler),
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        THREE_HOURS_END,
        label="household-load",
        allow_negative=allow_negative,
    )


def _clamp_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(CLAMP_EVENT)
    ]


def test_negative_hour_flagged_suspect_by_its_contributors_is_clamped() -> None:
    result = _aggregate_three_hours(_suspect_negative_hour_responses())

    assert result.start_time == START
    assert result.values_kw == pytest.approx((0.0, 0.5, 1.0))
    assert [item.status for item in result.quality] == ["suspect", "valid", "valid"]
    assert result.quality[0].reason in {"physical_limit_exceeded", "counter_reset"}


def test_negative_hour_is_clamped_when_only_one_contributor_is_suspect() -> None:
    result = _aggregate_three_hours(
        _suspect_negative_hour_responses(add_side_valid=True)
    )

    assert result.values_kw == pytest.approx((0.0, 0.5, 1.0))
    assert [item.status for item in result.quality] == ["suspect", "valid", "valid"]
    assert result.quality[0].reason == "counter_reset"
    assert result.quality[0].entity_id == SUBTRACT_ENTITY_ID


def test_clamped_hour_emits_one_warning_naming_timestamp_and_reasons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    _aggregate_three_hours(_suspect_negative_hour_responses())

    warnings = _clamp_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0]
    assert "label=household-load" in message
    assert "clamped_hour_count=1" in message
    assert "2026-01-01T00:00:00+00:00[" in message
    assert f"physical_limit_exceeded:{ADD_ENTITY_ID}" in message
    assert f"counter_reset:{SUBTRACT_ENTITY_ID}" in message
    assert "2026-01-01T01:00:00+00:00" not in message


def test_allow_negative_does_not_log_clamped_hours(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    result = _aggregate_three_hours(
        _suspect_negative_hour_responses(), allow_negative=True
    )

    assert result.values_kw == pytest.approx((0.0, 0.5, 1.0))
    assert _clamp_warnings(caplog) == []


def test_negative_hour_without_suspect_contribution_still_fails_by_timestamp() -> None:
    responses = _suspect_negative_hour_responses()
    responses[ADD_ENTITY_ID] = home_assistant_history_payload(
        ADD_ENTITY_ID,
        [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "1"),
            ("2026-01-01T02:00:00+00:00", "1.5"),
            ("2026-01-01T03:00:00+00:00", "4"),
        ],
    )
    responses[SUBTRACT_ENTITY_ID] = home_assistant_history_payload(
        SUBTRACT_ENTITY_ID,
        [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "0.5"),
            ("2026-01-01T02:00:00+00:00", "1.5"),
            ("2026-01-01T03:00:00+00:00", "2"),
        ],
    )

    with pytest.raises(
        HomeAssistantError,
        match=re.escape("negative value at 2026-01-01T01:00:00+00:00"),
    ):
        _aggregate_three_hours(responses)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_combined_value_is_rejected_even_when_flagged_suspect(
    monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    def normalize_flagged_entity(
        entity: HomeAssistantEnergyEntityConfiguration, *_: object
    ) -> HomeAssistantEnergySeries:
        return HomeAssistantEnergySeries(
            start_time=START,
            values_kw=(bad_value,),
            latest_observation_at=END,
            quality=(
                IntervalQuality(
                    status="suspect",
                    reason="counter_reset",
                    entity_id=entity.entity_id,
                ),
            ),
        )

    monkeypatch.setattr(
        home_assistant_energy, "normalize_counter_history", normalize_flagged_entity
    )
    responses = _pv_surplus_responses()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    with pytest.raises(
        HomeAssistantError,
        match=re.escape("non-finite value at 2026-01-01T00:00:00+00:00"),
    ):
        _aggregate(
            httpx.MockTransport(handler),
            [entity(ADD_ENTITY_ID, "add")],
            END,
            label="household-load",
        )
