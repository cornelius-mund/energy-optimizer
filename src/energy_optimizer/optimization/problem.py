"""Solver-independent input and output types of the schedule optimization."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type Kilowatt = float
type KilowattHour = float
type Euro = float
type EuroPerKilowattHour = float


class PlanStatus(StrEnum):
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    TIME_LIMIT = "time_limit"


@dataclass(frozen=True)
class BatterySpecification:
    """Technical limits and starting state of the battery.

    The horizon must end with at least ``final_soc_kwh``; without that bound the
    cheapest plan would drain the battery for free in the last hour.
    """

    minimum_soc_kwh: KilowattHour
    maximum_soc_kwh: KilowattHour
    initial_soc_kwh: KilowattHour
    final_soc_kwh: KilowattHour
    maximum_charge_kw: Kilowatt
    maximum_discharge_kw: Kilowatt
    charge_efficiency: float
    discharge_efficiency: float


@dataclass(frozen=True)
class EvSpecification:
    """An electric vehicle that charges while plugged in and leaves at a deadline.

    Charging is only planned before ``deadline_hours``; the car must hold
    ``target_soc_kwh`` by then. A charger delivers either nothing or a power
    between its minimum and maximum. A target that cannot be reached is not an
    error: the plan charges as much as possible and reports the shortfall.
    """

    capacity_kwh: KilowattHour
    initial_soc_kwh: KilowattHour
    target_soc_kwh: KilowattHour
    deadline_hours: int
    plugged_in: tuple[bool, ...]
    minimum_charge_kw: Kilowatt
    maximum_charge_kw: Kilowatt
    charge_efficiency: float


@dataclass(frozen=True)
class PlanningProblem:
    """Hourly inputs and technical limits of one planning horizon."""

    start_time: datetime
    load_kw: tuple[Kilowatt, ...]
    pv_forecast_kw: tuple[Kilowatt, ...]
    import_price_eur_per_kwh: tuple[EuroPerKilowattHour, ...]
    export_price_eur_per_kwh: tuple[EuroPerKilowattHour, ...]
    maximum_import_kw: Kilowatt
    maximum_export_kw: Kilowatt
    time_limit_seconds: float
    battery: BatterySpecification | None = None
    ev: EvSpecification | None = None

    def hours(self) -> int:
        return len(self.load_kw)


@dataclass(frozen=True)
class PlannedBatteryHour:
    charge_kw: Kilowatt
    discharge_kw: Kilowatt
    state_of_charge_kwh: KilowattHour


@dataclass(frozen=True)
class PlannedEvHour:
    charge_kw: Kilowatt
    state_of_charge_kwh: KilowattHour


@dataclass(frozen=True)
class PlannedHour:
    start_time: datetime
    load_kw: Kilowatt
    pv_generation_kw: Kilowatt
    pv_self_consumption_kw: Kilowatt
    pv_curtailment_kw: Kilowatt
    grid_import_kw: Kilowatt
    grid_export_kw: Kilowatt
    import_cost_eur: Euro
    export_revenue_eur: Euro
    battery: PlannedBatteryHour | None = None
    ev: PlannedEvHour | None = None


@dataclass(frozen=True)
class Plan:
    """A schedule exists only if the solver proved it optimal.

    The objective is the import cost minus the export revenue, without the
    penalty the solver applies to an EV shortfall.
    """

    status: PlanStatus
    objective_eur: Euro | None
    hours: tuple[PlannedHour, ...]
    ev_shortfall_kwh: KilowattHour = 0.0

    def has_schedule(self) -> bool:
        return self.status is PlanStatus.OPTIMAL
