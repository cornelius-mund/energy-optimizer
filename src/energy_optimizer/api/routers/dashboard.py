"""Dashboard HTTP API routes and response mapping helpers."""

from datetime import datetime, timedelta, timezone
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request

from energy_optimizer.api.routers.context import (
    ELECTRICITY_PRICE_ADAPTER,
    PV_GENERATION_ADAPTER,
    logger,
)
from energy_optimizer.api.schemas import (
    MAX_HORIZON_HOURS,
    DashboardDataResponse,
    DashboardMetric,
    DashboardPlanSummary,
    DashboardSeries,
    HistoricHouseholdLoadResponse,
    SourceMetadata,
)
from energy_optimizer.api.series import align_hourly_values
from energy_optimizer.api.validation import require_aware_timestamps
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyData,
    ElectricityPriceData,
    PvGenerationData,
)
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStoreError

from .provider import historic_household_load

router = APIRouter()


def _dashboard_source(data: object) -> SourceMetadata:
    """Map normalized provider identity to the dashboard response model."""
    source = getattr(data, "source", None)
    if not isinstance(source, ProviderSourceMetadata):
        raise ProviderDataStoreError("normalized dashboard data has no source identity")
    return SourceMetadata(provider=source.provider, entity_id=source.entity_id)


def _dashboard_range(
    start_time: datetime,
    end_time: datetime,
) -> tuple[datetime, datetime]:
    """Validate and normalize one dashboard half-open hourly range."""
    try:
        require_aware_timestamps([start_time, end_time])
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="start_time and end_time must include a timezone",
        )
    start = start_time.astimezone(timezone.utc)
    end = end_time.astimezone(timezone.utc)
    if (
        start.minute
        or start.second
        or start.microsecond
        or end.minute
        or end.second
        or end.microsecond
    ):
        raise HTTPException(
            status_code=422,
            detail="dashboard range boundaries must be aligned to the hour",
        )
    if end <= start:
        raise HTTPException(
            status_code=422,
            detail="end_time must be later than start_time",
        )
    if end - start > timedelta(hours=MAX_HORIZON_HOURS):
        raise HTTPException(
            status_code=422,
            detail=f"requested range must not exceed {MAX_HORIZON_HOURS} hours",
        )
    return start, end


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
    if not series:
        status: Literal[
            "validated",
            "partial",
            "stale",
            "empty",
            "unavailable",
            "invalid",
            "infeasible",
        ] = (
            "invalid"
            if any("invalid" in diagnostic for diagnostic in diagnostics)
            else ("unavailable" if diagnostics else "empty")
        )
    elif any(item.freshness == "stale" for item in series):
        status = "stale"
    elif any(not item.timestamps for item in series):
        status = "empty"
    elif (
        check_coverage
        and any(
            (
                item.available_start_time is not None
                and item.available_start_time > start
            )
            or (item.available_end_time is not None and item.available_end_time < end)
            for item in series
        )
        or any(item.missing_intervals for item in series)
    ):
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


