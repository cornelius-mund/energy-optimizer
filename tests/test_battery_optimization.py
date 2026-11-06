"""Battery dispatch in the planning model, solved by the real HiGHS solver."""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as strategies

from energy_optimizer.optimization import BatterySpecification, PlanStatus
from test_optimization import TOLERANCE, problem, solve

SOC_TOLERANCE = 1e-5


def battery(
    minimum_soc_kwh: float = 0.0,
    maximum_soc_kwh: float = 10.0,
    initial_soc_kwh: float = 5.0,
    final_soc_kwh: float | None = None,
    maximum_charge_kw: float = 3.0,
    maximum_discharge_kw: float = 2.0,
    charge_efficiency: float = 1.0,
    discharge_efficiency: float = 1.0,
) -> BatterySpecification:
    return BatterySpecification(
        minimum_soc_kwh=minimum_soc_kwh,
        maximum_soc_kwh=maximum_soc_kwh,
        initial_soc_kwh=initial_soc_kwh,
        final_soc_kwh=initial_soc_kwh if final_soc_kwh is None else final_soc_kwh,
        maximum_charge_kw=maximum_charge_kw,
        maximum_discharge_kw=maximum_discharge_kw,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
    )


def test_the_battery_shifts_energy_from_the_cheap_to_the_expensive_hour() -> None:
    plan = solve(problem((2.0, 2.0), (0.1, 0.5), battery=battery()))

    first, second = plan.hours
    assert first.battery is not None and second.battery is not None
    assert first.battery.charge_kw == pytest.approx(2.0)
    assert second.battery.discharge_kw == pytest.approx(2.0)
    assert [first.battery.state_of_charge_kwh, second.battery.state_of_charge_kwh] == (
        pytest.approx([7.0, 5.0])
    )
    assert first.grid_import_kw == pytest.approx(4.0)
    assert second.grid_import_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(0.4)


def test_conversion_losses_make_the_battery_charge_more_than_it_discharges() -> None:
    plan = solve(
        problem(
            (2.0, 2.0),
            (0.1, 0.5),
            battery=battery(charge_efficiency=0.9, discharge_efficiency=0.8),
        )
    )

    first, second = plan.hours
    assert first.battery is not None and second.battery is not None
    assert first.battery.charge_kw == pytest.approx(2.5 / 0.9)
    assert second.battery.discharge_kw == pytest.approx(2.0)
    assert second.battery.state_of_charge_kwh == pytest.approx(5.0)
    assert plan.objective_eur == pytest.approx((2.0 + 2.5 / 0.9) * 0.1)


def test_a_price_spread_that_does_not_pay_for_the_losses_leaves_the_battery_idle() -> (
    None
):
    plan = solve(
        problem(
            (2.0, 2.0),
            (0.30, 0.31),
            battery=battery(charge_efficiency=0.9, discharge_efficiency=0.9),
        )
    )

    charged_kw = [hour.battery.charge_kw for hour in plan.hours if hour.battery]
    assert charged_kw == pytest.approx([0.0, 0.0], abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(2.0 * 0.30 + 2.0 * 0.31)


def test_the_battery_never_charges_and_discharges_in_the_same_hour() -> None:
    plan = solve(problem((0.0, 0.0), (-0.5, -0.5), battery=battery()))

    assert plan.status is PlanStatus.OPTIMAL
    for hour in plan.hours:
        assert hour.battery is not None
        assert min(hour.battery.charge_kw, hour.battery.discharge_kw) <= TOLERANCE


def test_the_state_of_charge_stays_within_the_configured_band() -> None:
    plan = solve(
        problem(
            (0.0, 0.0, 0.0, 0.0),
            (0.5, 0.1, 0.1, 0.5),
            battery=battery(
                minimum_soc_kwh=2.0,
                maximum_soc_kwh=6.0,
                initial_soc_kwh=4.0,
                maximum_charge_kw=5.0,
                maximum_discharge_kw=5.0,
            ),
        )
    )

    levels = [hour.battery.state_of_charge_kwh for hour in plan.hours if hour.battery]
    assert min(levels) >= 2.0 - TOLERANCE
    assert max(levels) <= 6.0 + TOLERANCE
    assert levels[-1] >= 4.0 - TOLERANCE


def test_a_final_charge_above_what_the_charge_limit_allows_is_infeasible() -> None:
    plan = solve(
        problem(
            (1.0,),
            (0.3,),
            battery=battery(final_soc_kwh=10.0, maximum_charge_kw=1.0),
        )
    )

    assert plan.status is PlanStatus.INFEASIBLE
    assert plan.hours == ()


def test_a_plan_without_a_battery_reports_no_battery_hours() -> None:
    plan = solve(problem((1.0,), (0.3,)))

    assert plan.hours[0].battery is None


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    hourly_inputs=strategies.lists(
        strategies.tuples(
            strategies.floats(min_value=0, max_value=6),
            strategies.floats(min_value=0, max_value=6),
            strategies.floats(min_value=-0.5, max_value=1),
            strategies.floats(min_value=-0.5, max_value=1),
        ),
        min_size=1,
        max_size=12,
    ),
    initial_soc_kwh=strategies.floats(min_value=1, max_value=9),
    charge_efficiency=strategies.floats(min_value=0.5, max_value=1),
    discharge_efficiency=strategies.floats(min_value=0.5, max_value=1),
)
def test_the_balance_and_state_of_charge_hold_for_any_horizon(
    hourly_inputs: list[tuple[float, float, float, float]],
    initial_soc_kwh: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> None:
    load_kw, pv_kw, import_prices, export_prices = (
        tuple(values) for values in zip(*hourly_inputs, strict=True)
    )
    specification = battery(
        minimum_soc_kwh=1.0,
        maximum_soc_kwh=9.0,
        initial_soc_kwh=initial_soc_kwh,
        maximum_charge_kw=4.0,
        maximum_discharge_kw=4.0,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
    )

    plan = solve(
        problem(
            load_kw,
            import_prices,
            pv_forecast_kw=pv_kw,
            export_price_eur_per_kwh=export_prices,
            battery=specification,
        )
    )

    assert plan.status is PlanStatus.OPTIMAL
    previous_soc_kwh = initial_soc_kwh
    for hour in plan.hours:
        assert hour.battery is not None
        supplied_kw = (
            hour.pv_generation_kw + hour.grid_import_kw + hour.battery.discharge_kw
        )
        consumed_kw = hour.load_kw + hour.grid_export_kw + hour.battery.charge_kw
        assert supplied_kw == pytest.approx(consumed_kw, abs=TOLERANCE)
        expected_soc_kwh = (
            previous_soc_kwh
            + hour.battery.charge_kw * charge_efficiency
            - hour.battery.discharge_kw / discharge_efficiency
        )
        assert hour.battery.state_of_charge_kwh == pytest.approx(
            expected_soc_kwh, abs=SOC_TOLERANCE
        )
        assert 1.0 - TOLERANCE <= hour.battery.state_of_charge_kwh <= 9.0 + TOLERANCE
        assert min(hour.battery.charge_kw, hour.battery.discharge_kw) <= TOLERANCE
        previous_soc_kwh = hour.battery.state_of_charge_kwh
    assert previous_soc_kwh >= initial_soc_kwh - SOC_TOLERANCE
