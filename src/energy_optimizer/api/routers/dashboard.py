"""Dashboard HTTP API routes and response mapping helpers."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Query, Request

from energy_optimizer.api import historic
from energy_optimizer.api.excluded import read_excluded_hours
from energy_optimizer.api.routers.context import (
    BATTERY_EFFICIENCY_ADAPTER,
    ELECTRICITY_PRICE_ADAPTER,
    PV_GENERATION_ADAPTER,
    logger,
)
from energy_optimizer.api.routers.provider import (
    Freshness,
    api_source,
    validated_utc_range,
)
from energy_optimizer.api.schemas import (
    DashboardAssetAvailability,
    DashboardDataResponse,
    DashboardMetric,
    DashboardPlanSummary,
    DashboardSeries,
    DashboardSettingsResponse,
    ExcludedHoursResponse,
)
from energy_optimizer.api.series import align_hourly_values
from energy_optimizer.providers.awattar import AwattarImporter
from energy_optimizer.providers.forecast_solar import ForecastSolarImporter
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyData,
    ElectricityPriceData,
    PvGenerationData,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStoreError

router = APIRouter()


def _has_coverage_gap(item: DashboardSeries, start: datetime, end: datetime) -> bool:
    """Tell whether a series' available range misses part of the requested range."""
    return (
        item.available_start_time is not None and item.available_start_time > start
    ) or (item.available_end_time is not None and item.available_end_time < end)


def _dashboard_response(
    start: datetime,
    end: datetime,
    series: list[DashboardSeries],
    diagnostics: list[str],
    *,
    plan_summary: DashboardPlanSummary | None = None,
    check_coverage: bool = True,
    metrics: list[DashboardMetric] | None = None,
) -> DashboardDataResponse:
    """Build the common envelope from explicit series states."""
    status: Literal["validated", "partial", "stale", "empty", "unavailable", "invalid"]
    if not series:
        status = (
            "invalid"
            if any("invalid" in diagnostic for diagnostic in diagnostics)
            else ("unavailable" if diagnostics else "empty")
        )
    elif any(item.freshness == "stale" for item in series):
        status = "stale"
    elif any(not item.timestamps for item in series):
        status = "empty"
    elif (
        check_coverage and any(_has_coverage_gap(item, start, end) for item in series)
    ) or any(item.missing_intervals for item in series):
        status = "partial"
    else:
        status = "validated"
    return DashboardDataResponse(
        schema_version="1",
        status=status,
        requested_start_time=start,
        requested_end_time=end,
        interval_minutes=60,
        series=series,
        metrics=metrics or [],
        diagnostics=diagnostics,
        plan_summary=plan_summary,
    )


def _actual_status(
    start: datetime,
    end: datetime,
    series: list[DashboardSeries],
    results: list[historic.HistoricAssetResult],
) -> Literal["validated", "partial", "stale", "empty", "unavailable", "invalid"]:
    """Summarize the returned actual series; absent assets never degrade them."""
    if not series:
        return (
            "invalid"
            if any(result.status == "invalid" for result in results)
            else "unavailable"
        )
    if any(item.freshness == "stale" for item in series):
        return "stale"
    if all(value is None for item in series for value in item.values):
        return "empty"
    if any(item.missing_intervals for item in series) or any(
        _has_coverage_gap(item, start, end) for item in series
    ):
        return "partial"
    return "validated"


