"""Application orchestration for scheduled provider retrieval and planning."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, TypeVar

from pydantic import TypeAdapter

from energy_optimizer.config import (
    Configuration,
    DataSourceScheduleConfiguration,
    OrchestrationConfiguration,
)
from energy_optimizer.providers.awattar import AwattarImporter
from energy_optimizer.providers.forecast_solar import (
    FORECAST_SOLAR_MIN_INTERVAL_SECONDS,
    ForecastSolarImporter,
)
from energy_optimizer.providers.home_assistant import HomeAssistantLoadImporter
from energy_optimizer.providers.home_assistant_battery import (
    HomeAssistantBatteryImporter,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    HomeAssistantBatteryEfficiencyImporter,
    calculate_battery_efficiency,
    merge_battery_efficiency_history,
)
from energy_optimizer.providers.home_assistant_energy import HomeAssistantError
from energy_optimizer.providers.home_assistant_grid_flow import (
    HomeAssistantGridFlowImporter,
)
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    BatteryData,
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

logger = logging.getLogger(__name__)

RunStatus = Literal["success", "failed", "stale", "suspect", "skipped"]
PlanStatus = Literal[
    "disabled",
    "not-ready",
    "unavailable",
    "created",
    "failed",
]


@dataclass(frozen=True)
class ProviderDataSnapshot:
    """The normalized data used for one plan-generation attempt."""

    captured_at: datetime
    data: Mapping[str, object]


PlanGenerator = Callable[[ProviderDataSnapshot], object]
DataT = TypeVar("DataT")


@dataclass(frozen=True)
class ProviderRegistration:
    """Connect a scheduled source to its provider-independent data contract."""

    name: str
    data_type: str
    adapter: TypeAdapter[Any]
    fetch: Callable[[datetime, DataSourceScheduleConfiguration], object | None]
    is_fresh: Callable[[object, datetime], bool]
    load: Callable[[], object | None] | None = None


@dataclass(frozen=True)
class ProviderRun:
    """Outcome of one scheduled provider attempt."""

    source: str
    status: RunStatus
    started_at: datetime
    completed_at: datetime
    error: str | None = None


@dataclass(frozen=True)
class OrchestrationCycle:
    """Observable outcome of one orchestration cycle."""

    started_at: datetime
    completed_at: datetime
    provider_runs: tuple[ProviderRun, ...]
    plan_status: PlanStatus
    plan_error: str | None = None


class OrchestrationError(RuntimeError):
    """Raised when orchestration configuration cannot be composed safely."""


def _make_freshness_checker(
    data_type: type[DataT],
    is_fresh: Callable[..., bool],
) -> Callable[[object, datetime], bool]:
    def check(data: object, now: datetime) -> bool:
        return isinstance(data, data_type) and is_fresh(data, now=now)

    return check


def _make_provider_registration(
    *,
    name: str,
    data_type: str,
    data_class: type[DataT],
    adapter: TypeAdapter[DataT],
    fetch: Callable[[datetime, DataSourceScheduleConfiguration], DataT | None],
    is_fresh: Callable[..., bool],
    load: Callable[[], DataT | None],
) -> ProviderRegistration:
    return ProviderRegistration(
        name=name,
        data_type=data_type,
        adapter=adapter,
        fetch=fetch,
        is_fresh=_make_freshness_checker(data_class, is_fresh),
        load=load,
    )


class ProviderOrchestrator:
    """Run configured providers, persist valid results, and trigger planning.

    ``run_due`` is synchronous so it can be driven by a deterministic clock in
    tests. ``run_forever`` supplies the asynchronous service lifecycle wrapper.
    A cycle never catches up missed intervals: a source that is due is fetched
    once and its next due time starts at the end of that attempt.
    """

    def __init__(
        self,
        configuration: OrchestrationConfiguration,
        registrations: list[ProviderRegistration],
        store: ProviderDataStore,
        plan_generator: PlanGenerator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        registered = {registration.name for registration in registrations}
        configured = set(configuration.sources)
        unknown = configured - registered
        if unknown:
            names = ", ".join(sorted(unknown))
            raise OrchestrationError(
                f"no provider is registered for source(s): {names}"
            )
        missing_required = set(configuration.optimization.required_sources) - registered
        if missing_required:
            names = ", ".join(sorted(missing_required))
            raise OrchestrationError(
                f"no provider is registered for required source(s): {names}"
            )

        self.configuration = configuration
        self.registrations = tuple(registrations)
        self.store = store
        self.plan_generator = plan_generator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._next_due: dict[str, datetime] = {}
        self._latest_data: dict[str, object] = {}
        self._cycle_lock = threading.Lock()
        self._history: deque[OrchestrationCycle] = deque(maxlen=100)
        for registration in self.registrations:
            if registration.load is not None:
                try:
                    data = registration.load()
                except Exception as error:
                    logger.error(
                        "event=orchestration_restore_failed component=orchestration "
                        "operation=restore source=%s error_type=%s error=%s",
                        registration.name,
                        error.__class__.__name__,
                        error,
                        exc_info=True,
                    )
                else:
                    if data is not None:
                        self._latest_data[registration.name] = data
                        logger.info(
                            "event=orchestration_source_restored "
                            "component=orchestration operation=restore source=%s",
                            registration.name,
                        )
        logger.info(
            "event=orchestration_configured component=orchestration operation=startup "
            "source_count=%s optimization_enabled=%s",
            len(self.configuration.sources),
            self.configuration.optimization.enabled,
        )

    @property
    def history(self) -> tuple[OrchestrationCycle, ...]:
        """Return recent cycle outcomes for service diagnostics and tests."""
        return tuple(self._history)

    @property
    def poll_interval_seconds(self) -> float:
        """Return the shortest configured interval for the lifecycle loop."""
        intervals = [
            schedule.interval_seconds
            for name, schedule in self.configuration.sources.items()
            if schedule.enabled
            and name in {registration.name for registration in self.registrations}
        ]
        return min(intervals, default=60.0)

    def run_due(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
    ) -> OrchestrationCycle:
        """Run each source that is due and return its observable outcomes."""
        explicit_time = now is not None
        current_time = now if now is not None else self.clock()
        started_at = self._as_utc(current_time)
        cycle_clock = (lambda: started_at) if explicit_time else self.clock
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning(
                "event=orchestration_cycle_skipped component=orchestration "
                "operation=cycle reason=concurrent_cycle"
            )
            cycle = OrchestrationCycle(
                started_at=started_at,
                completed_at=started_at,
                provider_runs=tuple(
                    ProviderRun(
                        source=name,
                        status="skipped",
                        started_at=started_at,
                        completed_at=started_at,
                        error="another orchestration cycle is still running",
                    )
                    for name in self.configuration.sources
                ),
                plan_status="not-ready",
            )
            self._history.append(cycle)
            return cycle

        try:
            return self._run_due_locked(
                started_at, force=force, cycle_clock=cycle_clock
            )
        finally:
            self._cycle_lock.release()

    def _run_due_locked(
        self,
        now: datetime,
        *,
        force: bool,
        cycle_clock: Callable[[], datetime],
    ) -> OrchestrationCycle:
        provider_runs: list[ProviderRun] = []
        fresh_data: dict[str, object] = {}
        for registration in self.registrations:
            schedule = self.configuration.sources.get(registration.name)
            if schedule is None or not schedule.enabled:
                continue
            if not force and not self._is_due(registration.name, now, schedule):
                logger.warning(
                    "event=provider_refresh_skipped component=orchestration "
                    "operation=refresh source=%s reason=not_due",
                    registration.name,
                )
                provider_runs.append(
                    ProviderRun(
                        source=registration.name,
                        status="skipped",
                        started_at=now,
                        completed_at=now,
                        error="source is not due",
                    )
                )
                continue

            attempt_started = self._as_utc(cycle_clock())
            try:
                data = registration.fetch(now, schedule)
                if data is None:
                    attempt_completed = self._as_utc(cycle_clock())
                    self._set_next_due(registration.name, attempt_completed, schedule)
                    provider_runs.append(
                        ProviderRun(
                            source=registration.name,
                            status="skipped",
                            started_at=attempt_started,
                            completed_at=attempt_completed,
                            error="no missing completed hours",
                        )
                    )
                    logger.info(
                        "event=provider_refresh_skipped component=orchestration "
                        "operation=refresh source=%s reason=no_missing_completed_hours",
                        registration.name,
                    )
                    continue
                saved_data = self.store.save(
                    self._key_for(registration.data_type, data),
                    registration.adapter,
                    data,
                )
                self._latest_data[registration.name] = saved_data
                attempt_completed = self._as_utc(cycle_clock())
                self._set_next_due(registration.name, attempt_completed, schedule)
                status: RunStatus
                if self._has_suspect_quality(saved_data):
                    status = "suspect"
                    error = "provider returned suspect interval data"
                elif registration.is_fresh(saved_data, now):
                    fresh_data[registration.name] = saved_data
                    status = "success"
                    error = None
                else:
                    status = "stale"
                    error = "provider returned data outside its freshness threshold"
                provider_runs.append(
                    ProviderRun(
                        source=registration.name,
                        status=status,
                        started_at=attempt_started,
                        completed_at=attempt_completed,
                        error=error,
                    )
                )
                log_method = (
                    logger.warning if status in {"stale", "suspect"} else logger.info
                )
                log_method(
                    "event=provider_refresh_completed component=orchestration "
                    "operation=refresh source=%s status=%s duration_seconds=%.3f",
                    registration.name,
                    status,
                    (attempt_completed - attempt_started).total_seconds(),
                )
            except Exception as error:
                attempt_completed = self._as_utc(cycle_clock())
                self._set_next_due(registration.name, attempt_completed, schedule)
                message = str(error) or error.__class__.__name__
                provider_runs.append(
                    ProviderRun(
                        source=registration.name,
                        status="failed",
                        started_at=attempt_started,
                        completed_at=attempt_completed,
                        error=message,
                    )
                )
                logger.error(
                    "event=provider_refresh_failed component=orchestration "
                    "operation=refresh source=%s error_type=%s error=%s",
                    registration.name,
                    error.__class__.__name__,
                    message,
                    exc_info=(None if isinstance(error, HomeAssistantError) else True),
                )

        blocked_sources = {
            run.source for run in provider_runs if run.status in {"failed", "stale"}
        }
        plan_status, plan_error = self._trigger_plan(now, fresh_data, blocked_sources)
        completed_at = self._as_utc(cycle_clock())
        cycle = OrchestrationCycle(
            started_at=now,
            completed_at=completed_at,
            provider_runs=tuple(provider_runs),
            plan_status=plan_status,
            plan_error=plan_error,
        )
        self._history.append(cycle)
        logger.info(
            "event=orchestration_cycle_completed component=orchestration "
            "operation=cycle provider_run_count=%s plan_status=%s "
            "duration_seconds=%.3f",
            len(provider_runs),
            plan_status,
            (completed_at - now).total_seconds(),
        )
        return cycle

    def _trigger_plan(
        self,
        captured_at: datetime,
        fresh_data: Mapping[str, object],
        blocked_sources: set[str],
    ) -> tuple[PlanStatus, str | None]:
        optimization = self.configuration.optimization
        if not optimization.enabled:
            return "disabled", None
        if not fresh_data:
            logger.warning(
                "event=optimization_skipped component=orchestration operation=plan "
                "reason=no_refreshed_provider_data"
            )
            return "not-ready", "no provider data was refreshed in this cycle"
        latest_data = dict(self._latest_data)
        latest_data.update(fresh_data)
        required_sources = optimization.required_sources
        required = set(required_sources)
        if required & blocked_sources:
            logger.warning(
                "event=optimization_skipped component=orchestration operation=plan "
                "reason=required_source_blocked source_count=%s",
                len(required & blocked_sources),
            )
            return (
                "not-ready",
                "a required source failed, returned stale data, or contains "
                "suspect data",
            )
        if not required.issubset(fresh_data):
            logger.warning(
                "event=optimization_skipped component=orchestration operation=plan "
                "reason=required_source_not_refreshed"
            )
            return "not-ready", "not all required sources refreshed successfully"
        if self.plan_generator is None:
            logger.warning(
                "event=optimization_unavailable component=orchestration operation=plan "
                "reason=generator_not_configured"
            )
            return "unavailable", "optimization plan generator is not configured"

        stale_sources = [
            source
            for source in required
            if not self._is_latest_data_fresh(source, latest_data[source], captured_at)
        ]
        if stale_sources:
            logger.warning(
                "event=optimization_skipped component=orchestration operation=plan "
                "reason=required_source_stale source_count=%s",
                len(stale_sources),
            )
            return "not-ready", "required source data is stale"

        snapshot = ProviderDataSnapshot(
            captured_at=captured_at,
            data={source: latest_data[source] for source in required_sources},
        )
        try:
            self.plan_generator(snapshot)
        except Exception as error:
            message = str(error) or error.__class__.__name__
            logger.error(
                "event=optimization_plan_failed component=orchestration "
                "operation=plan error_type=%s error=%s",
                error.__class__.__name__,
                message,
                exc_info=True,
            )
            return "failed", message
        logger.info(
            "event=optimization_plan_created component=orchestration operation=plan "
            "source_count=%s",
            len(required_sources),
        )
        return "created", None

    def _is_latest_data_fresh(
        self,
        source: str,
        data: object,
        now: datetime,
    ) -> bool:
        registration = next(
            registration
            for registration in self.registrations
            if registration.name == source
        )
        return registration.is_fresh(data, now)

    @staticmethod
    def _has_suspect_quality(data: object) -> bool:
        """Return whether normalized provider data contains suspect intervals."""
        quality = getattr(data, "quality", ())
        return any(getattr(item, "status", None) == "suspect" for item in quality)

    def _is_due(
        self,
        name: str,
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> bool:
        next_due = self._next_due.get(name)
        if next_due is None:
            if self.configuration.startup_fetch:
                return True
            self._next_due[name] = now + timedelta(seconds=schedule.interval_seconds)
            return False
        return now >= next_due

    def _set_next_due(
        self,
        name: str,
        completed_at: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> None:
        self._next_due[name] = completed_at + timedelta(
            seconds=schedule.interval_seconds
        )

    @staticmethod
    def _key_for(data_type: str, data: object) -> ProviderDataKey:
        source = getattr(data, "source", None)
        if not isinstance(source, SourceMetadata):
            raise OrchestrationError(
                "normalized provider data must expose SourceMetadata as source"
            )
        return ProviderDataKey(
            data_type=data_type,
            provider=source.provider,
            entity_id=source.entity_id,
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run scheduled collection until the application requests shutdown."""
        logger.info(
            "event=orchestration_started component=orchestration operation=run_forever"
        )
        while not stop_event.is_set():
            await asyncio.to_thread(self.run_due)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.poll_interval_seconds
                )
            except TimeoutError:
                pass
        logger.info(
            "event=orchestration_stopped component=orchestration operation=run_forever"
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise OrchestrationError("orchestration times must include a timezone")
        return value.astimezone(timezone.utc)


ConfiguredRegistrationFactory = Callable[
    [Configuration, OrchestrationConfiguration, ProviderDataStore],
    ProviderRegistration | None,
]


def _build_household_load_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.household_load_entities is None
        or "household_load" not in orchestration.sources
    ):
        return None

    importer = HomeAssistantLoadImporter(home_assistant)
    adapter = TypeAdapter(HouseholdLoadData)
    key = ProviderDataKey(
        data_type="household-load",
        provider="home-assistant",
        entity_id=home_assistant.household_load_source_id,
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> HouseholdLoadData | None:
        end_time = now.replace(minute=0, second=0, microsecond=0)
        persisted = store.load(key, adapter)
        if persisted is None:
            start_time = end_time - timedelta(hours=HOUSEHOLD_LOAD_MAX_VALUES)
        else:
            start_time = persisted.start_time + timedelta(hours=len(persisted.load_kw))
        if start_time >= end_time:
            return None
        return importer.fetch(
            start_time,
            end_time,
            schedule.history_lookback_seconds,
            now=now,
        )

    def load() -> HouseholdLoadData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="household_load",
        data_type="household-load",
        data_class=HouseholdLoadData,
        adapter=adapter,
        fetch=fetch,
        is_fresh=importer.is_fresh,
        load=load,
    )


