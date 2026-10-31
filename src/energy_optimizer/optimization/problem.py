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
    import_price_eur_per_kwh: tuple[EuroPerKilowattHour, ...]
    maximum_import_kw: Kilowatt
    time_limit_seconds: float

    def hours(self) -> int:
        return len(self.load_kw)


@dataclass(frozen=True)
class PlannedHour:
    start_time: datetime
    load_kw: Kilowatt
    grid_import_kw: Kilowatt
    import_cost_eur: Euro


@dataclass(frozen=True)
class Plan:
    """A schedule exists only if the solver proved it optimal."""

    status: PlanStatus
    objective_eur: Euro | None
    hours: tuple[PlannedHour, ...]

    def has_schedule(self) -> bool:
        return self.status is PlanStatus.OPTIMAL