def _household_dashboard_series(
    data: HistoricHouseholdLoadResponse,
) -> DashboardSeries:
    """Map the historic household-load response to the unified series shape."""
    return DashboardSeries(
        id="household_load_actual",
        data_type="household_load",
        scenario_kind="actual",
        timestamps=data.timestamps,
        values=cast(list[float | None], data.load_kw),
        unit=data.unit,
        source=data.source,
        requested_start_time=data.start_time,
        requested_end_time=data.end_time,
        available_start_time=data.available_start_time,
        available_end_time=data.available_end_time,
        retrieved_at=data.retrieved_at,
        freshness=data.freshness,
        validation_status=data.validation_status,
        missing_intervals=[
            data.start_time + timedelta(hours=index)
            for index in range(
                int((data.end_time - data.start_time).total_seconds() // 3600)
            )
            if data.start_time + timedelta(hours=index) not in data.timestamps
        ],
    )


def _pv_dashboard_series(
    data: PvGenerationData,
    start: datetime,
    end: datetime,
    freshness: Literal["fresh", "stale", "unknown"],
) -> DashboardSeries:
    """Map a persisted PV forecast to the common series shape."""
    timestamps = [
        data.start_time + timedelta(hours=index)
        for index in range(len(data.generation_kw))
    ]
    values_by_timestamp = dict(zip(timestamps, data.generation_kw))
    selected = align_hourly_values(values_by_timestamp, start, end)
    available_end = data.start_time + timedelta(hours=len(data.generation_kw))
    return DashboardSeries(
        id="pv_generation_forecast",
        data_type="pv_generation",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=_dashboard_source(data),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.start_time,
        available_end_time=available_end,
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
    freshness: Literal["fresh", "stale", "unknown"],
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
    values_by_timestamp = dict(zip(data.timestamps, values))
    selected = align_hourly_values(values_by_timestamp, start, end)
    return DashboardSeries(
        id=f"{direction}_price_forecast",
        data_type=f"{direction}_price",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=_dashboard_source(data),
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
    data: BatteryEfficiencyData,
    start: datetime,
    end: datetime,
    name: str,
) -> DashboardSeries:
    """Map one calculated efficiency component to a scalar actual series."""
    value = getattr(data, name)
    timestamp = (
        data.history_end - timedelta(hours=1)
        if data.history_end is not None
        else data.latest_observation_at
    )
    in_range = value is not None
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
        timestamps=[timestamp] if in_range else [],
        values=[value] if in_range else [],
        unit="ratio",
        source=_dashboard_source(data),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.history_start,
        available_end_time=data.history_end,
        retrieved_at=data.retrieved_at,
        freshness="unknown",
        validation_status=validation_status,
        missing_intervals=[] if in_range else [timestamp],
        is_default=name in data.defaulted_components,
        calculation_status=component_status,
    )


def _efficiency_dashboard_data(
    request: Request,
    start: datetime,
    end: datetime,
) -> DashboardDataResponse:
    """Return the latest calculated efficiency components for the dashboard."""
    store = request.app.state.provider_data_store
    if store is None:
        return _dashboard_response(
            start, end, [], ["battery efficiency persistence is not configured"]
        )
    from energy_optimizer.api.routers.context import BATTERY_EFFICIENCY_ADAPTER

    key = ProviderDataKey(
        data_type="battery-efficiency",
        provider="home-assistant",
        entity_id="battery_efficiency",
    )
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
        ("battery_efficiency", "Battery round-trip efficiency"),
        ("inverter_charge_efficiency", "Inverter charge efficiency"),
        ("inverter_discharge_efficiency", "Inverter discharge efficiency"),
        ("round_trip_efficiency", "Complete round-trip efficiency"),
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
            for name, label in names
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
    request: Request,
    start: datetime,
    end: datetime,
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
            from energy_optimizer.providers.forecast_solar import ForecastSolarImporter

            importer = ForecastSolarImporter(configuration.forecast_solar)
            freshness: Literal["fresh", "stale", "unknown"] = (
                "fresh" if importer.is_fresh(data) else "stale"
            )
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
            from energy_optimizer.providers.awattar import AwattarImporter

            price_importer = AwattarImporter(configuration.awattar)
            price_freshness: Literal["fresh", "stale", "unknown"] = (
                "fresh" if price_importer.is_fresh(data) else "stale"
            )
            for direction in ("import", "export"):
                try:
                    direction_series = _price_dashboard_series(
                        data, start, end, direction, price_freshness
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
    """Return the versioned dashboard contract without mixing scenarios."""
    start, end = _dashboard_range(start_time, end_time)
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
    if request.app.state.provider_data_store is None:
        return _dashboard_response(
            start,
            end,
            [],
            ["household-load data persistence is not configured"],
        )
    try:
        actuals = historic_household_load(request, start, end)
    except HTTPException as error:
        if error.status_code in {404, 503}:
            return _dashboard_response(start, end, [], [str(error.detail)])
        raise
    return _dashboard_response(
        start,
        end,
        [_household_dashboard_series(actuals)],
        (
            ["no household-load points are available in the requested range"]
            if actuals.status == "empty"
            else []
        ),
    )


@router.get(
    "/api/v1/forecast",
    response_model=DashboardDataResponse,
    include_in_schema=False,
)
def forecast_data(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> DashboardDataResponse:
    """Return forecast data through the dashboard forecast read path."""
    start, end = _dashboard_range(start_time, end_time)
    return _forecast_dashboard_data(request, start, end)
