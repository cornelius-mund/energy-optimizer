"""Core HTTP API routes."""

from fastapi import APIRouter, HTTPException
from starlette.responses import RedirectResponse

from energy_optimizer import __version__
from energy_optimizer.api.schemas import HourlyOptimizationRequest, OptimizationResponse

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


@router.post("/optimize", response_model=OptimizationResponse)
def optimize(request: HourlyOptimizationRequest) -> OptimizationResponse:
    """Validate an hourly optimization request at the versioned API boundary."""
    return OptimizationResponse(
        status="validated",
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        hours=len(request.load_kw),
    )
