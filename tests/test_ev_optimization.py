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
    maximum_charge_kw: float = 4.0,
    charge_efficiency: float = 0.9,
) -> EvSpecification:
    return EvSpecification(
        capacity_kwh=60.0,
        initial_soc_kwh=INITIAL_SOC_KWH,
        target_soc_kwh=INITIAL_SOC_KWH + stored_kwh_needed,
        deadline_hours=deadline_hours,
        plugged_in=plugged_in,
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


def test_a_small_target_is_met_without_rounding_up_the_hourly_energy() -> None:
    plan = solve_with(electric_vehicle(stored_kwh_needed=1.0))

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([0.0, 1 / 0.9, 0.0, 0.0], abs=TOLERANCE)
    assert ev_soc_kwh(plan, 1) == pytest.approx(11.0)
    assert plan.objective_eur == pytest.approx(0.10 / 0.9)


def test_the_cheapest_hour_is_full_before_a_small_remainder_uses_the_next_hour() -> (
    None
):
    ev = electric_vehicle(
        stored_kwh_needed=11.805 * 0.9,
        deadline_hours=2,
        plugged_in=(True, True),
        maximum_charge_kw=11.0,
    )

    plan = solve(
        replace(problem((0.0, 0.0), (0.35, 0.40), maximum_import_kw=20.0), ev=ev)
    )

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([11.0, 0.805], abs=TOLERANCE)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)
    assert ev_soc_kwh(plan, 1) == pytest.approx(ev.target_soc_kwh)
    assert plan.objective_eur == pytest.approx(11 * 0.35 + 0.805 * 0.40)
    previous = ev.initial_soc_kwh
    for hour in plan.hours:
        assert hour.ev is not None
        assert hour.ev.state_of_charge_kwh == pytest.approx(
            previous + hour.ev.charge_kw * ev.charge_efficiency
        )
        assert hour.pv_generation_kw + hour.grid_import_kw == pytest.approx(
            hour.load_kw + hour.grid_export_kw + hour.ev.charge_kw
        )
        previous = hour.ev.state_of_charge_kwh


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
    assert charged_kw(plan) == pytest.approx([0.0, 3.0, 0.5, 0.0], abs=TOLERANCE)
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


@pytest.mark.parametrize("prices", [PRICES, (-0.30,) * 4])
def test_existing_energy_above_the_charge_limit_does_not_block_the_household_plan(
    prices: tuple[float, ...],
) -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        charge_limit_kwh=5.0,
        value_eur_per_kwh=1.0,
    )
    plan = solve(replace(problem((1.0,) * 4, prices), ev=ev))

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([0.0] * 4, abs=TOLERANCE)
    assert [ev_soc_kwh(plan, i) for i in range(4)] == pytest.approx([10.0] * 4)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)
    for hour in plan.hours:
        assert hour.grid_import_kw == pytest.approx(hour.load_kw)


def test_driving_does_not_allow_recharging_above_the_limit_to_the_initial_state() -> (
    None
):
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        target_soc_kwh=0.0,
        charge_limit_kwh=5.0,
        plugged_in=(True, False, False, True),
        consumption_kwh=(0.0, 2.0, 5.0, 0.0),
        value_eur_per_kwh=1.0,
    )

    plan = solve_with(ev, prices=(-0.30,) * 4)

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([0.0, 0.0, 0.0, 2 / 0.9])
    assert [ev_soc_kwh(plan, i) for i in range(4)] == pytest.approx([10, 8, 3, 5])
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)


@pytest.mark.parametrize("initial_soc_kwh", [0.0, INITIAL_SOC_KWH])
def test_a_zero_charge_limit_prevents_charging_even_with_negative_prices(
    initial_soc_kwh: float,
) -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        initial_soc_kwh=initial_soc_kwh,
        target_soc_kwh=initial_soc_kwh,
        charge_limit_kwh=0.0,
    )

    plan = solve_with(ev, prices=(-0.30,) * 4)

    assert plan.status is PlanStatus.OPTIMAL
    assert charged_kw(plan) == pytest.approx([0.0] * 4, abs=TOLERANCE)
    assert [ev_soc_kwh(plan, i) for i in range(4)] == pytest.approx(
        [initial_soc_kwh] * 4
    )


def test_an_omitted_charge_limit_allows_charging_up_to_capacity() -> None:
    ev = replace(electric_vehicle(stored_kwh_needed=0.0), initial_soc_kwh=58.0)

    plan = solve_with(ev, prices=(-0.30,) * 4)

    assert plan.status is PlanStatus.OPTIMAL
    assert ev_soc_kwh(plan, 3) == pytest.approx(ev.capacity_kwh)
    assert sum(charged_kw(plan)) * ev.charge_efficiency == pytest.approx(2.0)


def test_energy_above_the_minimum_is_charged_only_while_it_is_worth_it() -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        charge_limit_kwh=INITIAL_SOC_KWH + 7.2,
        value_eur_per_kwh=0.20,
    )

    plan = solve_with(ev)

    # A stored kWh is worth 0.20 but costs price / 0.9: 0.10 pays, 0.20 does not.
    assert charged_kw(plan) == pytest.approx([0.0, 4.0, 0.0, 0.0], abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(4.0 * 0.10)


def test_a_higher_value_makes_the_next_cheapest_hour_worth_charging() -> None:
    ev = replace(
        electric_vehicle(stored_kwh_needed=0.0),
        charge_limit_kwh=INITIAL_SOC_KWH + 7.2,
        value_eur_per_kwh=0.25,
    )

    plan = solve_with(ev)

    assert charged_kw(plan) == pytest.approx([0.0, 4.0, 4.0, 0.0], abs=TOLERANCE)


def test_the_minimum_is_charged_even_when_the_energy_costs_more_than_it_is_worth() -> (
    None
):
    ev = replace(
        electric_vehicle(stored_kwh_needed=3.6),
        charge_limit_kwh=INITIAL_SOC_KWH + 7.2,
        value_eur_per_kwh=0.01,
    )

    plan = solve_with(ev, prices=(0.30, 0.50, 0.40, 0.60))

    assert charged_kw(plan) == pytest.approx([4.0, 0.0, 0.0, 0.0], abs=TOLERANCE)
    assert plan.ev_shortfall_kwh == pytest.approx(0.0, abs=TOLERANCE)
