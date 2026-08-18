"""Provider-backed HTTP API routes."""

from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import TypeAdapter

from energy_optimizer.api.persistence import load_provider_data, persist_provider_data
from energy_optimizer.api.routers.context import (
    GRID_FLOW_ADAPTER,
    HOUSEHOLD_LOAD_ADAPTER,
)
from energy_optimizer.api.schemas import (
    MAX_HORIZON_HOURS,
    BatteryRequest,
    BatteryResponse,
    ElectricityPriceRequest,
    ElectricityPriceResponse,
    GridFlowRequest,
    GridFlowResponse,
    HistoricHouseholdLoadResponse,
    HouseholdLoadQuality,
    HouseholdLoadRequest,
    HouseholdLoadResponse,
    PvGenerationRequest,
    PvGenerationResponse,
    SourceMetadata,
)
from energy_optimizer.api.validation import require_aware_timestamps
from energy_optimizer.providers.interfaces import (
    GridFlowData,
    HouseholdLoadData,
    IntervalQuality,
)
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStoreError

router = APIRouter()
tail_router = APIRouter()


def _provider_source(source: SourceMetadata) -> ProviderSourceMetadata:
    """Convert HTTP source metadata to the provider-independent representation."""
    return ProviderSourceMetadata(provider=source.provider, entity_id=source.entity_id)


def _persist_configured_submission(
    request: Request,
    data: HouseholdLoadRequest | GridFlowRequest,
    provider_data: HouseholdLoadData | GridFlowData,
    adapter: TypeAdapter[HouseholdLoadData] | TypeAdapter[GridFlowData],
    *,
    data_type: str,
    data_label: str,
    is_configured: Callable[[str, str | None], bool],
) -> HouseholdLoadData | GridFlowData | None:
    """Persist a submission only when it identifies a configured source."""
    store = request.app.state.provider_data_store
    if (
        store is None
        or data.source is None
        or not is_configured(data.source.provider, data.source.entity_id)
    ):
        return None
    key = ProviderDataKey(
        data_type=data_type,
        provider=data.source.provider,
        entity_id=data.source.entity_id,
    )
    return cast(
        HouseholdLoadData | GridFlowData | None,
        persist_provider_data(
            store,
            key,
            provider_data,
            adapter,
            data_label=data_label,
        ),
    )


def _household_load_response(
    data: HouseholdLoadRequest | HouseholdLoadData,
) -> HouseholdLoadResponse:
    """Map either an HTTP request or persisted model to one response shape."""
    source = (
        SourceMetadata(provider=data.source.provider, entity_id=data.source.entity_id)
        if isinstance(data, HouseholdLoadData)
        else data.source
    )
    return HouseholdLoadResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        load_kw=(
            list(data.load_kw) if isinstance(data, HouseholdLoadData) else data.load_kw
        ),
        unit=data.unit,
        source=source,
        retrieved_at=data.retrieved_at,
        latest_observation_at=data.latest_observation_at,
    )


def _grid_flow_response(
    data: GridFlowRequest | GridFlowData,
) -> GridFlowResponse:
    """Map either an HTTP request or persisted model to one response shape."""
    source = (
        SourceMetadata(provider=data.source.provider, entity_id=data.source.entity_id)
        if isinstance(data, GridFlowData)
        else data.source
    )
    return GridFlowResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        import_kw=(
            list(data.import_kw) if isinstance(data, GridFlowData) else data.import_kw
        ),
        export_kw=(
            list(data.export_kw) if isinstance(data, GridFlowData) else data.export_kw
        ),
        unit=data.unit,
        source=source,
        retrieved_at=data.retrieved_at,
        latest_observation_at=data.latest_observation_at,
    )


@router.post("/api/v1/battery", response_model=BatteryResponse)
def battery(request: BatteryRequest) -> BatteryResponse:
    """Validate a versioned hourly battery state and capabilities object."""
    return BatteryResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        state_of_charge_kwh=request.state_of_charge_kwh,
        capacity_kwh=request.capacity_kwh,
        minimum_soc_kwh=request.minimum_soc_kwh,
        maximum_soc_kwh=request.maximum_soc_kwh,
        initial_soc_kwh=request.initial_soc_kwh,
        maximum_charge_kw=request.maximum_charge_kw,
        maximum_discharge_kw=request.maximum_discharge_kw,
        charge_efficiency=request.charge_efficiency,
        discharge_efficiency=request.discharge_efficiency,
        unit=request.unit,
        power_unit=request.power_unit,
        source=request.source,
    )


