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
    HouseholdLoadRequest,
    HouseholdLoadResponse,
    PvGenerationRequest,
    PvGenerationResponse,
    SourceMetadata,
)
from energy_optimizer.api.validation import require_aware_timestamps
from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.heat_pump import (
    HeatPumpLoad,
    HeatPumpLoadResponse,
    HeatPumpSource,
)
from energy_optimizer.providers.interfaces import GridFlowData, HouseholdLoadData
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

router = APIRouter()
tail_router = APIRouter()
Freshness = Literal["fresh", "stale", "unknown"]


@router.post("/api/v1/heat-pump", response_model=HeatPumpLoadResponse)
def heat_pump(data: HeatPumpLoad) -> HeatPumpLoadResponse:
    """Validate normalized heat-pump load and electrical flexibility constraints."""
    return HeatPumpLoadResponse(**data.model_dump())


@router.get(
    "/api/v1/heat-pump",
    response_model=HeatPumpLoadResponse,
    responses={
        404: {"description": "No imported heat-pump snapshot"},
        503: {"description": "Mappings or persistence unavailable"},
    },
)
def persisted_heat_pump(request: Request) -> HeatPumpLoadResponse:
    """Return the latest imported heat-pump snapshot with polling freshness."""
    store, configuration = _require_home_assistant(request, "heat-pump")
    if configuration.heat_pump is None:
        raise HTTPException(
            status_code=503,
            detail="Home Assistant heat-pump mappings are not configured",
        )
    data = _load_persisted(
        store,
        ProviderDataKey("heat-pump", "home-assistant", "heat_pump"),
        TypeAdapter(HeatPumpLoad),
    )
    freshness = polling_freshness(
        data.latest_observation_at,
        configuration.max_data_age_seconds,
        datetime.now(timezone.utc),
    )
    return HeatPumpLoadResponse(
        **data.model_dump(),
        freshness=freshness,
        status="stale" if freshness == "stale" else "validated",
    )


def validated_utc_range(
    start_time: datetime, end_time: datetime, *, hour_aligned: bool = False
) -> tuple[datetime, datetime]:
    """Validate one half-open request range and normalize its bounds to UTC."""
    try:
        require_aware_timestamps([start_time, end_time])
    except ValueError:
        raise HTTPException(
            status_code=422, detail="start_time and end_time must include a timezone"
        )
    start = start_time.astimezone(timezone.utc)
    end = end_time.astimezone(timezone.utc)
    if hour_aligned and any(
        bound.minute or bound.second or bound.microsecond for bound in (start, end)
    ):
        raise HTTPException(
            status_code=422,
            detail="dashboard range boundaries must be aligned to the hour",
        )
    if end <= start:
        raise HTTPException(
            status_code=422, detail="end_time must be later than start_time"
        )
    if end - start > timedelta(hours=MAX_HORIZON_HOURS):
        raise HTTPException(
            status_code=422,
            detail=f"requested range must not exceed {MAX_HORIZON_HOURS} hours",
        )
    return start, end


def polling_freshness(
    latest_observation_at: datetime, max_age_seconds: float | None, now: datetime
) -> Freshness:
    """Assess polling freshness without invalidating historical actuals."""
    if max_age_seconds is None:
        return "unknown"
    age_seconds = (now - latest_observation_at).total_seconds()
    return "fresh" if age_seconds <= max_age_seconds else "stale"


def api_source(
    source: ProviderSourceMetadata | SourceMetadata | HeatPumpSource,
) -> SourceMetadata:
    """Convert source metadata to the HTTP representation."""
    return SourceMetadata(provider=source.provider, entity_id=source.entity_id)


def _provider_source(source: SourceMetadata) -> ProviderSourceMetadata:
    """Convert HTTP source metadata to the provider-independent representation."""
    return ProviderSourceMetadata(provider=source.provider, entity_id=source.entity_id)


def _persist_configured_submission[ModelT: (HouseholdLoadData, GridFlowData)](
    request: Request,
    provider_data: ModelT,
    adapter: TypeAdapter[ModelT],
    *,
    data_label: str,
    is_configured: Callable[[str, str | None], bool],
) -> ModelT | None:
    """Persist a submission only when it identifies a configured source."""
    store = request.app.state.provider_data_store
    source = provider_data.source
    if store is None or not is_configured(source.provider, source.entity_id):
        return None
    key = ProviderDataKey(
        data_type=data_label, provider=source.provider, entity_id=source.entity_id
    )
    return cast(
        ModelT | None,
        persist_provider_data(
            store, key, provider_data, adapter, data_label=data_label
        ),
    )


def _require_home_assistant(
    request: Request, data_label: str
) -> tuple[ProviderDataStore, HomeAssistantConfiguration]:
    """Return the store and Home Assistant configuration behind a persisted read."""
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503, detail="provider data persistence is not configured"
        )
    home_assistant = request.app.state.configuration.home_assistant
    if home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail=f"the Home Assistant {data_label} provider is not configured",
        )
    return store, home_assistant


def _load_persisted[ModelT](
    store: ProviderDataStore, key: ProviderDataKey, adapter: TypeAdapter[ModelT]
) -> ModelT:
    """Load one persisted model, labelling errors with the key's data type."""
    return cast(
        ModelT, load_provider_data(store, key, adapter, data_label=key.data_type)
    )


