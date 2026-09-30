"""Tests for the shared Home Assistant energy aggregate's combined hours."""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint, HourExclusion
from energy_optimizer.providers import home_assistant_energy
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
)
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantError,
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    Readings,
    aggregate_configuration,
    home_assistant_configuration_factory,
)
from home_assistant_fixtures import home_assistant_history_payload as history_payload

ADD_ENTITY_ID = "sensor.charging_battery_energy"
SUBTRACT_ENTITY_ID = "sensor.pv_yield"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)

configuration = home_assistant_configuration_factory()


def hour(index: int) -> datetime:
    return START + timedelta(hours=index)


def readings(*points: tuple[str, str]) -> Readings:
    """Return ``(time, state)`` points as readings stamped on the start day."""
    return [
        (datetime.fromisoformat(f"2026-01-01T{time}+00:00").isoformat(), state)
        for time, state in points
    ]


def hourly(*states: str) -> Readings:
    """Return one reading per hour from 00:00."""
    return [(hour(index).isoformat(), state) for index, state in enumerate(states)]


def entity(entity_id: str) -> dict[str, str]:
    return {"entity_id": entity_id, "state_class": "total_increasing", "unit": "kWh"}


def aggregate(
    add: Readings,
    subtract: Readings | None = None,
    hours: int = 1,
    *,
    part: Literal["net", "positive"] = "net",
    label: str = "household-load",
) -> HomeAssistantEnergySeries:
    """Aggregate the added entity's readings less the subtracted entity's, if any."""
    payloads = {ADD_ENTITY_ID: history_payload(ADD_ENTITY_ID, add)}
    if subtract is not None:
        payloads[SUBTRACT_ENTITY_ID] = history_payload(SUBTRACT_ENTITY_ID, subtract)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=payloads[request.url.params["filter_entity_id"]]
        )

    aggregation = aggregate_configuration(
        add=[entity(ADD_ENTITY_ID)],
        subtract=[] if subtract is None else [entity(SUBTRACT_ENTITY_ID)],
        part=part,
    )
    energy_aggregate = EnergyAggregate(aggregation, START, hour(hours), label=label)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            energy_aggregate.needs()
        )
    finally:
        client.close()
    return energy_aggregate.build(history)


def reasons(
    exclusions: tuple[HourExclusion, ...],
) -> list[tuple[datetime, list[tuple[str | None, str]]]]:
    return [
        (item.hour_start, [(cause.entity_id, cause.reason) for cause in item.causes])
        for item in exclusions
    ]


