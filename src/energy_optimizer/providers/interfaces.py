"""Provider-independent data returned by external integrations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

HOUSEHOLD_LOAD_SOURCE_ID = "household_load"
HOUSEHOLD_LOAD_MAX_VALUES = 87_672
PV_GENERATION_SOURCE_ID = "pv_generation"
GRID_FLOW_SOURCE_ID = "grid_flow"


@dataclass(frozen=True)
class SourceMetadata:
    """Identify the system that supplied normalized data."""

    provider: str
    entity_id: str | None = None


@dataclass(frozen=True)
class HouseholdLoadData:
    """Normalized hourly household-load data from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    load_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime


@dataclass(frozen=True)
class PvGenerationData:
    """Normalized hourly PV-generation forecast from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    generation_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class GridFlowData:
    """Normalized hourly grid import and export data from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    import_kw: tuple[float, ...]
    export_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime


class HouseholdLoadProvider(Protocol):
    """Retrieve normalized household-load data for a requested period."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HouseholdLoadData:
        """Fetch hourly household load for the requested half-open period."""

    def is_fresh(
        self,
        data: HouseholdLoadData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether data is within the configured polling age threshold."""


class PvForecastProvider(Protocol):
    """Retrieve normalized hourly PV generation forecasts."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> PvGenerationData:
        """Fetch a forecast for the requested half-open period."""

    def is_fresh(
        self,
        data: PvGenerationData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether the forecast is still usable."""


class GridFlowProvider(Protocol):
    """Retrieve normalized hourly grid import and export data."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> GridFlowData:
        """Fetch hourly grid flow for the requested half-open period."""

    def is_fresh(
        self,
        data: GridFlowData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether data is within the configured polling age threshold."""
