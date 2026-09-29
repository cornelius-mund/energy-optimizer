"""Tests for measurement jitter in ``total``-class Home Assistant energy counters.

Regression tests for issue #179: a single 1 Wh decrease in a ``total`` counter
that exposes no ``last_reset`` used to fail the whole import.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.providers.home_assistant_energy import (
    HomeAssistantEnergyAggregator,
    HomeAssistantEnergySeries,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
)

ENTITY_ID = "sensor.charging_battery_energy"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ONE_HOUR = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
TWO_HOURS = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
JITTER_EVENT = "event=home_assistant_counter_jitter_tolerated"

configuration = home_assistant_configuration_factory()


def entity(
    state_class: str = "total", unit: str = "kWh", **settings: Any
) -> HomeAssistantEnergyEntityConfiguration:
    return HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": ENTITY_ID,
            "state_class": state_class,
            "unit": unit,
            "operation": "add",
            **settings,
        }
    )


def aggregate(
    readings: list[tuple[str, str]],
    *,
    end: datetime = ONE_HOUR,
    state_class: str = "total",
    unit: str = "kWh",
    last_resets: list[str | None] | None = None,
    **settings: Any,
) -> HomeAssistantEnergySeries:
    payload = home_assistant_history_payload(
        ENTITY_ID,
        readings,
        unit=unit,
        state_class=state_class,
        last_resets=last_resets,
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        return HomeAssistantEnergyAggregator(configuration(), client).aggregate(
            [entity(state_class, unit, **settings)],
            START,
            end,
            label="battery efficiency battery input",
        )
    finally:
        client.close()


def test_a_one_watt_hour_dip_is_ignored_and_its_recovery_is_not_counted() -> None:
    # Observed on a real installation: 3280.294 -> 3280.293 kWh with no
    # last_reset. The counter returns to 3280.294 at the next sample.
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "3280.000"),
            ("2026-01-01T00:20:00+00:00", "3280.294"),
            ("2026-01-01T00:20:12+00:00", "3280.293"),
            ("2026-01-01T00:20:24+00:00", "3280.294"),
            ("2026-01-01T00:40:00+00:00", "3280.500"),
            ("2026-01-01T01:00:00+00:00", "3280.600"),
        ]
    )

    # The hour holds exactly the counter's net rise, not 0.601 kWh.
    assert series.values_kw == pytest.approx((0.6,), abs=1e-9)
    assert all(value >= 0 for value in series.values_kw)
    # Jitter is not a data-quality problem: a suspect hour would block
    # optimization.
    assert series.quality == ()


def test_a_dip_that_recovers_in_the_next_hour_does_not_add_energy_there() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:30:00+00:00", "10.500"),
            ("2026-01-01T00:59:50+00:00", "10.499"),
            ("2026-01-01T01:00:02+00:00", "10.500"),
            ("2026-01-01T01:30:00+00:00", "10.800"),
            ("2026-01-01T02:00:00+00:00", "11.000"),
        ],
        end=TWO_HOURS,
    )

    assert series.values_kw == pytest.approx((0.5, 0.5), abs=1e-9)
    assert series.quality == ()


def test_a_dip_that_lasts_several_samples_is_measured_against_the_peak() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:10:00+00:00", "10.500"),
            ("2026-01-01T00:10:12+00:00", "10.499"),
            ("2026-01-01T00:10:24+00:00", "10.498"),
            ("2026-01-01T00:10:36+00:00", "10.499"),
            ("2026-01-01T00:10:48+00:00", "10.500"),
            ("2026-01-01T00:50:00+00:00", "10.700"),
        ]
    )

    assert series.values_kw == pytest.approx((0.7,), abs=1e-9)
    assert series.quality == ()


def test_a_dip_that_recovers_above_the_peak_counts_only_the_excess() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:10:00+00:00", "10.500"),
            ("2026-01-01T00:10:12+00:00", "10.499"),
            ("2026-01-01T00:10:24+00:00", "10.510"),
        ]
    )

    assert series.values_kw == pytest.approx((0.51,), abs=1e-9)


def test_a_dip_around_unavailable_samples_is_still_not_counted_twice() -> None:
    payload = home_assistant_history_payload(
        ENTITY_ID,
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:10:00+00:00", "10.500"),
            ("2026-01-01T00:10:12+00:00", "10.499"),
            ("2026-01-01T00:10:24+00:00", "unavailable"),
            ("2026-01-01T00:10:36+00:00", "10.500"),
            ("2026-01-01T00:30:00+00:00", "10.600"),
        ],
        state_class="total",
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        series = HomeAssistantEnergyAggregator(configuration(), client).aggregate(
            [entity()], START, ONE_HOUR, label="battery efficiency battery input"
        )
    finally:
        client.close()

    assert series.values_kw == pytest.approx((0.6,), abs=1e-9)


def test_tolerance_is_compared_in_kwh_for_a_wh_counter() -> None:
    # A 5 Wh dip is 0.005 kWh, within the 0.01 kWh default. Compared in the
    # counter's own unit (5 > 0.01) it would wrongly be rejected.
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "3280000"),
            ("2026-01-01T00:10:00+00:00", "3280294"),
            ("2026-01-01T00:10:12+00:00", "3280289"),
            ("2026-01-01T00:10:24+00:00", "3280294"),
            ("2026-01-01T00:40:00+00:00", "3280500"),
        ],
        unit="Wh",
    )

    assert series.values_kw == pytest.approx((0.5,), abs=1e-9)


def test_tolerance_is_compared_in_kwh_for_an_mwh_counter() -> None:
    # A 0.0001 MWh dip is 0.1 kWh, above the 0.01 kWh default. Compared in the
    # counter's own unit (0.0001 < 0.01) it would wrongly be accepted as jitter.
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "3.2800"),
            ("2026-01-01T00:10:00+00:00", "3.2799"),
        ],
        unit="MWh",
    )

    assert series.values_kw == (0.0,)
    assert series.quality[0].reason == "counter_reset"


def test_energy_equals_the_net_rise_above_the_peak_for_any_jitter_pattern() -> None:
    """Property: jitter within the tolerance never adds or removes energy.

    Each generated counter rises by whole watt hours and every sample may fall
    up to 10 Wh (the default tolerance) below the true value. The counted energy
    must equal the highest value reached minus the first reading, whatever the
    pattern of dips and recoveries. The seed keeps the test deterministic.
    """
    generator = random.Random(179)
    for _ in range(200):
        true_wh = 0
        first_wh = 1_000_000 - generator.randint(0, 10)
        observed_wh = [first_wh]
        for _ in range(generator.randint(1, 60)):
            true_wh += generator.choice([0, 0, 1, 5, 50])
            observed_wh.append(1_000_000 + true_wh - generator.randint(0, 10))
        start = START
        readings = [
            ((start + timedelta(seconds=12 * index)).isoformat(), f"{value / 1000:.3f}")
            for index, value in enumerate(observed_wh)
        ]

        series = aggregate(readings)

        expected_kwh = (max(observed_wh) - first_wh) / 1000
        assert series.values_kw == pytest.approx((expected_kwh,), abs=1e-9), readings
        assert series.quality == ()


def test_a_larger_decrease_does_not_fail_and_marks_the_hour_suspect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        series = aggregate(
            [
                ("2026-01-01T00:00:00+00:00", "10.000"),
                ("2026-01-01T00:20:00+00:00", "10.500"),
                ("2026-01-01T00:40:00+00:00", "9.000"),
                ("2026-01-01T00:50:00+00:00", "9.300"),
            ]
        )

    # The decreasing step adds nothing, and growth from the new level counts.
    assert series.values_kw == pytest.approx((0.8,), abs=1e-9)
    assert series.quality[0].status == "suspect"
    assert series.quality[0].reason == "counter_reset"
    assert series.quality[0].entity_id == ENTITY_ID
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if "event=home_assistant_counter_reset" in record.getMessage()
    ]
    assert f"entity_id={ENTITY_ID}" in warning
    assert "timestamp=2026-01-01T00:40:00+00:00" in warning
    assert "previous_value=10.5" in warning
    assert "current_value=9.0" in warning
    assert "decrease_kwh=1.500000" in warning
    assert "decrease_tolerance_kwh=0.01" in warning


def test_a_larger_decrease_leaves_every_other_hour_intact() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:20:00+00:00", "10.500"),
            ("2026-01-01T00:40:00+00:00", "9.000"),
            ("2026-01-01T01:00:00+00:00", "9.500"),
            ("2026-01-01T01:30:00+00:00", "10.000"),
            ("2026-01-01T02:00:00+00:00", "10.500"),
        ],
        end=TWO_HOURS,
    )

    assert series.values_kw == pytest.approx((1.0, 1.0), abs=1e-9)
    assert [item.status for item in series.quality] == ["suspect", "valid"]


def test_a_return_to_the_peak_after_a_larger_decrease_is_not_energy() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:20:00+00:00", "10.500"),
            ("2026-01-01T00:30:00+00:00", "9.000"),
            ("2026-01-01T00:40:00+00:00", "10.500"),
            ("2026-01-01T00:50:00+00:00", "10.700"),
        ]
    )

    # The 1.5 kWh climb back to 10.5 is recovery, not new energy.
    assert series.values_kw == pytest.approx((0.7,), abs=1e-9)
    assert series.quality[0].status == "suspect"


def test_a_larger_decrease_after_a_dip_still_treats_the_return_as_recovery() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:10:00+00:00", "10.500"),
            ("2026-01-01T00:10:12+00:00", "10.499"),
            ("2026-01-01T00:20:00+00:00", "9.000"),
            ("2026-01-01T00:30:00+00:00", "10.500"),
            ("2026-01-01T00:40:00+00:00", "10.700"),
        ]
    )

    assert series.values_kw == pytest.approx((0.7,), abs=1e-9)


def test_a_larger_decrease_that_returns_to_the_earlier_value_is_a_spike() -> None:
    # The same transient-spike correction as for total_increasing counters.
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "0.0182"),
            ("2026-01-01T00:10:38+00:00", "57.2166"),
            ("2026-01-01T00:11:30+00:00", "0.0182"),
            ("2026-01-01T00:30:00+00:00", "0.5"),
        ]
    )

    assert series.values_kw == pytest.approx((0.4818,), abs=1e-9)
    assert series.quality[0].reason == "transient_counter_spike"


def test_a_slow_drift_below_the_peak_is_not_accepted_step_by_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Each step is within the tolerance, but the counter ends 0.015 kWh below
    # its peak, so the accumulated decrease is a reset and not jitter.
    with caplog.at_level(logging.WARNING):
        series = aggregate(
            [
                ("2026-01-01T00:00:00+00:00", "10.000"),
                ("2026-01-01T00:10:00+00:00", "10.500"),
                ("2026-01-01T00:15:00+00:00", "10.495"),
                ("2026-01-01T00:20:00+00:00", "10.490"),
                ("2026-01-01T00:30:00+00:00", "10.485"),
                ("2026-01-01T00:50:00+00:00", "11.000"),
            ]
        )

    assert series.values_kw == pytest.approx((0.5 + 0.515,), abs=1e-9)
    assert series.quality[0].reason == "counter_reset"
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if "event=home_assistant_counter_reset" in record.getMessage()
    ]
    assert "timestamp=2026-01-01T00:30:00+00:00" in warning


def test_a_decrease_exactly_at_the_tolerance_is_accepted() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "3280.000"),
            ("2026-01-01T00:20:00+00:00", "3280.294"),
            ("2026-01-01T00:40:00+00:00", "3280.293"),
        ],
        decrease_tolerance_kwh=0.001,
    )

    assert series.values_kw == pytest.approx((0.294,), abs=1e-9)


@pytest.mark.parametrize("tolerance", [0.0005, 0])
def test_a_smaller_configured_tolerance_flags_the_same_dip_as_a_reset(
    tolerance: float,
) -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "3280.294"),
            ("2026-01-01T00:20:00+00:00", "3280.293"),
        ],
        decrease_tolerance_kwh=tolerance,
    )

    assert series.values_kw == (0.0,)
    assert series.quality[0].status == "suspect"
    assert series.quality[0].reason == "counter_reset"


def test_a_larger_configured_tolerance_accepts_a_bigger_dip() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:20:00+00:00", "10.500"),
            ("2026-01-01T00:30:00+00:00", "10.450"),
            ("2026-01-01T00:40:00+00:00", "10.500"),
            ("2026-01-01T00:50:00+00:00", "10.600"),
        ],
        decrease_tolerance_kwh=0.1,
    )

    assert series.values_kw == pytest.approx((0.6,), abs=1e-9)


def test_a_changed_last_reset_is_still_a_reset_not_jitter() -> None:
    reset_time = "2026-01-01T00:40:00+00:00"
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.0"),
            ("2026-01-01T00:20:00+00:00", "10.5"),
            ("2026-01-01T00:40:00+00:00", "10.499"),
            ("2026-01-01T01:00:00+00:00", "11.499"),
        ],
        last_resets=[None, None, reset_time, reset_time],
    )

    # The reset reading contributes nothing; growth from the new baseline does.
    assert series.values_kw == pytest.approx((1.5,), abs=1e-9)
    assert series.quality[0].status == "suspect"
    assert series.quality[0].reason == "counter_reset"


def test_a_reset_after_a_dip_does_not_reuse_the_pre_dip_peak() -> None:
    reset_time = "2026-01-01T00:40:00+00:00"
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:20:00+00:00", "10.500"),
            ("2026-01-01T00:30:00+00:00", "10.499"),
            ("2026-01-01T00:40:00+00:00", "0.100"),
            ("2026-01-01T00:50:00+00:00", "0.300"),
        ],
        last_resets=[None, None, None, reset_time, reset_time],
    )

    assert series.values_kw == pytest.approx((0.5 + 0.2,), abs=1e-9)
    assert series.quality[0].reason == "counter_reset"


def test_total_increasing_decrease_is_still_a_counter_reset() -> None:
    series = aggregate(
        [
            ("2026-01-01T00:00:00+00:00", "10.000"),
            ("2026-01-01T00:20:00+00:00", "10.500"),
            ("2026-01-01T00:40:00+00:00", "10.499"),
            ("2026-01-01T00:50:00+00:00", "10.699"),
        ],
        state_class="total_increasing",
    )

    assert series.values_kw == pytest.approx((0.7,), abs=1e-9)
    assert series.quality[0].status == "suspect"
    assert series.quality[0].reason == "counter_reset"


def test_tolerated_decreases_are_summarized_in_one_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        aggregate(
            [
                ("2026-01-01T00:00:00+00:00", "3280.000"),
                ("2026-01-01T00:10:00+00:00", "3280.294"),
                ("2026-01-01T00:10:12+00:00", "3280.293"),
                ("2026-01-01T00:10:24+00:00", "3280.294"),
                ("2026-01-01T00:20:00+00:00", "3280.400"),
                ("2026-01-01T00:20:12+00:00", "3280.398"),
                ("2026-01-01T00:20:24+00:00", "3280.400"),
            ]
        )

    summaries = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(JITTER_EVENT)
    ]
    assert len(summaries) == 1
    assert f"entity_id={ENTITY_ID}" in summaries[0]
    assert "decrease_count=2" in summaries[0]
    assert "first_timestamp=2026-01-01T00:10:12+00:00" in summaries[0]
    assert "largest_decrease_kwh=0.002000" in summaries[0]


def test_no_jitter_summary_is_logged_for_a_clean_counter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        aggregate(
            [
                ("2026-01-01T00:00:00+00:00", "1.0"),
                ("2026-01-01T00:30:00+00:00", "2.0"),
            ]
        )

    assert not [
        record for record in caplog.records if JITTER_EVENT in record.getMessage()
    ]
