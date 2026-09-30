"""Provider-independent electrical heat-pump constraints; no thermal model."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from energy_optimizer.providers.normalization import as_utc

Power = Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)]


class HeatPumpSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    entity_id: str | None = Field(default=None, min_length=1, max_length=255)


class HeatPumpLoad(BaseModel):
    """An exact electrical energy requirement movable across available hours."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    load_kw: list[Power] = Field(min_length=1, max_length=168)
    available: list[StrictBool] = Field(min_length=1, max_length=168)
    minimum_power_kw: Power
    maximum_power_kw: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    required_energy_kwh: Annotated[float, Field(ge=0, le=168000, allow_inf_nan=False)]
    unit: Literal["kW"]
    energy_unit: Literal["kWh"]
    source: HeatPumpSource | None = None
    retrieved_at: datetime
    latest_observation_at: datetime

    @model_validator(mode="after")
    def validate_constraints(self) -> "HeatPumpLoad":
        """Normalize timestamps and reject contradictory electrical limits."""
        for name in ("start_time", "retrieved_at", "latest_observation_at"):
            setattr(
                self,
                name,
                as_utc(
                    getattr(self, name),
                    error_factory=ValueError,
                    message=f"{name} must include a timezone",
                ),
            )
        if self.latest_observation_at > self.retrieved_at:
            raise ValueError(
                "latest_observation_at must not be later than retrieved_at"
            )
        if len(self.load_kw) != len(self.available):
            raise ValueError("load_kw and available must have the same length")
        if self.minimum_power_kw > self.maximum_power_kw:
            raise ValueError("minimum_power_kw must not exceed maximum_power_kw")
        if any(value > self.maximum_power_kw for value in self.load_kw):
            raise ValueError("load_kw must not exceed maximum_power_kw")
        if self.required_energy_kwh > sum(self.available) * self.maximum_power_kw:
            raise ValueError("required_energy_kwh exceeds available hourly capacity")
        if 0 < self.required_energy_kwh < self.minimum_power_kw:
            raise ValueError(
                "required_energy_kwh is below minimum hourly operating energy"
            )
        return self


class HeatPumpLoadResponse(HeatPumpLoad):
    status: Literal["validated", "stale"] = "validated"
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
