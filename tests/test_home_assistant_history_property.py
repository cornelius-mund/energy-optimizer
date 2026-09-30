"""Property-based tests for the shared Home Assistant history import.

A consumer that reads its window of a series shared with other consumers must get
exactly what it would have got from an independent request for that window: the
same hourly values, the same excluded hours, and the same causes.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hypothesis import event, given, settings
from hypothesis import strategies as st

from energy_optimizer.exclusions import ExcludedDataPoint, ExclusionCause
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
)
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantError,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    FakeHomeAssistant,
    aggregate_configuration,
    home_assistant_configuration_factory,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY_ID = "sensor.energy"
AGGREGATION = aggregate_configuration(
    add=[{"entity_id": ENTITY_ID, "state_class": "total_increasing", "unit": "kWh"}]
)
configuration = home_assistant_configuration_factory()

# Mostly rising readings, some resets (the counter restarts from a small value,
# which is a decrease), some large steps that only exceed the default maximum of
# 100 kWh together within one hour, some jumps above it in a single step, and
# some invalid samples of every kind.
STEP_KINDS = st.sampled_from(
    ["rise"] * 6
    + ["reset", "large", "jump", "unavailable", "garbage", "nan", "negative"]
)
INVALID_STATES = {
    "unavailable": "unavailable",
    "garbage": "n/a",
    "nan": "nan",
    "negative": "-1.000",
}


@st.composite
def counter_states(draw: st.DrawFn) -> list[tuple[datetime, str]]:
    steps = draw(
        st.lists(
            st.tuples(st.integers(1, 60), st.integers(0, 3000), STEP_KINDS),
            min_size=10,
            max_size=60,
        )
    )
    minute = draw(st.integers(0, 120))
    milli_kwh = draw(st.integers(0, 10_000))
    states: list[tuple[datetime, str]] = []
    for gap_minutes, rise_milli_kwh, kind in steps:
        minute += gap_minutes
        timestamp = BASE + timedelta(minutes=minute)
        if kind in INVALID_STATES:
            states.append((timestamp, INVALID_STATES[kind]))
            continue
        if kind == "reset":
            milli_kwh = rise_milli_kwh
        elif kind == "large":
            milli_kwh += 60_000
        elif kind == "jump":
            milli_kwh += 150_000
        else:
            milli_kwh += rise_milli_kwh
        states.append((timestamp, f"{milli_kwh / 1000:.3f}"))
    return states


# (start hour, length in hours, lookback in seconds); windows may begin before
# any history exists and may overlap or nest.
WINDOWS = st.lists(
    st.tuples(
        st.integers(-3, 24), st.integers(1, 12), st.sampled_from([0, 3600, 7200])
    ),
    min_size=1,
    max_size=3,
)


def build_or_none(
    aggregate: EnergyAggregate, history: HomeAssistantHistory
) -> HomeAssistantEnergySeries | None:
    try:
        return aggregate.build(history)
    except HomeAssistantError:
        return None


ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


def as_seen_by_an_independent_request(
    series: HomeAssistantEnergySeries, window_start: datetime
) -> HomeAssistantEnergySeries:
    """Hide the one thing that a shared series knows and a request cannot.

    Home Assistant answers a request with the state in force at its start,
    stamped with that start, so an independent request cannot tell when that state
    was really recorded. The shared series can, and the data points and messages
    of its exclusions report that earlier time. Clamping every time to the
    window start leaves everything else, such as values, excluded hours, reasons,
    and counts, to be compared exactly.
    """

    def clamp(timestamp: datetime | None) -> datetime | None:
        return None if timestamp is None else max(timestamp, window_start)

    def restamp_message(match: re.Match[str]) -> str:
        return max(datetime.fromisoformat(match.group()), window_start).isoformat()

    def restamp_point(point: ExcludedDataPoint) -> ExcludedDataPoint:
        timestamp = clamp(point.timestamp)
        assert timestamp is not None
        return replace(
            point,
            timestamp=timestamp,
            previous_timestamp=clamp(point.previous_timestamp),
        )

    def restamp_cause(cause: ExclusionCause) -> ExclusionCause:
        return replace(
            cause,
            message=ISO_TIMESTAMP.sub(restamp_message, cause.message),
            data_points=tuple(restamp_point(point) for point in cause.data_points),
        )

    return replace(
        series,
        exclusions=tuple(
            replace(item, causes=tuple(restamp_cause(cause) for cause in item.causes))
            for item in series.exclusions
        ),
    )


@settings(max_examples=200, deadline=None, derandomize=True)
@given(states=counter_states(), windows=WINDOWS)
def test_a_window_of_the_shared_series_equals_an_independent_fetch_of_it(
    states: list[tuple[datetime, str]], windows: list[tuple[int, int, int]]
) -> None:
    aggregates = [
        EnergyAggregate(
            AGGREGATION,
            BASE + timedelta(hours=start_hour),
            BASE + timedelta(hours=start_hour + length_hours),
            lookback_seconds,
            label="household-load",
        )
        for start_hour, length_hours, lookback_seconds in windows
    ]

    def import_needs(*imported: EnergyAggregate) -> HomeAssistantHistory:
        with FakeHomeAssistant({ENTITY_ID: states}).client() as client:
            return HomeAssistantHistoryImporter(configuration(), client).import_history(
                [need for aggregate in imported for need in aggregate.needs()]
            )

    shared_history = import_needs(*aggregates)
    shared = [build_or_none(aggregate, shared_history) for aggregate in aggregates]
    independent = [
        build_or_none(aggregate, import_needs(aggregate)) for aggregate in aggregates
    ]

    for series in independent:
        if series is None:
            event("consumer without a complete hour")
            continue
        for item in series.exclusions:
            for cause in item.causes:
                event(f"excluded for {cause.reason}")
    history_starts = [
        BASE + timedelta(hours=start_hour, seconds=-lookback_seconds)
        for start_hour, _, lookback_seconds in windows
    ]
    assert [
        None
        if series is None
        else as_seen_by_an_independent_request(series, history_start)
        for series, history_start in zip(shared, history_starts)
    ] == independent
