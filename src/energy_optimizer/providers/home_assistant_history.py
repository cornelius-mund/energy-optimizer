"""Shared Home Assistant history import: every entity is fetched once per cycle.

An orchestration cycle first collects what every due source needs
(:class:`HistoryNeed`), then imports and cleans each entity exactly once
(:class:`HomeAssistantHistoryImporter`), and finally lets every source build its
final record from the shared cleaned series (:class:`HomeAssistantHistory`).

This layer only performs cleaning that does not depend on the consuming
aggregate. It never drops or repairs a sample: an unavailable, non-numeric,
non-finite or negative state stays in the series as an invalid sample that keeps
its raw state and its reason. Everything that depends on a consumer's own
settings, such as counter steps, unit and state-class validation, or physical
limits, happens in the pure normalization steps that read a consumer's window
from the shared series and exclude every hour an invalid sample touches.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Hashable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from operator import attrgetter
from time import perf_counter
from typing import Any, Generic, Literal, TypeVar, cast
from urllib.parse import quote

import httpx

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.exclusions import ExclusionReason
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)

HOME_ASSISTANT_HISTORY_CHUNK = timedelta(days=7)
_UNAVAILABLE_STATES = frozenset({"unknown", "unavailable"})

HistoryKind = Literal["counter", "state"]
"""``counter`` is a cumulative-energy counter, ``state`` is plain state history."""

DataT_co = TypeVar("DataT_co", covariant=True)
ComputedT = TypeVar("ComputedT")


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant energy data cannot be imported safely."""


def as_import_utc(value: datetime) -> datetime:
    """Require a timezone-aware import time and return it in UTC."""
    return as_utc(
        value,
        error_factory=HomeAssistantError,
        message="Home Assistant import times must include a timezone",
    )


class HomeAssistantAuthenticationError(HomeAssistantError):
    """Raised when Home Assistant rejects the configured token.

    Unlike a failure that concerns one entity, a rejected token would reject
    every further request too, so an import stops sending requests after it.
    """


class HistoryPlanError(RuntimeError):
    """Raised when planned history needs and history reads are inconsistent.

    This signals a programming or composition error, not a data problem: two
    sources plan one entity with different kinds, or a build reads history that
    no source planned.
    """


@dataclass(frozen=True, slots=True)
class HistoryNeed:
    """One entity's history that a source needs for a time range.

    The range already includes the source's own lookback.
    """

    entity_id: str
    kind: HistoryKind
    start_time: datetime
    end_time: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", as_import_utc(self.start_time))
        object.__setattr__(self, "end_time", as_import_utc(self.end_time))
        if self.end_time <= self.start_time:
            raise HomeAssistantError(
                f"Home Assistant history range for {self.entity_id} must end after "
                "it starts"
            )


@dataclass(frozen=True, slots=True)
class HistorySample:
    """One cleaned history sample.

    ``unit``, ``state_class``, and ``last_reset`` are carried forward from the
    previous sample for counters, because Home Assistant may omit unchanged
    attributes. ``state`` is the raw state exactly as Home Assistant reported it.
    An invalid sample has no ``value`` and names its reason in ``invalid``; it is
    kept so a consumer can exclude every hour it touches and show the raw state.
    A window re-stamps the state in force at its start to that start;
    ``observed_at`` then keeps the time Home Assistant recorded it.
    """

    timestamp: datetime
    value: float | None
    unit: str | None
    state_class: str | None
    last_reset: datetime | None
    state: str | None = None
    invalid: ExclusionReason | None = None
    observed_at: datetime | None = None

    @property
    def recorded_at(self) -> datetime:
        return self.observed_at or self.timestamp


