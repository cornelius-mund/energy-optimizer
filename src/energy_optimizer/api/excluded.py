"""Read model for the excluded-hours dashboard view.

Every source that imports Home Assistant history persists, next to its hourly
values, the exclusions that explain each hour without a value. This module reads
those exclusions for one UTC range and maps them to the API contract. Loaders
only read persisted data; a source that is absent, empty, or corrupt is reported
with its status and never hides the exclusions of the other sources.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Callable, Literal

from fastapi import Request

from energy_optimizer.api.routers.context import (
    BATTERY_EFFICIENCY_HISTORY_ADAPTER,
    GRID_FLOW_ADAPTER,
    HOUSEHOLD_LOAD_ADAPTER,
    logger,
)
from energy_optimizer.api.schemas import (
    ExcludedHour,
    ExcludedHoursResponse,
    ExcludedHoursSource,
    ExcludedHoursSummary,
    ExclusionCause,
)
from energy_optimizer.config import Configuration
from energy_optimizer.exclusions import HourExclusion, exclusion_summary
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

ExcludedSource = Literal["household_load", "grid_flow", "battery_efficiency"]
SourceStatus = Literal["available", "not_configured", "unavailable", "invalid"]
_SOURCE_ORDER: tuple[ExcludedSource, ...] = (
    "household_load",
    "grid_flow",
    "battery_efficiency",
)


class _SourceRead:
    """The outcome of reading one source's exclusions."""

    def __init__(
        self,
        status: SourceStatus,
        reason: str | None = None,
        exclusions: tuple[HourExclusion, ...] = (),
    ) -> None:
        self.status = status
        self.reason = reason
        self.exclusions = exclusions


def read_excluded_hours(
    request: Request, start: datetime, end: datetime
) -> ExcludedHoursResponse:
    """Return every excluded hour of every source in the half-open range."""
    configuration: Configuration = request.app.state.configuration
    store: ProviderDataStore | None = request.app.state.provider_data_store
    request_id = str(getattr(request.state, "request_id", "none"))
    readers: dict[ExcludedSource, Callable[[], _SourceRead]] = {
        "household_load": lambda: _read_household_load(
            configuration, store, start, end
        ),
        "grid_flow": lambda: _read_grid_flow(configuration, store, start, end),
        "battery_efficiency": lambda: _read_battery_efficiency(
            configuration, store, start, end
        ),
    }
    reads: dict[ExcludedSource, _SourceRead] = {}
    for source in _SOURCE_ORDER:
        try:
            reads[source] = readers[source]()
        except (ProviderDataStoreError, ValueError) as error:
            logger.warning(
                "event=dashboard_excluded_hours_invalid component=dashboard "
                "operation=read source=%s request_id=%s error_type=%s error=%s",
                source,
                request_id,
                error.__class__.__name__,
                error,
            )
            reads[source] = _SourceRead(
                "invalid",
                f"persisted {source.replace('_', ' ')} data is invalid or could not "
                "be recovered and is withheld",
            )

    hours = sorted(
        (
            (item.hour_start, _SOURCE_ORDER.index(source), source, item)
            for source, read in reads.items()
            for item in read.exclusions
        ),
        key=lambda entry: (entry[0], entry[1]),
    )
    return ExcludedHoursResponse(
        schema_version="1",
        requested_start_time=start,
        requested_end_time=end,
        excluded_hour_count=len(hours),
        sources=[
            ExcludedHoursSource(
                source=source,
                status=read.status,
                reason=read.reason,
                excluded_hour_count=len(read.exclusions),
            )
            for source, read in reads.items()
        ],
        summary=[
            ExcludedHoursSummary(
                source=source, reason=reason, excluded_hour_count=count
            )
            for source, read in reads.items()
            for reason, count in exclusion_summary(read.exclusions).items()
        ],
        hours=[
            ExcludedHour(
                hour_start=hour_start,
                source=source,
                causes=[
                    ExclusionCause.model_validate(asdict(cause))
                    for cause in item.causes
                ],
            )
            for hour_start, _, source, item in hours
        ],
    )


def _in_range(
    exclusions: tuple[HourExclusion, ...], start: datetime, end: datetime
) -> tuple[HourExclusion, ...]:
    return tuple(item for item in exclusions if start <= item.hour_start < end)


def _read_household_load(
    configuration: Configuration,
    store: ProviderDataStore | None,
    start: datetime,
    end: datetime,
) -> _SourceRead:
    home_assistant = configuration.home_assistant
    if home_assistant is None or home_assistant.household_load is None:
        return _SourceRead(
            "not_configured", "no Home Assistant household-load entities are configured"
        )
    if store is None:
        return _SourceRead(
            "unavailable", "household-load data persistence is not configured"
        )
    key = ProviderDataKey(
        "household-load", "home-assistant", home_assistant.household_load_source_id
    )
    data = store.load_household_load_range(key, start, end)
    if data is None and store.load(key, HOUSEHOLD_LOAD_ADAPTER) is None:
        return _SourceRead(
            "unavailable", "no persisted household-load data is available yet"
        )
    return _SourceRead(
        "available", exclusions=_in_range(data.exclusions, start, end) if data else ()
    )


def _read_grid_flow(
    configuration: Configuration,
    store: ProviderDataStore | None,
    start: datetime,
    end: datetime,
) -> _SourceRead:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.grid_import is None
        or home_assistant.grid_export is None
    ):
        return _SourceRead(
            "not_configured",
            "no Home Assistant grid import and export entities are configured",
        )
    if store is None:
        return _SourceRead(
            "unavailable", "grid-flow data persistence is not configured"
        )
    data = store.load(
        ProviderDataKey(
            "grid-flow", "home-assistant", home_assistant.grid_flow_source_id
        ),
        GRID_FLOW_ADAPTER,
    )
    if data is None:
        return _SourceRead(
            "unavailable", "no persisted grid-flow data is available yet"
        )
    return _SourceRead("available", exclusions=_in_range(data.exclusions, start, end))


def _read_battery_efficiency(
    configuration: Configuration,
    store: ProviderDataStore | None,
    start: datetime,
    end: datetime,
) -> _SourceRead:
    home_assistant = configuration.home_assistant
    battery = home_assistant.battery if home_assistant is not None else None
    if battery is None or battery.efficiency_calculation is None:
        return _SourceRead(
            "not_configured",
            "battery efficiency history is retained only when "
            "battery.efficiency_calculation is configured",
        )
    if store is None:
        return _SourceRead(
            "unavailable", "battery efficiency data persistence is not configured"
        )
    data = store.load(
        ProviderDataKey(
            "battery-efficiency-history",
            "home-assistant",
            "battery_efficiency_history",
        ),
        BATTERY_EFFICIENCY_HISTORY_ADAPTER,
    )
    if data is None:
        return _SourceRead(
            "unavailable", "no persisted battery efficiency history is available yet"
        )
    return _SourceRead("available", exclusions=_in_range(data.exclusions, start, end))
