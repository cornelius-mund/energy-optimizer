"""Historic multi-asset actuals mapped to the unified dashboard series contract.

Every asset is read by one loader that returns explicit series and availability,
so an absent, stale, or corrupt asset never invalidates unrelated valid series.
Loaders only read persisted normalized provider data; forecasts and optimizer
plans are served by other dashboard scenarios and are never mixed in here.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal

from fastapi import HTTPException, Request
from pydantic import TypeAdapter

from energy_optimizer.api.routers.context import (
    BATTERY_EFFICIENCY_HISTORY_ADAPTER,
    ELECTRICITY_PRICE_ADAPTER,
    GRID_FLOW_ADAPTER,
    logger,
)
from energy_optimizer.api.routers.provider import (
    Freshness,
    api_source,
    historic_household_load,
    polling_freshness,
)
from energy_optimizer.api.schemas import DashboardSeries, SourceMetadata
from energy_optimizer.api.series import align_hourly_values
from energy_optimizer.appliances import account_loads
from energy_optimizer.config import Configuration
from energy_optimizer.history_merge import grid_flow_points, price_points
from energy_optimizer.providers.interfaces import BatteryEfficiencyHistoryData
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

AssetStatus = Literal[
    "available", "empty", "stale", "not_configured", "unavailable", "invalid"
]
_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class HistoricAssetResult:
    """The outcome of reading one asset's historic actuals."""

    asset: str
    status: AssetStatus
    series: tuple[DashboardSeries, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class HistoricReadContext:
    """One validated UTC half-open range and the application state to read it."""

    request: Request
    start: datetime
    end: datetime
    now: datetime

    @property
    def configuration(self) -> Configuration:
        configuration: Configuration = self.request.app.state.configuration
        return configuration

    @property
    def store(self) -> ProviderDataStore | None:
        """Return the durable provider-data store, when persistence is configured."""
        store: ProviderDataStore | None = self.request.app.state.provider_data_store
        return store

    @property
    def request_id(self) -> str:
        return str(getattr(self.request.state, "request_id", "none"))


HistoricAssetLoader = Callable[[HistoricReadContext], HistoricAssetResult]


def _not_configured(asset: str, reason: str) -> HistoricAssetResult:
    return HistoricAssetResult(asset, "not_configured", reason=reason)


def _unavailable(asset: str, reason: str) -> HistoricAssetResult:
    return HistoricAssetResult(asset, "unavailable", reason=reason)


def _invalid(
    context: HistoricReadContext, asset: str, reason: str, error: object
) -> HistoricAssetResult:
    """Withhold corrupt data and log its technical cause for operators."""
    logger.warning(
        "event=dashboard_actual_data_invalid component=dashboard operation=read "
        "asset=%s request_id=%s error_type=%s error=%s",
        asset,
        context.request_id,
        error.__class__.__name__,
        error,
    )
    return HistoricAssetResult(asset, "invalid", reason=reason)


def _load[ModelT](
    context: HistoricReadContext,
    asset: str,
    key: ProviderDataKey,
    adapter: TypeAdapter[ModelT],
    label: str,
) -> ModelT | HistoricAssetResult:
    """Load one persisted model or return the result explaining its absence."""
    store = context.store
    if store is None:
        return _unavailable(asset, f"{label} data persistence is not configured")
    try:
        data = store.load(key, adapter)
    except ProviderDataStoreError as error:
        return _invalid(
            context,
            asset,
            f"persisted {label} data is invalid or could not be recovered and is "
            "withheld",
            error,
        )
    if data is None:
        return _unavailable(asset, f"no persisted {label} data is available yet")
    return data


def _series(
    context: HistoricReadContext,
    *,
    series_id: str,
    data_type: str,
    unit: str,
    source: ProviderSourceMetadata | SourceMetadata,
    values: dict[datetime, float | None],
    available_start: datetime | None,
    available_end: datetime | None,
    retrieved_at: datetime,
    freshness: Freshness,
) -> DashboardSeries:
    """Align one asset series to the requested hours, keeping gaps as nulls.

    Hours without a value, including excluded hours, become null gaps and are
    listed in ``missing_intervals``; they are never filled with a number.
    """
    selected = align_hourly_values(values, context.start, context.end)
    return DashboardSeries(
        id=series_id,
        data_type=data_type,
        scenario_kind="actual",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=unit,
        source=api_source(source),
        requested_start_time=context.start,
        requested_end_time=context.end,
        available_start_time=available_start,
        available_end_time=available_end,
        retrieved_at=retrieved_at,
        freshness=freshness,
        validation_status="valid",
        missing_intervals=[timestamp for timestamp, value in selected if value is None],
    )


def _result(
    asset: str, label: str, series: list[DashboardSeries]
) -> HistoricAssetResult:
    """Derive one asset's availability from the series it produced."""
    if not any(value is not None for item in series for value in item.values):
        return HistoricAssetResult(
            asset,
            "empty",
            tuple(series),
            f"no {label} observations exist in the requested range",
        )
    if any(item.freshness == "stale" for item in series):
        return HistoricAssetResult(
            asset,
            "stale",
            tuple(series),
            f"the newest {label} observation is older than the configured "
            "polling threshold",
        )
    return HistoricAssetResult(asset, "available", tuple(series))


def load_household_load(context: HistoricReadContext) -> HistoricAssetResult:
    """Read household-load actuals through the established historic endpoint."""
    home_assistant = context.configuration.home_assistant
    if home_assistant is None or home_assistant.household_load is None:
        return _not_configured(
            "household_load", "no Home Assistant household-load entities are configured"
        )
    if context.store is None:
        return _unavailable(
            "household_load", "household-load data persistence is not configured"
        )
    try:
        actuals = historic_household_load(context.request, context.start, context.end)
    except HTTPException as error:
        if error.status_code == 404:
            return _unavailable("household_load", str(error.detail))
        if error.status_code == 503:
            return _invalid(context, "household_load", str(error.detail), error)
        raise
    series = _series(
        context,
        series_id="household_load_actual",
        data_type="household_load",
        unit=actuals.unit,
        source=actuals.source,
        values=dict(zip(actuals.timestamps, actuals.load_kw)),
        available_start=actuals.available_start_time,
        available_end=actuals.available_end_time,
        retrieved_at=actuals.retrieved_at,
        freshness=actuals.freshness,
    )
    return _result("household_load", "household-load", [series])


def load_grid_flow(context: HistoricReadContext) -> HistoricAssetResult:
    """Read retained grid import and export history."""
    home_assistant = context.configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.grid_import is None
        or home_assistant.grid_export is None
    ):
        return _not_configured(
            "grid_flow",
            "no Home Assistant grid import and export entities are configured",
        )
    data = _load(
        context,
        "grid_flow",
        ProviderDataKey(
            "grid-flow", "home-assistant", home_assistant.grid_flow_source_id
        ),
        GRID_FLOW_ADAPTER,
        "grid-flow",
    )
    if isinstance(data, HistoricAssetResult):
        return data
    try:
        points = grid_flow_points(data)
        available_end = min(points) + len(points) * _HOUR
    except ProviderDataStoreError as error:
        return _invalid(
            context,
            "grid_flow",
            "persisted grid-flow data is invalid and is withheld",
            error,
        )
    freshness = polling_freshness(
        data.latest_observation_at, home_assistant.max_data_age_seconds, context.now
    )
    directions = (
        ("import", {timestamp: point[0] for timestamp, point in points.items()}),
        ("export", {timestamp: point[1] for timestamp, point in points.items()}),
    )
    series = [
        _series(
            context,
            series_id=f"grid_{direction}_actual",
            data_type=f"grid_{direction}",
            unit=data.unit,
            source=data.source,
            values=values,
            available_start=min(points),
            available_end=available_end,
            retrieved_at=data.retrieved_at,
            freshness=freshness,
        )
        for direction, values in directions
    ]
    return _result("grid_flow", "grid-flow", series)


