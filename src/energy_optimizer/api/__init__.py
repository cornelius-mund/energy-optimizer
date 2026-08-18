"""Public HTTP API package."""

from importlib import import_module
from pathlib import Path
from typing import Any, cast

from starlette.responses import RedirectResponse

_app_module = import_module("energy_optimizer.api.app")
app = cast(Any, _app_module.app)
MAX_HORIZON_HOURS = cast(int, _app_module.MAX_HORIZON_HOURS)
configured_frontend_directory = cast(Any, _app_module.configured_frontend_directory)

# Keep this module-level compatibility hook usable by callers and tests that
# replace the configured dashboard directory through ``energy_optimizer.api``.
FRONTEND_DIRECTORY = cast(Path, getattr(_app_module, "FRONTEND_DIRECTORY"))


def dashboard_redirect() -> RedirectResponse:
    """Delegate to the application handler using the public compatibility value."""
    configured = getattr(_app_module, "FRONTEND_DIRECTORY")
    setattr(_app_module, "FRONTEND_DIRECTORY", FRONTEND_DIRECTORY)
    try:
        return cast(RedirectResponse, _app_module.dashboard_redirect())
    finally:
        setattr(_app_module, "FRONTEND_DIRECTORY", configured)


def __getattr__(name: str) -> Any:
    """Expose schema and handler symbols from the implementation module."""
    return getattr(_app_module, name)


__all__ = [
    "MAX_HORIZON_HOURS",
    "BatteryRequest",
    "BatteryResponse",
    "DashboardDataResponse",
    "DashboardPlanSummary",
    "DashboardSeries",
    "ElectricityPriceRequest",
    "ElectricityPriceResponse",
    "FRONTEND_DIRECTORY",
    "GridFlowRequest",
    "GridFlowResponse",
    "HistoricHouseholdLoadResponse",
    "HourlyOptimizationRequest",
    "HouseholdLoadQuality",
    "HouseholdLoadRequest",
    "HouseholdLoadResponse",
    "OptimizationResponse",
    "PvGenerationRequest",
    "PvGenerationResponse",
    "SourceMetadata",
    "app",
    "configured_frontend_directory",
    "dashboard_redirect",
    "health",
    "optimize",
    "battery",
    "electricity_prices",
    "household_load",
    "persisted_household_load",
    "historic_household_load",
    "dashboard_data",
    "forecast_data",
    "pv_generation",
    "grid_flow",
    "persisted_grid_flow",
]
