"""Core HTTP API routes."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from energy_optimizer import __version__
from energy_optimizer.api.schemas import HourlyOptimizationRequest, OptimizationResponse
from energy_optimizer.optimization import (
    BatteryLimits,
    OptimizationError,
    solve_schedule,
)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}


@router.get("/dashboard", include_in_schema=False)
def dashboard_redirect() -> RedirectResponse:
    """Redirect the dashboard root to its trailing-slash entry point."""
    # Read the application value at call time so the package compatibility
    # wrapper can continue to override it for direct callers and tests.
    from energy_optimizer.api.app import FRONTEND_DIRECTORY

    if not FRONTEND_DIRECTORY.is_dir():
        raise HTTPException(
            status_code=503,
            detail="dashboard assets are not available in this deployment",
        )
    return RedirectResponse(url="/dashboard/", status_code=307)


@router.post(
    "/optimize",
    response_model=OptimizationResponse,
    responses={503: {"description": "Solver unavailable or optimum not reached"}},
)
def optimize(request: Request, data: HourlyOptimizationRequest) -> OptimizationResponse:
    """Solve an hourly electrical schedule with optional flexible heat-pump load."""
    configuration = request.app.state.configuration
    battery = data.battery
    limits = (
        None
        if battery is None
        else BatteryLimits(
            minimum_soc_kwh=battery.minimum_soc_kwh,
            maximum_soc_kwh=battery.maximum_soc_kwh,
            initial_soc_kwh=battery.initial_soc_kwh,
            maximum_charge_kw=battery.maximum_charge_kw,
            maximum_discharge_kw=battery.maximum_discharge_kw,
            battery_efficiency=battery.battery_efficiency,
        )
    )
    try:
        schedule = solve_schedule(
            data.load_kw,
            data.pv_generation_kw,
            data.import_price_eur_per_kwh,
            data.export_price_eur_per_kwh,
            maximum_import_kw=configuration.grid.maximum_import_kw,
            maximum_export_kw=configuration.grid.maximum_export_kw,
            solver_name=configuration.solver.name,
            time_limit_seconds=configuration.solver.time_limit_seconds,
            heat_pump=data.heat_pump,
            battery=limits,
        )
    except OptimizationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return OptimizationResponse(
        **asdict(schedule),
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        hours=len(data.load_kw),
        diagnostics=["Load and operating limits cannot be fulfilled"]
        if schedule.status == "infeasible"
        else [],
    )