def load_electricity_prices(context: HistoricReadContext) -> HistoricAssetResult:
    """Read retained hourly prices that applied during completed hours."""
    awattar = context.configuration.awattar
    if awattar is None:
        return _not_configured(
            "electricity_prices", "no electricity-price provider is configured"
        )
    data = _load(
        context,
        "electricity_prices",
        ProviderDataKey(
            "electricity-price-history",
            "awattar.de",
            awattar.electricity_price_source_id,
        ),
        ELECTRICITY_PRICE_ADAPTER,
        "electricity-price history",
    )
    if isinstance(data, HistoricAssetResult):
        return data
    try:
        all_points = price_points(data)
    except ProviderDataStoreError as error:
        return _invalid(
            context,
            "electricity_prices",
            "persisted electricity-price history is invalid and is withheld",
            error,
        )
    # An hour is an actual only once it has elapsed; later hours are forecasts.
    completed_end = context.now.replace(minute=0, second=0, microsecond=0)
    points = {
        timestamp: point
        for timestamp, point in all_points.items()
        if timestamp < completed_end
    }
    freshness = polling_freshness(
        data.retrieved_at, awattar.max_data_age_seconds, context.now
    )
    series = [
        _series(
            context,
            series_id=f"{direction}_price_actual",
            data_type=f"{direction}_price",
            unit=data.unit,
            source=data.source,
            values={timestamp: point[index] for timestamp, point in points.items()},
            available_start=min(points) if points else None,
            available_end=max(points) + _HOUR if points else None,
            retrieved_at=data.retrieved_at,
            freshness=freshness,
        )
        for index, direction in enumerate(("import", "export"))
    ]
    return _result("electricity_prices", "electricity-price", series)


