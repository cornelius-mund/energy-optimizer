"""Home Assistant household-load provider composition."""

import logging
from datetime import datetime, timezone

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    is_fresh,
    latest_completed_hour,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantError,
    HomeAssistantHistory,
    as_import_utc,
)
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_SOURCE_ID,
    HouseholdLoadData,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantError", "HomeAssistantLoadImporter"]


class HomeAssistantLoadImporter:
    """Declare and build household-load history from Home Assistant."""

    def __init__(self, configuration: HomeAssistantConfiguration) -> None:
        self.configuration = configuration

    def plan(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[HouseholdLoadData]:
        """Declare the history a period needs and how to build its record.

        Building reads the shared history that the caller imported for the
        declared needs; this importer makes no Home Assistant request itself.
        """
        aggregation = self.configuration.household_load
        entity_count = len(aggregation.entity_ids) if aggregation is not None else 0
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "start_time=%s end_time=%s history_lookback_seconds=%s entity_count=%s",
            start_time,
            end_time,
            history_lookback_seconds,
            entity_count,
        )
        retrieved_at = as_import_utc(now or datetime.now(timezone.utc))
        effective_end_time = end_time or latest_completed_hour(retrieved_at)

        def log_failure(error: Exception) -> None:
            logger.error(
                "event=provider_fetch_failed component=home_assistant "
                "operation=fetch error_type=%s entity_count=%s error=%s",
                error.__class__.__name__,
                entity_count,
                error,
                exc_info=(None if isinstance(error, HomeAssistantError) else True),
            )

        try:
            aggregate = EnergyAggregate(
                aggregation,
                start_time,
                effective_end_time,
                history_lookback_seconds,
                label="household-load",
            )
        except Exception as error:
            log_failure(error)
            raise

        def build(history: HomeAssistantHistory) -> HouseholdLoadData:
            try:
                series = aggregate.build(history)
            except Exception as error:
                log_failure(error)
                raise
            data = HouseholdLoadData(
                schema_version="1",
                start_time=series.start_time,
                interval_minutes=60,
                load_kw=series.values_kw,
                unit="kW",
                source=SourceMetadata(
                    provider="home-assistant", entity_id=HOUSEHOLD_LOAD_SOURCE_ID
                ),
                retrieved_at=retrieved_at,
                latest_observation_at=series.latest_observation_at,
                exclusions=series.exclusions,
            )
            logger.info(
                "event=provider_fetch_succeeded component=home_assistant "
                "operation=fetch start_time=%s end_time=%s record_count=%s "
                "excluded_hour_count=%s entity_count=%s",
                data.start_time,
                effective_end_time,
                len(data.load_kw),
                len(data.exclusions),
                entity_count,
            )
            return data

        return HistoryPlan(needs=aggregate.needs(), build=build)

    def is_fresh(self, data: HouseholdLoadData, *, now: datetime | None = None) -> bool:
        return is_fresh(
            data.latest_observation_at, self.configuration.max_data_age_seconds, now=now
        )