def _actual_dashboard_data(
    request: Request, start: datetime, end: datetime
) -> DashboardDataResponse:
    """Return every available historic actual series with per-asset availability."""
    context = historic.HistoricReadContext(
        request=request, start=start, end=end, now=datetime.now(timezone.utc)
    )
    results = historic.read_historic_assets(context)
    series = [item for result in results for item in result.series]
    diagnostics = [
        f"{result.asset.replace('_', ' ')}: {result.reason}"
        for result in results
        if result.reason is not None and result.status != "not_configured"
    ]
    if not series and not diagnostics:
        diagnostics.append("no historic energy asset is configured")
    for result in results:
        if result.status in {"unavailable", "invalid"}:
            logger.warning(
                "event=dashboard_actual_asset_unavailable component=dashboard "
                "operation=read asset=%s status=%s request_id=%s",
                result.asset,
                result.status,
                context.request_id,
            )
    return DashboardDataResponse(
        schema_version="1",
        status=_actual_status(start, end, series, results),
        requested_start_time=start,
        requested_end_time=end,
        interval_minutes=60,
        series=series,
        assets=[
            DashboardAssetAvailability(
                asset=result.asset,
                status=result.status,
                series_ids=[item.id for item in result.series],
                reason=result.reason,
            )
            for result in results
        ],
        diagnostics=diagnostics,
    )


def _pv_dashboard_series(
    data: PvGenerationData, start: datetime, end: datetime, freshness: Freshness
) -> DashboardSeries:
    """Map a persisted PV forecast to the common series shape."""
    timestamps = [
        data.start_time + timedelta(hours=index)
        for index in range(len(data.generation_kw))
    ]
    selected = align_hourly_values(
        dict(zip(timestamps, data.generation_kw)), start, end
    )
    return DashboardSeries(
        id="pv_generation_forecast",
        data_type="pv_generation",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=api_source(data.source),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.start_time,
        available_end_time=data.start_time + timedelta(hours=len(data.generation_kw)),
        retrieved_at=data.retrieved_at,
        generated_at=data.generated_at,
        published_at=data.published_at,
        freshness=freshness,
        validation_status="valid",
        missing_intervals=[timestamp for timestamp, value in selected if value is None],
    )


def _price_dashboard_series(
    data: ElectricityPriceData,
    start: datetime,
    end: datetime,
    direction: Literal["import", "export"],
    freshness: Freshness,
) -> DashboardSeries | None:
    """Map one normalized price direction to a forecast series."""
    values = (
        data.import_price_eur_per_kwh
        if direction == "import"
        else data.export_price_eur_per_kwh
    )
    if not values:
        return None
    if len(data.timestamps) != len(values):
        raise ProviderDataStoreError(
            f"persisted {direction}-price forecast values are misaligned"
        )
    selected = align_hourly_values(dict(zip(data.timestamps, values)), start, end)
    return DashboardSeries(
        id=f"{direction}_price_forecast",
        data_type=f"{direction}_price",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=api_source(data.source),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.timestamps[0],
        available_end_time=data.timestamps[-1] + timedelta(hours=1),
        retrieved_at=data.retrieved_at,
        freshness=freshness,
        validation_status="valid",
        missing_intervals=[timestamp for timestamp, value in selected if value is None],
    )


def _battery_efficiency_dashboard_series(
    data: BatteryEfficiencyData, start: datetime, end: datetime, name: str
) -> DashboardSeries:
    """Map one calculated efficiency component to a scalar actual series."""
    timestamp = (
        data.history_end - timedelta(hours=1)
        if data.history_end is not None
        else data.latest_observation_at
    )
    if data.component_statuses is not None:
        component_status = data.component_statuses.get(name, "calculated")
    elif name in data.defaulted_components:
        component_status = "defaulted"
    elif name == "round_trip_efficiency" and data.defaulted_components:
        component_status = "calculated_with_defaults"
    else:
        component_status = "calculated"
    validation_status: Literal["valid", "suspect", "invalid"] = (
        "valid"
        if component_status in {"calculated", "calculated_with_defaults"}
        else "invalid"
        if component_status == "invalid"
        else "suspect"
    )
    return DashboardSeries(
        id=f"{name}_actual",
        data_type="battery_efficiency",
        scenario_kind="actual",
        timestamps=[timestamp],
        values=[getattr(data, name)],
        unit="ratio",
        source=api_source(data.source),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.history_start,
        available_end_time=data.history_end,
        retrieved_at=data.retrieved_at,
        freshness="unknown",
        validation_status=validation_status,
        is_default=name in data.defaulted_components,
        calculation_status=component_status,
    )


