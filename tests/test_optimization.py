"""Tests of the planning model, solved by the real HiGHS solver."""

import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as strategies
from linopy import Model

from energy_optimizer.optimization import (
    BatterySpecification,
    LinopyPlanSolver,
    Plan,
    PlanningProblem,
    PlanStatus,
)
from energy_optimizer.optimization.linopy_solver import UnexpectedSolverResult

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
TOLERANCE = 1e-6
MAXIMUM_IMPORT_KW = 10.0
MAXIMUM_EXPORT_KW = 10.0


def problem(
    load_kw: tuple[float, ...],
    import_price_eur_per_kwh: tuple[float, ...],
    pv_forecast_kw: tuple[float, ...] | None = None,
    export_price_eur_per_kwh: tuple[float, ...] | None = None,
    maximum_import_kw: float = MAXIMUM_IMPORT_KW,
    maximum_export_kw: float = MAXIMUM_EXPORT_KW,
    battery: BatterySpecification | None = None,
) -> PlanningProblem:
    hours = len(load_kw)
    return PlanningProblem(
        start_time=START,
        load_kw=load_kw,
        pv_forecast_kw=pv_forecast_kw or (0.0,) * hours,
        import_price_eur_per_kwh=import_price_eur_per_kwh,
        export_price_eur_per_kwh=export_price_eur_per_kwh or (0.05,) * hours,
        maximum_import_kw=maximum_import_kw,
        maximum_export_kw=maximum_export_kw,
        time_limit_seconds=30,
        battery=battery,
    )


def solve(planning_problem: PlanningProblem) -> Plan:
    return LinopyPlanSolver().solve(planning_problem)


def test_household_load_is_fully_supplied_from_the_grid_at_the_expected_cost() -> None:
    plan = solve(
        problem(
            load_kw=(1.0, 2.0, 3.0, 4.0), import_price_eur_per_kwh=(0.3, 0.1, 0.2, 0.5)
        )
    )

    assert plan.status is PlanStatus.OPTIMAL
    assert [hour.grid_import_kw for hour in plan.hours] == pytest.approx(
        [1.0, 2.0, 3.0, 4.0]
    )
    assert [hour.import_cost_eur for hour in plan.hours] == pytest.approx(
        [0.3, 0.2, 0.6, 2.0]
    )
    assert plan.objective_eur == pytest.approx(3.1)


def test_every_hour_keeps_its_own_start_time() -> None:
    plan = solve(problem((1.0, 1.0, 1.0), (0.1, 0.1, 0.1)))

    assert [hour.start_time for hour in plan.hours] == [
        START + timedelta(hours=offset) for offset in range(3)
    ]


def test_pv_that_covers_only_part_of_the_load_is_self_consumed_completely() -> None:
    plan = solve(problem((3.0,), (0.3,), pv_forecast_kw=(1.0,)))

    hour = plan.hours[0]
    assert hour.pv_generation_kw == pytest.approx(1.0)
    assert hour.pv_self_consumption_kw == pytest.approx(1.0)
    assert hour.pv_curtailment_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert hour.grid_import_kw == pytest.approx(2.0)
    assert hour.grid_export_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(0.6)


def test_pv_surplus_is_exported_when_the_export_price_is_positive() -> None:
    plan = solve(
        problem((2.0,), (0.3,), pv_forecast_kw=(3.0,), export_price_eur_per_kwh=(0.1,))
    )

    hour = plan.hours[0]
    assert hour.pv_generation_kw == pytest.approx(3.0)
    assert hour.pv_self_consumption_kw == pytest.approx(2.0)
    assert hour.pv_curtailment_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert hour.grid_export_kw == pytest.approx(1.0)
    assert hour.export_revenue_eur == pytest.approx(0.1)
    assert plan.objective_eur == pytest.approx(-0.1)


def test_pv_surplus_is_curtailed_when_exporting_would_cost_money() -> None:
    plan = solve(
        problem(
            (2.0,), (0.3,), pv_forecast_kw=(3.0,), export_price_eur_per_kwh=(-0.05,)
        )
    )

    hour = plan.hours[0]
    assert hour.pv_generation_kw == pytest.approx(2.0)
    assert hour.pv_curtailment_kw == pytest.approx(1.0)
    assert hour.grid_export_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(0.0, abs=TOLERANCE)


