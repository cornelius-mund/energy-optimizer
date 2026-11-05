"""Electric-vehicle charging constraints of the planning model."""

from dataclasses import dataclass

import pandas
from linopy import Model, Variable

from energy_optimizer.optimization.problem import EvSpecification, PlannedEvHour

HOUR_DIMENSION = "hour"
SHORTFALL_PENALTY_EUR_PER_KWH = 1000.0


@dataclass(frozen=True)
class EvVariables:
    """Hourly charge power, the state of charge at the end of each hour and the
    energy missing at the deadline."""

    charge: Variable
    state_of_charge: Variable
    shortfall: Variable


def add_ev(
    model: Model, hours: pandas.RangeIndex, specification: EvSpecification
) -> EvVariables:
    can_charge = pandas.Series(
        [
            float(plugged and hour < specification.deadline_hours)
            for hour, plugged in zip(hours, specification.plugged_in, strict=True)
        ],
        index=hours,
    )
    variables = EvVariables(
        charge=model.add_variables(
            lower=0,
            upper=specification.maximum_charge_kw * can_charge,
            coords=[hours],
            name="ev_charge_kw",
        ),
        state_of_charge=model.add_variables(
            lower=0,
            upper=specification.capacity_kwh,
            coords=[hours],
            name="ev_soc_kwh",
        ),
        shortfall=model.add_variables(lower=0, name="ev_shortfall_kwh"),
    )
    _limit_power_to_the_charger_range(
        model, hours, specification, variables, can_charge
    )
    _add_state_of_charge_dynamics(model, hours, specification, variables)
    _require_target_at_deadline(model, hours, specification, variables)
    return variables


def _limit_power_to_the_charger_range(
    model: Model,
    hours: pandas.RangeIndex,
    specification: EvSpecification,
    variables: EvVariables,
    can_charge: pandas.Series,
) -> None:
    """A wall box cannot charge below its minimum current, so the power is either
    zero or within the range."""
    charging = model.add_variables(binary=True, coords=[hours], name="ev_charging")
    model.add_constraints(charging <= can_charge, name="ev_charging_only_when_possible")
    model.add_constraints(
        variables.charge >= specification.minimum_charge_kw * charging,
        name="ev_minimum_power",
    )
    model.add_constraints(
        variables.charge <= specification.maximum_charge_kw * charging,
        name="ev_maximum_power",
    )


def _add_state_of_charge_dynamics(
    model: Model,
    hours: pandas.RangeIndex,
    specification: EvSpecification,
    variables: EvVariables,
) -> None:
    initial_state = pandas.Series(0.0, index=hours)
    initial_state.iloc[0] = specification.initial_soc_kwh
    model.add_constraints(
        variables.state_of_charge
        == variables.state_of_charge.shift({HOUR_DIMENSION: 1}).fillna(0)
        + initial_state
        + variables.charge * specification.charge_efficiency,
        name="ev_state_of_charge_dynamics",
    )


def _require_target_at_deadline(
    model: Model,
    hours: pandas.RangeIndex,
    specification: EvSpecification,
    variables: EvVariables,
) -> None:
    if specification.deadline_hours < 1:
        model.add_constraints(variables.shortfall == 0, name="ev_no_deadline")
        return
    deadline_hour = hours[specification.deadline_hours - 1]
    model.add_constraints(
        variables.state_of_charge.loc[deadline_hour] + variables.shortfall
        >= specification.target_soc_kwh,
        name="ev_target_at_deadline",
    )


def read_ev_hour(variables: EvVariables, hour: int) -> PlannedEvHour:
    return PlannedEvHour(
        charge_kw=float(variables.charge.solution.values[hour]),
        state_of_charge_kwh=float(variables.state_of_charge.solution.values[hour]),
    )
