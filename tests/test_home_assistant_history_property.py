"""Property-based tests for the shared Home Assistant history import.

A consumer that reads its window of a series shared with other consumers must get
exactly what it would have got from an independent request for that window.
"""

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
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
    home_assistant_configuration_factory,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY_ID = "sensor.energy"
ENTITY = HomeAssistantEnergyEntityConfiguration.model_validate(
    {
        "entity_id": ENTITY_ID,
        "state_class": "total_increasing",
        "unit": "kWh",
        "operation": "add",
    }
)
configuration = home_assistant_configuration_factory()

# Mostly rising readings, some resets (the counter restarts from a small value)
# and some unavailable samples.
STEP_KINDS = st.sampled_from(["rise"] * 8 + ["reset", "unavailable"])


@st.composite
def counter_states(draw: st.DrawFn) -> list[tuple[datetime, str]]:
    """Draw the state changes of one counter over about five days."""
    steps = draw(
        st.lists(
            st.tuples(st.integers(1, 200), st.integers(0, 3000), STEP_KINDS),
            min_size=1,
            max_size=40,
        )
    )
    minute = draw(st.integers(0, 120))
    milli_kwh = draw(st.integers(0, 10_000))
    states: list[tuple[datetime, str]] = []
    for gap_minutes, rise_milli_kwh, kind in steps:
        minute += gap_minutes
        timestamp = BASE + timedelta(minutes=minute)
        if kind == "unavailable":
            states.append((timestamp, "unavailable"))
            continue
        milli_kwh = rise_milli_kwh if kind == "reset" else milli_kwh + rise_milli_kwh
        states.append((timestamp, f"{milli_kwh / 1000:.3f}"))
    return states


# (start hour, length in hours, lookback in seconds); windows may begin before
# any history exists and may overlap or nest.
WINDOWS = st.lists(
    st.tuples(
        st.integers(-3, 120), st.integers(1, 12), st.sampled_from([0, 3600, 7200])
    ),
    min_size=1,
    max_size=3,
)


def build_or_none(
    aggregate: EnergyAggregate, history: HomeAssistantHistory
) -> HomeAssistantEnergySeries | None:
    """Return the built series, or ``None`` when the consumer's build fails."""
    try:
        return aggregate.build(history)
    except HomeAssistantError:
        return None


@settings(max_examples=200, deadline=None, derandomize=True)
@given(states=counter_states(), windows=WINDOWS)
def test_a_window_of_the_shared_series_equals_an_independent_fetch_of_it(
    states: list[tuple[datetime, str]],
    windows: list[tuple[int, int, int]],
) -> None:
    aggregates = [
        EnergyAggregate(
            [ENTITY],
            BASE + timedelta(hours=start_hour),
            BASE + timedelta(hours=start_hour + length_hours),
            lookback_seconds,
            label="household-load",
        )
        for start_hour, length_hours, lookback_seconds in windows
    ]

    def import_needs(
        aggregates_to_import: list[EnergyAggregate],
    ) -> HomeAssistantHistory:
        client = FakeHomeAssistant({ENTITY_ID: states}).client()
        try:
            return HomeAssistantHistoryImporter(configuration(), client).import_history(
                [
                    need
                    for aggregate in aggregates_to_import
                    for need in aggregate.needs()
                ]
            )
        finally:
            client.close()

    shared_history = import_needs(aggregates)
    shared = [build_or_none(aggregate, shared_history) for aggregate in aggregates]
    independent = [
        build_or_none(aggregate, import_needs([aggregate])) for aggregate in aggregates
    ]

    assert shared == independent
