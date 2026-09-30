"""The hour rule of the Home Assistant counter import.

An hour is imported only if every data point that contributes to it is valid. Every
other hour has no value and keeps every cause with its exact data points. These
tests exercise the pure normalization directly on cleaned samples, so each rule is
pinned independently of the HTTP layer.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.exclusions import (
    EXCLUSION_REASONS,
    MAX_DATA_POINTS_PER_ENTITY_HOUR,
    ExcludedDataPoint,
    ExclusionCause,
    HourExclusion,
    cap_data_points,
    exclusion_summary,
    merge_exclusions,
)
from energy_optimizer.providers.home_assistant_energy import (
    HomeAssistantEnergySeries,
    hour_index,
    normalize_counter_history,
    overlapping_hours,
)
from energy_optimizer.providers.home_assistant_history import (
    HistorySample,
    HomeAssistantError,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY_ID = "sensor.energy"
INVALID_STATES = {
    "unavailable": "unavailable",
    "unknown": "unavailable",
    "n/a": "non_numeric",
    "nan": "not_finite",
    "inf": "not_finite",
    "-1": "negative_value",
}


def entity(**overrides: object) -> HomeAssistantEnergyEntityConfiguration:
    return HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
            **overrides,
        }
    )


def at(hours: float) -> datetime:
    return BASE + timedelta(hours=hours)


def valid(
    hours: float,
    value: float,
    *,
    last_reset: datetime | None = None,
    unit: str | None = "kWh",
    state_class: str | None = "total_increasing",
) -> HistorySample:
    return HistorySample(
        at(hours), value, unit, state_class, last_reset, str(value), None
    )


def invalid(hours: float, state: str) -> HistorySample:
    return HistorySample(
        at(hours),
        None,
        "kWh",
        "total_increasing",
        None,
        state,
        INVALID_STATES[state],  # type: ignore[arg-type]
    )


def normalize(
    samples: list[HistorySample],
    hours: int,
    *,
    config: HomeAssistantEnergyEntityConfiguration | None = None,
) -> HomeAssistantEnergySeries:
    return normalize_counter_history(
        config or entity(), tuple(samples), at(0), at(hours)
    )


def excluded_hours(series: HomeAssistantEnergySeries) -> list[int]:
    return [
        int((item.hour_start - series.start_time) / timedelta(hours=1))
        for item in series.exclusions
    ]


def reasons(series: HomeAssistantEnergySeries, hour: int) -> list[str]:
    item = next(
        item
        for item in series.exclusions
        if item.hour_start == series.start_time + timedelta(hours=hour)
    )
    return [cause.reason for cause in item.causes]


@pytest.mark.parametrize(("state", "reason"), sorted(INVALID_STATES.items()))
def test_one_invalid_sample_excludes_only_its_hour(state: str, reason: str) -> None:
    samples = [valid(quarter / 4, quarter / 4) for quarter in range(0, 49)]
    samples[26] = invalid(6.5, state)

    series = normalize(samples, 12)

    assert series.values_kw == tuple(None if hour == 6 else 1.0 for hour in range(12))
    assert excluded_hours(series) == [6]
    [cause] = series.exclusions[0].causes
    assert cause.reason == reason
    assert cause.entity_id == ENTITY_ID
    assert [(point.timestamp, point.state) for point in cause.data_points] == [
        (at(6.5), state)
    ]
    assert cause.data_point_count == 1
    assert ENTITY_ID in cause.message


def test_an_outage_excludes_every_hour_it_is_in_force_including_the_return_hour() -> (
    None
):
    samples = [
        valid(0, 0),
        valid(4, 4),
        invalid(4.5, "unavailable"),
        valid(7.5, 12),
        valid(8, 13),
        valid(12, 17),
    ]

    series = normalize(samples, 12)

    assert excluded_hours(series) == [4, 5, 6, 7]
    assert series.values_kw == (
        0.0,
        0.0,
        0.0,
        4.0,
        None,
        None,
        None,
        None,
        0.0,
        0.0,
        0.0,
        4.0,
    )
    # The 8 kWh that accrued during the outage are attributed to no hour.
    assert sum(value or 0 for value in series.values_kw) == 8.0
    assert reasons(series, 7) == ["unavailable"]


def test_an_invalid_state_beginning_on_a_closing_boundary_excludes_that_hour() -> None:
    """A sensor publishing on the hour has no closing reading if it is unavailable."""
    series = normalize(
        [valid(0, 0), invalid(1, "unavailable"), valid(2, 2), valid(3, 3)], 3
    )

    assert excluded_hours(series) == [0, 1]
    assert series.values_kw == (None, None, 1.0)


def test_a_trailing_invalid_state_excludes_every_hour_to_the_end() -> None:
    series = normalize([valid(0, 0), valid(2, 2), invalid(3.5, "unknown")], 6)

    assert excluded_hours(series) == [3, 4, 5]
    assert series.values_kw[:3] == (0.0, 2.0, 0.0)
    assert "none in the imported period" in series.exclusions[0].causes[0].message


def test_an_invalid_state_in_force_at_the_start_excludes_until_a_valid_sample() -> None:
    baseline = HistorySample(
        at(0),
        None,
        "kWh",
        "total_increasing",
        None,
        "unavailable",
        "unavailable",
        observed_at=at(-3),
    )

    series = normalize([baseline, valid(2.5, 5), valid(4, 6)], 4)

    assert excluded_hours(series) == [0, 1, 2]
    assert series.values_kw[3] == 1.0
    [point] = series.exclusions[0].causes[0].data_points
    # The data point shows when Home Assistant recorded the state, not the window.
    assert point.timestamp == at(-3)


def test_a_one_watt_hour_decrease_excludes_only_the_hour_it_happens_in() -> None:
    series = normalize(
        [
            valid(0, 3280.294),
            valid(3.2, 3280.293),
            valid(3.4, 3280.294),
            valid(6, 3281.0),
            valid(12, 3282.0),
        ],
        12,
    )

    # Home Assistant records changes only, so the quiet hours before the change
    # are valid and carry no energy.
    assert excluded_hours(series) == [3]
    assert reasons(series, 3) == ["counter_decrease", "step_after_decrease"]
    assert series.values_kw[:3] == (0.0, 0.0, 0.0)
    [decrease, _] = series.exclusions[0].causes
    [point] = decrease.data_points
    assert point.previous_value == 3280.294
    assert point.value == 3280.293
    assert point.state == "3280.293"
    assert point.step_kwh == pytest.approx(-0.001)


def test_a_dropout_to_zero_and_back_excludes_its_hours_and_nothing_else() -> None:
    series = normalize(
        [valid(0, 700), valid(5.99, 700.1), valid(6.01, 0), valid(6.02, 700.25)], 8
    )

    assert series.values_kw == (0.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.0)
    assert reasons(series, 5) == ["counter_decrease"]
    assert reasons(series, 6) == [
        "counter_decrease",
        "step_after_decrease",
        "step_above_maximum",
    ]


def test_a_return_from_zero_within_the_maximum_is_not_trusted() -> None:
    series = normalize(
        [
            valid(0, 45),
            valid(5.99, 45.1),
            valid(6.01, 0),
            valid(7.5, 50),
            valid(9, 51),
        ],
        9,
        config=entity(maximum_interval_energy_kwh=1000),
    )

    # The 50 kWh step from zero cannot be told apart from a real reset followed
    # by consumption, so no energy is fabricated from it.
    assert series.values_kw == (0.0, 0.0, 0.0, 0.0, 0.0, None, None, None, 1.0)
    assert reasons(series, 7) == ["step_after_decrease"]


def test_a_step_equal_to_the_maximum_is_imported_and_a_larger_one_is_not() -> None:
    series = normalize(
        [valid(0, 0), valid(1, 100), valid(2, 201), valid(3, 202), valid(4, 203)], 4
    )

    assert series.values_kw == (100.0, None, 1.0, 1.0)
    assert reasons(series, 1) == ["step_above_maximum"]
    [point] = series.exclusions[0].causes[0].data_points
    assert point.step_kwh == pytest.approx(101.0)
    assert point.maximum_kwh == 100.0
    assert point.previous_value == 100
    assert point.value == 201


def test_an_hour_whose_steps_together_exceed_the_maximum_is_excluded() -> None:
    samples = [valid(0, 0)] + [valid(0.1 * step, 30 * step) for step in range(1, 5)]
    samples.append(valid(2, 121))

    series = normalize(samples, 2)

    assert series.values_kw == (None, 1.0)
    # No single step is above the maximum; only their sum is.
    assert reasons(series, 0) == ["hour_above_maximum"]
    [point] = series.exclusions[0].causes[0].data_points
    assert point.timestamp == at(0)
    assert point.step_kwh == pytest.approx(120.0)
    assert point.maximum_kwh == 100.0


def test_the_maximum_is_converted_from_the_unit_of_the_counter() -> None:
    config = entity(unit="Wh", maximum_interval_energy_kwh=2)

    series = normalize(
        [
            valid(0, 0, unit="Wh"),
            valid(1, 2000, unit="Wh"),
            valid(2, 4001, unit="Wh"),
        ],
        2,
        config=config,
    )

    assert series.values_kw == (2.0, None)
    assert reasons(series, 1) == ["step_above_maximum"]


def test_a_changed_last_reset_excludes_the_hours_of_both_observations() -> None:
    series = normalize(
        [
            valid(0, 5, last_reset=at(-10)),
            valid(3.5, 6, last_reset=at(3.4)),
            valid(6, 7, last_reset=at(3.4)),
        ],
        6,
        config=entity(state_class="total_increasing"),
    )

    assert excluded_hours(series) == [3]
    assert reasons(series, 3) == ["last_reset_changed"]
    assert series.values_kw[5] == 1.0


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (valid(2, 2, unit="Wh"), "unit_mismatch"),
        (valid(2, 2, unit=None), "unit_missing"),
        (valid(2, 2, state_class="total"), "state_class_mismatch"),
    ],
)
def test_a_sample_with_unexpected_attributes_is_an_invalid_sample(
    sample: HistorySample, reason: str
) -> None:
    series = normalize([valid(0, 0), sample, valid(3, 3), valid(4, 4)], 4)

    assert excluded_hours(series) == [1, 2]
    assert reasons(series, 1) == [reason]


def test_a_power_unit_or_duplicate_timestamps_or_no_samples_are_hard_errors() -> None:
    with pytest.raises(HomeAssistantError, match="instantaneous power unit"):
        normalize([valid(0, 0, unit="kW"), valid(1, 1, unit="kW")], 2)
    with pytest.raises(HomeAssistantError, match="duplicate timestamps"):
        normalize([valid(0, 0), valid(1, 1), valid(1, 2)], 2)
    with pytest.raises(HomeAssistantError, match="no history"):
        normalize([], 2)


def test_only_the_recorded_data_points_beyond_the_bound_are_dropped_not_counted() -> (
    None
):
    samples = [valid(0, 0)] + [
        invalid(0.1 + index * 0.001, "unavailable") for index in range(120)
    ]
    samples.append(valid(1.5, 1))

    series = normalize(samples, 2)

    assert excluded_hours(series) == [0, 1]
    [cause] = series.exclusions[0].causes
    assert cause.data_point_count == 120
    assert len(cause.data_points) == MAX_DATA_POINTS_PER_ENTITY_HOUR
    assert cause.data_points[0].timestamp == at(0.1)


def test_the_first_hour_starts_after_the_first_recorded_state() -> None:
    series = normalize_counter_history(
        entity(), (valid(2.5, 5), valid(4, 6), valid(6, 7)), at(0), at(6)
    )

    assert series.start_time == at(3)
    assert series.values_kw == (1.0, 0.0, 1.0)
    assert series.exclusions == ()


def test_hours_without_a_sample_are_valid_and_carry_no_energy() -> None:
    series = normalize([valid(0, 10), valid(5, 11)], 6)

    assert series.values_kw == (0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    assert series.exclusions == ()


def test_exclusion_helpers_merge_cap_and_count() -> None:
    first = ExclusionCause.of("unavailable", "a", "sensor.a", [])
    second = ExclusionCause.of("counter_decrease", "b", "sensor.b", [])
    hour = at(1)

    merged = merge_exclusions(
        [HourExclusion(at(2), (second,)), HourExclusion(hour, (first,))],
        [HourExclusion(hour, (first, second))],
    )

    assert [item.hour_start for item in merged] == [hour, at(2)]
    assert merged[0].causes == (first, second)
    assert exclusion_summary(merged) == {"counter_decrease": 2, "unavailable": 1}
    points = [ExcludedDataPoint(at(index / 100)) for index in range(80)]
    capped = cap_data_points(
        [
            ExclusionCause.of("unavailable", "x", "sensor.a", points),
            ExclusionCause.of("non_numeric", "y", "sensor.a", points),
            ExclusionCause.of("unavailable", "x", "sensor.b", points[:10]),
        ]
    )
    assert [len(cause.data_points) for cause in capped] == [50, 0, 10]
    assert [cause.data_point_count for cause in capped] == [80, 80, 10]


def test_hour_arithmetic_attributes_a_boundary_to_the_earlier_hour() -> None:
    assert hour_index(at(0), at(1)) == 0
    assert hour_index(at(0), at(1) + timedelta(seconds=1)) == 1
    assert list(overlapping_hours(at(0), at(1), at(2), 5)) == [0, 1]
    assert list(overlapping_hours(at(0), at(-3), at(0.5), 5)) == [0]
    assert list(overlapping_hours(at(0), at(2.5), at(9), 5)) == [2, 3, 4]
    assert list(overlapping_hours(at(0), at(-3), at(-1), 5)) == []


# --- the partition property -------------------------------------------------

EVENTS = st.one_of(
    st.tuples(st.just("rise"), st.floats(0, 3)),
    st.tuples(st.just("fall"), st.floats(0.0005, 60)),
    st.tuples(st.just("jump"), st.floats(80, 400)),
    st.tuples(st.just("invalid"), st.sampled_from(sorted(INVALID_STATES))),
)


@st.composite
def sample_series(draw: st.DrawFn) -> list[HistorySample]:
    hours_per_step = st.floats(0.01, 2.5)
    value = draw(st.floats(0, 1000))
    clock = 0.0
    samples = [valid(0, value)]
    for kind, amount in draw(st.lists(EVENTS, min_size=1, max_size=40)):
        clock += draw(hours_per_step)
        if kind == "invalid":
            samples.append(invalid(clock, str(amount)))
            continue
        if kind == "rise":
            value += float(amount)
        elif kind == "jump":
            value += float(amount)
        else:
            value = max(0.0, value - float(amount))
        samples.append(valid(clock, round(value, 6)))
    return samples


@given(sample_series())
@settings(max_examples=300, deadline=None)
def test_imported_and_excluded_hours_partition_the_covered_window(
    samples: list[HistorySample],
) -> None:
    hours = 12
    assume(len({sample.timestamp for sample in samples}) == len(samples))
    series = normalize_counter_history(entity(), tuple(samples), at(0), at(hours))

    excluded = set(excluded_hours(series))
    assert len(series.values_kw) == hours
    assert excluded == {
        hour for hour, value in enumerate(series.values_kw) if value is None
    }
    assert [item.hour_start for item in series.exclusions] == sorted(
        item.hour_start for item in series.exclusions
    )
    for item in series.exclusions:
        assert item.causes
        for cause in item.causes:
            assert cause.reason in EXCLUSION_REASONS
            assert cause.message
            assert cause.entity_id == ENTITY_ID
            assert cause.data_point_count >= len(cause.data_points)
            assert len(cause.data_points) <= MAX_DATA_POINTS_PER_ENTITY_HOUR

    for hour, value in enumerate(series.values_kw):
        if value is not None:
            assert math.isfinite(value) and 0 <= value <= 100

    # Every invalid sample inside the window excludes the hour it lies in.
    for sample in samples:
        if sample.invalid is not None and sample.timestamp <= at(hours):
            assert hour_index(at(0), sample.timestamp) in excluded or (
                hour_index(at(0), sample.timestamp) < 0
            )

    # Every decrease between two consecutive valid samples excludes the hours of
    # both of its observations.
    for earlier, later in zip(samples, samples[1:]):
        if (
            earlier.value is not None
            and later.value is not None
            and later.value < earlier.value
            and later.timestamp <= at(hours)
        ):
            for observation in (earlier, later):
                hour = hour_index(at(0), observation.timestamp)
                if 0 <= hour < hours:
                    assert hour in excluded

    # An imported hour holds exactly the energy of the steps attributed to it:
    # the rises between consecutive valid observations that end in that hour.
    for hour, value in enumerate(series.values_kw):
        if value is None:
            continue
        attributed = sum(
            later.value - earlier.value
            for earlier, later in zip(samples, samples[1:])
            if earlier.value is not None
            and later.value is not None
            and hour_index(at(0), later.timestamp) == hour
        )
        assert value == pytest.approx(attributed)