def _build_pv_generation_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    forecast_solar = configuration.forecast_solar
    if forecast_solar is None or "pv_generation" not in orchestration.sources:
        return None

    schedule = orchestration.sources["pv_generation"]
    if (
        schedule.enabled
        and schedule.interval_seconds < FORECAST_SOLAR_MIN_INTERVAL_SECONDS
    ):
        raise OrchestrationError(
            "pv_generation polling interval must be at least "
            f"{FORECAST_SOLAR_MIN_INTERVAL_SECONDS} seconds for the "
            "Forecast.Solar public rate limit"
        )
    importer = ForecastSolarImporter(forecast_solar)
    adapter = TypeAdapter(PvGenerationData)
    key = ProviderDataKey(
        data_type="pv-generation",
        provider="forecast.solar",
        entity_id=forecast_solar.pv_generation_source_id,
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> PvGenerationData:
        del schedule
        start_time = now.replace(minute=0, second=0, microsecond=0)
        return importer.fetch(start_time, now=now)

    def load() -> PvGenerationData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="pv_generation",
        data_type="pv-generation",
        data_class=PvGenerationData,
        adapter=adapter,
        fetch=fetch,
        is_fresh=importer.is_fresh,
        load=load,
    )


def _build_electricity_prices_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    awattar = configuration.awattar
    if awattar is None or "electricity_prices" not in orchestration.sources:
        return None

    importer = AwattarImporter(awattar)
    adapter = TypeAdapter(ElectricityPriceData)
    key = ProviderDataKey(
        data_type="electricity-prices",
        provider="awattar.de",
        entity_id=awattar.electricity_price_source_id,
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> ElectricityPriceData:
        del schedule
        start_time = now.replace(minute=0, second=0, microsecond=0)
        return importer.fetch(start_time, now=now)

    def load() -> ElectricityPriceData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="electricity_prices",
        data_type="electricity-prices",
        data_class=ElectricityPriceData,
        adapter=adapter,
        fetch=fetch,
        is_fresh=importer.is_fresh,
        load=load,
    )


