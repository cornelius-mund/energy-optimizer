"""Tests for the shared Home Assistant energy aggregate's combined hours."""

import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.exclusions import ExcludedDataPoint, HourExclusion
from energy_optimizer.providers import home_assistant_energy
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
)
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
)

ADD_ENTITY_ID = "sensor.charging_battery_energy"
SUBTRACT_ENTITY_ID = "sensor.pv_yield"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
THREE_HOURS_END = datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
FOUR_HOURS_END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)

configuration = home_assistant_configuration_factory()

Responses = dict[str, list[list[dict[str, object]]]]


def entity(entity_id: str, operation: str) -> HomeAssistantEnergyEntityConfiguration:
    return HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": entity_id,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": operation,
        }
    )


def hour(index: int) -> datetime:
    return START + timedelta(hours=index)


def _aggregate(
    responses: Responses,
    entities: list[HomeAssistantEnergyEntityConfiguration],
    end_time: datetime,
    *,
    label: str,
) -> HomeAssistantEnergySeries:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    aggregate = EnergyAggregate(entities, START, end_time, label=label)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            aggregate.needs()
        )
    finally:
        client.close()
    return aggregate.build(history)


def _reasons(
    exclusions: tuple[HourExclusion, ...],
) -> list[tuple[datetime, list[tuple[str | None, str]]]]:
    return [
        (item.hour_start, [(cause.entity_id, cause.reason) for cause in item.causes])
        for item in exclusions
    ]


def _pv_surplus_responses() -> Responses:
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


def test_negative_combined_value_is_excluded_and_never_clamped() -> None:
    result = _aggregate(
        _pv_surplus_responses(),
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        END,
        label="battery efficiency inverter_charge output",
    )

    assert result.start_time == START
    assert result.values_kw == (None,)
    (exclusion,) = result.exclusions
    assert exclusion.hour_start == START
    (cause,) = exclusion.causes
    assert cause.reason == "combined_negative"
    assert cause.entity_id is None
    assert "battery efficiency inverter_charge output" in cause.message
    assert "-0.802 kWh" in cause.message
    # The components are listed with their signed energy so the sum is traceable.
    assert cause.data_point_count == 2
    assert [(point.entity_id, point.timestamp) for point in cause.data_points] == [
        (ADD_ENTITY_ID, START),
        (SUBTRACT_ENTITY_ID, START),
    ]
    assert [point.step_kwh for point in cause.data_points] == pytest.approx(
        [0.738, -1.54]
    )


def test_ordinary_positive_result_has_no_exclusions() -> None:
    responses = {
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
            ],
        ),
    }

    result = _aggregate(
        responses,
        [entity(ADD_ENTITY_ID, "add")],
        END,
        label="battery efficiency battery input",
    )

    assert result.values_kw == (1.0,)
    assert result.exclusions == ()


@pytest.mark.parametrize(
    ("subtracted", "expected", "excluded"),
    [
        # A combined value that is only a float rounding error below zero is zero.
        ("1.0000000001", (0.0,), False),
        ("1.000001", (None,), True),
    ],
)
def test_combined_value_below_zero_is_excluded_only_beyond_rounding_error(
    subtracted: str, expected: tuple[float | None, ...], excluded: bool
) -> None:
    responses = {
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
            ],
        ),
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", subtracted),
            ],
        ),
    }

    result = _aggregate(
        responses,
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        END,
        label="household-load",
    )

    assert result.values_kw == expected
    assert [item.hour_start for item in result.exclusions] == (
        [START] if excluded else []
    )


def _negative_middle_hour_responses() -> Responses:
    return {
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "1.5"),
                ("2026-01-01T03:00:00+00:00", "4"),
            ],
        ),
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "1.5"),
                ("2026-01-01T03:00:00+00:00", "2"),
            ],
        ),
    }


def test_negative_hour_is_excluded_by_timestamp_and_other_hours_keep_values() -> None:
    result = _aggregate(
        _negative_middle_hour_responses(),
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        THREE_HOURS_END,
        label="household-load",
    )

    assert result.start_time == START
    assert result.values_kw == (0.5, None, 2.0)
    assert _reasons(result.exclusions) == [(hour(1), [(None, "combined_negative")])]


def test_an_excluded_entity_hour_excludes_the_combined_hour_with_every_cause() -> None:
    responses = {
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "2"),
                ("2026-01-01T02:10:00+00:00", "abc"),
                ("2026-01-01T03:00:00+00:00", "3"),
            ],
        ),
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T01:40:00+00:00", "unavailable"),
                ("2026-01-01T02:20:00+00:00", "1"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
            ],
        ),
    }

    result = _aggregate(
        responses,
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        THREE_HOURS_END,
        label="household-load",
    )

    # Hour 1 is valid for the add entity and hour 2 for neither of them, but a
    # combined value needs every contributor, so no partial sum is returned.
    assert result.values_kw == (0.5, None, None)
    assert _reasons(result.exclusions) == [
        (hour(1), [(SUBTRACT_ENTITY_ID, "unavailable")]),
        (
            hour(2),
            [(ADD_ENTITY_ID, "non_numeric"), (SUBTRACT_ENTITY_ID, "unavailable")],
        ),
    ]
    add_cause = result.exclusions[1].causes[0]
    assert add_cause.data_points == (
        ExcludedDataPoint(
            datetime(2026, 1, 1, 2, 10, tzinfo=timezone.utc), state="abc", unit="kWh"
        ),
    )


def test_exclusions_before_the_common_start_are_not_reported() -> None:
    responses = {
        # Hour 0 is excluded here, but the other entity has no data before 02:00.
        ADD_ENTITY_ID: home_assistant_history_payload(
            ADD_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T00:20:00+00:00", "unavailable"),
                ("2026-01-01T00:40:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "2"),
                ("2026-01-01T03:00:00+00:00", "3"),
                ("2026-01-01T04:00:00+00:00", "4"),
            ],
        ),
        SUBTRACT_ENTITY_ID: home_assistant_history_payload(
            SUBTRACT_ENTITY_ID,
            [
                ("2026-01-01T01:30:00+00:00", "0.25"),
                ("2026-01-01T02:30:00+00:00", "0.5"),
                ("2026-01-01T03:30:00+00:00", "1"),
            ],
        ),
    }

    result = _aggregate(
        responses,
        [entity(ADD_ENTITY_ID, "add"), entity(SUBTRACT_ENTITY_ID, "subtract")],
        FOUR_HOURS_END,
        label="household-load",
    )

    assert result.start_time == hour(2)
    assert result.values_kw == pytest.approx((0.75, 0.5))
    assert result.exclusions == ()


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_combined_value_is_excluded(
    monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    def normalize_non_finite_entity(
        entity: HomeAssistantEnergyEntityConfiguration, *_: object
    ) -> HomeAssistantEnergySeries:
        return HomeAssistantEnergySeries(
            start_time=START, values_kw=(bad_value,), latest_observation_at=END
        )

    monkeypatch.setattr(
        home_assistant_energy, "normalize_counter_history", normalize_non_finite_entity
    )

    result = _aggregate(
        _pv_surplus_responses(),
        [entity(ADD_ENTITY_ID, "add")],
        END,
        label="household-load",
    )

    assert result.values_kw == (None,)
    (exclusion,) = result.exclusions
    assert exclusion.hour_start == START
    (cause,) = exclusion.causes
    assert cause.reason == "combined_not_finite"
    assert cause.entity_id is None
    assert "not finite" in cause.message
    assert [point.entity_id for point in cause.data_points] == [ADD_ENTITY_ID]
