"""The cost of every planned hour as the optimizer values it.

The objective adds wear and penalties to the cash cost and credits stored energy:
the battery's energy at the end of the plan and every kWh charged into the car.
Spreading the battery credit over the hours as the change of its state of charge
makes each hour's value readable; the hourly costs then sum to the objective
minus the value of the battery energy at the start, a constant the plan cannot
change.
"""

import numpy

from energy_optimizer.optimization.battery_model import BatteryVariables
from energy_optimizer.optimization.ev_model import (
    SHORTFALL_PENALTY_EUR_PER_KWH,
    EvVariables,
)
from energy_optimizer.optimization.problem import (
    BatterySpecification,
    EvSpecification,
)

HOURS_PER_DAY = 24


def battery_hourly_costs(
    variables: BatteryVariables, specification: BatterySpecification
) -> list[float]:
    """Wear, the value of the stored energy given up, and the reserve penalty of
    each planned day at the hour that is furthest below the reserve."""
    state_of_charge = variables.state_of_charge.solution.values.astype(float)
    discharge = variables.discharge.solution.values.astype(float)
    previous = numpy.concatenate(
        ([specification.initial_soc_kwh], state_of_charge[:-1])
    )
    costs = (
        specification.throughput_cost_eur_per_kwh * discharge
        - specification.terminal_value_eur_per_kwh * (state_of_charge - previous)
    ).tolist()
    for hour, penalty in _reserve_penalties(variables, specification, state_of_charge):
        costs[hour] += penalty
    for hour, penalty in _headroom_penalties(variables, specification, state_of_charge):
        costs[hour] += penalty
    return [float(cost) for cost in costs]


def _reserve_penalties(
    variables: BatteryVariables,
    specification: BatterySpecification,
    state_of_charge: numpy.ndarray,
) -> list[tuple[int, float]]:
    if variables.reserve_deficits is None or specification.reserve_kwh is None:
        return []
    first, second = variables.reserve_deficits
    first_penalty, second_penalty = specification.reserve_penalties_eur_per_kwh
    above_reserve = state_of_charge - (
        specification.minimum_soc_kwh + numpy.asarray(specification.reserve_kwh)
    )
    penalties = []
    for day, (first_kwh, second_kwh) in enumerate(
        zip(first.solution.values, second.solution.values, strict=True)
    ):
        day_start = day * HOURS_PER_DAY
        day_slack = above_reserve[day_start : day_start + HOURS_PER_DAY]
        binding_hour = day_start + int(numpy.argmin(day_slack))
        penalties.append(
            (
                binding_hour,
                first_penalty * float(first_kwh) + second_penalty * float(second_kwh),
            )
        )
    return penalties


def _headroom_penalties(
    variables: BatteryVariables,
    specification: BatterySpecification,
    state_of_charge: numpy.ndarray,
) -> list[tuple[int, float]]:
    if variables.headroom_overshoots is None or specification.headroom_kwh is None:
        return []
    first, second = variables.headroom_overshoots
    first_penalty, second_penalty = specification.headroom_penalties_eur_per_kwh
    into_headroom = state_of_charge - (
        specification.maximum_soc_kwh - numpy.asarray(specification.headroom_kwh)
    )
    penalties = []
    for day, (first_kwh, second_kwh) in enumerate(
        zip(first.solution.values, second.solution.values, strict=True)
    ):
        day_start = day * HOURS_PER_DAY
        day_overshoot = into_headroom[day_start : day_start + HOURS_PER_DAY]
        binding_hour = day_start + int(numpy.argmax(day_overshoot))
        penalties.append(
            (
                binding_hour,
                first_penalty * float(first_kwh) + second_penalty * float(second_kwh),
            )
        )
    return penalties


def ev_hourly_costs(
    variables: EvVariables, specification: EvSpecification
) -> list[float]:
    """The value of the charged energy as a credit (including the cheap-price
    bonus), and penalties for energy missing for driving or a morning minimum and
    at the deadline."""
    charge = variables.charge.solution.values.astype(float)
    missing = variables.missing_energy.solution.values.astype(float)
    stored_value = numpy.full(len(charge), specification.value_eur_per_kwh, dtype=float)
    if specification.cheap_charge_bonus_eur_per_kwh is not None:
        stored_value = stored_value + numpy.asarray(
            specification.cheap_charge_bonus_eur_per_kwh, dtype=float
        )
    costs = (
        SHORTFALL_PENALTY_EUR_PER_KWH * missing
        - stored_value * specification.charge_efficiency * charge
    ).tolist()
    if specification.deadline_hours >= 1:
        costs[specification.deadline_hours - 1] += (
            SHORTFALL_PENALTY_EUR_PER_KWH * float(variables.shortfall.solution.item())
        )
    return [float(cost) for cost in costs]
