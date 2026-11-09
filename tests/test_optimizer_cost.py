"""The cost of each planned hour as the optimizer values it, with the real solver."""

from dataclasses import replace

import pytest

from test_battery_optimization import battery
from test_ev_patterns import (
    FEED_IN_EUR_PER_KWH,
    IMPORT_EUR_PER_KWH,
    _valued_car,
)
from test_optimization import TOLERANCE, problem, solve

BATTERY_VALUE_EUR_PER_KWH = 0.20


def test_storing_pv_in_the_battery_is_an_optimizer_gain_without_cash() -> None:
    valued_battery = replace(
        battery(initial_soc_kwh=5.0, final_soc_kwh=0.0, maximum_charge_kw=5.0),
        terminal_value_eur_per_kwh=BATTERY_VALUE_EUR_PER_KWH,
    )

    plan = solve(
        problem(
            (0.0,),
            (IMPORT_EUR_PER_KWH,),
            pv_forecast_kw=(3.0,),
            export_price_eur_per_kwh=(FEED_IN_EUR_PER_KWH,),
            battery=valued_battery,
        )
    )

    (hour,) = plan.hours
    assert hour.import_cost_eur - hour.export_revenue_eur == pytest.approx(
        0.0, abs=TOLERANCE
    )
    assert hour.optimizer_cost_eur == pytest.approx(-3.0 * BATTERY_VALUE_EUR_PER_KWH)


def test_charging_the_car_from_pv_is_credited_with_the_stored_value() -> None:
    plan = solve(
        replace(
            problem(
                (1.0, 1.0),
                (IMPORT_EUR_PER_KWH,) * 2,
                pv_forecast_kw=(7.0, 0.0),
                export_price_eur_per_kwh=(FEED_IN_EUR_PER_KWH,) * 2,
            ),
            ev=_valued_car(state_of_charge_kwh=50.0, hours=2),
        )
    )

    sunny, dark = plan.hours
    assert sunny.optimizer_cost_eur == pytest.approx(-6.0 * 0.9 * IMPORT_EUR_PER_KWH)
    assert dark.optimizer_cost_eur == pytest.approx(dark.import_cost_eur)


def test_the_reserve_penalty_is_charged_once_at_the_deepest_hour() -> None:
    reserved_battery = replace(
        battery(initial_soc_kwh=3.0, final_soc_kwh=0.0, maximum_discharge_kw=5.0),
        reserve_kwh=(2.0, 2.0),
        reserve_penalties_eur_per_kwh=(0.05, 0.15),
    )

    plan = solve(problem((3.0, 0.0), (0.10, 0.10), battery=reserved_battery))

    cash = sum(hour.import_cost_eur - hour.export_revenue_eur for hour in plan.hours)
    optimizer = sum(hour.optimizer_cost_eur for hour in plan.hours)
    assert optimizer == pytest.approx(cash + 0.05 * 1.0)
    assert plan.hours[0].optimizer_cost_eur == pytest.approx(
        plan.hours[0].import_cost_eur + 0.05
    )