def _build_grid_flow_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.grid_import_entities is None
        or home_assistant.grid_export_entities is None
        or "grid_flow" not in orchestration.sources
    ):
        return None

    importer = HomeAssistantGridFlowImporter(home_assistant)
    adapter = TypeAdapter(GridFlowData)
    key = ProviderDataKey(
        data_type="grid-flow",
        provider="home-assistant",
        entity_id=home_assistant.grid_flow_source_id,
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> GridFlowData:
        end_time = now.replace(minute=0, second=0, microsecond=0)
        start_time = end_time - timedelta(hours=1)
        return importer.fetch(
            start_time,
            end_time,
            schedule.history_lookback_seconds,
            now=now,
        )

    def load() -> GridFlowData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="grid_flow",
        data_type="grid-flow",
        data_class=GridFlowData,
        adapter=adapter,
        fetch=fetch,
        is_fresh=importer.is_fresh,
        load=load,
    )


def _build_battery_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.battery is None
        or "battery" not in orchestration.sources
    ):
        return None

    importer = HomeAssistantBatteryImporter(home_assistant)
    adapter = TypeAdapter(BatteryData)
    key = ProviderDataKey(
        data_type="battery",
        provider="home-assistant",
        entity_id=home_assistant.battery_source_id,
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> BatteryData:
        del schedule
        efficiency_data = store.load(
            ProviderDataKey(
                data_type="battery-efficiency",
                provider="home-assistant",
                entity_id="battery_efficiency",
            ),
            TypeAdapter(BatteryEfficiencyData),
        )
        if efficiency_data is None:
            return importer.fetch(now=now)
        return importer.fetch(now=now, efficiency_data=efficiency_data)

    def load() -> BatteryData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="battery",
        data_type="battery",
        data_class=BatteryData,
        adapter=adapter,
        fetch=fetch,
        is_fresh=importer.is_fresh,
        load=load,
    )


