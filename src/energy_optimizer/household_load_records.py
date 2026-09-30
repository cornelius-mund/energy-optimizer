"""Validated household-load records and pure history transformations."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from energy_optimizer.exclusions import HourExclusion, history_unavailable_exclusions
from energy_optimizer.legacy_quality import legacy_exclusion
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    HouseholdLoadData,
    SourceMetadata,
)
from energy_optimizer.storage_errors import ProviderDataStoreError

_HOUR = timedelta(hours=1)
_EXCLUSION_ADAPTER = TypeAdapter(HourExclusion)
logger = logging.getLogger(__name__)


class _HouseholdLoadRecordSource(BaseModel):
    """Source identity embedded in one persisted household-load record."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    entity_id: str | None


class _HouseholdLoadRecordQuality(BaseModel):
    """Quality metadata that records of earlier versions embed.

    It is only read: ``HouseholdLoadRecord.decode`` turns a suspect record into an
    excluded hour, and new records never contain it.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "suspect"]
    reason: str | None
    entity_id: str | None


def _normalize_record_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("record timestamp is invalid") from error
    else:
        raise ValueError("record timestamp is not a string")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("record timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


class HouseholdLoadRecord(BaseModel):
    """Validated and serializable representation of one NDJSON record.

    An excluded hour has ``load_kw`` null and carries its ``exclusion``.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    load_kw: float | None
    exclusion: HourExclusion | None = None
    quality: _HouseholdLoadRecordQuality | None = None
    schema_version: Literal["1"]
    unit: Literal["kW"]
    source: _HouseholdLoadRecordSource
    retrieved_at: datetime
    latest_observation_at: datetime

    @field_validator(
        "timestamp", "retrieved_at", "latest_observation_at", mode="before"
    )
    @classmethod
    def validate_timestamp(cls, value: object) -> datetime:
        return _normalize_record_timestamp(value)

    @field_validator("timestamp")
    @classmethod
    def validate_hourly_timestamp(cls, value: datetime) -> datetime:
        if value.minute or value.second or value.microsecond:
            raise ValueError("record timestamp is not hourly aligned")
        return value

    @field_validator("load_kw", mode="before")
    @classmethod
    def validate_load(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("record load is invalid")
        load_value = float(value)
        if not math.isfinite(load_value) or load_value < 0:
            raise ValueError("record load is invalid")
        return load_value

    @model_validator(mode="after")
    def validate_exclusion(self) -> HouseholdLoadRecord:
        """An hour has a value or an explanation for its absence, never both."""
        suspect = self.quality is not None and self.quality.status == "suspect"
        if (self.load_kw is None) != (self.exclusion is not None) and not suspect:
            raise ValueError("record load and exclusion do not match")
        if self.exclusion is not None and (
            _normalize_record_timestamp(self.exclusion.hour_start) != self.timestamp
        ):
            raise ValueError("record exclusion belongs to another hour")
        return self

    @classmethod
    def decode(cls, value: object) -> HouseholdLoadRecord:
        """Validate one decoded JSON value and convert suspect records of earlier
        versions into excluded hours."""
        try:
            record = cls.model_validate(value)
        except (TypeError, ValidationError) as error:
            raise ValueError("record does not match the NDJSON contract") from error
        update: dict[str, object] = {"quality": None}
        quality = record.quality
        if quality is not None and quality.status == "suspect":
            update["load_kw"] = None
            update["exclusion"] = legacy_exclusion(
                record.timestamp, quality.reason, quality.entity_id
            )
        return record.model_copy(update=update)

    def encode(self) -> bytes:
        """Encode this record using the established compact NDJSON format."""
        payload: dict[str, object] = {
            "timestamp": self.timestamp.isoformat(),
            "load_kw": self.load_kw,
            "schema_version": self.schema_version,
            "unit": self.unit,
            "source": self.source.model_dump(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "latest_observation_at": self.latest_observation_at.isoformat(),
        }
        if self.exclusion is not None:
            payload["exclusion"] = _EXCLUSION_ADAPTER.dump_python(
                self.exclusion, mode="json"
            )
        return json.dumps(payload, separators=(",", ":")).encode() + b"\n"


@dataclass(frozen=True)
class HouseholdLoadHistory:
    """Parsed household-load history and its physical record count."""

    model: HouseholdLoadData
    record_count: int
    ignored_incomplete_final_line: bool = False


def bridge_household_load_gap(
    existing: HouseholdLoadData, incoming: HouseholdLoadData
) -> HouseholdLoadData:
    """Extend ``incoming`` back to the end of ``existing`` over a gap.

    When the provider no longer holds the hours between the persisted history and
    the fetched range, retrying can never fill them. Every such hour is added to
    ``incoming`` as an excluded hour with the reason ``history_unavailable``, so
    the merged history stays contiguous. An incoming range that overlaps or
    directly follows the persisted history is returned unchanged.
    """
    existing_end = as_utc(existing.start_time) + len(existing.load_kw) * _HOUR
    incoming_start = as_utc(incoming.start_time)
    if incoming_start <= existing_end:
        return incoming
    household_load_points(incoming)
    gap = history_unavailable_exclusions("household_load", existing_end, incoming_start)
    return replace(
        incoming,
        start_time=existing_end,
        load_kw=(None,) * len(gap) + incoming.load_kw,
        exclusions=gap + incoming.exclusions,
        quality=(),
    )


def _with_points(
    template: HouseholdLoadData,
    points: Sequence[tuple[datetime, float | None]],
    exclusions: dict[datetime, HourExclusion],
) -> HouseholdLoadData:
    """Build a series of ordered ``points`` that keeps the metadata of ``template``."""
    return replace(
        template,
        start_time=points[0][0],
        load_kw=tuple(value for _, value in points),
        retrieved_at=as_utc(template.retrieved_at),
        latest_observation_at=as_utc(template.latest_observation_at),
        exclusions=tuple(
            exclusions[timestamp] for timestamp, _ in points if timestamp in exclusions
        ),
        quality=(),
    )


def merge_household_load_history(
    existing: HouseholdLoadData, incoming: HouseholdLoadData
) -> HouseholdLoadData:
    """Merge hourly household-load data, preferring the incoming hours.

    An incoming hour replaces the stored one completely: its value and its
    exclusion. Excluded hours stay in the series without a value, so the hours
    remain contiguous. Hours that lie between the persisted history and the
    incoming range are excluded as ``history_unavailable``.
    """
    incoming = bridge_household_load_gap(existing, incoming)
    existing_points = household_load_points(existing)
    incoming_points = household_load_points(incoming)
    points = existing_points | incoming_points
    exclusions = {
        timestamp: item
        for timestamp, item in household_load_exclusion_points(existing).items()
        if timestamp not in incoming_points
    } | household_load_exclusion_points(incoming)
    logger.debug(
        "event=persistence_merge component=storage operation=merge "
        "existing_count=%s incoming_count=%s merged_count=%s",
        len(existing_points),
        len(incoming_points),
        len(points),
    )
    ordered_points = sorted(points.items())[-HOUSEHOLD_LOAD_MAX_VALUES:]
    require_contiguous_hours(ordered_points, "household-load")
    return _with_points(incoming, ordered_points, exclusions)


def slice_household_load(
    data: HouseholdLoadData, start_time: datetime, end_time: datetime
) -> HouseholdLoadData | None:
    """Return hourly observations overlapping a half-open range."""
    points = household_load_points(data)
    exclusions = household_load_exclusion_points(data)
    selected = [
        (timestamp, value)
        for timestamp, value in sorted(points.items())
        if start_time <= timestamp < end_time
    ]
    return _with_points(data, selected, exclusions) if selected else None


def household_load_records(data: HouseholdLoadData) -> tuple[HouseholdLoadRecord, ...]:
    """Convert a validated model into self-contained NDJSON records."""
    points = household_load_points(data)
    exclusions = household_load_exclusion_points(data)
    source = _HouseholdLoadRecordSource(
        provider=data.source.provider, entity_id=data.source.entity_id
    )
    return tuple(
        HouseholdLoadRecord(
            timestamp=timestamp,
            load_kw=value,
            exclusion=exclusions.get(timestamp),
            schema_version=data.schema_version,
            unit=data.unit,
            source=source,
            retrieved_at=as_utc(data.retrieved_at),
            latest_observation_at=as_utc(data.latest_observation_at),
        )
        for timestamp, value in sorted(points.items())
    )


def encode_household_records(records: tuple[HouseholdLoadRecord, ...]) -> bytes:
    """Encode complete household-load records as newline-delimited JSON."""
    return b"".join(record.encode() for record in records)


def encode_household_load(data: HouseholdLoadData) -> bytes:
    """Encode a validated model as newline-delimited JSON records."""
    return encode_household_records(household_load_records(data))


def bounded_household_load(data: HouseholdLoadData) -> HouseholdLoadData:
    """Trim an incoming or migrated model to the retained hourly window."""
    all_points = sorted(household_load_points(data).items())
    require_contiguous_hours(all_points, "household-load")
    points = all_points[-HOUSEHOLD_LOAD_MAX_VALUES:]
    if len(points) == len(data.load_kw):
        return data
    return _with_points(data, points, household_load_exclusion_points(data))


def _looks_like_incomplete_household_record(content: bytes) -> bool:
    """Recognize a truncated JSON object without accepting arbitrary bad text."""
    stripped = content.strip()
    if not stripped.startswith(b"{"):
        return False
    stack: list[int] = []
    in_string = False
    escaped = False
    pairs = {ord("}"): ord("{"), ord("]"): ord("[")}
    for byte in stripped:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
        elif byte == ord('"'):
            in_string = True
        elif byte in (ord("{"), ord("[")):
            stack.append(byte)
        elif byte in pairs:
            if not stack or stack.pop() != pairs[byte]:
                return False
    return in_string or bool(stack)


def parse_household_history(payload: bytes, path: Path) -> HouseholdLoadHistory:
    """Parse NDJSON and reconstruct the latest contiguous retained history."""
    records: list[HouseholdLoadRecord] = []
    ignored_incomplete_final_line = False
    lines = payload.splitlines(keepends=True)
    if not lines:
        raise ProviderDataStoreError(f"household-load history is empty: {path}")

    for raw_line in lines:
        content = raw_line.rstrip(b"\r\n")
        # Only the final line can lack a line ending.
        is_unterminated = not raw_line.endswith((b"\n", b"\r"))
        if not content.strip():
            if is_unterminated:
                ignored_incomplete_final_line = True
                break
            raise ProviderDataStoreError(
                f"household-load history contains an empty record: {path}"
            )
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, ValueError) as error:
            if is_unterminated and _looks_like_incomplete_household_record(content):
                ignored_incomplete_final_line = True
                break
            raise ProviderDataStoreError(
                f"household-load history contains malformed JSON: {path}"
            ) from error
        try:
            records.append(HouseholdLoadRecord.decode(value))
        except (OverflowError, TypeError, ValueError, KeyError) as error:
            raise ProviderDataStoreError(
                f"household-load history contains an invalid record: {path}"
            ) from error

    if not records:
        raise ProviderDataStoreError(
            f"household-load history contains no complete records: {path}"
        )
    if any(record.source != records[0].source for record in records):
        raise ProviderDataStoreError(
            "household-load history contains inconsistent source identity"
        )

    # The latest record of an hour defines both its value and its exclusion.
    latest_by_hour = {record.timestamp: record for record in records}
    ordered = sorted(latest_by_hour.items())[-HOUSEHOLD_LOAD_MAX_VALUES:]
    require_contiguous_hours(ordered, "household-load")
    latest = records[-1]
    model = HouseholdLoadData(
        schema_version=latest.schema_version,
        start_time=ordered[0][0],
        interval_minutes=60,
        load_kw=tuple(record.load_kw for _, record in ordered),
        unit=latest.unit,
        source=SourceMetadata(latest.source.provider, latest.source.entity_id),
        retrieved_at=latest.retrieved_at,
        latest_observation_at=latest.latest_observation_at,
        exclusions=tuple(
            record.exclusion for _, record in ordered if record.exclusion is not None
        ),
    )
    return HouseholdLoadHistory(
        model=model,
        record_count=len(records),
        ignored_incomplete_final_line=ignored_incomplete_final_line,
    )


def household_load_points(data: HouseholdLoadData) -> dict[datetime, float | None]:
    """Validate and index one normalized hourly household-load series.

    An hour without a value must be explained by exactly one exclusion, and an
    exclusion must belong to an hour without a value.
    """
    start_time = as_utc(data.start_time)
    if start_time.minute or start_time.second or start_time.microsecond:
        raise ProviderDataStoreError(
            "household-load start_time must be aligned to the UTC hour"
        )
    if data.interval_minutes != 60 or data.unit != "kW":
        raise ProviderDataStoreError("household-load data must use hourly kW values")
    as_utc(data.retrieved_at)
    as_utc(data.latest_observation_at)
    if not data.load_kw:
        raise ProviderDataStoreError("household-load history must not be empty")
    points: dict[datetime, float | None] = {}
    for index, value in enumerate(data.load_kw):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ProviderDataStoreError(
                "household-load values must be finite and non-negative"
            )
        points[start_time + (index * _HOUR)] = None if value is None else float(value)
    excluded = {timestamp for timestamp, value in points.items() if value is None}
    if excluded != set(household_load_exclusion_points(data)):
        raise ProviderDataStoreError(
            "household-load hours without a value must match their exclusions"
        )
    return points


def household_load_exclusion_points(
    data: HouseholdLoadData,
) -> dict[datetime, HourExclusion]:
    """Index the exclusions of one series by their UTC hour."""
    return index_exclusions(data.exclusions, "household-load")


def index_exclusions(
    exclusions: Iterable[HourExclusion], label: str
) -> dict[datetime, HourExclusion]:
    """Index the exclusions of a ``label`` history by their UTC hour."""
    indexed: dict[datetime, HourExclusion] = {}
    for item in exclusions:
        timestamp = as_utc(item.hour_start)
        if timestamp in indexed:
            raise ProviderDataStoreError(
                f"{label} history has two exclusions for one hour"
            )
        indexed[timestamp] = item
    return indexed


def require_contiguous_hours(
    points: Sequence[tuple[datetime, object]], label: str
) -> None:
    """Reject ordered hourly points of a ``label`` history that skip an hour."""
    if any(later - earlier != _HOUR for (earlier, _), (later, _) in pairwise(points)):
        raise ProviderDataStoreError(
            f"{label} history must contain contiguous hourly timestamps"
        )


def as_utc(value: datetime) -> datetime:
    """Return a timezone-aware timestamp in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderDataStoreError(
            "household-load timestamps must include a timezone"
        )
    return value.astimezone(timezone.utc)
