"""Turn the persisted provider snapshot into a stored optimized plan."""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from energy_optimizer.config import Configuration, EvChargingConfiguration
from energy_optimizer.control.ev_controller import (
    EV_SCHEDULE_ADAPTER,
    EV_SCHEDULE_KEY,
    merge_schedule,
)
from energy_optimizer.optimization.linopy_solver import LinopyPlanSolver
from energy_optimizer.optimization.load_forecast import (
    LoadForecast,
    LoadForecastError,
    forecast_load,
)
from energy_optimizer.optimization.problem import (
    BatterySpecification,
    EvSpecification,
    Plan,
    PlanningProblem,
)
from energy_optimizer.optimization.solver import PlanSolver
from energy_optimizer.orchestration import ProviderDataSnapshot
from energy_optimizer.providers.interfaces import (
    BatteryData,
    ElectricityPriceData,
    EvData,
    HouseholdLoadData,
    PvGenerationData,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

logger = logging.getLogger(__name__)

SURPLUS_CHARGING_VALUE_EUR_PER_KWH = 1.0
ONE_HOUR = timedelta(hours=1)
PLAN_KEY = ProviderDataKey("optimization-plan", "energy-optimizer", "current")


class PlanGenerationError(RuntimeError):
    """Raised when the snapshot cannot produce a stored optimal plan."""


@dataclass(frozen=True)
class PlanSnapshot:
    """The latest optimal plan and what is needed to present it."""

    created_at: datetime
    start_time: datetime
    battery_capacity_kwh: float | None
    plan: Plan
    ev_capacity_kwh: float | None = None
    load_forecast_method: str | None = None
    load_forecast_mae_kw: float | None = None


PLAN_ADAPTER = TypeAdapter(PlanSnapshot)


def battery_specification(
    battery: BatteryData,
    throughput_cost_eur_per_kwh: float = 0.0,
    terminal_value_eur_per_kwh: float | None = None,
) -> BatterySpecification:
    """Split the battery round trip evenly between charging and discharging."""
    leg_efficiency = math.sqrt(battery.battery_efficiency)
    return BatterySpecification(
        minimum_soc_kwh=battery.minimum_soc_kwh,
        maximum_soc_kwh=battery.maximum_soc_kwh,
        initial_soc_kwh=battery.initial_soc_kwh,
        final_soc_kwh=(
            battery.initial_soc_kwh
            if terminal_value_eur_per_kwh is None
            else battery.minimum_soc_kwh
        ),
        maximum_charge_kw=battery.maximum_charge_kw,
        maximum_discharge_kw=battery.maximum_discharge_kw,
        charge_efficiency=leg_efficiency,
        discharge_efficiency=leg_efficiency,
        throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
        terminal_value_eur_per_kwh=terminal_value_eur_per_kwh or 0.0,
    )


def deadline_hours(
    deadline: time, hour_starts: list[datetime], time_zone: ZoneInfo
) -> int | None:
    """Count the planned hours before the next local ``deadline``.

    Returns None when the deadline lies beyond the planning horizon, because the
    car then does not need to be charged by this plan yet.
    """
    upcoming = next_deadline(
        dict.fromkeys(range(7), (100.0, deadline)), hour_starts[0].astimezone(time_zone)
    )
    return None if upcoming is None else hours_until(upcoming[0], hour_starts)


def hours_until(deadline: datetime, hour_starts: list[datetime]) -> int | None:
    count = -(-(deadline.astimezone(UTC) - hour_starts[0]) // ONE_HOUR)
    return count if count <= len(hour_starts) else None


def next_deadline(
    targets: dict[int, tuple[float, time]], first_hour: datetime
) -> tuple[datetime, float] | None:
    """Find the first local deadline after ``first_hour`` and its minimum percent."""
    for offset in range(8):
        day = first_hour.date() + timedelta(days=offset)
        target = targets.get(day.weekday())
        if target is None:
            continue
        percent, deadline = target
        candidate = datetime.combine(day, deadline, tzinfo=first_hour.tzinfo)
        if candidate > first_hour:
            return candidate, percent
    return None


def immediate_charging(ev: EvData, target_kwh: float, hours: int) -> EvSpecification:
    """Charge at full power from now on until the target is stored.

    The last hour is rounded up to the charger minimum, as a wall box would, but
    never beyond the capacity of the car.
    """
    schedule: list[float] = []
    stored_kwh = ev.state_of_charge_kwh
    for _ in range(hours):
        missing_kwh = min(target_kwh, ev.capacity_kwh) - stored_kwh
        wanted_kw = missing_kwh / ev.charge_efficiency
        room_kw = (ev.capacity_kwh - stored_kwh) / ev.charge_efficiency
        power_kw = (
            0.0
            if not ev.plugged_in or missing_kwh <= 1e-9
            else min(
                ev.maximum_charge_kw, max(ev.minimum_charge_kw, wanted_kw), room_kw
            )
        )
        schedule.append(power_kw)
        stored_kwh += power_kw * ev.charge_efficiency
    return EvSpecification(
        capacity_kwh=ev.capacity_kwh,
        initial_soc_kwh=ev.state_of_charge_kwh,
        target_soc_kwh=target_kwh,
        deadline_hours=hours,
        plugged_in=(ev.plugged_in,) * hours,
        minimum_charge_kw=ev.minimum_charge_kw,
        maximum_charge_kw=ev.maximum_charge_kw,
        charge_efficiency=ev.charge_efficiency,
        fixed_charge_kw=tuple(schedule),
    )


def pv_surplus_charging(
    ev: EvData, limit_kwh: float, surplus_kw: tuple[float, ...]
) -> EvSpecification:
    """Charge only from PV that the household does not use, up to the limit."""
    hours = len(surplus_kw)
    return EvSpecification(
        capacity_kwh=ev.capacity_kwh,
        initial_soc_kwh=ev.state_of_charge_kwh,
        target_soc_kwh=0.0,
        deadline_hours=hours,
        plugged_in=(ev.plugged_in,) * hours,
        minimum_charge_kw=ev.minimum_charge_kw,
        maximum_charge_kw=ev.maximum_charge_kw,
        charge_efficiency=ev.charge_efficiency,
        surplus_kw=surplus_kw,
        charge_limit_kwh=min(limit_kwh, ev.capacity_kwh),
        value_eur_per_kwh=SURPLUS_CHARGING_VALUE_EUR_PER_KWH,
    )


def ev_specification(
    ev: EvData,
    charging: EvChargingConfiguration,
    hour_starts: list[datetime],
    time_zone: ZoneInfo,
    surplus_kw: tuple[float, ...],
) -> EvSpecification | None:
    limit_kwh = charging.limit_soc_percent / 100 * ev.capacity_kwh
    if charging.strategy == "surplus":
        return pv_surplus_charging(ev, limit_kwh, surplus_kw)
    if charging.strategy == "immediate":
        return immediate_charging(ev, limit_kwh, len(hour_starts))
    soft_target = charging.strategy == "minimum_plus_target"
    upcoming = next_deadline(
        charging.day_targets(), hour_starts[0].astimezone(time_zone)
    )
    hours_until_deadline = (
        None if upcoming is None else hours_until(upcoming[0], hour_starts)
    )
    if upcoming is None or hours_until_deadline is None:
        logger.warning(
            "event=ev_deadline_beyond_horizon component=optimization operation=plan"
        )
        return None
    return EvSpecification(
        capacity_kwh=ev.capacity_kwh,
        initial_soc_kwh=ev.state_of_charge_kwh,
        target_soc_kwh=upcoming[1] / 100 * ev.capacity_kwh,
        deadline_hours=hours_until_deadline,
        plugged_in=(ev.plugged_in,) * len(hour_starts),
        minimum_charge_kw=ev.minimum_charge_kw,
        maximum_charge_kw=ev.maximum_charge_kw,
        charge_efficiency=ev.charge_efficiency,
        charge_limit_kwh=limit_kwh if soft_target else None,
        value_eur_per_kwh=(charging.value_eur_per_kwh or 0.0) if soft_target else 0.0,
    )


def planning_horizon(
    prices: ElectricityPriceData, pv: PvGenerationData
) -> list[datetime]:
    """Return the hours for which both prices and the PV forecast exist."""
    pv_end = pv.start_time + ONE_HOUR * len(pv.generation_kw)
    return [
        hour_start
        for hour_start in prices.timestamps
        if pv.start_time <= hour_start < pv_end
    ]


class PlanGenerator:
    """Plan callable for the orchestrator: solve the snapshot and persist the plan."""

    def __init__(
        self,
        configuration: Configuration,
        store: ProviderDataStore,
        solver: PlanSolver | None = None,
    ) -> None:
        self.configuration = configuration
        self.store = store
        self.solver = solver or LinopyPlanSolver()

    def __call__(self, snapshot: ProviderDataSnapshot) -> PlanSnapshot:
        problem, forecast = self._build(snapshot)
        plan = self.solver.solve(problem)
        if not plan.has_schedule():
            raise PlanGenerationError(
                f"the solver did not find an optimal plan: {plan.status.value}"
            )
        battery = snapshot.data.get("battery")
        ev = snapshot.data.get("ev")
        stored = PlanSnapshot(
            created_at=snapshot.captured_at,
            start_time=problem.start_time,
            battery_capacity_kwh=(
                battery.capacity_kwh if isinstance(battery, BatteryData) else None
            ),
            plan=plan,
            load_forecast_method=forecast.method,
            load_forecast_mae_kw=forecast.mean_absolute_error_kw,
            ev_capacity_kwh=(
                ev.capacity_kwh if isinstance(ev, EvData) and problem.ev else None
            ),
        )
        self.store.save(PLAN_KEY, PLAN_ADAPTER, stored)
        self._save_ev_schedule(stored)
        logger.info(
            "event=plan_stored component=optimization operation=plan hours=%s "
            "objective_eur=%.4f",
            problem.hours(),
            plan.objective_eur,
        )
        return stored

    def _import_prices(
        self,
        prices: ElectricityPriceData,
        hour_starts: list[datetime],
        prices_at: list[int],
    ) -> tuple[float, ...]:
        market = [prices.import_price_eur_per_kwh[i] for i in prices_at]
        tariff = self.configuration.tariff
        if tariff is None:
            return tuple(market)
        time_zone = ZoneInfo(self.configuration.timezone)
        return tuple(
            tariff.import_price_eur_per_kwh(price, start.astimezone(time_zone).hour)
            for price, start in zip(market, hour_starts, strict=True)
        )

    def _load_forecast(
        self, load: HouseholdLoadData, hour_starts: list[datetime]
    ) -> LoadForecast:
        settings = self.configuration.load_forecast
        try:
            return forecast_load(
                load,
                hour_starts,
                ZoneInfo(self.configuration.timezone),
                settings.method,
                settings.backtest_days,
            )
        except LoadForecastError as error:
            raise PlanGenerationError(str(error)) from error

    def _battery_specification(
        self, battery: BatteryData, import_prices: tuple[float, ...]
    ) -> BatterySpecification:
        return battery_specification(
            battery,
            self._throughput_cost(),
            self._terminal_value(battery, import_prices),
        )

    def _terminal_value(
        self, battery: BatteryData, import_prices: tuple[float, ...]
    ) -> float | None:
        home_assistant = self.configuration.home_assistant
        if home_assistant is None or home_assistant.battery is None:
            return None
        settings = home_assistant.battery
        if settings.end_of_horizon != "value_stored_energy":
            return None
        if settings.end_of_horizon_value_eur_per_kwh is not None:
            return settings.end_of_horizon_value_eur_per_kwh
        mean_import_price = sum(import_prices) / len(import_prices)
        return mean_import_price * math.sqrt(battery.battery_efficiency)

    def _throughput_cost(self) -> float:
        home_assistant = self.configuration.home_assistant
        if home_assistant is None or home_assistant.battery is None:
            return 0.0
        return home_assistant.battery.throughput_cost_eur_per_kwh

    def _save_ev_schedule(self, stored: PlanSnapshot) -> None:
        if stored.ev_capacity_kwh is None:
            return
        hours = [hour for hour in stored.plan.hours if hour.ev is not None]
        schedule = merge_schedule(
            self.store.load(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER),
            stored.created_at,
            tuple(hour.start_time for hour in hours),
            tuple(hour.ev.charge_kw for hour in hours if hour.ev),
        )
        self.store.save(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER, schedule)

    def planning_problem(self, snapshot: ProviderDataSnapshot) -> PlanningProblem:
        return self._build(snapshot)[0]

    def _build(
        self, snapshot: ProviderDataSnapshot
    ) -> tuple[PlanningProblem, LoadForecast]:
        load = _required(snapshot, "household_load", HouseholdLoadData)
        pv = _required(snapshot, "pv_generation", PvGenerationData)
        prices = _required(snapshot, "electricity_prices", ElectricityPriceData)
        battery = snapshot.data.get("battery")
        hour_starts = planning_horizon(prices, pv)
        if not hour_starts:
            raise PlanGenerationError(
                "prices and the PV forecast do not overlap in any future hour"
            )
        price_index = {start: i for i, start in enumerate(prices.timestamps)}
        prices_at = [price_index[start] for start in hour_starts]
        forecast = self._load_forecast(load, hour_starts)
        load_kw = forecast.load_kw
        pv_forecast_kw = tuple(
            pv.generation_kw[int((start - pv.start_time) / ONE_HOUR)]
            for start in hour_starts
        )
        surplus_kw = tuple(
            max(pv_kw - consumed_kw, 0.0)
            for pv_kw, consumed_kw in zip(pv_forecast_kw, load_kw, strict=True)
        )
        import_prices = self._import_prices(prices, hour_starts, prices_at)
        problem = PlanningProblem(
            start_time=hour_starts[0],
            load_kw=load_kw,
            pv_forecast_kw=pv_forecast_kw,
            import_price_eur_per_kwh=import_prices,
            export_price_eur_per_kwh=tuple(
                prices.export_price_eur_per_kwh[i] for i in prices_at
            ),
            maximum_import_kw=self.configuration.grid.maximum_import_kw,
            maximum_export_kw=self.configuration.grid.maximum_export_kw,
            time_limit_seconds=self.configuration.solver.time_limit_seconds,
            battery=(
                self._battery_specification(battery, import_prices)
                if isinstance(battery, BatteryData)
                else None
            ),
            ev=self._ev(snapshot, hour_starts, surplus_kw),
        )
        return problem, forecast

    def _ev(
        self,
        snapshot: ProviderDataSnapshot,
        hour_starts: list[datetime],
        surplus_kw: tuple[float, ...],
    ) -> EvSpecification | None:
        ev = snapshot.data.get("ev")
        charging = self.configuration.ev_charging
        if not isinstance(ev, EvData) or charging is None:
            return None
        return ev_specification(
            ev,
            charging,
            hour_starts,
            ZoneInfo(self.configuration.timezone),
            surplus_kw,
        )


def _required[DataT](
    snapshot: ProviderDataSnapshot, name: str, data_class: type[DataT]
) -> DataT:
    data = snapshot.data.get(name)
    if not isinstance(data, data_class):
        raise PlanGenerationError(f"the snapshot has no usable {name} data")
    return data
