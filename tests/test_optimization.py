"""Tests of the planning model, solved by the real HiGHS solver."""

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as strategies
from linopy import Model

from energy_optimizer.optimization import (
    LinopyPlanSolver,
    PlanningProblem,
    PlanStatus,
)
from energy_optimizer.optimization.linopy_solver import UnexpectedSolverResult

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
TOLERANCE = 1e-6


def problem(
    load_kw: tuple[float, ...],
    import_price_eur_per_kwh: tuple[float, ...],
    maximum_import_kw: float = 10.0,
) -> PlanningProblem:
    return PlanningProblem(
        start_time=START,
        load_kw=load_kw,
        import_price_eur_per_kwh=import_price_eur_per_kwh,
        maximum_import_kw=maximum_import_kw,
        time_limit_seconds=30,
    )


def test_household_load_is_fully_supplied_from_the_grid_at_the_expected_cost() -> None:
    plan = LinopyPlanSolver().solve(
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
    plan = LinopyPlanSolver().solve(problem((1.0, 1.0, 1.0), (0.1, 0.1, 0.1)))

    assert [hour.start_time for hour in plan.hours] == [
        START + timedelta(hours=offset) for offset in range(3)
    ]


def test_negative_prices_still_supply_the_mandatory_load_exactly() -> None:
    plan = LinopyPlanSolver().solve(problem((2.0, 2.0), (-0.1, 0.2)))

    assert plan.status is PlanStatus.OPTIMAL
    assert [hour.grid_import_kw for hour in plan.hours] == pytest.approx([2.0, 2.0])
    assert plan.objective_eur == pytest.approx(0.2)


def test_load_above_the_import_limit_is_reported_as_infeasible() -> None:
    plan = LinopyPlanSolver().solve(
        problem((1.0, 12.0), (0.3, 0.3), maximum_import_kw=10.0)
    )

    assert plan.status is PlanStatus.INFEASIBLE
    assert plan.objective_eur is None
    assert plan.hours == ()
    assert not plan.has_schedule()


def test_an_unknown_solver_termination_is_an_error_not_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Model, "solve", lambda *_, **__: ("warning", "numerical"))

    with pytest.raises(UnexpectedSolverResult, match="numerical"):
        LinopyPlanSolver().solve(problem((1.0,), (0.1,)))


@given(
    hourly_inputs=strategies.lists(
        strategies.tuples(
            strategies.floats(min_value=0, max_value=10),
            strategies.floats(min_value=-1, max_value=1),
        ),
        min_size=1,
        max_size=48,
    )
)
def test_the_balance_and_objective_hold_for_any_feasible_horizon(
    hourly_inputs: list[tuple[float, float]],
) -> None:
    load_kw = tuple(load for load, _ in hourly_inputs)
    prices = tuple(price for _, price in hourly_inputs)

    plan = LinopyPlanSolver().solve(problem(load_kw, prices))

    assert plan.status is PlanStatus.OPTIMAL
    for hour, load in zip(plan.hours, load_kw, strict=True):
        assert hour.grid_import_kw == pytest.approx(load, abs=TOLERANCE)
    expected_cost = sum(load * price for load, price in hourly_inputs)
    assert plan.objective_eur == pytest.approx(expected_cost, abs=1e-4)