def _battery_state_points(
    data: BatteryEfficiencyHistoryData,
) -> dict[datetime, float | None]:
    """Validate retained state of charge, one boundary sample per hour.

    A boundary without a valid value, such as one next to an excluded hour, is
    ``None`` and shows as a gap.
    """
    start = data.start_time
    count = len(data.battery_energy_in_kwh)
    if (
        start.tzinfo is None
        or start.minute
        or start.second
        or start.microsecond
        or data.interval_minutes != 60
        or count == 0
        or len(data.state_of_charge_percent) != count + 1
    ):
        raise ProviderDataStoreError("battery state history is misaligned")
    if any(
        value is not None and not math.isfinite(value)
        for value in data.state_of_charge_percent
    ):
        raise ProviderDataStoreError("battery state history has non-finite values")
    return {
        start + index * _HOUR: None if value is None else float(value)
        for index, value in enumerate(data.state_of_charge_percent)
    }


def load_battery_state(context: HistoricReadContext) -> HistoricAssetResult:
    """Read the retained hourly battery state of charge."""
    configuration = context.configuration
    home_assistant = configuration.home_assistant
    battery = home_assistant.battery if home_assistant is not None else None
    if battery is None:
        return _not_configured("battery", "no Home Assistant battery is configured")
    if battery.efficiency_calculation is None:
        return _not_configured(
            "battery",
            "battery state history is retained only when "
            "battery.efficiency_calculation is configured",
        )
    data = _load(
        context,
        "battery",
        ProviderDataKey(
            "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
        ),
        BATTERY_EFFICIENCY_HISTORY_ADAPTER,
        "battery state history",
    )
    if isinstance(data, HistoricAssetResult):
        return data
    try:
        values = _battery_state_points(data)
    except ProviderDataStoreError as error:
        return _invalid(
            context,
            "battery",
            "persisted battery state history is invalid and is withheld",
            error,
        )
    orchestration = configuration.orchestration
    schedule = (
        orchestration.sources.get("battery_efficiency")
        if orchestration is not None
        else None
    )
    freshness = polling_freshness(
        data.latest_observation_at,
        schedule.interval_seconds * 2 if schedule is not None else None,
        context.now,
    )
    series = _series(
        context,
        series_id="battery_state_of_charge_actual",
        data_type="battery_state_of_charge",
        unit="%",
        source=data.source,
        values=values,
        available_start=min(values),
        available_end=max(values) + _HOUR,
        retrieved_at=data.retrieved_at,
        freshness=freshness,
    )
    return _result("battery", "battery state", [series])


