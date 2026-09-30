"""Electrical appliance capabilities and explicit household-load accounting."""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class ApplianceCapabilities(BaseModel):
    """Continuous power or enumerated fractions of rated maximum power."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=100)
    included_in_household_load: StrictBool
    maximum_power_kw: float = Field(gt=0, le=1000)
    control: Literal["continuous", "discrete"]
    power_levels: list[Annotated[float, Field(ge=0, le=1)]] | None = None

    @model_validator(mode="after")
    def validate_control(self) -> "ApplianceCapabilities":
        if self.control == "continuous":
            if self.power_levels is not None:
                raise ValueError("continuous control must not define power_levels")
        else:
            levels = self.power_levels
            if levels is None or len(levels) < 2:
                raise ValueError("discrete control requires at least two power_levels")
            if levels != sorted(set(levels)) or levels[0] != 0 or levels[-1] != 1:
                raise ValueError(
                    "power_levels must be unique, ascending, start at 0 and end at 1"
                )
        return self


def account_loads(
    household_kw: float | None,
    appliances: list[tuple[bool, float | None]],
) -> tuple[float | None, float | None]:
    """Return unmanaged household and total consumption without double counting.

    The household measurement is preserved. Included appliances are subtracted
    only from the unmanaged remainder; additional appliances are added only to
    total consumption. Missing observations propagate only to affected totals.
    """
    if household_kw is None:
        return None, None
    included = [power for is_included, power in appliances if is_included]
    additional = [power for is_included, power in appliances if not is_included]
    residual = (
        None
        if any(power is None for power in included)
        else household_kw - sum(power for power in included if power is not None)
    )
    if residual is not None:
        residual = 0.0 if math.isclose(residual, 0, abs_tol=1e-9) else residual
        if residual < 0:
            residual = None
    total = (
        None
        if any(power is None for power in additional)
        else household_kw + sum(power for power in additional if power is not None)
    )
    return residual, total