def test_export_is_limited_and_the_rest_of_the_pv_is_curtailed() -> None:
    plan = solve(
        problem(
            (0.0,),
            (0.3,),
            pv_forecast_kw=(5.0,),
            export_price_eur_per_kwh=(0.1,),
            maximum_export_kw=2.0,
        )
    )

    hour = plan.hours[0]
    assert hour.grid_export_kw == pytest.approx(2.0)
    assert hour.pv_curtailment_kw == pytest.approx(3.0)
    assert plan.objective_eur == pytest.approx(-0.2)


def test_the_grid_is_not_used_to_import_and_export_at_the_same_time() -> None:
    plan = solve(problem((1.0,), (0.1,), export_price_eur_per_kwh=(0.5,)))

    hour = plan.hours[0]
    assert hour.grid_import_kw == pytest.approx(1.0)
    assert hour.grid_export_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.objective_eur == pytest.approx(0.1)


def test_negative_prices_still_supply_the_mandatory_load_exactly() -> None:
    plan = solve(problem((2.0, 2.0), (-0.1, 0.2)))

    assert plan.status is PlanStatus.OPTIMAL
    assert [hour.grid_import_kw for hour in plan.hours] == pytest.approx([2.0, 2.0])
    assert plan.objective_eur == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("load_kw", "pv_forecast_kw"),
    [(12.0, 0.0), (12.0, 1.0)],
    ids=["no-pv", "pv-too-small"],
)
def test_load_that_the_import_limit_cannot_cover_is_infeasible(
    load_kw: float, pv_forecast_kw: float
) -> None:
    plan = solve(problem((load_kw,), (0.3,), pv_forecast_kw=(pv_forecast_kw,)))

    assert plan.status is PlanStatus.INFEASIBLE
    assert plan.objective_eur is None
    assert plan.hours == ()
    assert not plan.has_schedule()


def test_an_unknown_solver_termination_is_an_error_not_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Model, "solve", lambda *_, **__: ("warning", "numerical"))

    with pytest.raises(UnexpectedSolverResult, match="numerical"):
        solve(problem((1.0,), (0.1,)))


@settings(deadline=None)
@given(
    hourly_inputs=strategies.lists(
        strategies.tuples(
            strategies.floats(min_value=0, max_value=10),
            strategies.floats(min_value=0, max_value=10),
            strategies.floats(min_value=-1, max_value=1),
            strategies.floats(min_value=-1, max_value=1),
        ),
        min_size=1,
        max_size=24,
    )
)
def test_every_hour_balances_and_respects_the_physical_limits(
    hourly_inputs: list[tuple[float, float, float, float]],
) -> None:
    load_kw, pv_kw, import_prices, export_prices = (
        tuple(values) for values in zip(*hourly_inputs, strict=True)
    )

    plan = solve(
        problem(
            load_kw,
            import_prices,
            pv_forecast_kw=pv_kw,
            export_price_eur_per_kwh=export_prices,
        )
    )

    assert plan.status is PlanStatus.OPTIMAL
    for hour, forecast_kw in zip(plan.hours, pv_kw, strict=True):
        supplied_kw = hour.pv_generation_kw + hour.grid_import_kw
        consumed_kw = hour.load_kw + hour.grid_export_kw
        assert supplied_kw == pytest.approx(consumed_kw, abs=TOLERANCE)
        assert hour.pv_generation_kw <= forecast_kw + TOLERANCE
        assert hour.pv_generation_kw + hour.pv_curtailment_kw == pytest.approx(
            forecast_kw, abs=TOLERANCE
        )
        assert min(hour.grid_import_kw, hour.grid_export_kw) <= TOLERANCE
    net_cost = sum(
        hour.import_cost_eur - hour.export_revenue_eur for hour in plan.hours
    )
    assert plan.objective_eur == pytest.approx(net_cost, abs=1e-4)