def load_pv_generation(context: HistoricReadContext) -> HistoricAssetResult:
    """Report PV actuals as absent; Forecast.Solar only supplies forecasts."""
    return _not_configured(
        "pv_generation",
        "no historic PV-generation importer is available; PV forecasts are served "
        "by scenario_kind=forecast",
    )


def load_electric_vehicle(context: HistoricReadContext) -> HistoricAssetResult:
    """Report electric-vehicle history as absent until an importer exists."""
    return _not_configured(
        "electric_vehicle", "no electric-vehicle importer is available"
    )


def load_heat_pump(context: HistoricReadContext) -> HistoricAssetResult:
    """Report heat-pump history as absent until an importer exists."""
    return _not_configured("heat_pump", "no heat-pump importer is available")


# Order is the order of the returned series. Register a loader here when a new
# asset importer starts persisting normalized history.
HISTORIC_ASSET_LOADERS: tuple[HistoricAssetLoader, ...] = (
    load_household_load,
    load_pv_generation,
    load_grid_flow,
    load_electricity_prices,
    load_battery_state,
    load_electric_vehicle,
    load_heat_pump,
)


def read_historic_assets(context: HistoricReadContext) -> list[HistoricAssetResult]:
    """Run every registered loader, isolating one asset's data from the others."""
    results: list[HistoricAssetResult] = []
    for loader in HISTORIC_ASSET_LOADERS:
        try:
            results.append(loader(context))
        except (ProviderDataStoreError, ValueError) as error:
            asset = loader.__name__.removeprefix("load_")
            results.append(
                _invalid(
                    context,
                    asset,
                    f"persisted {asset.replace('_', ' ')} data is invalid and is "
                    "withheld",
                    error,
                )
            )
    from energy_optimizer.api.routers.appliances import energy_history

    configuration = context.configuration
    sources = configuration.configured_energy_histories()
    # Configured generic sources supersede fixed absent-asset placeholders.
    configured_names = {source.split(".", 1)[1] for source in sources}
    results = [
        result
        for result in results
        if not (
            result.status == "not_configured"
            and result.asset in configured_names
            and result.asset in {"heat_pump", "electric_vehicle", "pv_generation"}
        )
    ]
    appliance_series: dict[str, DashboardSeries] = {}
    for source_id in sources:
        try:
            item = energy_history(
                context.request, source_id, context.start, context.end
            )
            results.append(_result(source_id, source_id, [item]))
            if source_id.startswith("appliance."):
                appliance_series[source_id.removeprefix("appliance.")] = item
        except HTTPException as error:
            results.append(
                HistoricAssetResult(
                    source_id,
                    "invalid" if error.status_code == 503 else "unavailable",
                    reason=str(error.detail),
                )
            )
    if configuration.appliances:
        household = next(
            (
                item
                for result in results
                for item in result.series
                if item.id == "household_load_actual"
            ),
            None,
        )
        if household is not None:
            residual: list[float | None] = []
            total: list[float | None] = []
            for index, power in enumerate(household.values):
                unmanaged, combined = account_loads(
                    power,
                    [
                        (
                            appliance.included_in_household_load,
                            appliance_series[name].values[index]
                            if name in appliance_series
                            else None,
                        )
                        for name, appliance in configuration.appliances.items()
                    ],
                )
                residual.append(unmanaged)
                total.append(combined)
            derived = [
                household.model_copy(
                    update={
                        "id": name,
                        "data_type": name.removesuffix("_actual"),
                        "values": values,
                        "source": None,
                        "freshness": "stale"
                        if household.freshness == "stale"
                        or any(
                            item.freshness == "stale"
                            for item in appliance_series.values()
                        )
                        else "unknown",
                        "missing_intervals": [
                            time
                            for time, value in zip(household.timestamps, values)
                            if value is None
                        ],
                    }
                )
                for name, values in (
                    ("unmanaged_household_load_actual", residual),
                    ("total_consumption_actual", total),
                )
            ]
            results.append(_result("load_accounting", "accounted consumption", derived))
    return results
