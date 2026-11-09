"""Core HTTP API routes."""

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from energy_optimizer import __version__
from energy_optimizer.api.schemas import (
    HourlyOptimizationRequest,
    LoadForecastResponse,
    OptimizationResponse,
    PlannedBatteryResponse,
    PlannedEvResponse,
    PlannedHourResponse,
    PlanningBatteryRequest,
    PlanSnapshotResponse,
)
from energy_optimizer.config import Configuration
from energy_optimizer.optimization import (
    BatterySpecification,
    LinopyPlanSolver,
    Plan,
    PlannedBatteryHour,
    PlannedEvHour,
    PlanningProblem,
    PlanSolver,
)
from energy_optimizer.optimization.plan_generation import PLAN_ADAPTER, PLAN_KEY

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
        battery=None if request.battery is None else _battery(request.battery),
    )


def _battery(battery: PlanningBatteryRequest) -> BatterySpecification:
    def energy_kwh(percent: float) -> float:
        return battery.capacity_kwh * percent / 100

    final_percent = (
        battery.initial_soc_percent
        if battery.final_soc_percent is None
        else battery.final_soc_percent
    )
    return BatterySpecification(
        minimum_soc_kwh=energy_kwh(battery.minimum_soc_percent),
        maximum_soc_kwh=energy_kwh(battery.maximum_soc_percent),
        initial_soc_kwh=energy_kwh(battery.initial_soc_percent),
        final_soc_kwh=energy_kwh(final_percent),
        maximum_charge_kw=battery.maximum_charge_kw,
        maximum_discharge_kw=battery.maximum_discharge_kw,
        charge_efficiency=battery.charge_efficiency,
        discharge_efficiency=battery.discharge_efficiency,
        throughput_cost_eur_per_kwh=battery.throughput_cost_eur_per_kwh,
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
        battery_throughput_kwh=plan.battery_throughput_kwh,
        degradation_cost_eur=plan.degradation_cost_eur,
        ev_shortfall_kwh=plan.ev_shortfall_kwh,
        schedule=_planned_hours(
            plan, request.battery.capacity_kwh if request.battery else None
        ),
    )


def _planned_hours(
    plan: Plan,
    battery_capacity_kwh: float | None,
    ev_capacity_kwh: float | None = None,
) -> list[PlannedHourResponse]:
    return [
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
            battery=_planned_battery(battery_capacity_kwh, hour.battery),
            ev=_planned_ev(ev_capacity_kwh, hour.ev),
        )
        for hour in plan.hours
    ]


@router.get("/plan", response_model=PlanSnapshotResponse)
def current_plan(http_request: Request) -> PlanSnapshotResponse:
    """Return the latest plan that the orchestration cycle stored."""
    store = http_request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503, detail="provider data persistence is not configured"
        )
    stored = store.load(PLAN_KEY, PLAN_ADAPTER)
    if stored is None:
        raise HTTPException(status_code=404, detail="no plan has been created yet")
    return PlanSnapshotResponse(
        created_at=stored.created_at,
        status=stored.plan.status.value,
        start_time=stored.start_time,
        interval_minutes=60,
        hours=len(stored.plan.hours),
        objective_eur=stored.plan.objective_eur,
        load_forecast=(
            None
            if stored.load_forecast_method is None
            else LoadForecastResponse(
                method=stored.load_forecast_method,
                mean_absolute_error_kw=stored.load_forecast_mae_kw,
            )
        ),
        price_forecast_from=stored.price_forecast_from,
        ev_shortfall_kwh=stored.plan.ev_shortfall_kwh,
        battery_throughput_kwh=stored.plan.battery_throughput_kwh,
        degradation_cost_eur=stored.plan.degradation_cost_eur,
        schedule=_planned_hours(
            stored.plan, stored.battery_capacity_kwh, stored.ev_capacity_kwh
        ),
    )


def _planned_ev(
    capacity_kwh: float | None, planned: PlannedEvHour | None
) -> PlannedEvResponse | None:
    if capacity_kwh is None or planned is None:
        return None
    return PlannedEvResponse(
        charge_kw=planned.charge_kw,
        state_of_charge_kwh=planned.state_of_charge_kwh,
        state_of_charge_percent=planned.state_of_charge_kwh / capacity_kwh * 100,
        expected_plugged_in=planned.plugged_in,
        consumption_kwh=planned.consumption_kwh,
    )


def _planned_battery(
    capacity_kwh: float | None, planned: PlannedBatteryHour | None
) -> PlannedBatteryResponse | None:
    if capacity_kwh is None or planned is None:
        return None
    return PlannedBatteryResponse(
        charge_kw=planned.charge_kw,
        discharge_kw=planned.discharge_kw,
        state_of_charge_kwh=planned.state_of_charge_kwh,
        state_of_charge_percent=planned.state_of_charge_kwh / capacity_kwh * 100,
        degradation_cost_eur=planned.degradation_cost_eur,
    )
