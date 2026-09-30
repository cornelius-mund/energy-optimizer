"""Home Assistant grid import and export provider composition."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.exclusions import merge_exclusions
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
    is_fresh,
    latest_completed_hour,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantError,
    HomeAssistantHistory,
)
from energy_optimizer.providers.interfaces import (
    GRID_FLOW_SOURCE_ID,
    GridFlowData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc

logger = logging.getLogger(__name__)


class HomeAssistantGridFlowImporter:
    """Declare and build grid import and export history."""

    def __init__(self, configuration: HomeAssistantConfiguration) -> None:
        self.configuration = configuration

    def plan(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[GridFlowData]:
        """Declare the history both grid channels need for a half-open period.

        Building reads the shared history that the caller imported for the
        declared needs; this importer makes no Home Assistant request itself.
        """
        retrieved_at = as_utc(
            now or datetime.now(timezone.utc),
            error_factory=HomeAssistantError,
            message="Home Assistant import times must include a timezone",
        )
        effective_end_time = end_time or latest_completed_hour(retrieved_at)
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "data_type=grid_flow start_time=%s end_time=%s "
            "history_lookback_seconds=%s import_entity_count=%s "
            "export_entity_count=%s",
            start_time,
            end_time,
            history_lookback_seconds,
            len(self.configuration.grid_import_entities or []),
            len(self.configuration.grid_export_entities or []),
        )
        try:
            import_aggregate = EnergyAggregate(
                self.configuration.grid_import_entities,
                start_time,
                effective_end_time,
                history_lookback_seconds,
                label="grid import",
            )
            export_aggregate = EnergyAggregate(
                self.configuration.grid_export_entities,
                start_time,
                effective_end_time,
                history_lookback_seconds,
                label="grid export",
            )
        except Exception as error:
            self._log_failure(error)
            raise

        def build(history: HomeAssistantHistory) -> GridFlowData:
            try:
                import_series = import_aggregate.build(history)
                export_series = export_aggregate.build(history)
                data = self._combine(
                    import_series, export_series, effective_end_time, retrieved_at
                )
            except Exception as error:
                self._log_failure(error)
                raise
            logger.info(
                "event=provider_fetch_succeeded component=home_assistant "
                "operation=fetch data_type=grid_flow start_time=%s end_time=%s "
                "record_count=%s excluded_hour_count=%s",
                data.start_time,
                effective_end_time,
                len(data.import_kw),
                len(data.exclusions),
            )
            return data

        return HistoryPlan(
            needs=import_aggregate.needs() + export_aggregate.needs(), build=build
        )

    @staticmethod
    def _log_failure(error: Exception) -> None:
        logger.error(
            "event=provider_fetch_failed component=home_assistant "
            "operation=fetch data_type=grid_flow error_type=%s",
            error.__class__.__name__,
            exc_info=True,
        )

    def _combine(
        self,
        import_series: HomeAssistantEnergySeries,
        export_series: HomeAssistantEnergySeries,
        effective_end_time: datetime,
        retrieved_at: datetime,
    ) -> GridFlowData:
        """Align both channels to their common available start."""
        aggregate_start = max(import_series.start_time, export_series.start_time)
        value_count = int(
            (effective_end_time - aggregate_start).total_seconds() // 3600
        )
        import_offset = int(
            (aggregate_start - import_series.start_time).total_seconds() // 3600
        )
        export_offset = int(
            (aggregate_start - export_series.start_time).total_seconds() // 3600
        )
        import_values = import_series.values_kw[
            import_offset : import_offset + value_count
        ]
        export_values = export_series.values_kw[
            export_offset : export_offset + value_count
        ]
        if len(import_values) != value_count or len(export_values) != value_count:
            raise HomeAssistantError(
                "Home Assistant grid import and export entities returned "
                "misaligned hourly series"
            )
        # An hour that either channel excludes is excluded for both, so the
        # stored import and export of an hour always describe the same hour.
        incomplete = [
            imported is None or exported is None
            for imported, exported in zip(import_values, export_values)
        ]
        import_values = tuple(
            None if skip else value for value, skip in zip(import_values, incomplete)
        )
        export_values = tuple(
            None if skip else value for value, skip in zip(export_values, incomplete)
        )
        return GridFlowData(
            schema_version="1",
            start_time=aggregate_start,
            interval_minutes=60,
            import_kw=import_values,
            export_kw=export_values,
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id=GRID_FLOW_SOURCE_ID
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=min(
                import_series.latest_observation_at,
                export_series.latest_observation_at,
            ),
            exclusions=merge_exclusions(
                *(
                    [
                        item
                        for item in series.exclusions
                        if aggregate_start <= item.hour_start < effective_end_time
                    ]
                    for series in (import_series, export_series)
                )
            ),
        )

    def is_fresh(
        self,
        data: GridFlowData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check whether both grid channels are within the age threshold."""
        return is_fresh(
            data.latest_observation_at,
            self.configuration.max_data_age_seconds,
            now=now,
        )
