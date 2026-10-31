"""Solver-independent input and output types of the schedule optimization."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type Kilowatt = float
type Euro = float
type EuroPerKilowattHour = float


class PlanStatus(StrEnum):
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    TIME_LIMIT = "time_limit"


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

    def hours(self) -> int:
        return len(self.load_kw)


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


@dataclass(frozen=True)
class Plan:
    """A schedule exists only if the solver proved it optimal.

    The objective is the import cost minus the export revenue.
    """

    status: PlanStatus
    objective_eur: Euro | None
    hours: tuple[PlannedHour, ...]

    def has_schedule(self) -> bool:
        return self.status is PlanStatus.OPTIMAL
