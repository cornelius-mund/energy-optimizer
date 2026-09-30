"""Application orchestration for scheduled provider retrieval and planning."""

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Callable, Literal, Mapping, TypeVar

import httpx
from pydantic import TypeAdapter

from energy_optimizer.config import (
    Configuration,
    DataSourceScheduleConfiguration,
    OrchestrationConfiguration,
)
from energy_optimizer.exclusions import exclusion_summary
from energy_optimizer.heat_pump import HeatPumpLoad, HeatPumpSource
from energy_optimizer.history_merge import HISTORY_RETENTION_HOURS, merge_price_history
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
from energy_optimizer.providers.home_assistant_grid_flow import (
    HomeAssistantGridFlowImporter,
)
from energy_optimizer.providers.home_assistant_heat_pump import (
    HomeAssistantHeatPumpImporter,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantError,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from energy_optimizer.providers.interfaces import (
    BATTERY_EFFICIENCY_SOURCE_ID,
    BATTERY_SOURCE_ID,
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
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

logger = logging.getLogger(__name__)

RunStatus = Literal["success", "failed", "stale", "skipped"]
PlanStatus = Literal["disabled", "not-ready", "unavailable", "created", "failed"]


@dataclass(frozen=True)
class ProviderDataSnapshot:
    """The normalized data used for one plan-generation attempt."""

    captured_at: datetime
    data: Mapping[str, object]


PlanGenerator = Callable[[ProviderDataSnapshot], object]
ProviderPlan = Callable[
    [datetime, DataSourceScheduleConfiguration], HistoryPlan[Any] | None
]
DataT = TypeVar("DataT")


@dataclass(frozen=True)
class ProviderRegistration:
    """Connect a scheduled source to its provider-independent data contract.

    ``plan`` runs for a due source before anything is imported. It returns the
    Home Assistant history the source needs and how to build its record from
    that history, or ``None`` when there is nothing to fetch, for example
    because every completed hour is already persisted.
    """

    name: str
    data_type: str
    adapter: TypeAdapter[Any]
    plan: ProviderPlan
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


@dataclass(frozen=True)
class _PlannedSource:
    """A due source whose plan is ready for the import and build phases."""

    registration: ProviderRegistration
    schedule: DataSourceScheduleConfiguration
    started_at: datetime
    plan: HistoryPlan[Any]


class ProviderOrchestrator:
    """Run configured providers, persist valid results, and trigger planning.

    ``run_due`` is synchronous so it can be driven by a deterministic clock in
    tests. ``run_forever`` supplies the asynchronous service lifecycle wrapper.
    A cycle never catches up missed intervals: a source that is due is fetched
    once and its next due time starts at the end of that attempt.

    A cycle has three phases. Every due source plans the Home Assistant history
    it needs, the needs of all sources are imported once per distinct entity, and
    each source then builds and persists its record from that shared history.
    """

    def __init__(
        self,
        configuration: OrchestrationConfiguration,
        registrations: list[ProviderRegistration],
        store: ProviderDataStore,
        plan_generator: PlanGenerator | None = None,
        clock: Callable[[], datetime] | None = None,
        history_importer: HomeAssistantHistoryImporter | None = None,
    ) -> None:
        registered = {registration.name for registration in registrations}
        for kind, names in (
            ("source(s)", configuration.sources),
            ("required source(s)", configuration.optimization.required_sources),
        ):
            unregistered = sorted(set(names) - registered)
            if unregistered:
                raise OrchestrationError(
                    f"no provider is registered for {kind}: {', '.join(unregistered)}"
                )

        self.configuration = configuration
        self.registrations = tuple(registrations)
        self.store = store
        self.plan_generator = plan_generator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.history_importer = history_importer
        self._next_due: dict[str, datetime] = {}
        self._latest_data: dict[str, object] = {}
        self._cycle_lock = threading.Lock()
        for registration in self.registrations:
            if registration.load is None:
                continue
            restore_started_at = perf_counter()
            logger.info(
                "event=orchestration_restore_started component=orchestration "
                "operation=restore source=%s",
                registration.name,
            )
            try:
                data = registration.load()
            except Exception as error:
                logger.error(
                    "event=orchestration_restore_failed component=orchestration "
                    "operation=restore source=%s error_type=%s error=%s "
                    "duration_seconds=%.3f",
                    registration.name,
                    error.__class__.__name__,
                    error,
                    perf_counter() - restore_started_at,
                    exc_info=True,
                )
                continue
            if data is not None:
                self._latest_data[registration.name] = data
            logger.info(
                "event=orchestration_restore_completed component=orchestration "
                "operation=restore source=%s status=%s duration_seconds=%.3f",
                registration.name,
                "restored" if data is not None else "empty",
                perf_counter() - restore_started_at,
            )
        logger.info(
            "event=orchestration_configured component=orchestration operation=startup "
            "source_count=%s optimization_enabled=%s",
            len(self.configuration.sources),
            self.configuration.optimization.enabled,
        )

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
        self, now: datetime | None = None, *, force: bool = False
    ) -> OrchestrationCycle:
        """Run each source that is due and return its observable outcomes."""
        started_at = self._as_utc(self.clock() if now is None else now)
        cycle_clock = self.clock if now is None else (lambda: started_at)
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning(
                "event=orchestration_cycle_skipped component=orchestration "
                "operation=cycle reason=concurrent_cycle"
            )
            return OrchestrationCycle(
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

        try:
            return self._run_due_locked(
                started_at, force=force, cycle_clock=cycle_clock
            )
        finally:
            self._cycle_lock.release()

    def _run_due_locked(
        self, now: datetime, *, force: bool, cycle_clock: Callable[[], datetime]
    ) -> OrchestrationCycle:
        provider_runs: list[ProviderRun] = []
        fresh_data: dict[str, object] = {}

        # Phase 1: every due source declares the history it needs and how to
        # build its record. Nothing is imported yet.
        slots: list[ProviderRun | _PlannedSource] = []
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
                slots.append(
                    ProviderRun(
                        registration.name, "skipped", now, now, "source is not due"
                    )
                )
                continue

            attempt_started = self._as_utc(cycle_clock())
            try:
                plan = registration.plan(now, schedule)
            except Exception as error:
                slots.append(
                    self._failed_run(
                        registration, schedule, attempt_started, cycle_clock, error
                    )
                )
                continue
            if plan is None:
                attempt_completed = self._as_utc(cycle_clock())
                self._set_next_due(registration.name, attempt_completed, schedule)
                slots.append(
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
            slots.append(_PlannedSource(registration, schedule, attempt_started, plan))

        # Phase 2: import every distinct entity that any source needs, once.
        planned = [slot for slot in slots if isinstance(slot, _PlannedSource)]
        history, import_error = self._import_history(planned)

        # Phase 3: build and persist each record from the shared history.
        for slot in slots:
            if isinstance(slot, ProviderRun):
                provider_runs.append(slot)
                continue
            provider_runs.append(
                self._build_and_persist(
                    slot,
                    history,
                    import_error if slot.plan.needs else None,
                    now,
                    cycle_clock,
                    fresh_data,
                )
            )

        blocked_sources = {
            run.source for run in provider_runs if run.status in {"failed", "stale"}
        }
        plan_status, plan_error = self._trigger_plan(now, fresh_data, blocked_sources)
        completed_at = self._as_utc(cycle_clock())
        logger.info(
            "event=orchestration_cycle_completed component=orchestration "
            "operation=cycle provider_run_count=%s plan_status=%s "
            "duration_seconds=%.3f",
            len(provider_runs),
            plan_status,
            (completed_at - now).total_seconds(),
        )
        return OrchestrationCycle(
            started_at=now,
            completed_at=completed_at,
            provider_runs=tuple(provider_runs),
            plan_status=plan_status,
            plan_error=plan_error,
        )

    def _import_history(
        self, planned: list[_PlannedSource]
    ) -> tuple[HomeAssistantHistory, Exception | None]:
        """Import the history of all planned sources; never request without needs.

        A failure of a single entity is recorded inside the returned history and
        reaches only the sources that read it. An error that prevents the whole
        import, such as inconsistent plans, is returned so that every source with
        Home Assistant needs fails with it.
        """
        needs = [need for source in planned for need in source.plan.needs]
        if not needs:
            return HomeAssistantHistory(), None
        try:
            if self.history_importer is None:
                raise OrchestrationError(
                    "a Home Assistant history importer is required to import "
                    "planned history needs"
                )
            return self.history_importer.import_history(needs), None
        except Exception as error:
            logger.error(
                "event=history_import_failed component=orchestration "
                "operation=import error_type=%s error=%s",
                error.__class__.__name__,
                error,
                exc_info=(None if isinstance(error, HomeAssistantError) else True),
            )
            return HomeAssistantHistory(), error

    def _build_and_persist(
        self,
        source: _PlannedSource,
        history: HomeAssistantHistory,
        import_error: Exception | None,
        now: datetime,
        cycle_clock: Callable[[], datetime],
        fresh_data: dict[str, object],
    ) -> ProviderRun:
        """Build one planned source's record, persist it, and report its status."""
        registration = source.registration
        try:
            if import_error is not None:
                raise import_error
            data = source.plan.build(history)
            saved_data = self.store.save(
                self._key_for(registration.data_type, data),
                registration.adapter,
                data,
            )
            self._latest_data[registration.name] = saved_data
            attempt_completed = self._as_utc(cycle_clock())
            self._set_next_due(registration.name, attempt_completed, source.schedule)
            _log_excluded_hours(registration.name, getattr(data, "exclusions", ()))
            is_fresh = registration.is_fresh(saved_data, now)
            if is_fresh:
                fresh_data[registration.name] = saved_data
            status: RunStatus = "success" if is_fresh else "stale"
            logger.log(
                logging.INFO if is_fresh else logging.WARNING,
                "event=provider_refresh_completed component=orchestration "
                "operation=refresh source=%s status=%s duration_seconds=%.3f",
                registration.name,
                status,
                (attempt_completed - source.started_at).total_seconds(),
            )
            error_message = (
                None
                if is_fresh
                else "provider returned data outside its freshness threshold"
            )
            return ProviderRun(
                source=registration.name,
                status=status,
                started_at=source.started_at,
                completed_at=attempt_completed,
                error=error_message,
            )
        except Exception as error:
            return self._failed_run(
                registration, source.schedule, source.started_at, cycle_clock, error
            )

    def _failed_run(
        self,
        registration: ProviderRegistration,
        schedule: DataSourceScheduleConfiguration,
        attempt_started: datetime,
        cycle_clock: Callable[[], datetime],
        error: Exception,
    ) -> ProviderRun:
        """Record a failed attempt; the last valid persisted data stays in place."""
        attempt_completed = self._as_utc(cycle_clock())
        self._set_next_due(registration.name, attempt_completed, schedule)
        message = str(error) or error.__class__.__name__
        logger.error(
            "event=provider_refresh_failed component=orchestration "
            "operation=refresh source=%s error_type=%s error=%s",
            registration.name,
            error.__class__.__name__,
            message,
            exc_info=(None if isinstance(error, HomeAssistantError) else True),
        )
        return ProviderRun(
            registration.name, "failed", attempt_started, attempt_completed, message
        )

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
        latest_data = {**self._latest_data, **fresh_data}
        required_sources = optimization.required_sources
        required = set(required_sources)
        if required & blocked_sources:
            logger.warning(
                "event=optimization_skipped component=orchestration operation=plan "
                "reason=required_source_blocked source_count=%s",
                len(required & blocked_sources),
            )
            return "not-ready", "a required source failed or returned stale data"
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

    def _is_latest_data_fresh(self, source: str, data: object, now: datetime) -> bool:
        registration = next(r for r in self.registrations if r.name == source)
        return registration.is_fresh(data, now)

    def _is_due(
        self, name: str, now: datetime, schedule: DataSourceScheduleConfiguration
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
        if not isinstance(source, (SourceMetadata, HeatPumpSource)):
            raise OrchestrationError(
                "normalized provider data must expose SourceMetadata as source"
            )
        return ProviderDataKey(data_type, source.provider, source.entity_id)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run scheduled collection until the application requests shutdown."""
        logger.info(
            "event=orchestration_started component=orchestration operation=run_forever"
        )
        while not stop_event.is_set():
            await asyncio.to_thread(self.run_due)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.poll_interval_seconds
                )
        logger.info(
            "event=orchestration_stopped component=orchestration operation=run_forever"
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise OrchestrationError("orchestration times must include a timezone")
        return value.astimezone(timezone.utc)


def _log_excluded_hours(source: str, exclusions: object) -> None:
    """Report the hours a refresh excluded, once per source, counted by reason."""
    if not isinstance(exclusions, tuple) or not exclusions:
        return
    summary = exclusion_summary(exclusions)
    logger.warning(
        "event=provider_hours_excluded component=orchestration operation=refresh "
        "source=%s excluded_hour_count=%s reasons=%s first_hour=%s last_hour=%s",
        source,
        len(exclusions),
        ",".join(f"{reason}:{count}" for reason, count in summary.items()),
        exclusions[0].hour_start.isoformat(),
        exclusions[-1].hour_start.isoformat(),
    )


_Sources = dict[str, DataSourceScheduleConfiguration]
_BATTERY_KEY = ProviderDataKey("battery", "home-assistant", BATTERY_SOURCE_ID)
_BATTERY_ADAPTER = TypeAdapter(BatteryData)
_BATTERY_EFFICIENCY_KEY = ProviderDataKey(
    "battery-efficiency", "home-assistant", BATTERY_EFFICIENCY_SOURCE_ID
)
_BATTERY_EFFICIENCY_ADAPTER = TypeAdapter(BatteryEfficiencyData)


def _registration(
    name: str,
    data_class: type[DataT],
    adapter: TypeAdapter[DataT],
    key: ProviderDataKey,
    store: ProviderDataStore,
    plan: ProviderPlan,
    is_fresh: Callable[..., bool],
) -> ProviderRegistration:
    """Register a source that persists and restores its record under ``key``."""

    def check(data: object, now: datetime) -> bool:
        return isinstance(data, data_class) and is_fresh(data, now=now)

    return ProviderRegistration(
        name=name,
        data_type=key.data_type,
        adapter=adapter,
        plan=plan,
        is_fresh=check,
        load=lambda: store.load(key, adapter),
    )


def _fetch_plan(fetch: Callable[[datetime], object]) -> ProviderPlan:
    """Plan a source that needs no Home Assistant history and fetches by itself."""
    return lambda now, schedule: HistoryPlan(needs=(), build=lambda history: fetch(now))


def _build_household_load_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.household_load is None
        or "household_load" not in sources
    ):
        return None

    importer = HomeAssistantLoadImporter(home_assistant)
    adapter = TypeAdapter(HouseholdLoadData)
    key = ProviderDataKey(
        "household-load", "home-assistant", home_assistant.household_load_source_id
    )

    def plan(
        now: datetime, schedule: DataSourceScheduleConfiguration
    ) -> HistoryPlan[HouseholdLoadData] | None:
        end_time = now.replace(minute=0, second=0, microsecond=0)
        persisted = store.load(key, adapter)
        if persisted is None:
            start_time = end_time - timedelta(hours=HOUSEHOLD_LOAD_MAX_VALUES)
        else:
            start_time = persisted.start_time + timedelta(hours=len(persisted.load_kw))
        if start_time >= end_time:
            return None
        return importer.plan(
            start_time, end_time, schedule.history_lookback_seconds, now=now
        )

    return _registration(
        "household_load",
        HouseholdLoadData,
        adapter,
        key,
        store,
        plan,
        importer.is_fresh,
    )


def _build_pv_generation_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    forecast_solar = configuration.forecast_solar
    if forecast_solar is None or "pv_generation" not in sources:
        return None

    schedule = sources["pv_generation"]
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
    key = ProviderDataKey(
        "pv-generation", "forecast.solar", forecast_solar.pv_generation_source_id
    )

    def fetch(now: datetime) -> PvGenerationData:
        start_time = now.replace(minute=0, second=0, microsecond=0)
        return importer.fetch(start_time, now=now)

    return _registration(
        "pv_generation",
        PvGenerationData,
        TypeAdapter(PvGenerationData),
        key,
        store,
        _fetch_plan(fetch),
        importer.is_fresh,
    )


def _build_electricity_prices_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    awattar = configuration.awattar
    if awattar is None or "electricity_prices" not in sources:
        return None

    importer = AwattarImporter(awattar)
    adapter = TypeAdapter(ElectricityPriceData)
    source_id = awattar.electricity_price_source_id
    key = ProviderDataKey("electricity-prices", "awattar.de", source_id)
    history_key = ProviderDataKey("electricity-price-history", "awattar.de", source_id)

    def fetch(now: datetime) -> ElectricityPriceData:
        start_time = now.replace(minute=0, second=0, microsecond=0)
        data = importer.fetch(start_time, now=now)
        # The forecast record is replaced by every run, so hours that have since
        # elapsed would be lost. Retain them separately as historic prices. A
        # failing history must never block the planning inputs, so it is only
        # reported.
        try:
            store.save(
                history_key,
                adapter,
                merge_price_history(store.load(history_key, adapter), data),
            )
        except ProviderDataStoreError as error:
            logger.error(
                "event=price_history_persistence_failed component=orchestration "
                "operation=refresh source=electricity_prices error_type=%s error=%s",
                error.__class__.__name__,
                error,
            )
        return data

    return _registration(
        "electricity_prices",
        ElectricityPriceData,
        adapter,
        key,
        store,
        _fetch_plan(fetch),
        importer.is_fresh,
    )


def _build_grid_flow_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.grid_import is None
        or home_assistant.grid_export is None
        or "grid_flow" not in sources
    ):
        return None

    importer = HomeAssistantGridFlowImporter(home_assistant)
    adapter = TypeAdapter(GridFlowData)
    key = ProviderDataKey(
        "grid-flow", "home-assistant", home_assistant.grid_flow_source_id
    )

    def plan(
        now: datetime, schedule: DataSourceScheduleConfiguration
    ) -> HistoryPlan[GridFlowData] | None:
        end_time = now.replace(minute=0, second=0, microsecond=0)
        persisted = store.load(key, adapter)
        if persisted is None:
            start_time = end_time - timedelta(hours=HISTORY_RETENTION_HOURS)
        else:
            start_time = persisted.start_time + timedelta(
                hours=len(persisted.import_kw)
            )
        if start_time >= end_time:
            return None
        return importer.plan(
            start_time, end_time, schedule.history_lookback_seconds, now=now
        )

    return _registration(
        "grid_flow", GridFlowData, adapter, key, store, plan, importer.is_fresh
    )


def _build_battery_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.battery is None
        or "battery" not in sources
    ):
        return None

    importer = HomeAssistantBatteryImporter(home_assistant)

    def fetch(now: datetime) -> BatteryData:
        efficiency_data = store.load(
            _BATTERY_EFFICIENCY_KEY, _BATTERY_EFFICIENCY_ADAPTER
        )
        if efficiency_data is None:
            return importer.fetch(now=now)
        return importer.fetch(now=now, efficiency_data=efficiency_data)

    return _registration(
        "battery",
        BatteryData,
        _BATTERY_ADAPTER,
        _BATTERY_KEY,
        store,
        _fetch_plan(fetch),
        importer.is_fresh,
    )


def _build_battery_efficiency_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if home_assistant is None or home_assistant.battery is None:
        return None
    battery = home_assistant.battery
    calculation = battery.efficiency_calculation
    if calculation is None or "battery_efficiency" not in sources:
        return None

    importer = HomeAssistantBatteryEfficiencyImporter(home_assistant)
    history_adapter = TypeAdapter(BatteryEfficiencyHistoryData)
    history_key = ProviderDataKey(
        "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
    )
    interval_seconds = sources["battery_efficiency"].interval_seconds

    def plan(
        now: datetime, _: DataSourceScheduleConfiguration
    ) -> HistoryPlan[BatteryEfficiencyData]:
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

        # Without missing completed hours the persisted history is recalculated
        # without any Home Assistant request.
        history_plan = (
            None
            if persisted is not None and start_time >= end_time
            else importer.plan(start_time, end_time, now=now)
        )

        def build(imported: HomeAssistantHistory) -> BatteryEfficiencyData:
            if history_plan is None:
                assert persisted is not None
                history = persisted
            else:
                incoming = history_plan.build(imported)
                _log_excluded_hours(BATTERY_EFFICIENCY_SOURCE_ID, incoming.exclusions)
                history = merge_battery_efficiency_history(persisted, incoming)
                store.save(history_key, history_adapter, history)
            # Read at build time so that a battery record persisted earlier in
            # the same cycle supplies the capacity.
            capacity: float | None = None
            battery_data = store.load(_BATTERY_KEY, _BATTERY_ADAPTER)
            if battery_data is not None:
                capacity = battery_data.capacity_kwh
            elif hasattr(battery.capacity, "value"):
                capacity = float(battery.capacity.value)
                if battery.capacity.unit == "Wh":
                    capacity /= 1000
            result = calculate_battery_efficiency(
                history,
                calculation,
                capacity_kwh=capacity,
                now=now,
            )
            store.save(_BATTERY_EFFICIENCY_KEY, _BATTERY_EFFICIENCY_ADAPTER, result)
            return result

        return HistoryPlan(
            needs=history_plan.needs if history_plan is not None else (),
            build=build,
        )

    def is_fresh(_: object, now: datetime) -> bool:
        """Check freshness against the calculated source's own recompute interval.

        The shared Home Assistant ``max_data_age_seconds`` threshold is meant for
        live entity polling and is typically much shorter than the daily
        recompute interval used here, which would otherwise report this source
        as stale for most of every day even when it is working correctly.
        """
        history = store.load(history_key, history_adapter)
        if history is None:
            return False
        age_seconds = (now - history.latest_observation_at).total_seconds()
        return age_seconds <= interval_seconds * 2

    return _registration(
        "battery_efficiency",
        BatteryEfficiencyData,
        _BATTERY_EFFICIENCY_ADAPTER,
        _BATTERY_EFFICIENCY_KEY,
        store,
        plan,
        is_fresh,
    )


def _build_heat_pump_registration(
    configuration: Configuration, sources: _Sources, store: ProviderDataStore
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.heat_pump is None
        or "heat_pump" not in sources
    ):
        return None
    importer = HomeAssistantHeatPumpImporter(home_assistant)
    return _registration(
        "heat_pump",
        HeatPumpLoad,
        TypeAdapter(HeatPumpLoad),
        ProviderDataKey("heat-pump", "home-assistant", "heat_pump"),
        store,
        _fetch_plan(lambda now: importer.fetch(now=now)),
        importer.is_fresh,
    )


_REGISTRATION_FACTORIES = (
    _build_heat_pump_registration,
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
    *,
    home_assistant_client: httpx.Client | None = None,
) -> ProviderOrchestrator | None:
    """Compose currently configured concrete providers into the orchestrator.

    ``home_assistant_client`` is the one HTTP client that every Home Assistant
    history import of a cycle uses. Without it, each import opens its own.
    """
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
        registration = factory(configuration, orchestration.sources, store)
        if registration is not None:
            registrations.append(registration)

    history_importer = (
        HomeAssistantHistoryImporter(
            configuration.home_assistant, home_assistant_client
        )
        if configuration.home_assistant is not None
        else None
    )
    orchestrator = ProviderOrchestrator(
        configuration=orchestration,
        registrations=registrations,
        store=store,
        plan_generator=plan_generator,
        history_importer=history_importer,
    )
    logger.info(
        "event=orchestration_composed component=orchestration operation=compose "
        "registration_count=%s",
        len(registrations),
    )
    return orchestrator
