"""Provider-independent data returned by external integrations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


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
