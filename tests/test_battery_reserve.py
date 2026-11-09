"""The soft battery reserve for load forecast errors."""

import math
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from energy_optimizer.config import BatteryReserveConfiguration
from energy_optimizer.optimization import PlanStatus
from energy_optimizer.optimization.battery_reserve import (
    STANDARD_DEVIATION_PER_MEAN_ABSOLUTE_ERROR,
    load_error_kw,
    reserve_schedule,
)
from energy_optimizer.optimization.plan_generation import PlanGenerator
from energy_optimizer.storage import ProviderDataStore
from test_battery_optimization import battery
from test_optimization import TOLERANCE, problem, solve
from test_plan_generation import (
    _battery_configuration,
    flat_history,
    prices,
    pv_forecast,
    snapshot,
)
from test_plan_generation import battery as battery_data

SETTINGS = BatteryReserveConfiguration()


def test_no_reserve_is_kept_in_the_first_unplanned_hours() -> None:
    reserves = reserve_schedule(SETTINGS, 4, error_kw=1.0, usable_kwh=40.0)

    assert reserves[:2] == (0.0, 0.0)
    assert reserves[2:] == pytest.approx((1.0, math.sqrt(2)))


def test_the_reserve_grows_with_the_square_root_of_the_lead_time_up_to_the_cap() -> (
    None
):
    settings = BatteryReserveConfiguration(
        unplanned_hours=0, growth_cap_hours=4, confidence=2.0
    )

    reserves = reserve_schedule(settings, 6, error_kw=0.5, usable_kwh=40.0)

    assert reserves == pytest.approx((1.0, math.sqrt(2), math.sqrt(3), 2.0, 2.0, 2.0))


def test_the_reserve_never_exceeds_the_usable_energy() -> None:
    reserves = reserve_schedule(SETTINGS, 30, error_kw=10.0, usable_kwh=5.0)

    assert max(reserves) == 5.0


def test_the_load_error_comes_from_the_backtest_or_the_configuration() -> None:
    assert load_error_kw(SETTINGS, 0.3) == pytest.approx(
        0.3 * STANDARD_DEVIATION_PER_MEAN_ABSOLUTE_ERROR
    )
    assert load_error_kw(SETTINGS, None) == 0.0
    configured = BatteryReserveConfiguration(forecast_error_kw=0.8)
    assert load_error_kw(configured, 0.3) == 0.8


def test_the_second_penalty_must_not_be_below_the_first() -> None:
    with pytest.raises(ValidationError, match="second_penalty"):
        BatteryReserveConfiguration(
            first_penalty_eur_per_kwh=0.2, second_penalty_eur_per_kwh=0.1
        )


def _ending_state_of_charge_kwh(import_price_eur_per_kwh: float) -> float:
    """A 3 kWh evening load, a battery holding 3 kWh and a 2 kWh reserve whose
    halves cost 5 and 15 ct per kWh of the day's deepest shortfall."""
    reserved_battery = replace(
        battery(initial_soc_kwh=3.0, final_soc_kwh=0.0, maximum_discharge_kw=5.0),
        reserve_kwh=(2.0, 2.0),
        reserve_penalties_eur_per_kwh=(0.05, 0.15),
    )
    plan = solve(
        problem(
            (3.0, 0.0),
            (import_price_eur_per_kwh, import_price_eur_per_kwh),
            battery=reserved_battery,
        )
    )
    assert plan.status is PlanStatus.OPTIMAL
    last = plan.hours[-1].battery
    assert last is not None
    assert plan.hours[0].battery is not None
    assert plan.hours[0].battery.reserve_kwh == 2.0
    return last.state_of_charge_kwh


def test_an_expensive_import_still_empties_the_reserve() -> None:
    assert _ending_state_of_charge_kwh(0.40) == pytest.approx(0.0, abs=TOLERANCE)


def test_a_small_saving_keeps_the_more_expensive_half_of_the_reserve() -> None:
    assert _ending_state_of_charge_kwh(0.10) == pytest.approx(1.0, abs=TOLERANCE)


def test_a_saving_below_both_penalties_keeps_the_whole_reserve() -> None:
    assert _ending_state_of_charge_kwh(0.03) == pytest.approx(2.0, abs=TOLERANCE)


def test_the_plan_sizes_the_reserve_from_the_load_forecast_error(
    tmp_path: Path,
) -> None:
    configuration = _battery_configuration(
        reserve={"forecast_error_kw": 0.5, "unplanned_hours": 1}
    )

    problem_with_reserve = PlanGenerator(
        configuration, ProviderDataStore(tmp_path)
    ).planning_problem(
        snapshot(
            household_load=flat_history(1.0),
            pv_generation=pv_forecast([0.0] * 3),
            electricity_prices=prices([0.30, 0.30, 0.30]),
            battery=battery_data(efficiency=1.0),
        )
    )

    assert problem_with_reserve.battery is not None
    assert problem_with_reserve.battery.reserve_kwh == pytest.approx(
        (0.0, 0.5, 0.5 * math.sqrt(2))
    )
    assert problem_with_reserve.battery.reserve_penalties_eur_per_kwh == (0.05, 0.15)
