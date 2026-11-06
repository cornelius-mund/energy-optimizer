"""Tests of the planning model, solved by the real HiGHS solver."""

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
