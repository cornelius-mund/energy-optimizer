"""Generic appliance capabilities and retained energy-history reads."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import TypeAdapter

from energy_optimizer.api.routers.provider import polling_freshness, validated_utc_range
from energy_optimizer.api.schemas import DashboardSeries, SourceMetadata
from energy_optimizer.api.series import align_hourly_values
from energy_optimizer.appliances import ApplianceCapabilities
from energy_optimizer.household_load_records import household_load_points
from energy_optimizer.providers.home_assistant_energy_history import EnergyHistoryData
from energy_optimizer.storage import ProviderDataKey, ProviderDataStoreError

router = APIRouter()


@router.get("/api/v1/appliances", response_model=dict[str, ApplianceCapabilities])
def appliances(request: Request) -> dict[str, ApplianceCapabilities]:
    """List configured appliances and their explicit accounting/control modes."""
    return {
        name: ApplianceCapabilities.model_validate(item.model_dump(exclude={"history"}))
        for name, item in request.app.state.configuration.appliances.items()
    }


@router.post("/api/v1/appliances/validate", response_model=ApplianceCapabilities)
def validate_appliance(data: ApplianceCapabilities) -> ApplianceCapabilities:
    """Validate provider-independent electrical appliance capabilities."""
    return data


@router.get("/api/v1/energy-history/{source_id}", response_model=DashboardSeries)
def energy_history(
    request: Request,
    source_id: str,
    start_time: datetime = Query(),
    end_time: datetime = Query(),
) -> DashboardSeries:
    """Read appliance or generic measured energy history for a UTC-hour range."""
    start, end = validated_utc_range(start_time, end_time, hour_aligned=True)
    configuration = request.app.state.configuration
    home_assistant = configuration.home_assistant
    known = configuration.configured_energy_histories()
    if source_id not in known:
        raise HTTPException(404, "energy history source is not configured")
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(503, "energy history persistence is not configured")
    try:
        data = store.load(
            ProviderDataKey("energy-history", "home-assistant", source_id),
            TypeAdapter(EnergyHistoryData),
        )
        if data is None:
            raise HTTPException(404, "no imported energy history is available")
        points = household_load_points(data.as_load())
    except ProviderDataStoreError as error:
        raise HTTPException(
            503, f"energy history is invalid or unavailable: {error}"
        ) from error
    selected = align_hourly_values(points, start, end)
    return DashboardSeries(
        id=f"{source_id}_actual",
        data_type=source_id,
        scenario_kind="actual",
        timestamps=[time for time, _ in selected],
        values=[value for _, value in selected],
        unit="kW",
        source=SourceMetadata(
            provider=data.source.provider, entity_id=data.source.entity_id
        ),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.start_time,
        available_end_time=data.start_time + timedelta(hours=len(data.power_kw)),
        retrieved_at=data.retrieved_at,
        freshness=polling_freshness(
            data.latest_observation_at,
            home_assistant.max_data_age_seconds if home_assistant else None,
            datetime.now(timezone.utc),
        ),
        validation_status="valid",
        missing_intervals=[time for time, value in selected if value is None],
    )
