"""How much battery energy the plan keeps back for load forecast errors.

Hourly forecast errors partly cancel, so the energy the house may draw beyond
the forecast grows with the square root of the lead time. The plan is
recomputed every hour with the measured state of charge, so the first hours need
no reserve, and beyond a day PV or cheap night hours refill the battery anyway.
"""

import math

from energy_optimizer.config import BatteryReserveConfiguration

STANDARD_DEVIATION_PER_MEAN_ABSOLUTE_ERROR = math.sqrt(math.pi / 2)


def load_error_kw(
    settings: BatteryReserveConfiguration, mean_absolute_error_kw: float | None
) -> float:
    """The configured standard deviation, else the one of a normal error with the
    backtested mean absolute error."""
    if settings.forecast_error_kw is not None:
        return settings.forecast_error_kw
    if mean_absolute_error_kw is None:
        return 0.0
    return mean_absolute_error_kw * STANDARD_DEVIATION_PER_MEAN_ABSOLUTE_ERROR


def reserve_schedule(
    settings: BatteryReserveConfiguration,
    hours: int,
    error_kw: float,
    usable_kwh: float,
) -> tuple[float, ...]:
    """Reserve above the minimum state of charge at the end of every planned hour."""
    reserves = []
    for hour in range(hours):
        lead_hours = hour + 1 - settings.unplanned_hours
        if lead_hours <= 0:
            reserves.append(0.0)
            continue
        reserve_kwh = (
            settings.confidence
            * error_kw
            * math.sqrt(min(lead_hours, settings.growth_cap_hours))
        )
        reserves.append(min(reserve_kwh, usable_kwh))
    return tuple(reserves)