def _build_battery_efficiency_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    battery = home_assistant.battery if home_assistant is not None else None
    calculation = battery.efficiency_calculation if battery is not None else None
    if (
        home_assistant is None
        or calculation is None
        or "battery_efficiency" not in orchestration.sources
    ):
        return None

    importer = HomeAssistantBatteryEfficiencyImporter(home_assistant)
    history_adapter = TypeAdapter(BatteryEfficiencyHistoryData)
    result_adapter = TypeAdapter(BatteryEfficiencyData)
    history_key = ProviderDataKey(
        data_type="battery-efficiency-history",
        provider="home-assistant",
        entity_id="battery_efficiency_history",
    )
    result_key = ProviderDataKey(
        data_type="battery-efficiency",
        provider="home-assistant",
        entity_id="battery_efficiency",
    )

    def fetch(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> BatteryEfficiencyData:
        del schedule
        end_time = now.replace(minute=0, second=0, microsecond=0)
        persisted = store.load(history_key, history_adapter)
        if persisted is not None:
            start_time = persisted.start_time + timedelta(
                hours=len(persisted.battery_energy_in_kwh)
            )
        elif calculation.history_start is not None:
            start_time = calculation.history_start
        else:
            start_time = end_time - timedelta(hours=HOUSEHOLD_LOAD_MAX_VALUES)

        if persisted is not None and start_time >= end_time:
            history = persisted
        else:
            incoming = importer.fetch(start_time, end_time, now=now)
            history = merge_battery_efficiency_history(persisted, incoming)
            store.save(history_key, history_adapter, history)
        capacity: float | None = None
        battery_data = store.load(
            ProviderDataKey("battery", "home-assistant", "battery"),
            TypeAdapter(BatteryData),
        )
        if battery_data is not None:
            capacity = battery_data.capacity_kwh
        elif battery is not None and hasattr(battery.capacity, "value"):
            capacity = float(battery.capacity.value)
            if battery.capacity.unit == "Wh":
                capacity /= 1000
        result = calculate_battery_efficiency(
            history,
            calculation,
            capacity_kwh=capacity,
            now=now,
        )
        store.save(result_key, result_adapter, result)
        return result

    def load() -> BatteryEfficiencyData | None:
        return store.load(result_key, result_adapter)

    schedule_interval_seconds = orchestration.sources[
        "battery_efficiency"
    ].interval_seconds

    return _make_provider_registration(
        name="battery_efficiency",
        data_type="battery-efficiency",
        data_class=BatteryEfficiencyData,
        adapter=result_adapter,
        fetch=fetch,
        is_fresh=lambda data, now=None: _efficiency_history_is_fresh(
            store, history_key, history_adapter, schedule_interval_seconds, now
        ),
        load=load,
    )


def _efficiency_history_is_fresh(
    store: ProviderDataStore,
    key: ProviderDataKey,
    adapter: TypeAdapter[BatteryEfficiencyHistoryData],
    interval_seconds: float,
    now: datetime | None,
) -> bool:
    """Check freshness against the calculated source's own recompute interval.

    The shared Home Assistant ``max_data_age_seconds`` threshold is meant for
    live entity polling and is typically much shorter than the daily
    recompute interval used here, which would otherwise report this source
    as stale for most of every day even when it is working correctly.
    """
    history = store.load(key, adapter)
    if history is None:
        return False
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - history.latest_observation_at).total_seconds()
    return age_seconds <= interval_seconds * 2


