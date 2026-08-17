"""Home Assistant household-load provider composition."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant_energy import (
    HomeAssistantEnergyAggregator,
    HomeAssistantError,
    is_fresh,
    latest_completed_hour,
)
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_SOURCE_ID,
    HouseholdLoadData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantError", "HomeAssistantLoadImporter"]


class HomeAssistantLoadImporter:
    """Retrieve and normalize household-load history from Home Assistant."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._aggregator = HomeAssistantEnergyAggregator(configuration, client)

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HouseholdLoadData:
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "start_time=%s end_time=%s history_lookback_seconds=%s entity_count=%s",
            start_time,
            end_time,
            history_lookback_seconds,
            len(self.configuration.household_load_entities or []),
        )
        retrieved_at = as_utc(
            now or datetime.now(timezone.utc),
            error_factory=HomeAssistantError,
            message="Home Assistant import times must include a timezone",
        )
        effective_end_time = end_time or latest_completed_hour(retrieved_at)
        try:
            series = self._aggregator.aggregate(
                self.configuration.household_load_entities,
                start_time,
                effective_end_time,
                history_lookback_seconds,
                label="household-load",
            )
        except Exception as error:
            if isinstance(error, HomeAssistantError) and any(
                marker in str(error) for marker in ("unknown", "unavailable")
            ):
                logger.warning(
                    "event=provider_data_degraded component=home_assistant "
                    "operation=fetch error_type=%s entity_count=%s error=%s",
                    error.__class__.__name__,
                    len(self.configuration.household_load_entities or []),
                    error,
                )
            logger.error(
                "event=provider_fetch_failed component=home_assistant "
                "operation=fetch error_type=%s entity_count=%s",
                error.__class__.__name__,
                len(self.configuration.household_load_entities or []),
                exc_info=True,
            )
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
        )
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant operation=fetch "
            "start_time=%s end_time=%s record_count=%s entity_count=%s",
            data.start_time,
            effective_end_time,
            len(data.load_kw),
            len(self.configuration.household_load_entities or []),
        )
        return data

    def is_fresh(
        self,
        data: HouseholdLoadData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check whether a poll result is within the configured age threshold."""
        return is_fresh(
            data.latest_observation_at,
            self.configuration.max_data_age_seconds,
            now=now,
        )
