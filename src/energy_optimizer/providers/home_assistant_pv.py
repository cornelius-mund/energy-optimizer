"""Measured PV actuals built from shared Home Assistant energy-counter history."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.exclusions import HourExclusion
from energy_optimizer.providers.home_assistant_energy import is_fresh
from energy_optimizer.providers.home_assistant_energy_history import (
    EnergyHistoryData,
    HomeAssistantEnergyHistoryImporter,
    merge_energy_history,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantError,
)
from energy_optimizer.providers.interfaces import (
    PV_GENERATION_SOURCE_ID,
    SourceMetadata,
)
from energy_optimizer.storage_errors import ProviderDataStoreError


@dataclass(frozen=True)
class PvGenerationHistoryData:
    """Hourly measured PV power; excluded hours remain explicit gaps, not zeroes."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    generation_kw: tuple[float | None, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime
    exclusions: tuple[HourExclusion, ...] = ()
    scenario_kind: Literal["actual"] = "actual"

    def as_energy_history(self) -> EnergyHistoryData:
        return EnergyHistoryData(
            self.schema_version,
            self.start_time,
            self.interval_minutes,
            self.generation_kw,
            self.unit,
            self.source,
            self.retrieved_at,
            self.latest_observation_at,
            self.exclusions,
        )


def pv_actuals(data: EnergyHistoryData) -> PvGenerationHistoryData:
    """Map validated energy history to the PV-generation contract and provenance."""
    return PvGenerationHistoryData(
        data.schema_version,
        data.start_time,
        data.interval_minutes,
        data.power_kw,
        data.unit,
        data.source,
        data.retrieved_at,
        data.latest_observation_at,
        data.exclusions,
    )


def merge_pv_history(
    existing: PvGenerationHistoryData | None, incoming: PvGenerationHistoryData
) -> PvGenerationHistoryData:
    """Retain measured hours, gaps and exclusions using the shared history rules."""
    if incoming.source != SourceMetadata("home-assistant", PV_GENERATION_SOURCE_ID):
        raise ProviderDataStoreError(
            "PV history must identify measured Home Assistant generation"
        )
    return pv_actuals(
        merge_energy_history(
            existing.as_energy_history() if existing is not None else None,
            incoming.as_energy_history(),
        )
    )


class HomeAssistantPvImporter:
    """Declare PV counter needs for the single shared Home Assistant import."""

    def __init__(self, configuration: HomeAssistantConfiguration) -> None:
        self.configuration = configuration

    def plan(
        self,
        start: datetime,
        end: datetime,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[PvGenerationHistoryData]:
        mapping = self.configuration.pv_generation
        if mapping is None:
            raise HomeAssistantError(
                "no Home Assistant PV-generation entities are configured"
            )
        plan = HomeAssistantEnergyHistoryImporter(
            mapping, PV_GENERATION_SOURCE_ID
        ).plan(start, end, history_lookback_seconds, now=now)
        return HistoryPlan(
            needs=plan.needs, build=lambda history: pv_actuals(plan.build(history))
        )

    def is_fresh(self, data: PvGenerationHistoryData, *, now: datetime) -> bool:
        return is_fresh(
            data.latest_observation_at, self.configuration.max_data_age_seconds, now=now
        )