def _household_load_response(
    data: HouseholdLoadRequest | HouseholdLoadData,
) -> HouseholdLoadResponse:
    """Map either an HTTP request or persisted model to one response shape."""
    return HouseholdLoadResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        load_kw=list[float | None](data.load_kw),
        unit=data.unit,
        source=None if data.source is None else api_source(data.source),
        retrieved_at=data.retrieved_at,
        latest_observation_at=data.latest_observation_at,
    )


def _grid_flow_response(data: GridFlowRequest | GridFlowData) -> GridFlowResponse:
    """Map either an HTTP request or persisted model to one response shape."""
    return GridFlowResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        import_kw=list[float | None](data.import_kw),
        export_kw=list[float | None](data.export_kw),
        unit=data.unit,
        source=None if data.source is None else api_source(data.source),
        retrieved_at=data.retrieved_at,
        latest_observation_at=data.latest_observation_at,
    )


@router.post("/api/v1/battery", response_model=BatteryResponse)
def battery(request: BatteryRequest) -> BatteryResponse:
    """Validate a versioned hourly battery state and capabilities object."""
    return BatteryResponse(status="validated", **request.model_dump())


@router.post("/api/v1/electricity-prices", response_model=ElectricityPriceResponse)
def electricity_prices(request: ElectricityPriceRequest) -> ElectricityPriceResponse:
    """Validate normalized hourly electricity-price data."""
    return ElectricityPriceResponse(status="validated", **request.model_dump())


@router.post(
    "/api/v1/household-load",
    response_model=HouseholdLoadResponse,
    responses={503: {"description": "Provider data could not be persisted"}},
)
def household_load(
    request: Request, data: HouseholdLoadRequest
) -> HouseholdLoadResponse:
    """Validate a versioned hourly household-load data series."""
    configuration = request.app.state.configuration
    persisted = None
    if data.source is not None:
        persisted = _persist_configured_submission(
            request,
            HouseholdLoadData(
                schema_version=data.schema_version,
                start_time=data.start_time,
                interval_minutes=data.interval_minutes,
                load_kw=tuple(data.load_kw),
                unit=data.unit,
                source=_provider_source(data.source),
                retrieved_at=data.retrieved_at,
                latest_observation_at=data.latest_observation_at,
            ),
            HOUSEHOLD_LOAD_ADAPTER,
            data_label="household-load",
            is_configured=configuration.is_configured_household_load_source,
        )
    return _household_load_response(persisted or data)


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
    store, home_assistant = _require_home_assistant(request, "household-load")
    key = ProviderDataKey(
        "household-load", "home-assistant", home_assistant.household_load_source_id
    )
    return _household_load_response(_load_persisted(store, key, HOUSEHOLD_LOAD_ADAPTER))


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
    start, end = validated_utc_range(start_time, end_time)
    store, home_assistant = _require_home_assistant(request, "household-load")
    key = ProviderDataKey(
        "household-load", "home-assistant", home_assistant.household_load_source_id
    )
    complete_data = _load_persisted(store, key, HOUSEHOLD_LOAD_ADAPTER)
    try:
        provider_data = store.load_household_load_range(key, start, end)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover household-load provider data: {error}",
        ) from error

    available_start = complete_data.start_time.astimezone(timezone.utc)
    available_end = available_start + timedelta(hours=len(complete_data.load_kw))
    freshness = polling_freshness(
        complete_data.latest_observation_at.astimezone(timezone.utc),
        home_assistant.max_data_age_seconds,
        datetime.now(timezone.utc),
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
        status=(
            "empty"
            if provider_data is None
            else ("stale" if freshness == "stale" else "validated")
        ),
        data_type="household_load",
        schema_version=complete_data.schema_version,
        start_time=start,
        end_time=end,
        interval_minutes=complete_data.interval_minutes,
        timestamps=timestamps,
        load_kw=list(provider_data.load_kw) if provider_data is not None else [],
        unit=complete_data.unit,
        source=api_source(complete_data.source),
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
        freshness=freshness,
        freshness_checked_at=datetime.now(timezone.utc),
    )


@tail_router.post("/api/v1/pv-generation", response_model=PvGenerationResponse)
def pv_generation(request: PvGenerationRequest) -> PvGenerationResponse:
    """Validate a versioned hourly PV-generation data series."""
    return PvGenerationResponse(status="validated", **request.model_dump())


@tail_router.post("/api/v1/grid-flow", response_model=GridFlowResponse)
def grid_flow(request: Request, data: GridFlowRequest) -> GridFlowResponse:
    """Validate a versioned hourly grid import and export data series."""
    configuration = request.app.state.configuration
    persisted = None
    if data.source is not None:
        persisted = _persist_configured_submission(
            request,
            GridFlowData(
                schema_version=data.schema_version,
                start_time=data.start_time,
                interval_minutes=data.interval_minutes,
                import_kw=tuple(data.import_kw),
                export_kw=tuple(data.export_kw),
                unit=data.unit,
                source=_provider_source(data.source),
                retrieved_at=data.retrieved_at,
                latest_observation_at=data.latest_observation_at,
            ),
            GRID_FLOW_ADAPTER,
            data_label="grid-flow",
            is_configured=configuration.is_configured_grid_flow_source,
        )
    return _grid_flow_response(persisted or data)


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
    store, home_assistant = _require_home_assistant(request, "grid-flow")
    key = ProviderDataKey(
        "grid-flow", "home-assistant", home_assistant.grid_flow_source_id
    )
    return _grid_flow_response(_load_persisted(store, key, GRID_FLOW_ADAPTER))