@router.post("/api/v1/electricity-prices", response_model=ElectricityPriceResponse)
def electricity_prices(
    request: ElectricityPriceRequest,
) -> ElectricityPriceResponse:
    """Validate normalized hourly electricity-price data."""
    return ElectricityPriceResponse(
        status="validated",
        schema_version=request.schema_version,
        timestamps=request.timestamps,
        interval_minutes=request.interval_minutes,
        import_price_eur_per_kwh=request.import_price_eur_per_kwh,
        export_price_eur_per_kwh=request.export_price_eur_per_kwh,
        unit=request.unit,
        source=request.source,
        retrieved_at=request.retrieved_at,
        expires_at=request.expires_at,
    )


@router.post(
    "/api/v1/household-load",
    response_model=HouseholdLoadResponse,
    responses={503: {"description": "Provider data could not be persisted"}},
)
def household_load(
    request: Request,
    data: HouseholdLoadRequest,
) -> HouseholdLoadResponse:
    """Validate a versioned hourly household-load data series."""
    configuration = request.app.state.configuration
    provider_data: HouseholdLoadData | None = (
        HouseholdLoadData(
            schema_version=data.schema_version,
            start_time=data.start_time,
            interval_minutes=data.interval_minutes,
            load_kw=tuple(data.load_kw),
            unit=data.unit,
            source=_provider_source(data.source) if data.source is not None else None,
            retrieved_at=data.retrieved_at,
            latest_observation_at=data.latest_observation_at,
        )
        if data.source is not None
        else None
    )
    persisted = (
        _persist_configured_submission(
            request,
            data,
            provider_data,
            HOUSEHOLD_LOAD_ADAPTER,
            data_type="household-load",
            data_label="household-load",
            is_configured=configuration.is_configured_household_load_source,
        )
        if provider_data is not None
        else None
    )
    persisted_data = cast(HouseholdLoadData | None, persisted)
    return _household_load_response(persisted_data or data)


@router.get(
    "/api/v1/household-load",
    response_model=HouseholdLoadResponse,
    responses={
        404: {"description": "No persisted provider data is available"},
        503: {"description": "Provider data persistence is unavailable"},
    },
)
def persisted_household_load(request: Request) -> HouseholdLoadResponse:
    """Return the latest persisted normalized household-load provider data."""
    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant household-load provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=configuration.home_assistant.household_load_source_id,
    )
    key = ProviderDataKey(
        data_type="household-load",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    provider_data = cast(
        HouseholdLoadData,
        load_provider_data(
            store, key, HOUSEHOLD_LOAD_ADAPTER, data_label="household-load"
        ),
    )
    return _household_load_response(provider_data)


@router.get(
    "/api/v1/historic/household-load",
    response_model=HistoricHouseholdLoadResponse,
    responses={
        404: {"description": "No persisted household-load data is available"},
        503: {"description": "Persisted household-load data is unavailable"},
    },
)
def historic_household_load(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> HistoricHouseholdLoadResponse:
    """Return persisted household-load actuals for a requested time range."""
    try:
        require_aware_timestamps([start_time, end_time])
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="start_time and end_time must include a timezone",
        )
    start = start_time.astimezone(timezone.utc)
    end = end_time.astimezone(timezone.utc)
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

    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant household-load provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=configuration.home_assistant.household_load_source_id,
    )
    key = ProviderDataKey(
        data_type="household-load",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    complete_data = cast(
        HouseholdLoadData,
        load_provider_data(
            store, key, HOUSEHOLD_LOAD_ADAPTER, data_label="household-load"
        ),
    )
    try:
        provider_data = store.load_household_load_range(key, start, end)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover household-load provider data: {error}",
        ) from error

    available_start = complete_data.start_time.astimezone(timezone.utc)
    available_end = available_start + timedelta(hours=len(complete_data.load_kw))
    freshness = _household_load_freshness(
        complete_data,
        configuration.home_assistant.max_data_age_seconds,
    )
    has_suspect_quality = provider_data is not None and any(
        item.status == "suspect" for item in _expanded_quality(provider_data)
    )
    response_status: Literal["validated", "stale", "suspect", "empty"] = (
        "empty"
        if provider_data is None
        else (
            "suspect"
            if has_suspect_quality
            else ("stale" if freshness == "stale" else "validated")
        )
    )
    timestamps = (
        [
            provider_data.start_time.astimezone(timezone.utc) + timedelta(hours=index)
            for index in range(len(provider_data.load_kw))
        ]
        if provider_data is not None
        else []
    )
    return HistoricHouseholdLoadResponse(
        status=response_status,
        data_type="household_load",
        schema_version=complete_data.schema_version,
        start_time=start,
        end_time=end,
        interval_minutes=complete_data.interval_minutes,
        timestamps=timestamps,
        load_kw=list(provider_data.load_kw) if provider_data is not None else [],
        quality=(
            [
                HouseholdLoadQuality(
                    status=item.status,
                    reason=item.reason,
                    entity_id=item.entity_id,
                )
                for item in _expanded_quality(provider_data)
            ]
            if provider_data is not None
            else []
        ),
        unit=complete_data.unit,
        source=SourceMetadata(
            provider=complete_data.source.provider,
            entity_id=complete_data.source.entity_id,
        ),
        coverage_start_time=(
            provider_data.start_time if provider_data is not None else None
        ),
        coverage_end_time=(
            provider_data.start_time + timedelta(hours=len(provider_data.load_kw))
            if provider_data is not None
            else None
        ),
        available_start_time=available_start,
        available_end_time=available_end,
        retrieved_at=complete_data.retrieved_at,
        latest_observation_at=complete_data.latest_observation_at,
        validation_status="suspect" if has_suspect_quality else "valid",
        freshness=freshness,
        freshness_checked_at=datetime.now(timezone.utc),
    )


