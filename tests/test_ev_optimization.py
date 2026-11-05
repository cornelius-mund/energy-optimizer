"""EV deadline charging in the planning model, solved by the real HiGHS solver."""

from dataclasses import replace

import pytest

from energy_optimizer.optimization import EvSpecification, Plan, PlanStatus
from test_optimization import TOLERANCE, problem, solve

PRICES = (0.30, 0.10, 0.20, 0.40)
INITIAL_SOC_KWH = 10.0


def electric_vehicle(
    stored_kwh_needed: float,
    deadline_hours: int = 4,
    plugged_in: tuple[bool, ...] = (True, True, True, True),
    minimum_charge_kw: float = 1.4,
    maximum_charge_kw: float = 4.0,
    charge_efficiency: float = 0.9,
) -> EvSpecification:
    return EvSpecification(
        capacity_kwh=60.0,
        initial_soc_kwh=INITIAL_SOC_KWH,
        target_soc_kwh=INITIAL_SOC_KWH + stored_kwh_needed,
        deadline_hours=deadline_hours,
        plugged_in=plugged_in,
        minimum_charge_kw=minimum_charge_kw,
        maximum_charge_kw=maximum_charge_kw,
        charge_efficiency=charge_efficiency,
    )


def solve_with(
    ev: EvSpecification,
    prices: tuple[float, ...] = PRICES,
    pv_forecast_kw: tuple[float, ...] | None = None,
) -> Plan:
    hours = len(prices)
    return solve(
        replace(problem((0.0,) * hours, prices, pv_forecast_kw=pv_forecast_kw), ev=ev)
    )


def charged_kw(plan: Plan) -> list[float]:
    return [hour.ev.charge_kw for hour in plan.hours if hour.ev is not None]


def test_the_car_charges_in_the_cheapest_hour_when_one_hour_suffices() -> None:
    plan = solve_with(electric_vehicle(stored_kwh_needed=3.6))

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([0.0, 4.0, 0.0, 0.0], abs=TOLERANCE)
    assert ev_soc_kwh(plan, 1) == pytest.approx(13.6)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(4.0 * 0.10)


def test_the_car_adds_the_next_cheapest_hour_when_the_charger_power_is_not_enough() -> (
    None
):
    plan = solve_with(electric_vehicle(stored_kwh_needed=6.3))

    assert charged_kw(plan) == pytest.approx([0.0, 4.0, 3.0, 0.0], abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(4.0 * 0.10 + 3.0 * 0.20)


def test_the_charger_minimum_power_forces_more_energy_than_needed() -> None:
    plan = solve_with(electric_vehicle(stored_kwh_needed=1.0))

    assert charged_kw(plan) == pytest.approx([0.0, 1.4, 0.0, 0.0], abs=TOLERANCE)
    assert ev_soc_kwh(plan, 1) == pytest.approx(10.0 + 1.4 * 0.9)
    assert plan.objective_eur == pytest.approx(1.4 * 0.10)


def test_the_car_does_not_charge_while_it_is_unplugged() -> None:
    plan = solve_with(
        electric_vehicle(stored_kwh_needed=3.6, plugged_in=(True, False, True, True))
    )

    assert charged_kw(plan) == pytest.approx([0.0, 0.0, 4.0, 0.0], abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(4.0 * 0.20)


def test_the_car_does_not_charge_after_the_deadline() -> None:
    plan = solve_with(
        electric_vehicle(stored_kwh_needed=3.6, deadline_hours=2),
        prices=(0.30, 0.20, 0.05, 0.05),
    )

    assert charged_kw(plan) == pytest.approx([0.0, 4.0, 0.0, 0.0], abs=TOLERANCE)


def test_an_unreachable_target_reports_the_shortfall_and_charges_what_is_possible() -> (
    None
):
    plan = solve_with(electric_vehicle(stored_kwh_needed=20.0, deadline_hours=2))

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([4.0, 4.0, 0.0, 0.0], abs=TOLERANCE)
    assert plan.ev_shortfall_kwh == pytest.approx(20.0 - 8.0 * 0.9)
    assert plan.objective_eur == pytest.approx(4.0 * 0.30 + 4.0 * 0.10)


def test_the_car_charges_from_the_pv_surplus_when_that_is_cheaper_than_the_grid() -> (
    None
):
    plan = solve_with(
        electric_vehicle(stored_kwh_needed=3.6), pv_forecast_kw=(0.0, 0.0, 0.0, 4.0)
    )

    assert charged_kw(plan) == pytest.approx([0.0, 0.0, 0.0, 4.0], abs=TOLERANCE)
    assert plan.hours[3].grid_import_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(0.0, abs=TOLERANCE)


def test_a_deadline_that_has_passed_plans_no_charging_and_no_shortfall() -> None:
    plan = solve_with(electric_vehicle(stored_kwh_needed=5.0, deadline_hours=0))

    assert charged_kw(plan) == pytest.approx([0.0] * 4, abs=TOLERANCE)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)


def test_the_energy_balance_includes_the_car() -> None:
    plan = solve_with(electric_vehicle(stored_kwh_needed=6.3))

    for hour in plan.hours:
        assert hour.ev is not None
        assert hour.pv_generation_kw + hour.grid_import_kw == pytest.approx(
            hour.load_kw + hour.grid_export_kw + hour.ev.charge_kw, abs=TOLERANCE
        )


def ev_soc_kwh(plan: Plan, hour: int) -> float:
    ev = plan.hours[hour].ev
    assert ev is not None
    return ev.state_of_charge_kwh


def test_a_fixed_charge_schedule_is_followed_whatever_the_prices() -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=7.2),
        fixed_charge_kw=(4.0, 4.0, 0.0, 0.0),
    )

    plan = solve_with(ev)

    assert charged_kw(plan) == pytest.approx([4.0, 4.0, 0.0, 0.0], abs=TOLERANCE)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(4.0 * 0.30 + 4.0 * 0.10)


def test_surplus_charging_never_uses_more_than_the_pv_that_the_household_leaves() -> (
    None
):
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        surplus_kw=(0.0, 3.0, 0.5, 0.0),
        value_eur_per_kwh=1.0,
    )

    plan = solve_with(
        ev, prices=(0.30, 0.10, 0.20, 0.40), pv_forecast_kw=(0, 3, 0.5, 0)
    )

    # Charging is worth it whenever the export revenue forgone (0.05) is below
    # nothing: it is free energy otherwise lost, so the car takes what it can.
    assert charged_kw(plan) == pytest.approx([0.0, 3.0, 0.0, 0.0], abs=TOLERANCE)
    assert all(hour.grid_import_kw <= TOLERANCE for hour in plan.hours)


def test_surplus_charging_stops_at_the_charge_limit() -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        surplus_kw=(4.0, 4.0, 4.0, 4.0),
        charge_limit_kwh=INITIAL_SOC_KWH + 5.0,
        value_eur_per_kwh=1.0,
    )

    plan = solve_with(ev, pv_forecast_kw=(4.0, 4.0, 4.0, 4.0))

    assert sum(charged_kw(plan)) * 0.9 <= 5.0 + TOLERANCE
