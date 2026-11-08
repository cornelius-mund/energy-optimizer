"""Hourly EV state-of-charge and plug history from the Home Assistant InfluxDB.

Home Assistant writes a point only when a state changes, so each hour takes the
last value written up to its end and hours without a change carry the previous
value forward. Recent hours come from the raw retention policy; older hours come
from the unlimited policy of minute means.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from energy_optimizer.config import HomeAssistantEvConfiguration, InfluxDbConfiguration
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    EV_SOURCE_ID,
    EvHistoryData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc, is_fresh

logger = logging.getLogger(__name__)
ONE_HOUR = timedelta(hours=1)
PLUGGED_IN_SHARE_THRESHOLD = 0.5
PROVIDER_NAME = "influxdb"

type HourlyValues = dict[datetime, float]


class InfluxDbError(RuntimeError):
    """Raised when the InfluxDB history cannot be imported safely."""


@dataclass(frozen=True)
class InfluxSeries:
    """Where Home Assistant's InfluxDB integration stores one entity."""

    measurement: str
    entity_tag: str

    @classmethod
    def for_entity(cls, entity_id: str, unit: str | None) -> "InfluxSeries":
        """The measurement is the unit, or the entity ID for unitless entities;
        the ``entity_id`` tag omits the domain.
        """
        object_id = entity_id.split(".", 1)[-1]
        return cls(measurement=unit or entity_id, entity_tag=object_id)


class InfluxDbEvHistoryImporter:
    """Read the EV state of charge (%) and plug state for every hour of a window."""

    def __init__(
        self,
        influxdb: InfluxDbConfiguration,
        ev: HomeAssistantEvConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.influxdb = influxdb
        self.ev = ev
        self._http = JsonHttpClient(client)

    def fetch(
        self, start: datetime, end: datetime, *, now: datetime | None = None
    ) -> EvHistoryData:
        retrieved_at = _as_utc(now or datetime.now(timezone.utc))
        start, end = _hour(_as_utc(start)), _hour(_as_utc(end))
        if end <= start:
            raise InfluxDbError("InfluxDB history window must not be empty")
        soc_series = InfluxSeries.for_entity(
            self.ev.state_of_charge.entity_id, self.ev.state_of_charge.unit
        )
        plug_series = InfluxSeries.for_entity(
            self.influxdb.ev_plugged_in_entity_id or self.ev.plugged_in.entity_id,
            None,
        )
        state_of_charge = self._hourly(soc_series, start, end, retrieved_at)
        plug_share = self._hourly(plug_series, start, end, retrieved_at)
        hours = int((end - start) / ONE_HOUR)
        starts = [start + ONE_HOUR * index for index in range(hours)]
        data = EvHistoryData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            state_of_charge_percent=tuple(
                self._percent(state_of_charge.get(hour)) for hour in starts
            ),
            plugged_in=tuple(_plugged_in(plug_share.get(hour)) for hour in starts),
            source=SourceMetadata(provider=PROVIDER_NAME, entity_id=EV_SOURCE_ID),
            retrieved_at=retrieved_at,
        )
        logger.info(
            "event=provider_fetch_succeeded component=influxdb operation=ev_history "
            "start_time=%s hours=%s soc_hours=%s plug_hours=%s",
            start,
            hours,
            sum(value is not None for value in data.state_of_charge_percent),
            sum(value is not None for value in data.plugged_in),
        )
        return data

    def is_fresh(self, data: EvHistoryData, *, now: datetime | None = None) -> bool:
        return is_fresh(
            data.retrieved_at,
            self.influxdb.max_data_age_seconds,
            now=now,
            error_factory=InfluxDbError,
            now_message="InfluxDB freshness times must include a timezone",
        )

    def _percent(self, value: float | None) -> float | None:
        if value is None:
            return None
        if self.ev.state_of_charge.unit == "kWh":
            return value / self.ev.capacity_kwh * 100
        return value

    def _hourly(
        self, series: InfluxSeries, start: datetime, end: datetime, now: datetime
    ) -> HourlyValues:
        raw_policy_start = _hour(now - timedelta(days=self.influxdb.raw_retention_days))
        values: HourlyValues = {}
        if start < raw_policy_start:
            values |= self._query(
                series,
                self.influxdb.long_term_retention_policy,
                "mean_value",
                start,
                min(end, raw_policy_start),
            )
        if end > raw_policy_start:
            values |= self._query(
                series,
                self.influxdb.raw_retention_policy,
                "value",
                max(start, raw_policy_start),
                end,
            )
        return values

    def _query(
        self,
        series: InfluxSeries,
        retention_policy: str,
        field: str,
        start: datetime,
        end: datetime,
    ) -> HourlyValues:
        statement = (
            f'SELECT last("{field}") FROM "{retention_policy}"."{series.measurement}" '
            f"WHERE \"entity_id\" = '{series.entity_tag}' "
            f"AND time >= '{_influx_time(start)}' AND time < '{_influx_time(end)}' "
            "GROUP BY time(1h) fill(previous)"
        )
        payload = self._http.get_json(
            str(self.influxdb.base_url).rstrip("/") + "/query",
            params=self._parameters(statement),
            headers={"Accept": "application/json"},
            timeout_seconds=self.influxdb.timeout_seconds,
            error_factory=InfluxDbError,
            timeout_message="InfluxDB request timed out",
            transport_message="InfluxDB request failed: transport error",
            malformed_message="InfluxDB returned malformed JSON",
            status_error=_status_error,
            log_event="influxdb_request",
            component="influxdb",
            operation="query",
            log_context=f"policy={retention_policy} entity={series.entity_tag}",
        )
        return _parse_hourly(payload)

    def _parameters(self, statement: str) -> Mapping[str, str]:
        parameters = {"db": self.influxdb.database, "q": statement, "epoch": "s"}
        if self.influxdb.username is not None and self.influxdb.password is not None:
            parameters |= {
                "u": self.influxdb.username,
                "p": self.influxdb.password.get_secret_value(),
            }
        return parameters


