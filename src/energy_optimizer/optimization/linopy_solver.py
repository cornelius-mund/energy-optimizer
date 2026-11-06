"""Linopy and HiGHS implementation of the planning solver port."""

from collections.abc import Sequence
from datetime import timedelta

import pandas
from linopy import Model, Variable

from energy_optimizer.optimization.problem import (
    Plan,
    PlannedHour,
    PlanningProblem,
    PlanStatus,
)

SOLVER_NAME = "highs"
HOUR_DIMENSION = "hour"
PLAN_STATUS_BY_TERMINATION: dict[str, PlanStatus] = {
    "optimal": PlanStatus.OPTIMAL,
    "infeasible": PlanStatus.INFEASIBLE,
    "infeasible_or_unbounded": PlanStatus.INFEASIBLE,
    "time_limit": PlanStatus.TIME_LIMIT,
}


class UnexpectedSolverResult(RuntimeError):
    """The solver ended in a state that means neither a plan nor infeasibility."""


class LinopyPlanSolver:
    def solve(self, problem: PlanningProblem) -> Plan:
        model = Model()
        hours = pandas.RangeIndex(problem.hours(), name=HOUR_DIMENSION)
        grid_import = _add_grid_import(model, hours, problem)
        _add_energy_balance(model, grid_import, _hourly(problem.load_kw, hours))
        _minimize_import_cost(
            model, grid_import, _hourly(problem.import_price_eur_per_kwh, hours)
        )
        status = _solve(model, problem.time_limit_seconds)
        if status is not PlanStatus.OPTIMAL:
            return Plan(status=status, objective_eur=None, hours=())
        return _read_plan(problem, model, grid_import)


def _hourly(values: Sequence[float], hours: pandas.RangeIndex) -> pandas.Series:
    return pandas.Series(values, index=hours, dtype=float)


def _add_grid_import(
    model: Model, hours: pandas.RangeIndex, problem: PlanningProblem
) -> Variable:
    return model.add_variables(
        lower=0,
        upper=problem.maximum_import_kw,
        coords=[hours],
        name="grid_import_kw",
    )


def _add_energy_balance(
    model: Model, grid_import: Variable, load_kw: pandas.Series
) -> None:
    model.add_constraints(grid_import == load_kw, name="energy_balance")


def _minimize_import_cost(
    model: Model, grid_import: Variable, import_price: pandas.Series
) -> None:
    model.add_objective((grid_import * import_price).sum())


def _solve(model: Model, time_limit_seconds: float) -> PlanStatus:
    _, termination = model.solve(
        solver_name=SOLVER_NAME,
        io_api="direct",
        time_limit=time_limit_seconds,
        output_flag=False,
    )
    status = PLAN_STATUS_BY_TERMINATION.get(termination)
    if status is None:
        raise UnexpectedSolverResult(f"solver terminated with {termination!r}")
    return status


def _read_plan(problem: PlanningProblem, model: Model, grid_import: Variable) -> Plan:
    imported_kw = grid_import.solution.values
    hours = tuple(
        PlannedHour(
            start_time=problem.start_time + timedelta(hours=hour),
            load_kw=problem.load_kw[hour],
            grid_import_kw=float(imported_kw[hour]),
            import_cost_eur=float(imported_kw[hour])
            * problem.import_price_eur_per_kwh[hour],
        )
        for hour in range(problem.hours())
    )
    return Plan(
        status=PlanStatus.OPTIMAL, objective_eur=_objective_value(model), hours=hours
    )


def _objective_value(model: Model) -> float:
    objective_value = model.objective.value
    if objective_value is None:
        raise UnexpectedSolverResult("solver reported optimal without an objective")
    return float(objective_value)
