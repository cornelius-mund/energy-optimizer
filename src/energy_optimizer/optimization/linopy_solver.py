"""Linopy and HiGHS implementation of the planning solver port."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import pandas
from linopy import LinearExpression, Model, Variable

from energy_optimizer.optimization.battery_model import (
    BatteryVariables,
    add_battery,
    read_battery_hour,
)
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


@dataclass(frozen=True)
class _GridAndPvVariables:
    grid_import: Variable
    grid_export: Variable
    pv_used: Variable
    battery: BatteryVariables | None


class LinopyPlanSolver:
    def solve(self, problem: PlanningProblem) -> Plan:
        model = Model()
        hours = pandas.RangeIndex(problem.hours(), name=HOUR_DIMENSION)
        variables = _add_variables(model, hours, problem)
        _forbid_simultaneous_import_and_export(model, hours, problem, variables)
        _add_energy_balance(model, variables, _hourly(problem.load_kw, hours))
        _minimize_net_cost(model, variables, problem, hours)
        status = _solve(model, problem.time_limit_seconds)
        if status is not PlanStatus.OPTIMAL:
            return Plan(status=status, objective_eur=None, hours=())
        return _read_plan(problem, model, variables)


def _hourly(values: Sequence[float], hours: pandas.RangeIndex) -> pandas.Series:
    return pandas.Series(values, index=hours, dtype=float)


def _add_variables(
    model: Model, hours: pandas.RangeIndex, problem: PlanningProblem
) -> _GridAndPvVariables:
    return _GridAndPvVariables(
        battery=(
            None
            if problem.battery is None
            else add_battery(model, hours, problem.battery)
        ),
        grid_import=model.add_variables(
            lower=0,
            upper=problem.maximum_import_kw,
            coords=[hours],
            name="grid_import_kw",
        ),
        grid_export=model.add_variables(
            lower=0,
            upper=problem.maximum_export_kw,
            coords=[hours],
            name="grid_export_kw",
        ),
        pv_used=model.add_variables(
            lower=0,
            upper=_hourly(problem.pv_forecast_kw, hours),
            coords=[hours],
            name="pv_used_kw",
        ),
    )


def _forbid_simultaneous_import_and_export(
    model: Model,
    hours: pandas.RangeIndex,
    problem: PlanningProblem,
    variables: _GridAndPvVariables,
) -> None:
    """Without this, an export price above the import price would let the grid
    connection be used as a free loop that imports and exports at the same time.
    """
    importing = model.add_variables(binary=True, coords=[hours], name="importing")
    model.add_constraints(
        variables.grid_import <= problem.maximum_import_kw * importing,
        name="import_only_while_importing",
    )
    model.add_constraints(
        variables.grid_export <= problem.maximum_export_kw * (1 - importing),
        name="export_only_while_not_importing",
    )


def _add_energy_balance(
    model: Model, variables: _GridAndPvVariables, load_kw: pandas.Series
) -> None:
    model.add_constraints(
        _supplied_kw(variables) == _consumed_kw(variables, load_kw),
        name="energy_balance",
    )


def _supplied_kw(variables: _GridAndPvVariables) -> LinearExpression:
    supplied_kw = variables.pv_used + variables.grid_import
    if variables.battery is not None:
        supplied_kw = supplied_kw + variables.battery.discharge
    return supplied_kw


def _consumed_kw(
    variables: _GridAndPvVariables, load_kw: pandas.Series
) -> LinearExpression:
    consumed_kw = variables.grid_export + load_kw
    if variables.battery is not None:
        consumed_kw = consumed_kw + variables.battery.charge
    return consumed_kw


def _minimize_net_cost(
    model: Model,
    variables: _GridAndPvVariables,
    problem: PlanningProblem,
    hours: pandas.RangeIndex,
) -> None:
    import_cost = variables.grid_import * _hourly(
        problem.import_price_eur_per_kwh, hours
    )
    export_revenue = variables.grid_export * _hourly(
        problem.export_price_eur_per_kwh, hours
    )
    model.add_objective((import_cost - export_revenue).sum())


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


def _read_plan(
    problem: PlanningProblem, model: Model, variables: _GridAndPvVariables
) -> Plan:
    imported_kw = variables.grid_import.solution.values
    exported_kw = variables.grid_export.solution.values
    pv_used_kw = variables.pv_used.solution.values
    hours = tuple(
        PlannedHour(
            start_time=problem.start_time + timedelta(hours=hour),
            load_kw=problem.load_kw[hour],
            pv_generation_kw=float(pv_used_kw[hour]),
            pv_self_consumption_kw=float(pv_used_kw[hour] - exported_kw[hour]),
            pv_curtailment_kw=problem.pv_forecast_kw[hour] - float(pv_used_kw[hour]),
            grid_import_kw=float(imported_kw[hour]),
            grid_export_kw=float(exported_kw[hour]),
            import_cost_eur=float(imported_kw[hour])
            * problem.import_price_eur_per_kwh[hour],
            export_revenue_eur=float(exported_kw[hour])
            * problem.export_price_eur_per_kwh[hour],
            battery=(
                None
                if variables.battery is None
                else read_battery_hour(variables.battery, hour)
            ),
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
