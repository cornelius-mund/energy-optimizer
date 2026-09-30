"""Hourly electrical scheduling with an explicit balance and flexible heat pump."""

import logging
import math
from dataclasses import dataclass

import pyomo.environ as pyo

from energy_optimizer.heat_pump import HeatPumpLoad

logger = logging.getLogger(__name__)


class OptimizationError(RuntimeError):
    """The configured solver could not produce an optimal schedule."""


@dataclass(frozen=True)
class BatteryLimits:
    minimum_soc_kwh: float
    maximum_soc_kwh: float
    initial_soc_kwh: float
    maximum_charge_kw: float
    maximum_discharge_kw: float
    battery_efficiency: float


@dataclass(frozen=True)
class Schedule:
    status: str
    objective_eur: float | None = None
    grid_import_kw: tuple[float, ...] = ()
    grid_export_kw: tuple[float, ...] = ()
    pv_used_kw: tuple[float, ...] = ()
    heat_pump_kw: tuple[float, ...] = ()
    battery_charge_kw: tuple[float, ...] = ()
    battery_discharge_kw: tuple[float, ...] = ()
    battery_soc_kwh: tuple[float, ...] = ()


def solve_schedule(
    load_kw: list[float],
    pv_kw: list[float],
    import_prices: list[float],
    export_prices: list[float],
    *,
    maximum_import_kw: float,
    maximum_export_kw: float,
    heat_pump: HeatPumpLoad | None = None,
    battery: BatteryLimits | None = None,
    solver_name: str = "highs",
    time_limit_seconds: float = 60,
) -> Schedule:
    """Minimize cost while supplying mandatory load and exact heat-pump energy.

    Power is constant for each one-hour interval, so its numeric kW value also
    gives that interval's kWh. Binary modes prevent simultaneous grid directions,
    battery charge/discharge, and operation below the heat pump's on-state minimum.
    """
    if solver_name != "highs":
        raise OptimizationError("unsupported solver; configure solver.name as highs")
    model = pyo.ConcreteModel()
    model.hours = pyo.RangeSet(0, len(load_kw) - 1)
    model.grid_import = pyo.Var(model.hours, bounds=(0, maximum_import_kw))
    model.grid_export = pyo.Var(model.hours, bounds=(0, maximum_export_kw))
    model.grid_mode = pyo.Var(model.hours, domain=pyo.Binary)
    model.pv_used = pyo.Var(model.hours, domain=pyo.NonNegativeReals)
    model.heat_pump = pyo.Var(model.hours, domain=pyo.NonNegativeReals)
    model.heat_pump_on = pyo.Var(model.hours, domain=pyo.Binary)
    model.charge = pyo.Var(model.hours, domain=pyo.NonNegativeReals)
    model.discharge = pyo.Var(model.hours, domain=pyo.NonNegativeReals)
    model.battery_mode = pyo.Var(model.hours, domain=pyo.Binary)
    model.soc = pyo.Var(model.hours, domain=pyo.NonNegativeReals)
    model.constraints = pyo.ConstraintList()
    efficiency = math.sqrt(battery.battery_efficiency) if battery else 1
    for hour in model.hours:
        model.constraints.add(
            model.grid_import[hour] <= maximum_import_kw * model.grid_mode[hour]
        )
        model.constraints.add(
            model.grid_export[hour] <= maximum_export_kw * (1 - model.grid_mode[hour])
        )
        model.constraints.add(model.pv_used[hour] <= pv_kw[hour])
        if heat_pump is None or not heat_pump.available[hour]:
            model.heat_pump[hour].fix(0)
            model.heat_pump_on[hour].fix(0)
        else:
            model.constraints.add(
                model.heat_pump[hour]
                <= heat_pump.maximum_power_kw * model.heat_pump_on[hour]
            )
            model.constraints.add(
                model.heat_pump[hour]
                >= heat_pump.minimum_power_kw * model.heat_pump_on[hour]
            )
        if battery is None:
            for variable in (
                model.charge,
                model.discharge,
                model.soc,
                model.battery_mode,
            ):
                variable[hour].fix(0)
        else:
            model.constraints.add(
                model.charge[hour]
                <= battery.maximum_charge_kw * model.battery_mode[hour]
            )
            model.constraints.add(
                model.discharge[hour]
                <= battery.maximum_discharge_kw * (1 - model.battery_mode[hour])
            )
            model.constraints.add(model.soc[hour] >= battery.minimum_soc_kwh)
            model.constraints.add(model.soc[hour] <= battery.maximum_soc_kwh)
            previous = battery.initial_soc_kwh if hour == 0 else model.soc[hour - 1]
            model.constraints.add(
                model.soc[hour]
                == previous
                + efficiency * model.charge[hour]
                - model.discharge[hour] / efficiency
            )
        model.constraints.add(
            model.grid_import[hour] + model.pv_used[hour] + model.discharge[hour]
            == load_kw[hour]
            + model.heat_pump[hour]
            + model.charge[hour]
            + model.grid_export[hour]
        )
    if heat_pump is not None:
        model.constraints.add(
            sum(model.heat_pump[h] for h in model.hours)
            == heat_pump.required_energy_kwh
        )
    model.cost = pyo.Objective(
        expr=sum(
            model.grid_import[h] * import_prices[h]
            - model.grid_export[h] * export_prices[h]
            for h in model.hours
        ),
        sense=pyo.minimize,
    )
    try:
        solver = pyo.SolverFactory("highs")
        solver.options["time_limit"] = time_limit_seconds
        solver.options["threads"] = 1
        result = solver.solve(model, load_solutions=False)
        termination = result.solver.termination_condition
        if termination == pyo.TerminationCondition.infeasible:
            logger.info("event=optimization_infeasible component=optimization")
            return Schedule(status="infeasible")
        if termination != pyo.TerminationCondition.optimal:
            raise OptimizationError(f"solver did not reach an optimum: {termination}")
        model.solutions.load_from(result)
    except OptimizationError:
        raise
    except Exception as error:
        logger.exception("event=optimization_failed component=optimization")
        raise OptimizationError(
            "HiGHS solver failed; check solver installation and limits"
        ) from error

    def values(variable: pyo.Var) -> tuple[float, ...]:
        return tuple(float(pyo.value(variable[h])) for h in model.hours)

    logger.info(
        "event=optimization_succeeded component=optimization hours=%s", len(load_kw)
    )
    return Schedule(
        status="optimal",
        objective_eur=float(pyo.value(model.cost)),
        grid_import_kw=values(model.grid_import),
        grid_export_kw=values(model.grid_export),
        pv_used_kw=values(model.pv_used),
        heat_pump_kw=values(model.heat_pump),
        battery_charge_kw=values(model.charge),
        battery_discharge_kw=values(model.discharge),
        battery_soc_kwh=values(model.soc),
    )
