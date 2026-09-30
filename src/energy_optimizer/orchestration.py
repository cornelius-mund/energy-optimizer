"""Application orchestration for scheduled provider retrieval and planning."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
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
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistoryPlan,
    HomeAssistantError,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from energy_optimizer.providers.interfaces import (
    BATTERY_EFFICIENCY_SOURCE_ID,
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
    """Connect a scheduled source to its provider-independent data contract.

    ``plan`` runs for a due source before anything is imported. It returns the
    Home Assistant history the source needs and how to build its record from
    that history, or ``None`` when there is nothing to fetch, for example
    because every completed hour is already persisted.
    """

    name: str
    data_type: str
    adapter: TypeAdapter[Any]
    plan: Callable[[datetime, DataSourceScheduleConfiguration], HistoryPlan[Any] | None]
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
    plan: Callable[
        [datetime, DataSourceScheduleConfiguration], HistoryPlan[DataT] | None
    ],
    is_fresh: Callable[..., bool],
    load: Callable[[], DataT | None],
) -> ProviderRegistration:
    return ProviderRegistration(
        name=name,
        data_type=data_type,
        adapter=adapter,
        plan=plan,
        is_fresh=_make_freshness_checker(data_class, is_fresh),
        load=load,
    )


def _without_history(build: Callable[[], DataT]) -> HistoryPlan[DataT]:
    """Plan a source that needs no Home Assistant history and builds by itself."""
    return HistoryPlan(needs=(), build=lambda history: build())


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
        self.history_importer = history_importer
        self._next_due: dict[str, datetime] = {}
        self._latest_data: dict[str, object] = {}
        self._cycle_lock = threading.Lock()
        self._history: deque[OrchestrationCycle] = deque(maxlen=100)
        for registration in self.registrations:
            if registration.load is not None:
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
                else:
                    if data is not None:
                        self._latest_data[registration.name] = data
                    logger.info(
                        "event=orchestration_restore_completed "
                        "component=orchestration operation=restore source=%s "
                        "status=%s duration_seconds=%.3f",
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

    def _import_history(
        self, planned: list[_PlannedSource]
    ) -> tuple[HomeAssistantHistory, Exception | None]:
        """Import the history of all planned sources; never request without needs.

        A failure of a single entity is recorded inside the returned history and
        reaches only the sources that read it. An error that prevents the whole
        import, such as inconsistent plans, is returned so that every source with
        Home Assistant needs fails with it.
        """
        needs: list[HistoryNeed] = [
            need for source in planned for need in source.plan.needs
        ]
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
        schedule = source.schedule
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
            self._set_next_due(registration.name, attempt_completed, schedule)
            _log_excluded_hours(registration.name, getattr(data, "exclusions", ()))
            status: RunStatus
            error_message: str | None
            if registration.is_fresh(saved_data, now):
                fresh_data[registration.name] = saved_data
                status = "success"
                error_message = None
            else:
                status = "stale"
                error_message = "provider returned data outside its freshness threshold"
            run = ProviderRun(
                source=registration.name,
                status=status,
                started_at=source.started_at,
                completed_at=attempt_completed,
                error=error_message,
            )
            log_method = logger.warning if status == "stale" else logger.info
            log_method(
                "event=provider_refresh_completed component=orchestration "
                "operation=refresh source=%s status=%s duration_seconds=%.3f",
                registration.name,
                status,
                (attempt_completed - source.started_at).total_seconds(),
            )
            return run
        except Exception as error:
            return self._failed_run(
                registration, schedule, source.started_at, cycle_clock, error
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
            source=registration.name,
            status="failed",
            started_at=attempt_started,
            completed_at=attempt_completed,
            error=message,
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
                "a required source failed or returned stale data",
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


def _log_excluded_hours(source: str, exclusions: object) -> None:
    """Report the hours a refresh excluded, once per source, counted by reason."""
    items = tuple(exclusions) if isinstance(exclusions, tuple) else ()
    if not items:
        return
    summary = exclusion_summary(items)
    logger.warning(
        "event=provider_hours_excluded component=orchestration operation=refresh "
        "source=%s excluded_hour_count=%s reasons=%s first_hour=%s last_hour=%s",
        source,
        len(items),
        ",".join(f"{reason}:{count}" for reason, count in summary.items()),
        items[0].hour_start.isoformat(),
        items[-1].hour_start.isoformat(),
    )


def _build_household_load_registration(
    configuration: Configuration,
    orchestration: OrchestrationConfiguration,
    store: ProviderDataStore,
) -> ProviderRegistration | None:
    home_assistant = configuration.home_assistant
    if (
        home_assistant is None
        or home_assistant.household_load is None
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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
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
        plan=plan,
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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> HistoryPlan[PvGenerationData]:
        del schedule
        start_time = now.replace(minute=0, second=0, microsecond=0)
        return _without_history(lambda: importer.fetch(start_time, now=now))

    def load() -> PvGenerationData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="pv_generation",
        data_type="pv-generation",
        data_class=PvGenerationData,
        adapter=adapter,
        plan=plan,
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
    history_key = ProviderDataKey(
        data_type="electricity-price-history",
        provider="awattar.de",
        entity_id=awattar.electricity_price_source_id,
    )

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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> HistoryPlan[ElectricityPriceData]:
        del schedule
        return _without_history(lambda: fetch(now))

    def load() -> ElectricityPriceData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="electricity_prices",
        data_type="electricity-prices",
        data_class=ElectricityPriceData,
        adapter=adapter,
        plan=plan,
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
        or home_assistant.grid_import is None
        or home_assistant.grid_export is None
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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
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
        plan=plan,
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

    def fetch(now: datetime) -> BatteryData:
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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> HistoryPlan[BatteryData]:
        del schedule
        return _without_history(lambda: fetch(now))

    def load() -> BatteryData | None:
        return store.load(key, adapter)

    return _make_provider_registration(
        name="battery",
        data_type="battery",
        data_class=BatteryData,
        adapter=adapter,
        plan=plan,
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

    def plan(
        now: datetime,
        schedule: DataSourceScheduleConfiguration,
    ) -> HistoryPlan[BatteryEfficiencyData]:
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

        return HistoryPlan(
            needs=history_plan.needs if history_plan is not None else (),
            build=build,
        )

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
        plan=plan,
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
        registration = factory(configuration, orchestration, store)
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