def _efficiency_dashboard_data(
    request: Request, start: datetime, end: datetime
) -> DashboardDataResponse:
    """Return the latest calculated efficiency components for the dashboard."""
    store = request.app.state.provider_data_store
    if store is None:
        return _dashboard_response(
            start, end, [], ["battery efficiency persistence is not configured"]
        )
    key = ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency")
    try:
        data = store.load(key, BATTERY_EFFICIENCY_ADAPTER)
    except ProviderDataStoreError:
        return _dashboard_response(
            start,
            end,
            [],
            ["battery efficiency data is invalid and could not be recovered"],
        )
    if data is None:
        return _dashboard_response(
            start, end, [], ["battery efficiency data is unavailable"]
        )
    names = (
        "battery_efficiency",
        "inverter_charge_efficiency",
        "inverter_discharge_efficiency",
        "round_trip_efficiency",
    )
    diagnostics = list(data.warnings)
    if data.status == "invalid":
        diagnostics.insert(0, "efficiency calculation status: invalid")
    elif data.status == "insufficient_data":
        diagnostics.insert(
            0, "efficiency calculation uses default or unavailable components"
        )
    return _dashboard_response(
        start,
        end,
        [
            _battery_efficiency_dashboard_series(data, start, end, name)
            for name in names
            if getattr(data, name) is not None
        ],
        diagnostics,
        check_coverage=False,
        metrics=[
            DashboardMetric(
                id="battery_throughput",
                label="Battery throughput",
                value=data.battery_throughput_kwh,
                unit="kWh",
            ),
            DashboardMetric(
                id="inverter_charge_throughput",
                label="Inverter charge throughput",
                value=data.charge_throughput_kwh,
                unit="kWh",
            ),
            DashboardMetric(
                id="inverter_discharge_throughput",
                label="Inverter discharge throughput",
                value=data.discharge_throughput_kwh,
                unit="kWh",
            ),
            DashboardMetric(
                id="completed_battery_cycles",
                label="Completed battery cycles",
                value=data.complete_cycle_count,
                unit="cycles",
            ),
        ],
    )


def _forecast_dashboard_data(
    request: Request, start: datetime, end: datetime
) -> DashboardDataResponse:
    """Load the latest coherent persisted forecast series."""
    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        logger.warning(
            "event=dashboard_forecast_unavailable component=dashboard "
            "operation=read status=unavailable request_id=%s "
            "diagnostics=forecast_data_persistence_is_not_configured",
            getattr(request.state, "request_id", "none"),
        )
        return _dashboard_response(
            start, end, [], ["forecast data persistence is not configured"]
        )

    series: list[DashboardSeries] = []
    diagnostics: list[str] = []
    if configuration.forecast_solar is not None:
        key = ProviderDataKey(
            data_type="pv-generation",
            provider="forecast.solar",
            entity_id=configuration.forecast_solar.pv_generation_source_id,
        )
        try:
            data = store.load(key, PV_GENERATION_ADAPTER)
        except ProviderDataStoreError:
            data = None
            diagnostics.append("PV forecast data is invalid and could not be recovered")
        if data is None:
            diagnostics.append("PV forecast data is unavailable")
        else:
            importer = ForecastSolarImporter(configuration.forecast_solar)
            freshness: Freshness = "fresh" if importer.is_fresh(data) else "stale"
            series.append(_pv_dashboard_series(data, start, end, freshness))

    if configuration.awattar is not None:
        key = ProviderDataKey(
            data_type="electricity-prices",
            provider="awattar.de",
            entity_id=configuration.awattar.electricity_price_source_id,
        )
        try:
            data = store.load(key, ELECTRICITY_PRICE_ADAPTER)
        except ProviderDataStoreError:
            data = None
            diagnostics.append(
                "electricity-price forecast data is invalid and could not be recovered"
            )
        if data is None:
            diagnostics.append("electricity-price forecast data is unavailable")
        else:
            price_importer = AwattarImporter(configuration.awattar)
            freshness = "fresh" if price_importer.is_fresh(data) else "stale"
            for direction in ("import", "export"):
                try:
                    direction_series = _price_dashboard_series(
                        data, start, end, direction, freshness
                    )
                except ProviderDataStoreError:
                    diagnostics.append(
                        f"{direction}-price forecast data is invalid and could not "
                        "be recovered"
                    )
                    continue
                if direction_series is not None:
                    series.append(direction_series)
    response = _dashboard_response(start, end, series, diagnostics)
    if response.status == "unavailable":
        logger.warning(
            "event=dashboard_forecast_unavailable component=dashboard "
            "operation=read status=%s request_id=%s diagnostics=%s",
            response.status,
            getattr(request.state, "request_id", "none"),
            ";".join(
                diagnostic.replace(" ", "_") for diagnostic in response.diagnostics
            ),
        )
    return response