_REGISTRATION_FACTORIES: tuple[ConfiguredRegistrationFactory, ...] = (
    _build_household_load_registration,
    _build_pv_generation_registration,
    _build_electricity_prices_registration,
    _build_grid_flow_registration,
    _build_battery_registration,
    _build_battery_efficiency_registration,
)


def build_configured_orchestrator(
    configuration: Configuration,
    store: ProviderDataStore | None,
    plan_generator: PlanGenerator | None = None,
) -> ProviderOrchestrator | None:
    """Compose currently configured concrete providers into the orchestrator."""
    orchestration = configuration.orchestration
    if orchestration is None or not orchestration.enabled:
        logger.debug(
            "event=orchestration_disabled component=orchestration operation=compose"
        )
        return None
    if store is None:
        raise OrchestrationError(
            "a provider-data store is required when orchestration is enabled"
        )

    registrations: list[ProviderRegistration] = []
    for factory in _REGISTRATION_FACTORIES:
        registration = factory(configuration, orchestration, store)
        if registration is not None:
            registrations.append(registration)

    orchestrator = ProviderOrchestrator(
        configuration=orchestration,
        registrations=registrations,
        store=store,
        plan_generator=plan_generator,
    )
    logger.info(
        "event=orchestration_composed component=orchestration operation=compose "
        "registration_count=%s",
        len(registrations),
    )
    return orchestrator