def _expanded_quality(data: HouseholdLoadData) -> tuple[IntervalQuality, ...]:
    """Return one quality value for every persisted household-load interval."""
    if data.quality:
        if len(data.quality) != len(data.load_kw):
            raise HTTPException(
                status_code=503,
                detail="persisted household-load quality metadata is misaligned",
            )
        return data.quality
    return tuple(IntervalQuality() for _ in data.load_kw)


def _household_load_freshness(
    data: HouseholdLoadData,
    max_age_seconds: float | None,
) -> Literal["fresh", "stale", "unknown"]:
    """Assess polling freshness without invalidating historical actuals."""
    if max_age_seconds is None:
        return "unknown"
    age_seconds = (
        datetime.now(timezone.utc) - data.latest_observation_at.astimezone(timezone.utc)
    ).total_seconds()
    return "fresh" if age_seconds <= max_age_seconds else "stale"


@tail_router.post("/api/v1/pv-generation", response_model=PvGenerationResponse)
def pv_generation(request: PvGenerationRequest) -> PvGenerationResponse:
    """Validate a versioned hourly PV-generation data series."""
    return PvGenerationResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        generation_kw=request.generation_kw,
        unit=request.unit,
        source=request.source,
    )


@tail_router.post("/api/v1/grid-flow", response_model=GridFlowResponse)
def grid_flow(request: Request, data: GridFlowRequest) -> GridFlowResponse:
    """Validate a versioned hourly grid import and export data series."""
    configuration = request.app.state.configuration
    provider_data: GridFlowData | None = (
        GridFlowData(
            schema_version=data.schema_version,
            start_time=data.start_time,
            interval_minutes=data.interval_minutes,
            import_kw=tuple(data.import_kw),
            export_kw=tuple(data.export_kw),
            unit=data.unit,
            source=_provider_source(data.source) if data.source is not None else None,
            retrieved_at=data.retrieved_at,
            latest_observation_at=data.latest_observation_at,
        )
        if data.source is not None
        else None
    )
    persisted = (
        _persist_configured_submission(
            request,
            data,
            provider_data,
            GRID_FLOW_ADAPTER,
            data_type="grid-flow",
            data_label="grid-flow",
            is_configured=configuration.is_configured_grid_flow_source,
        )
        if provider_data is not None
        else None
    )
    return _grid_flow_response(cast(GridFlowData | None, persisted) or data)


@tail_router.get(
    "/api/v1/grid-flow",
    response_model=GridFlowResponse,
    responses={
        404: {"description": "No persisted provider data is available"},
        503: {"description": "Provider data persistence is unavailable"},
    },
)
def persisted_grid_flow(request: Request) -> GridFlowResponse:
    """Return the latest persisted normalized grid-flow provider data."""
    application_configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if application_configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant grid-flow provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=application_configuration.home_assistant.grid_flow_source_id,
    )
    key = ProviderDataKey(
        data_type="grid-flow",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    provider_data = cast(
        GridFlowData,
        load_provider_data(store, key, GRID_FLOW_ADAPTER, data_label="grid-flow"),
    )
    return _grid_flow_response(provider_data)