def patch_non_finite_entity(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Make the normalized series of every entity a single non-finite value."""
    series = HomeAssistantEnergySeries(
        start_time=START, values_kw=(value,), latest_observation_at=hour(1)
    )
    monkeypatch.setattr(
        home_assistant_energy, "normalize_counter_history", lambda *_: series
    )


def test_negative_net_sum_is_excluded_and_never_clamped() -> None:
    result = aggregate(
        # Battery charged 0.738 kWh from all sources this hour.
        hourly("0", "0.738"),
        # Combined PV yield produced 1.54 kWh, exceeding the battery charge;
        # the surplus was exported rather than stored.
        hourly("0", "1.54"),
        label="battery efficiency inverter_charge output",
    )

    assert result.start_time == START
    assert result.values_kw == (None,)
    assert result.clamped_hour_count == 0
    (exclusion,) = result.exclusions
    assert exclusion.hour_start == START
    (cause,) = exclusion.causes
    assert cause.reason == "combined_negative"
    assert cause.entity_id is None
    assert "battery efficiency inverter_charge output" in cause.message
    assert "-0.802 kWh" in cause.message
    assert "set part to positive" in cause.message
    # The components are listed with their signed energy so the sum is traceable.
    assert cause.data_point_count == 2
    assert [(point.entity_id, point.timestamp) for point in cause.data_points] == [
        (ADD_ENTITY_ID, START),
        (SUBTRACT_ENTITY_ID, START),
    ]
    assert [point.step_kwh for point in cause.data_points] == pytest.approx(
        [0.738, -1.54]
    )


@pytest.mark.parametrize(
    ("pv_yield", "expected_kwh", "expected_clamped_hours"),
    [
        # The battery charged 0.738 kWh; PV supplied part of it, the rest came
        # from AC, so the positive part is the plain difference.
        ("0.5", 0.238, 0),
        # PV exceeded the battery charge; nothing was charged from AC.
        ("1.54", 0.0, 1),
    ],
)
def test_positive_part_takes_the_positive_part_of_the_sum(
    pv_yield: str, expected_kwh: float, expected_clamped_hours: int
) -> None:
    result = aggregate(
        hourly("0", "0.738"),
        hourly("0", pv_yield),
        part="positive",
        label="battery efficiency inverter_charge output",
    )

    assert result.values_kw == pytest.approx((expected_kwh,))
    assert result.exclusions == ()
    assert result.clamped_hour_count == expected_clamped_hours


def test_positive_part_hour_stays_valid_next_to_an_excluded_hour() -> None:
    result = aggregate(
        readings(
            ("00:00", "0"),
            ("01:00", "0.5"),
            ("01:30", "unavailable"),
            ("02:30", "1"),
            ("03:00", "1.5"),
        ),
        readings(("00:00", "0"), ("01:00", "1"), ("03:00", "1")),
        3,
        part="positive",
        label="battery efficiency inverter_charge output",
    )

    # Hour 0 sums to -0.5 kWh and is clamped. The unavailable sample still
    # excludes the hours it is in force, so the positive part hides nothing.
    assert result.values_kw[0] == 0.0
    assert result.values_kw[1] is None
    assert result.clamped_hour_count == 1
    assert reasons(result.exclusions)[0] == (hour(1), [(ADD_ENTITY_ID, "unavailable")])


@pytest.mark.parametrize("part", ["net", "positive"])
def test_non_finite_sum_is_excluded_for_every_part(
    monkeypatch: pytest.MonkeyPatch, part: Literal["net", "positive"]
) -> None:
    patch_non_finite_entity(monkeypatch, -math.inf)

    result = aggregate(hourly("0", "0.738"), part=part)

    assert result.values_kw == (None,)
    assert result.clamped_hour_count == 0
    (cause,) = result.exclusions[0].causes
    assert cause.reason == "combined_not_finite"
    assert "set part to positive" not in cause.message


def test_a_float_rounding_error_below_zero_is_not_counted_as_a_clamp() -> None:
    result = aggregate(
        hourly("0", "0.3"), hourly("0", "0.30000000000000004"), part="positive"
    )

    assert result.values_kw == (0.0,)
    assert result.clamped_hour_count == 0


def test_clamped_hours_are_reported_in_the_aggregate_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=home_assistant_energy.__name__)

    aggregate(
        hourly("0", "0.738"),
        hourly("0", "1.54"),
        part="positive",
        label="battery efficiency inverter_charge output",
    )

    (summary,) = [
        record.getMessage()
        for record in caplog.records
        if "event=home_assistant_history_aggregate" in record.getMessage()
    ]
    assert "status=success" in summary
    assert "label=battery efficiency inverter_charge output" in summary
    assert "part=positive" in summary
    assert "excluded_hour_count=0" in summary
    assert "clamped_hour_count=1" in summary


def test_an_unconfigured_aggregation_is_rejected_with_its_label() -> None:
    with pytest.raises(
        HomeAssistantError,
        match="no Home Assistant household-load energy aggregation is configured",
    ):
        EnergyAggregate(None, START, hour(1), label="household-load")


def test_ordinary_positive_result_has_no_exclusions() -> None:
    result = aggregate(hourly("0", "1"), label="battery efficiency battery input")

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
    result = aggregate(hourly("0", "1"), hourly("0", subtracted))

    assert result.values_kw == expected
    assert [item.hour_start for item in result.exclusions] == (
        [START] if excluded else []
    )


def test_negative_hour_is_excluded_by_timestamp_and_other_hours_keep_values() -> None:
    result = aggregate(hourly("0", "1", "1.5", "4"), hourly("0", "0.5", "1.5", "2"), 3)

    assert result.start_time == START
    assert result.values_kw == (0.5, None, 2.0)
    assert reasons(result.exclusions) == [(hour(1), [(None, "combined_negative")])]


def test_an_excluded_entity_hour_excludes_the_combined_hour_with_every_cause() -> None:
    result = aggregate(
        readings(
            ("00:00", "0"),
            ("01:00", "1"),
            ("02:00", "2"),
            ("02:10", "abc"),
            ("03:00", "3"),
        ),
        readings(
            ("00:00", "0"),
            ("01:00", "0.5"),
            ("01:40", "unavailable"),
            ("02:20", "1"),
            ("03:00", "1.5"),
        ),
        3,
    )

    # Hour 1 is valid for the add entity and hour 2 for neither of them, but a
    # combined value needs every contributor, so no partial sum is returned.
    assert result.values_kw == (0.5, None, None)
    assert reasons(result.exclusions) == [
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
    result = aggregate(
        # Hour 0 is excluded here, but the other entity has no data before 02:00.
        readings(
            ("00:00", "0"),
            ("00:20", "unavailable"),
            ("00:40", "1"),
            ("02:00", "2"),
            ("03:00", "3"),
            ("04:00", "4"),
        ),
        readings(("01:30", "0.25"), ("02:30", "0.5"), ("03:30", "1")),
        4,
    )

    assert result.start_time == hour(2)
    assert result.values_kw == pytest.approx((0.75, 0.5))
    assert result.exclusions == ()


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_combined_value_is_excluded(
    monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    patch_non_finite_entity(monkeypatch, bad_value)

    result = aggregate(hourly("0", "0.738"))

    assert result.values_kw == (None,)
    (exclusion,) = result.exclusions
    assert exclusion.hour_start == START
    (cause,) = exclusion.causes
    assert cause.reason == "combined_not_finite"
    assert cause.entity_id is None
    assert "not finite" in cause.message
    assert [point.entity_id for point in cause.data_points] == [ADD_ENTITY_ID]
