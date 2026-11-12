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
    missing_energy: Variable


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
    fixed = specification.fixed_charge_kw
    variables = EvVariables(
        charge=model.add_variables(
            lower=0 if fixed is None else pandas.Series(fixed, index=hours),
            upper=(
                _power_bound(specification, hours, can_charge)
                if fixed is None
                else pandas.Series(fixed, index=hours)
            ),
            coords=[hours],
            name="ev_charge_kw",
        ),
        state_of_charge=model.add_variables(
            lower=0,
            upper=specification.charge_limit_kwh or specification.capacity_kwh,
            coords=[hours],
            name="ev_soc_kwh",
        ),
        shortfall=model.add_variables(lower=0, name="ev_shortfall_kwh"),
        missing_energy=model.add_variables(
            lower=0, coords=[hours], name="ev_missing_energy_kwh"
        ),
    )
    _add_state_of_charge_dynamics(model, hours, specification, variables)
    _require_target_at_deadline(model, hours, specification, variables)
    _require_morning_minimums(model, hours, specification, variables)
    return variables


def _power_bound(
    specification: EvSpecification, hours: pandas.RangeIndex, can_charge: pandas.Series
) -> pandas.Series:
    bound = specification.maximum_charge_kw * can_charge
    if specification.surplus_kw is None:
        return bound
    surplus = pandas.Series(specification.surplus_kw, index=hours, dtype=float)
    return bound.clip(upper=surplus)


def _add_state_of_charge_dynamics(
    model: Model,
    hours: pandas.RangeIndex,
    specification: EvSpecification,
    variables: EvVariables,
) -> None:
    """Driving draws ``consumption_kwh``; ``missing_energy`` is what the car
    would have to get elsewhere (penalized), so driving never makes the plan
    infeasible.
    """
    initial_state = pandas.Series(0.0, index=hours)
    initial_state.iloc[0] = specification.initial_soc_kwh
    consumption = pandas.Series(
        specification.consumption_kwh or 0.0, index=hours, dtype=float
    )
    model.add_constraints(
        variables.state_of_charge
        == variables.state_of_charge.shift({HOUR_DIMENSION: 1}).fillna(0)
        + initial_state
        + variables.charge * specification.charge_efficiency
        - consumption
        + variables.missing_energy,
        name="ev_state_of_charge_dynamics",
    )


def _require_morning_minimums(
    model: Model,
    hours: pandas.RangeIndex,
    specification: EvSpecification,
    variables: EvVariables,
) -> None:
    for hours_until_morning, minimum_kwh in specification.morning_minimums:
        morning_hour = hours[hours_until_morning - 1]
        model.add_constraints(
            variables.state_of_charge.loc[morning_hour] >= minimum_kwh,
            name=f"ev_morning_minimum_{morning_hour}",
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


def read_ev_hour(
    variables: EvVariables, specification: EvSpecification, hour: int
) -> PlannedEvHour:
    consumption = specification.consumption_kwh
    return PlannedEvHour(
        charge_kw=float(variables.charge.solution.values[hour]),
        state_of_charge_kwh=float(variables.state_of_charge.solution.values[hour]),
        plugged_in=specification.plugged_in[hour],
        consumption_kwh=0.0 if consumption is None else consumption[hour],
    )


def shortfall_kwh(variables: EvVariables) -> float:
    """Energy missing at the deadline plus energy missing for driving or a
    morning minimum."""
    at_deadline = float(variables.shortfall.solution.item())
    for_driving = float(variables.missing_energy.solution.sum())
    return max(at_deadline + for_driving, 0.0)
