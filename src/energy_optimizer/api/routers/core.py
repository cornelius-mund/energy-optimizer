"""Core HTTP API routes."""

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from energy_optimizer import __version__
from energy_optimizer.api.schemas import (
    HourlyOptimizationRequest,
    OptimizationResponse,
    PlannedHourResponse,
)
from energy_optimizer.config import Configuration
from energy_optimizer.optimization import (
    LinopyPlanSolver,
    Plan,
    PlanningProblem,
    PlanSolver,
)

router = APIRouter()
PLAN_SOLVER: PlanSolver = LinopyPlanSolver()


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


@router.post("/optimize", response_model=OptimizationResponse)
def optimize(
    request: HourlyOptimizationRequest, http_request: Request
) -> OptimizationResponse:
    """Calculate the cheapest schedule for the household load and PV forecast."""
    configuration: Configuration = http_request.app.state.configuration
    plan = PLAN_SOLVER.solve(_planning_problem(request, configuration))
    return _optimization_response(request, plan)


def _planning_problem(
    request: HourlyOptimizationRequest, configuration: Configuration
) -> PlanningProblem:
    return PlanningProblem(
        start_time=request.start_time,
        load_kw=tuple(request.load_kw),
        pv_forecast_kw=tuple(request.pv_generation_kw),
        import_price_eur_per_kwh=tuple(request.import_price_eur_per_kwh),
        export_price_eur_per_kwh=tuple(request.export_price_eur_per_kwh),
        maximum_import_kw=configuration.grid.maximum_import_kw,
        maximum_export_kw=configuration.grid.maximum_export_kw,
        time_limit_seconds=configuration.solver.time_limit_seconds,
    )


def _optimization_response(
    request: HourlyOptimizationRequest, plan: Plan
) -> OptimizationResponse:
    return OptimizationResponse(
        status=plan.status.value,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        hours=len(request.load_kw),
        objective_eur=plan.objective_eur,
        schedule=[
            PlannedHourResponse(
                start_time=hour.start_time,
                load_kw=hour.load_kw,
                pv_generation_kw=hour.pv_generation_kw,
                pv_self_consumption_kw=hour.pv_self_consumption_kw,
                pv_curtailment_kw=hour.pv_curtailment_kw,
                grid_import_kw=hour.grid_import_kw,
                grid_export_kw=hour.grid_export_kw,
                import_cost_eur=hour.import_cost_eur,
                export_revenue_eur=hour.export_revenue_eur,
            )
            for hour in plan.hours
        ],
    )