def merge_ev_history(
    persisted: EvHistoryData | None, update: EvHistoryData, keep_hours: int
) -> EvHistoryData:
    """Overlay ``update`` on ``persisted`` and keep the newest ``keep_hours``."""
    if persisted is None:
        return update
    by_hour = _by_hour(persisted) | {
        hour: value for hour, value in _by_hour(update).items() if value != (None, None)
    }
    end = max(by_hour) + ONE_HOUR
    start = max(min(by_hour), end - ONE_HOUR * keep_hours)
    starts = [
        start + ONE_HOUR * index for index in range(int((end - start) / ONE_HOUR))
    ]
    empty: tuple[float | None, bool | None] = (None, None)
    return EvHistoryData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        state_of_charge_percent=tuple(by_hour.get(hour, empty)[0] for hour in starts),
        plugged_in=tuple(by_hour.get(hour, empty)[1] for hour in starts),
        source=update.source,
        retrieved_at=update.retrieved_at,
    )


def _by_hour(
    data: EvHistoryData,
) -> dict[datetime, tuple[float | None, bool | None]]:
    return {
        data.start_time + ONE_HOUR * index: (soc, plugged)
        for index, (soc, plugged) in enumerate(
            zip(data.state_of_charge_percent, data.plugged_in, strict=True)
        )
    }


def _plugged_in(share: float | None) -> bool | None:
    return None if share is None else share >= PLUGGED_IN_SHARE_THRESHOLD


def _status_error(status: int) -> Exception | None:
    if status in (401, 403):
        return InfluxDbError(f"InfluxDB rejected the credentials (HTTP {status})")
    if status >= 400:
        return InfluxDbError(f"InfluxDB returned HTTP {status}")
    return None


def _parse_hourly(payload: Any) -> HourlyValues:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        raise InfluxDbError("InfluxDB response must contain results")
    result = results[0]
    if not isinstance(result, dict):
        raise InfluxDbError("InfluxDB result must be an object")
    if "error" in result:
        raise InfluxDbError(f"InfluxDB rejected the query: {result['error']}")
    values: HourlyValues = {}
    for series in result.get("series", []):
        for row in series.get("values", []):
            if not isinstance(row, list) or len(row) != 2:
                raise InfluxDbError("InfluxDB rows must hold a time and one value")
            timestamp, value = row
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InfluxDbError("InfluxDB values must be numeric")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                raise InfluxDbError("InfluxDB times must be epoch seconds")
            values[datetime.fromtimestamp(timestamp, timezone.utc)] = float(value)
    return values


def _hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _influx_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(value: datetime) -> datetime:
    return as_utc(
        value,
        error_factory=InfluxDbError,
        message="InfluxDB history times must include a timezone",
    )
