"""Read model for the excluded-hours dashboard view.

Every source that imports Home Assistant history persists, next to its hourly
values, the exclusions that explain each hour without a value. This module reads
those exclusions for one UTC range and maps them to the API contract. Loaders
only read persisted data; a source that is absent, empty, or corrupt is reported
with its status and never hides the exclusions of the other sources.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import Request
from pydantic import TypeAdapter

from energy_optimizer.api.historic import HistoricReadContext
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
from energy_optimizer.exclusions import HourExclusion, exclusion_summary
from energy_optimizer.providers.home_assistant_energy_history import EnergyHistoryData
from energy_optimizer.storage import ProviderDataKey, ProviderDataStoreError

ExcludedSource = str


@dataclass(frozen=True)
class _SourceRead:
    """The outcome of reading one source's exclusions."""

    status: Literal["available", "not_configured", "unavailable", "invalid"]
    reason: str | None = None
    exclusions: tuple[HourExclusion, ...] = ()


def read_excluded_hours(
    request: Request, start: datetime, end: datetime
) -> ExcludedHoursResponse:
    """Return every excluded hour of every source in the half-open range."""
    context = HistoricReadContext(request, start, end, datetime.now(timezone.utc))
    readers: dict[ExcludedSource, Callable[[HistoricReadContext], _SourceRead]] = {
        "household_load": _read_household_load,
        "grid_flow": _read_grid_flow,
        "pv_generation_history": _read_pv_history,
        "battery_efficiency": _read_battery_efficiency,
    }
    reads: dict[ExcludedSource, _SourceRead] = {}
    for source in context.configuration.configured_energy_histories():

        def read(context: HistoricReadContext, source: str = source) -> _SourceRead:
            return _read_energy_history(context, source)

        readers[source] = read
    for source, reader in readers.items():
        try:
            reads[source] = reader(context)
        except (ProviderDataStoreError, ValueError) as error:
            logger.warning(
                "event=dashboard_excluded_hours_invalid component=dashboard "
                "operation=read source=%s request_id=%s error_type=%s error=%s",
                source,
                context.request_id,
                error.__class__.__name__,
                error,
            )
            reads[source] = _SourceRead(
                "invalid",
                f"persisted {source.replace('_', ' ')} data is invalid or could not "
                "be recovered and is withheld",
            )

    hours = sorted(
        ((source, item) for source, read in reads.items() for item in read.exclusions),
        key=lambda entry: entry[1].hour_start,
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
                hour_start=item.hour_start,
                source=source,
                causes=[
                    ExclusionCause.model_validate(asdict(cause))
                    for cause in item.causes
                ],
            )
            for source, item in hours
        ],
    )


def _read_energy_history(context: HistoricReadContext, source: str) -> _SourceRead:
    if context.store is None:
        return _SourceRead(
            "unavailable", "energy history persistence is not configured"
        )
    data = context.store.load(
        ProviderDataKey("energy-history", "home-assistant", source),
        TypeAdapter(EnergyHistoryData),
    )
    if data is None:
        return _SourceRead("unavailable", "no imported energy history is available")
    return _SourceRead("available", exclusions=_in_range(data.exclusions, context))


def _read_pv_history(context: HistoricReadContext) -> _SourceRead:
    from energy_optimizer.providers.home_assistant_pv import PvGenerationHistoryData

    home_assistant = context.configuration.home_assistant
    if home_assistant is None or home_assistant.pv_generation is None:
        return _SourceRead(
            "not_configured", "no Home Assistant PV-generation entities are configured"
        )
    if context.store is None:
        return _SourceRead("unavailable", "PV-generation persistence is not configured")
    data = context.store.load(
        ProviderDataKey("pv-generation-history", "home-assistant", "pv_generation"),
        TypeAdapter(PvGenerationHistoryData),
    )
    if data is None:
        return _SourceRead(
            "unavailable", "no imported PV-generation history is available"
        )
    return _SourceRead("available", exclusions=_in_range(data.exclusions, context))


def _in_range(
    exclusions: tuple[HourExclusion, ...], context: HistoricReadContext
) -> tuple[HourExclusion, ...]:
    return tuple(
        item for item in exclusions if context.start <= item.hour_start < context.end
    )


def _read_household_load(context: HistoricReadContext) -> _SourceRead:
    home_assistant = context.configuration.home_assistant
    if home_assistant is None or home_assistant.household_load is None:
        return _SourceRead(
            "not_configured", "no Home Assistant household-load entities are configured"
        )
    store = context.store
    if store is None:
        return _SourceRead(
            "unavailable", "household-load data persistence is not configured"
        )
    key = ProviderDataKey(
        "household-load", "home-assistant", home_assistant.household_load_source_id
    )
    data = store.load_household_load_range(key, context.start, context.end)
    if data is None and store.load(key, HOUSEHOLD_LOAD_ADAPTER) is None:
        return _SourceRead(
            "unavailable", "no persisted household-load data is available yet"
        )
    return _SourceRead(
        "available", exclusions=_in_range(data.exclusions, context) if data else ()
    )


def _read_grid_flow(context: HistoricReadContext) -> _SourceRead:
    home_assistant = context.configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.grid_import is None
        or home_assistant.grid_export is None
    ):
        return _SourceRead(
            "not_configured",
            "no Home Assistant grid import and export entities are configured",
        )
    store = context.store
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
    return _SourceRead("available", exclusions=_in_range(data.exclusions, context))


def _read_battery_efficiency(context: HistoricReadContext) -> _SourceRead:
    home_assistant = context.configuration.home_assistant
    battery = home_assistant.battery if home_assistant is not None else None
    if battery is None or battery.efficiency_calculation is None:
        return _SourceRead(
            "not_configured",
            "battery efficiency history is retained only when "
            "battery.efficiency_calculation is configured",
        )
    store = context.store
    if store is None:
        return _SourceRead(
            "unavailable", "battery efficiency data persistence is not configured"
        )
    data = store.load(
        ProviderDataKey(
            "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
        ),
        BATTERY_EFFICIENCY_HISTORY_ADAPTER,
    )
    if data is None:
        return _SourceRead(
            "unavailable", "no persisted battery efficiency history is available yet"
        )
    return _SourceRead("available", exclusions=_in_range(data.exclusions, context))