@dataclass(frozen=True, slots=True)
class HistorySeries:
    """The cleaned samples of one entity over the imported time range."""

    entity_id: str
    kind: HistoryKind
    start_time: datetime
    end_time: datetime
    samples: tuple[HistorySample, ...]

    def window(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[HistorySample, ...]:
        """Return what an independent request for exactly this window would see.

        Home Assistant answers a history request with the state in force at the
        start of the requested period, stamped with that start, followed by
        every state change up to the end. Slicing the shared series the same way
        makes a consumer's result equal to the result of its own request. An
        invalid state in force is carried like any other, so a valid state is
        never carried across an outage.
        """
        first = bisect_left(self.samples, start_time, key=attrgetter("timestamp"))
        last = bisect_right(self.samples, end_time, key=attrgetter("timestamp"))
        inside = self.samples[first:last]
        if first > 0 and (not inside or inside[0].timestamp > start_time):
            in_force = self.samples[first - 1]
            return (
                replace(
                    in_force,
                    timestamp=start_time,
                    observed_at=in_force.recorded_at,
                ),
                *inside,
            )
        return inside


class HomeAssistantHistory:
    """Cleaned history for one orchestration cycle; discarded when it ends.

    It keeps only cleaned series, never raw responses, and is never reused by a
    later cycle.
    """

    def __init__(
        self,
        series: Mapping[str, HistorySeries] | None = None,
        failures: Mapping[str, Exception] | None = None,
    ) -> None:
        self._series = dict(series or {})
        self._failures = dict(failures or {})
        self._computed: dict[Hashable, Any] = {}

    def window(
        self,
        entity_id: str,
        kind: HistoryKind,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[HistorySample, ...]:
        """Return one entity's samples for a consumer's window.

        A failed import re-raises its recorded failure to every consumer of the
        entity. Reading an entity, kind, or range that was not planned raises
        :class:`HistoryPlanError`.
        """
        failure = self._failures.get(entity_id)
        if failure is not None:
            raise failure
        series = self._series.get(entity_id)
        if series is None:
            raise HistoryPlanError(
                f"Home Assistant history for {entity_id} was requested but not planned"
            )
        if series.kind != kind:
            raise HistoryPlanError(
                f"Home Assistant history for {entity_id} was planned as "
                f"{series.kind} history but requested as {kind} history"
            )
        if start_time < series.start_time or end_time > series.end_time:
            raise HistoryPlanError(
                f"Home Assistant history for {entity_id} was requested from "
                f"{start_time.isoformat()} to {end_time.isoformat()}, outside the "
                f"planned range {series.start_time.isoformat()} to "
                f"{series.end_time.isoformat()}"
            )
        return series.window(start_time, end_time)

    def computed(self, key: Hashable, compute: Callable[[], ComputedT]) -> ComputedT:
        """Compute a pure derivation of the series once per cycle.

        Sources whose derivation is identical share one computation. A failed
        computation is not remembered, so every consumer reports its own error.
        """
        if key not in self._computed:
            self._computed[key] = compute()
        return cast(ComputedT, self._computed[key])


@dataclass
class _ImportStatistics:
    """Requests attempted by one import, including those of failed entities."""

    request_count: int = 0


@dataclass(frozen=True)
class HistoryPlan(Generic[DataT_co]):
    """What one source needs from Home Assistant and how to build its record.

    ``needs`` may be empty. A source without Home Assistant history needs builds
    its record by itself.
    """

    needs: tuple[HistoryNeed, ...]
    build: Callable[[HomeAssistantHistory], DataT_co]


class HomeAssistantHistoryImporter:
    """Import the history that all sources of one cycle need, entity by entity."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._client = client

    def import_history(self, needs: Iterable[HistoryNeed]) -> HomeAssistantHistory:
        """Fetch and clean each distinct entity once for the merged range.

        A failure while importing one entity is recorded and re-raised only to
        the consumers that read that entity. A rejected token ends the import:
        the remaining entities are not requested and fail with the same
        authentication error. Importing never makes a request for an empty set
        of needs.
        """
        ranges = _merge_needs(needs)
        if not ranges:
            return HomeAssistantHistory()
        started_at = perf_counter()
        series: dict[str, HistorySeries] = {}
        failures: dict[str, Exception] = {}
        statistics = _ImportStatistics()
        invalid_sample_count = 0
        entities = list(ranges.items())
        with (
            nullcontext(self._client) if self._client is not None else httpx.Client()
        ) as client:
            http = JsonHttpClient(client)
            for position, (entity_id, (kind, start_time, end_time)) in enumerate(
                entities
            ):
                try:
                    records = self._fetch_records(
                        http, statistics, entity_id, start_time, end_time
                    )
                    samples = (
                        _clean_counter_records(entity_id, records)
                        if kind == "counter"
                        else _clean_state_records(entity_id, records)
                    )
                    invalid_sample_count += sum(
                        1 for sample in samples if sample.invalid is not None
                    )
                    series[entity_id] = HistorySeries(
                        entity_id, kind, start_time, end_time, samples
                    )
                except HomeAssistantAuthenticationError as error:
                    skipped = [skipped_id for skipped_id, _ in entities[position + 1 :]]
                    failures.update(self._record_rejection(entity_id, skipped, error))
                    break
                except Exception as error:
                    failures[entity_id] = self._record_failure(entity_id, error)
        logger.info(
            "event=home_assistant_history_import component=home_assistant "
            "operation=import status=%s entity_count=%s failed_entity_count=%s "
            "request_count=%s invalid_sample_count=%s duration_ms=%.1f",
            "partial" if failures else "success",
            len(ranges),
            len(failures),
            statistics.request_count,
            invalid_sample_count,
            (perf_counter() - started_at) * 1000,
        )
        return HomeAssistantHistory(series, failures)

    @staticmethod
    def _entity_failure(entity_id: str, error: Exception) -> Exception:
        """Turn any import failure into one that names its entity."""
        if isinstance(error, HomeAssistantError):
            failure: Exception = error
            if entity_id not in str(error):
                failure = error.__class__(f"{error} (entity {entity_id})")
                failure.__cause__ = error
        else:
            failure = HomeAssistantError(
                f"Home Assistant history import for {entity_id} failed "
                f"unexpectedly: {error.__class__.__name__}: {error}"
            )
            failure.__cause__ = error
        return failure

    def _record_failure(self, entity_id: str, error: Exception) -> Exception:
        failure = self._entity_failure(entity_id, error)
        logger.warning(
            "event=home_assistant_history_entity_failed component=home_assistant "
            "operation=import entity_id=%s error_type=%s error=%s",
            entity_id,
            error.__class__.__name__,
            failure,
            exc_info=(None if isinstance(error, HomeAssistantError) else error),
        )
        return failure

    def _record_rejection(
        self,
        rejected_entity_id: str,
        skipped_entity_ids: list[str],
        error: HomeAssistantAuthenticationError,
    ) -> dict[str, Exception]:
        """Fail the rejected and every not-yet-requested entity, logging once."""
        failures = {
            entity_id: self._entity_failure(entity_id, error)
            for entity_id in (rejected_entity_id, *skipped_entity_ids)
        }
        logger.warning(
            "event=home_assistant_history_authentication_failed "
            "component=home_assistant operation=import entity_id=%s "
            "skipped_entity_count=%s error=%s",
            rejected_entity_id,
            len(skipped_entity_ids),
            failures[rejected_entity_id],
        )
        return failures

    def _fetch_records(
        self,
        http: JsonHttpClient,
        statistics: _ImportStatistics,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[tuple[datetime, dict[str, Any]]]:
        """Request an entity's history in seven-day chunks and join the chunks."""
        records: list[tuple[datetime, dict[str, Any]]] = []
        record_indexes: dict[datetime, int] = {}
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + HOME_ASSISTANT_HISTORY_CHUNK, end_time)
            statistics.request_count += 1
            response = self._request(http, entity_id, chunk_start, chunk_end)
            chunk_timestamps: set[datetime] = set()
            for record in _parse_payload(response, entity_id):
                timestamp = _parse_timestamp(
                    record.get("last_updated", record.get("last_changed")), entity_id
                )
                if timestamp in chunk_timestamps:
                    # Preserve duplicate records within one provider response so
                    # normalization can reject the malformed history explicitly.
                    records.append((timestamp, record))
                    continue
                chunk_timestamps.add(timestamp)
                existing_index = record_indexes.get(timestamp)
                if existing_index is None:
                    record_indexes[timestamp] = len(records)
                    records.append((timestamp, record))
                else:
                    # Home Assistant may include a boundary observation in both
                    # adjacent half-open responses; retain the later response once.
                    records[existing_index] = (timestamp, record)
            chunk_start = chunk_end
        return records

    def _request(
        self,
        http: JsonHttpClient,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> Any:
        base_url = str(self.configuration.base_url).rstrip("/")
        return http.get_home_assistant_json(
            f"{base_url}/api/history/period/{quote(start_time.isoformat(), safe='')}"
            f"?end_time={quote(end_time.isoformat(), safe='')}"
            f"&filter_entity_id={quote(entity_id, safe='')}",
            token=self.configuration.token.get_secret_value(),
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            authentication_error_factory=HomeAssistantAuthenticationError,
            not_found_message=(
                f"Home Assistant history was not found for {entity_id}; check the "
                "configured entity ID and endpoint"
            ),
            status_message=lambda status: (
                f"Home Assistant returned HTTP {status} while retrieving history "
                f"for {entity_id}"
            ),
            timeout_message=(
                f"Home Assistant request for {entity_id} history timed out; check "
                "the endpoint and timeout"
            ),
            transport_message=(
                f"Home Assistant request for {entity_id} history failed: "
                "transport error"
            ),
            malformed_message=(
                f"Home Assistant returned malformed JSON for {entity_id} history"
            ),
            log_event="home_assistant_history_request",
            component="home_assistant",
            operation="history_request",
            success_log_level=logging.DEBUG,
            error_log_level=logging.DEBUG,
            log_context=(
                f"entity_id={entity_id} start_time={start_time.isoformat()} "
                f"end_time={end_time.isoformat()}"
            ),
        )


def _merge_needs(
    needs: Iterable[HistoryNeed],
) -> dict[str, tuple[HistoryKind, datetime, datetime]]:
    """Merge the needs per entity into one range: earliest start, latest end."""
    merged: dict[str, tuple[HistoryKind, datetime, datetime]] = {}
    for need in needs:
        kind, start_time, end_time = merged.get(
            need.entity_id, (need.kind, need.start_time, need.end_time)
        )
        if kind != need.kind:
            raise HistoryPlanError(
                f"Home Assistant entity {need.entity_id} is planned as both {kind} "
                f"and {need.kind} history"
            )
        merged[need.entity_id] = (
            kind,
            min(start_time, need.start_time),
            max(end_time, need.end_time),
        )
    return merged


def _parse_payload(payload: Any, entity_id: str) -> list[dict[str, Any]]:
    """Return the records of one entity series; an empty chunk is valid."""
    if not isinstance(payload, list) or len(payload) > 1:
        raise HomeAssistantError(
            f"Home Assistant history for {entity_id} must contain one entity series"
        )
    if not payload or not payload[0]:
        return []
    series = payload[0]
    if not isinstance(series, list):
        raise HomeAssistantError(
            f"Home Assistant history for {entity_id} must contain a list of records"
        )
    if not all(isinstance(record, dict) for record in series):
        raise HomeAssistantError(
            f"Home Assistant history for {entity_id} contains an invalid record"
        )
    return series


def _parse_timestamp(value: Any, entity_id: str) -> datetime:
    return parse_aware_timestamp(
        value,
        error_factory=HomeAssistantError,
        missing_message=(
            f"Home Assistant history for {entity_id} has a missing timestamp"
        ),
        invalid_message=lambda raw: (
            f"Home Assistant returned an invalid timestamp for {entity_id}: {raw!r}"
        ),
        naive_message=(
            f"Home Assistant timestamps for {entity_id} must include a timezone"
        ),
    )


def _clean_counter_records(
    entity_id: str, records: list[tuple[datetime, dict[str, Any]]]
) -> tuple[HistorySample, ...]:
    """Clean cumulative-counter records into sorted, attribute-complete samples.

    Every record becomes a sample. A record whose state cannot be used keeps its
    raw state and reason instead of being dropped or raising.
    """
    if not records:
        raise HomeAssistantError(f"Home Assistant returned no history for {entity_id}")
    samples: list[HistorySample] = []
    previous: HistorySample | None = None
    for timestamp, record in sorted(records, key=lambda item: item[0]):
        sample = _clean_counter_sample(entity_id, timestamp, record, previous)
        samples.append(sample)
        if sample.invalid != "invalid_attribute":
            previous = sample
    return tuple(samples)


def _clean_counter_sample(
    entity_id: str,
    timestamp: datetime,
    record: dict[str, Any],
    previous: HistorySample | None,
) -> HistorySample:
    state = record.get("state")
    value, invalid = _classify_state(state, reject_negative=True)
    unit = previous.unit if previous is not None else None
    state_class = previous.state_class if previous is not None else None
    last_reset = previous.last_reset if previous is not None else None
    attributes = record.get("attributes")
    attribute_problem = False
    if isinstance(attributes, dict):
        raw_unit = attributes.get("unit_of_measurement")
        if isinstance(raw_unit, str):
            unit = raw_unit.strip()
        if "state_class" in attributes:
            if isinstance(attributes["state_class"], str):
                state_class = attributes["state_class"]
            else:
                attribute_problem = True
        if "last_reset" in attributes:
            raw_reset = attributes["last_reset"]
            if raw_reset is None:
                last_reset = None
            elif isinstance(raw_reset, str):
                try:
                    last_reset = _parse_timestamp(raw_reset, entity_id)
                except HomeAssistantError:
                    attribute_problem = True
            else:
                attribute_problem = True
    if attribute_problem and invalid is None:
        value, invalid = None, "invalid_attribute"
    return HistorySample(
        timestamp,
        value,
        unit,
        state_class,
        last_reset,
        None if state is None else str(state),
        invalid,
    )


def _clean_state_records(
    entity_id: str, records: list[tuple[datetime, dict[str, Any]]]
) -> tuple[HistorySample, ...]:
    """Clean plain state records into sorted samples with their reported unit."""
    if not records:
        raise HomeAssistantError(
            f"Home Assistant returned no usable history for {entity_id}"
        )
    samples: list[HistorySample] = []
    for timestamp, record in sorted(records, key=lambda item: item[0]):
        state = record.get("state")
        value, invalid = _classify_state(state, reject_negative=False)
        attributes = record.get("attributes")
        unit = (
            attributes.get("unit_of_measurement")
            if isinstance(attributes, dict)
            else None
        )
        samples.append(
            HistorySample(
                timestamp,
                value,
                unit if isinstance(unit, str) else None,
                None,
                None,
                None if state is None else str(state),
                invalid,
            )
        )
    return tuple(samples)


def _classify_state(
    state: Any, *, reject_negative: bool
) -> tuple[float | None, ExclusionReason | None]:
    """Parse a state into a finite number, or name why it cannot be used."""
    if isinstance(state, str) and state.strip().lower() in _UNAVAILABLE_STATES:
        return None, "unavailable"
    try:
        value = float(state)
    except TypeError, ValueError:
        return None, "non_numeric"
    if not math.isfinite(value):
        return None, "not_finite"
    if reject_negative and value < 0:
        return None, "negative_value"
    return value, None


__all__ = [
    "HOME_ASSISTANT_HISTORY_CHUNK",
    "HistoryKind",
    "HistoryNeed",
    "HistoryPlan",
    "HistoryPlanError",
    "HistorySample",
    "HistorySeries",
    "HomeAssistantError",
    "HomeAssistantHistory",
    "HomeAssistantHistoryImporter",
]
