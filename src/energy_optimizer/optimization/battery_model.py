"""Battery dispatch and state-of-charge constraints of the planning model."""

from dataclasses import dataclass

import pandas
from linopy import Model, Variable

from energy_optimizer.optimization.problem import (
    BatterySpecification,
    PlannedBatteryHour,
)

HOUR_DIMENSION = "hour"


@dataclass(frozen=True)
class BatteryVariables:
    """Hourly battery power and the state of charge at the end of each hour."""

    charge: Variable
    discharge: Variable
    state_of_charge: Variable


def add_battery(
    model: Model, hours: pandas.RangeIndex, specification: BatterySpecification
) -> BatteryVariables:
    variables = BatteryVariables(
        charge=model.add_variables(
            lower=0,
            upper=specification.maximum_charge_kw,
            coords=[hours],
            name="battery_charge_kw",
        ),
        discharge=model.add_variables(
            lower=0,
            upper=specification.maximum_discharge_kw,
            coords=[hours],
            name="battery_discharge_kw",
        ),
        state_of_charge=model.add_variables(
            lower=specification.minimum_soc_kwh,
            upper=specification.maximum_soc_kwh,
            coords=[hours],
            name="battery_soc_kwh",
        ),
    )
    _forbid_simultaneous_charge_and_discharge(model, hours, specification, variables)
    _add_state_of_charge_dynamics(model, hours, specification, variables)
    _require_final_state_of_charge(model, hours, specification, variables)
    return variables


def _forbid_simultaneous_charge_and_discharge(
    model: Model,
    hours: pandas.RangeIndex,
    specification: BatterySpecification,
    variables: BatteryVariables,
) -> None:
    """Conversion losses would otherwise turn a negative price into free disposal
    of energy by charging and discharging in the same hour.
    """
    charging = model.add_variables(binary=True, coords=[hours], name="charging")
    model.add_constraints(
        variables.charge <= specification.maximum_charge_kw * charging,
        name="charge_only_while_charging",
    )
    model.add_constraints(
        variables.discharge <= specification.maximum_discharge_kw * (1 - charging),
        name="discharge_only_while_not_charging",
    )


def _add_state_of_charge_dynamics(
    model: Model,
    hours: pandas.RangeIndex,
    specification: BatterySpecification,
    variables: BatteryVariables,
) -> None:
    initial_state = pandas.Series(0.0, index=hours)
    initial_state.iloc[0] = specification.initial_soc_kwh
    stored_kwh = variables.charge * specification.charge_efficiency
    drawn_kwh = variables.discharge / specification.discharge_efficiency
    model.add_constraints(
        variables.state_of_charge
        == variables.state_of_charge.shift({HOUR_DIMENSION: 1}).fillna(0)
        + initial_state
        + stored_kwh
        - drawn_kwh,
        name="state_of_charge_dynamics",
    )


def _require_final_state_of_charge(
    model: Model,
    hours: pandas.RangeIndex,
    specification: BatterySpecification,
    variables: BatteryVariables,
) -> None:
    last_hour = hours[-1]
    model.add_constraints(
        variables.state_of_charge.loc[last_hour] >= specification.final_soc_kwh,
        name="final_state_of_charge",
    )


def read_battery_hour(
    variables: BatteryVariables, hour: int, throughput_cost_eur_per_kwh: float = 0.0
) -> PlannedBatteryHour:
    discharge_kw = float(variables.discharge.solution.values[hour])
    return PlannedBatteryHour(
        charge_kw=float(variables.charge.solution.values[hour]),
        discharge_kw=discharge_kw,
        degradation_cost_eur=discharge_kw * throughput_cost_eur_per_kwh,
        state_of_charge_kwh=float(variables.state_of_charge.solution.values[hour]),
    )