def _battery_week(hours: int = 168) -> PlanningProblem:
    """Enough charge decisions that the first solution HiGHS finds is not the best."""
    price = tuple(
        0.30 + 0.12 * math.sin(hour / 5.3) + 0.05 * math.cos(hour / 1.9)
        for hour in range(hours)
    )
    pv = tuple(
        max(0.0, 7 * math.sin((hour % 24 - 7) / 9 * math.pi))
        if 7 <= hour % 24 <= 16
        else 0.0
        for hour in range(hours)
    )
    return PlanningProblem(
        start_time=START,
        load_kw=tuple(0.6 + 0.5 * math.sin(hour / 3.7) ** 2 for hour in range(hours)),
        pv_forecast_kw=pv,
        import_price_eur_per_kwh=price,
        export_price_eur_per_kwh=(0.07,) * hours,
        maximum_import_kw=25,
        maximum_export_kw=20,
        time_limit_seconds=60,
        battery=BatterySpecification(
            minimum_soc_kwh=4.7,
            maximum_soc_kwh=47,
            initial_soc_kwh=10,
            final_soc_kwh=4.7,
            maximum_charge_kw=12,
            maximum_discharge_kw=12,
            charge_efficiency=0.866,
            discharge_efficiency=0.866,
            terminal_value_eur_per_kwh=min(price) * 0.85,
            reserve_kwh=tuple(
                min(0.4 * math.sqrt(max(hour - 1, 0)), 2.0) for hour in range(hours)
            ),
            reserve_penalties_eur_per_kwh=(0.05, 0.15),
            headroom_kwh=tuple(4.7 if value > 0 else 0.0 for value in pv),
            headroom_penalties_eur_per_kwh=(0.05, 0.15),
        ),
    )


def test_a_solve_stopped_by_a_limit_keeps_the_feasible_plan_it_found() -> None:
    plan = LinopyPlanSolver({"mip_max_improving_sols": 1}).solve(_battery_week())

    assert plan.status is PlanStatus.FEASIBLE
    assert plan.has_schedule()
    assert len(plan.hours) == 168
    assert plan.objective_eur is not None
    optimal = solve(_battery_week())
    assert optimal.status is PlanStatus.OPTIMAL
    assert optimal.objective_eur is not None
    assert plan.objective_eur >= optimal.objective_eur - TOLERANCE


def test_a_solve_stopped_by_a_limit_without_any_plan_has_no_schedule() -> None:
    plan = LinopyPlanSolver({"mip_max_nodes": 0}).solve(_battery_week())

    assert plan.status is PlanStatus.TIME_LIMIT
    assert plan.hours == ()
    assert not plan.has_schedule()


def test_pv_self_consumption_is_never_negative_when_the_battery_is_sold() -> None:
    plan = solve(
        PlanningProblem(
            start_time=START,
            load_kw=(0.0,) * 4,
            pv_forecast_kw=(0.0,) * 4,
            import_price_eur_per_kwh=(0.30,) * 4,
            export_price_eur_per_kwh=(0.50,) * 4,
            maximum_import_kw=25,
            maximum_export_kw=20,
            time_limit_seconds=30,
            battery=BatterySpecification(
                minimum_soc_kwh=1,
                maximum_soc_kwh=10,
                initial_soc_kwh=10,
                final_soc_kwh=1,
                maximum_charge_kw=5,
                maximum_discharge_kw=5,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
            ),
        )
    )

    assert sum(hour.grid_export_kw for hour in plan.hours) > 1
    assert all(hour.pv_self_consumption_kw >= 0 for hour in plan.hours)


def test_the_running_hour_costs_only_its_remaining_part() -> None:
    plan = solve(
        PlanningProblem(
            start_time=START,
            load_kw=(2.0, 2.0),
            pv_forecast_kw=(0.0, 0.0),
            import_price_eur_per_kwh=(0.3, 0.3),
            export_price_eur_per_kwh=(0.05, 0.05),
            maximum_import_kw=10,
            maximum_export_kw=10,
            time_limit_seconds=30,
            first_hour_fraction=0.5,
        )
    )

    assert [hour.grid_import_kw for hour in plan.hours] == pytest.approx([2.0, 2.0])
    assert [hour.import_cost_eur for hour in plan.hours] == pytest.approx([0.3, 0.6])
    assert plan.objective_eur == pytest.approx(0.9)
