"""HTTP API for the Energy Optimizer service."""

import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from energy_optimizer import __version__
from energy_optimizer.config import load_configuration

MAX_HORIZON_HOURS = 87_672


class HourlyOptimizationRequest(BaseModel):
    """Validated hourly inputs accepted by the optimization boundary."""

    model_config = ConfigDict(extra="forbid")

    start_time: datetime = Field(description="Timezone-aware start of the horizon")
    interval_minutes: Literal[60] = Field(
        description="Duration of every interval; hourly requests require 60"
    )
    load_kw: list[Annotated[float, Field(ge=0, description="Household load in kW")]] = (
        Field(min_length=1, max_length=168)
    )
    pv_generation_kw: list[
        Annotated[float, Field(ge=0, description="PV generation in kW")]
    ] = Field(min_length=1, max_length=168)
    import_price_eur_per_kwh: list[
        Annotated[float, Field(description="Grid import price in EUR/kWh")]
    ] = Field(min_length=1, max_length=168)
    export_price_eur_per_kwh: list[
        Annotated[float, Field(description="Grid export price in EUR/kWh")]
    ] = Field(min_length=1, max_length=168)

    @model_validator(mode="after")
    def validate_horizon(self) -> "HourlyOptimizationRequest":
        """Require a timezone and one value for every input series per hour."""
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("start_time must include a timezone")

        lengths = {
            "load_kw": len(self.load_kw),
            "pv_generation_kw": len(self.pv_generation_kw),
            "import_price_eur_per_kwh": len(self.import_price_eur_per_kwh),
            "export_price_eur_per_kwh": len(self.export_price_eur_per_kwh),
        }
        if len(set(lengths.values())) != 1:
            raise ValueError(f"time-series lengths must match: {lengths}")
        return self


class SourceMetadata(BaseModel):
    """Identify the system that supplied a normalized data series."""

    model_config = ConfigDict(extra="forbid")

    provider: Annotated[str, Field(min_length=1, max_length=100)]
    entity_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None


class ElectricityPriceRequest(BaseModel):
    """Versioned normalized hourly electricity-price data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    timestamps: list[datetime] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Timezone-aware hourly timestamps in ascending order",
    )
    interval_minutes: Literal[60] = Field(
        description="Duration of every price interval; hourly data requires 60"
    )
    import_price_eur_per_kwh: list[Annotated[float, Field(ge=-100, le=100)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid import price in EUR/kWh, one value per timestamp",
    )
    export_price_eur_per_kwh: list[Annotated[float, Field(ge=-100, le=100)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid export price in EUR/kWh, one value per timestamp",
    )
    unit: Literal["EUR/kWh"] = Field(description="Unit used by price fields")
    source: SourceMetadata = Field(description="Provider-independent source metadata")
    retrieved_at: datetime = Field(
        description="Time when the source data was retrieved"
    )
    expires_at: datetime = Field(description="Time after which the data is stale")

    @field_validator("timestamps", "retrieved_at", "expires_at")
    @classmethod
    def validate_aware_timestamps(
        cls, values: datetime | list[datetime]
    ) -> datetime | list[datetime]:
        """Reject naive timestamps that cannot be compared safely."""
        candidates = values if isinstance(values, list) else [values]
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in candidates
        ):
            raise ValueError("timestamps must include a timezone")
        return values

    @field_validator("import_price_eur_per_kwh", "export_price_eur_per_kwh")
    @classmethod
    def validate_finite_prices(cls, values: list[float]) -> list[float]:
        """Reject non-finite values that cannot represent prices."""
        if not all(math.isfinite(value) for value in values):
            raise ValueError("price values must be finite")
        return values

    @model_validator(mode="after")
    def validate_price_series(self) -> "ElectricityPriceRequest":
        """Require aligned, ordered, fresh price data with explicit coverage."""
        if len(self.timestamps) != len(self.import_price_eur_per_kwh):
            raise ValueError(
                "timestamps and import_price_eur_per_kwh must have the same length"
            )
        if len(self.timestamps) != len(self.export_price_eur_per_kwh):
            raise ValueError(
                "timestamps and export_price_eur_per_kwh must have the same length"
            )
        timestamp_values = [timestamp.timestamp() for timestamp in self.timestamps]
        if timestamp_values != sorted(set(timestamp_values)):
            raise ValueError("timestamps must be unique and in ascending order")
        if self.expires_at <= self.retrieved_at:
            raise ValueError("expires_at must be later than retrieved_at")
        if self.timestamps[0] < self.retrieved_at:
            raise ValueError("price coverage must not start before retrieved_at")
        if self.timestamps[-1] >= self.expires_at:
            raise ValueError("price data is stale at the end of its coverage")
        return self


class ElectricityPriceResponse(BaseModel):
    """Response returned after electricity-price data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    timestamps: list[datetime]
    interval_minutes: Literal[60]
    import_price_eur_per_kwh: list[float]
    export_price_eur_per_kwh: list[float]
    unit: Literal["EUR/kWh"]
    source: SourceMetadata
    retrieved_at: datetime
    expires_at: datetime


class OptimizationResponse(BaseModel):
    """Response returned after an hourly request passes API validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    start_time: datetime
    interval_minutes: Literal[60]
    hours: int = Field(ge=1, le=168)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load configuration before accepting requests."""
    path = Path(os.environ.get("ENERGY_OPTIMIZER_CONFIG", "config.yaml"))
    application.state.configuration = load_configuration(path)
    yield


app = FastAPI(title="Energy Optimizer", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}


@app.post("/optimize", response_model=OptimizationResponse)
def optimize(request: HourlyOptimizationRequest) -> OptimizationResponse:
    """Validate an hourly optimization request at the versioned API boundary."""
    return OptimizationResponse(
        status="validated",
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        hours=len(request.load_kw),
    )


@app.post("/api/v1/electricity-prices", response_model=ElectricityPriceResponse)
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