@router.get(
    "/api/v1/dashboard/data",
    response_model=DashboardDataResponse,
    responses={503: {"description": "Dashboard data persistence is unavailable"}},
)
def dashboard_data(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
    scenario_kind: Literal["actual", "forecast", "plan", "efficiency"] = Query(
        "actual"
    ),
) -> DashboardDataResponse:
    """Return the versioned dashboard contract without mixing scenarios.

    The actual scenario returns the hourly historic series of every available
    asset, aligned to the requested half-open UTC range with null gaps, and
    lists in `assets` why any asset contributed no series. Absent, stale, or
    corrupt assets never invalidate the series of other assets.
    """
    start, end = validated_utc_range(start_time, end_time, hour_aligned=True)
    if scenario_kind == "forecast":
        return _forecast_dashboard_data(request, start, end)
    if scenario_kind == "plan":
        return _dashboard_response(
            start,
            end,
            [],
            ["optimization plan data is unavailable"],
            plan_summary=DashboardPlanSummary(status="unavailable"),
        )
    if scenario_kind == "efficiency":
        return _efficiency_dashboard_data(request, start, end)
    return _actual_dashboard_data(request, start, end)


@router.get("/api/v1/dashboard/excluded-hours", response_model=ExcludedHoursResponse)
def excluded_hours(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> ExcludedHoursResponse:
    """List every hour excluded from imported history in a UTC range.

    An hour of Home Assistant history is imported only if every data point that
    contributes to it is valid. Every other hour has no value and is listed here
    with each cause: the entity, a reason code, a message, and the exact data
    points. Hours that Home Assistant no longer holds, because the service was
    down for longer than its history is retained, are listed with the reason
    `history_unavailable`; their cause names the missing range and has no entity
    and no data points. A source that is not configured, has no persisted history
    yet, or is corrupt is reported in `sources` and never hides the other sources.
    """
    start, end = validated_utc_range(start_time, end_time, hour_aligned=True)
    return read_excluded_hours(request, start, end)


@router.get("/api/v1/dashboard/settings", response_model=DashboardSettingsResponse)
def dashboard_settings(request: Request) -> DashboardSettingsResponse:
    """Return the configured time zone in which the dashboard shows times.

    The dashboard requests this before its first data request because its
    default range depends on the zone. Data requests and every API timestamp
    remain UTC.
    """
    return DashboardSettingsResponse(timezone=request.app.state.configuration.timezone)


@router.get(
    "/api/v1/forecast", response_model=DashboardDataResponse, include_in_schema=False
)
def forecast_data(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> DashboardDataResponse:
    """Return forecast data through the dashboard forecast read path."""
    start, end = validated_utc_range(start_time, end_time, hour_aligned=True)
    return _forecast_dashboard_data(request, start, end)
