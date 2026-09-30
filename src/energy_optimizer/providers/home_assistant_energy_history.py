"""Generic hourly history built from the same shared counters as household load."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from energy_optimizer.config import EnergyAggregateConfiguration
from energy_optimizer.exclusions import HourExclusion
from energy_optimizer.household_load_records import (
    bounded_household_load,
    merge_household_load_history,
)
from energy_optimizer.providers.home_assistant_energy import EnergyAggregate
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantHistory,
    as_import_utc,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage_errors import ProviderDataStoreError


@dataclass(frozen=True)
class EnergyHistoryData:
    """Measured hourly average power, with excluded hours preserved as gaps."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    power_kw: tuple[float | None, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime
    exclusions: tuple[HourExclusion, ...] = ()

    def as_load(self) -> HouseholdLoadData:
        return HouseholdLoadData(
            self.schema_version,
            self.start_time,
            self.interval_minutes,
            self.power_kw,
            self.unit,
            self.source,
            self.retrieved_at,
            self.latest_observation_at,
            self.exclusions,
        )


def merge_energy_history(
    existing: EnergyHistoryData | None, incoming: EnergyHistoryData
) -> EnergyHistoryData:
    """Reuse retained counter-history merging, including exclusions and gaps."""
    if existing is not None and existing.source != incoming.source:
        raise ProviderDataStoreError("energy history source identity does not match")
    merged = (
        bounded_household_load(incoming.as_load())
        if existing is None
        else merge_household_load_history(existing.as_load(), incoming.as_load())
    )
    return replace(
        incoming,
        start_time=merged.start_time,
        power_kw=merged.load_kw,
        retrieved_at=merged.retrieved_at,
        latest_observation_at=merged.latest_observation_at,
        exclusions=merged.exclusions,
    )


class HomeAssistantEnergyHistoryImporter:
    """Declare needs for any energy source; the shared importer retrieves them."""

    def __init__(
        self, aggregation: EnergyAggregateConfiguration, source_id: str
    ) -> None:
        self.aggregation = aggregation
        self.source_id = source_id

    def plan(
        self,
        start: datetime,
        end: datetime,
        lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[EnergyHistoryData]:
        retrieved_at = as_import_utc(now or datetime.now(timezone.utc))
        aggregate = EnergyAggregate(
            self.aggregation, start, end, lookback_seconds, label=self.source_id
        )

        def build(history: HomeAssistantHistory) -> EnergyHistoryData:
            series = aggregate.build(history)
            return EnergyHistoryData(
                "1",
                series.start_time,
                60,
                series.values_kw,
                "kW",
                SourceMetadata("home-assistant", self.source_id),
                retrieved_at,
                series.latest_observation_at,
                series.exclusions,
            )

        return HistoryPlan(needs=aggregate.needs(), build=build)
