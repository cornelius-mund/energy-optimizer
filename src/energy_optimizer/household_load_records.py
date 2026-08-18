"""Validated household-load records and pure history transformations."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    HouseholdLoadData,
    IntervalQuality,
    SourceMetadata,
)
from energy_optimizer.storage_errors import ProviderDataStoreError

_HOUR = timedelta(hours=1)
logger = logging.getLogger(__name__)


class _HouseholdLoadRecordSource(BaseModel):
    """Source identity embedded in one persisted household-load record."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    entity_id: str | None


class _HouseholdLoadRecordQuality(BaseModel):
    """Quality metadata embedded in one persisted household-load record."""

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
    """Validated and serializable representation of one NDJSON record."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    load_kw: float
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
        """Parse persisted timestamps and normalize them to UTC."""
        return _normalize_record_timestamp(value)

    @field_validator("timestamp")
    @classmethod
    def validate_hourly_timestamp(cls, value: datetime) -> datetime:
        if value.minute or value.second or value.microsecond:
            raise ValueError("record timestamp is not hourly aligned")
        return value

    @field_validator("load_kw", mode="before")
    @classmethod
    def validate_load(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("record load is invalid")
        load_value = float(value)
        if not math.isfinite(load_value) or load_value < 0:
            raise ValueError("record load is invalid")
        return load_value

    @classmethod
    def decode(cls, value: object) -> HouseholdLoadRecord:
        """Validate one decoded JSON value, accepting legacy quality omission."""
        try:
            return cls.model_validate(value)
        except (TypeError, ValidationError) as error:
            raise ValueError("record does not match the NDJSON contract") from error

    def encode(self) -> bytes:
        """Encode this record using the established compact NDJSON format."""
        quality = self.quality or _HouseholdLoadRecordQuality(
            status="valid",
            reason=None,
            entity_id=None,
        )
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "load_kw": self.load_kw,
            "quality": {
                "status": quality.status,
                "reason": quality.reason,
                "entity_id": quality.entity_id,
            },
            "schema_version": self.schema_version,
            "unit": self.unit,
            "source": {
                "provider": self.source.provider,
                "entity_id": self.source.entity_id,
            },
            "retrieved_at": self.retrieved_at.isoformat(),
            "latest_observation_at": self.latest_observation_at.isoformat(),
        }
        return (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
            + b"\n"
        )

    def interval_quality(self) -> IntervalQuality:
        """Return domain quality metadata, including the valid legacy default."""
        quality = self.quality
        if quality is None:
            return IntervalQuality()
        return IntervalQuality(
            status=quality.status,
            reason=quality.reason,
            entity_id=quality.entity_id,
        )

    def source_metadata(self) -> SourceMetadata:
        """Return the embedded source identity as normalized domain metadata."""
        return SourceMetadata(
            provider=self.source.provider,
            entity_id=self.source.entity_id,
        )


@dataclass(frozen=True)
class HouseholdLoadHistory:
    """Parsed household-load history and its physical record count."""

    model: HouseholdLoadData
    record_count: int
    ignored_incomplete_final_line: bool = False


def merge_household_load_history(
    existing: HouseholdLoadData,
    incoming: HouseholdLoadData,
) -> HouseholdLoadData:
    """Merge hourly household-load data, preferring the incoming values."""
    existing_points = household_load_points(existing)
    incoming_points = household_load_points(incoming)
    existing_quality = household_load_quality_points(existing)
    incoming_quality = household_load_quality_points(incoming)
    points = existing_points | incoming_points
    quality_points = existing_quality | incoming_quality
    logger.debug(
        "event=persistence_merge component=storage operation=merge "
        "existing_count=%s incoming_count=%s merged_count=%s",
        len(existing_points),
        len(incoming_points),
        len(points),
    )
    ordered_points = sorted(points.items())
    if len(ordered_points) > HOUSEHOLD_LOAD_MAX_VALUES:
        ordered_points = ordered_points[-HOUSEHOLD_LOAD_MAX_VALUES:]

    timestamps = [timestamp for timestamp, _ in ordered_points]
    if not timestamps or any(
        later - earlier != _HOUR for earlier, later in zip(timestamps, timestamps[1:])
    ):
        raise ProviderDataStoreError(
            "household-load history must contain contiguous hourly timestamps"
        )

    ordered_quality = tuple(quality_points[timestamp] for timestamp in timestamps)
    return HouseholdLoadData(
        schema_version=incoming.schema_version,
        start_time=timestamps[0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in ordered_points),
        unit=incoming.unit,
        source=incoming.source,
        retrieved_at=as_utc(incoming.retrieved_at),
        latest_observation_at=as_utc(incoming.latest_observation_at),
        quality=(
            ordered_quality
            if any(item.status == "suspect" for item in ordered_quality)
            else ()
        ),
    )


def slice_household_load(
    data: HouseholdLoadData,
    start_time: datetime,
    end_time: datetime,
) -> HouseholdLoadData | None:
    """Return hourly observations overlapping a half-open range."""
    points = household_load_points(data)
    selected = [
        (timestamp, value)
        for timestamp, value in sorted(points.items())
        if start_time <= timestamp < end_time
    ]
    if not selected:
        return None
    return HouseholdLoadData(
        schema_version=data.schema_version,
        start_time=selected[0][0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in selected),
        unit=data.unit,
        source=data.source,
        retrieved_at=as_utc(data.retrieved_at),
        latest_observation_at=as_utc(data.latest_observation_at),
        quality=quality_slice(
            data.quality, selected[0][0], data.start_time, len(selected)
        ),
    )


def household_load_records(
    data: HouseholdLoadData,
) -> tuple[HouseholdLoadRecord, ...]:
    """Convert a validated model into self-contained NDJSON records."""
    points = household_load_points(data)
    source = {
        "provider": data.source.provider,
        "entity_id": data.source.entity_id,
    }
    return tuple(
        HouseholdLoadRecord(
            timestamp=timestamp,
            load_kw=value,
            schema_version=data.schema_version,
            unit=data.unit,
            source=_HouseholdLoadRecordSource.model_validate(source),
            retrieved_at=as_utc(data.retrieved_at),
            latest_observation_at=as_utc(data.latest_observation_at),
            quality=_HouseholdLoadRecordQuality(
                status=quality.status,
                reason=quality.reason,
                entity_id=quality.entity_id,
            ),
        )
        for timestamp, value in sorted(points.items())
        for quality in (quality_at(data.quality, timestamp, data.start_time),)
    )


def encode_household_records(records: tuple[HouseholdLoadRecord, ...]) -> bytes:
    """Encode complete household-load records as newline-delimited JSON."""
    return b"".join(record.encode() for record in records)


def bounded_household_load(data: HouseholdLoadData) -> HouseholdLoadData:
    """Trim an incoming or migrated model to the retained hourly window."""
    all_points = sorted(household_load_points(data).items())
    all_timestamps = [timestamp for timestamp, _ in all_points]
    if any(
        later - earlier != _HOUR
        for earlier, later in zip(all_timestamps, all_timestamps[1:])
    ):
        raise ProviderDataStoreError(
            "household-load history must contain contiguous hourly timestamps"
        )
    points = all_points[-HOUSEHOLD_LOAD_MAX_VALUES:]
    timestamps = [timestamp for timestamp, _ in points]
    if len(points) == len(data.load_kw):
        return data
    return HouseholdLoadData(
        schema_version=data.schema_version,
        start_time=timestamps[0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in points),
        unit=data.unit,
        source=data.source,
        retrieved_at=as_utc(data.retrieved_at),
        latest_observation_at=as_utc(data.latest_observation_at),
        quality=(
            tuple(
                quality_at(data.quality, timestamp, data.start_time)
                for timestamp, _ in points
            )
            if any(
                quality_at(data.quality, timestamp, data.start_time).status == "suspect"
                for timestamp, _ in points
            )
            else ()
        ),
    )


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
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in (ord("{"), ord("[")):
            stack.append(byte)
        elif byte in pairs:
            if not stack or stack.pop() != pairs[byte]:
                return False
    return in_string or bool(stack)


def parse_household_history(
    payload: bytes,
    path: Path,
) -> HouseholdLoadHistory:
    """Parse NDJSON and reconstruct the latest contiguous retained history."""
    records: list[HouseholdLoadRecord] = []
    ignored_incomplete_final_line = False
    lines = payload.splitlines(keepends=True)
    if not lines:
        raise ProviderDataStoreError(f"household-load history is empty: {path}")

    for index, raw_line in enumerate(lines):
        content = raw_line.rstrip(b"\r\n")
        is_final_line = index == len(lines) - 1
        has_line_ending = raw_line.endswith((b"\n", b"\r"))
        if not content.strip():
            if is_final_line and not has_line_ending:
                ignored_incomplete_final_line = True
                break
            raise ProviderDataStoreError(
                f"household-load history contains an empty record: {path}"
            )
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, ValueError) as error:
            if (
                is_final_line
                and not has_line_ending
                and _looks_like_incomplete_household_record(content)
            ):
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
    first_source = records[0].source
    if any(record.source != first_source for record in records[1:]):
        raise ProviderDataStoreError(
            "household-load history contains inconsistent source identity"
        )

    points: dict[datetime, float] = {}
    for record in records:
        points[record.timestamp] = record.load_kw
    ordered_points = sorted(points.items())[-HOUSEHOLD_LOAD_MAX_VALUES:]
    records_by_timestamp = {record.timestamp: record for record in records}
    ordered_records = [
        records_by_timestamp[timestamp] for timestamp, _ in ordered_points
    ]
    timestamps = [timestamp for timestamp, _ in ordered_points]
    if any(
        later - earlier != _HOUR for earlier, later in zip(timestamps, timestamps[1:])
    ):
        raise ProviderDataStoreError(
            "household-load history must contain contiguous hourly timestamps"
        )
    latest = records[-1]
    model = HouseholdLoadData(
        schema_version=latest.schema_version,
        start_time=timestamps[0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in ordered_points),
        unit=latest.unit,
        source=latest.source_metadata(),
        retrieved_at=latest.retrieved_at,
        latest_observation_at=latest.latest_observation_at,
        quality=(
            tuple(record.interval_quality() for record in ordered_records)
            if any(
                record.interval_quality().status == "suspect"
                for record in ordered_records
            )
            else ()
        ),
    )
    return HouseholdLoadHistory(
        model=model,
        record_count=len(records),
        ignored_incomplete_final_line=ignored_incomplete_final_line,
    )


def household_load_points(data: HouseholdLoadData) -> dict[datetime, float]:
    """Validate and index one normalized hourly household-load series."""
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
    points: dict[datetime, float] = {}
    for index, value in enumerate(data.load_kw):
        if not math.isfinite(value) or value < 0:
            raise ProviderDataStoreError(
                "household-load values must be finite and non-negative"
            )
        points[start_time + (index * _HOUR)] = float(value)
    return points


def household_load_quality_points(
    data: HouseholdLoadData,
) -> dict[datetime, IntervalQuality]:
    """Index interval quality, treating omitted metadata as valid."""
    points = household_load_points(data)
    return {
        timestamp: quality_at(data.quality, timestamp, data.start_time)
        for timestamp in points
    }


def quality_at(
    quality: tuple[IntervalQuality, ...],
    timestamp: datetime,
    start_time: datetime,
) -> IntervalQuality:
    """Return the quality for one hourly point."""
    if not quality:
        return IntervalQuality()
    index = int((timestamp - as_utc(start_time)).total_seconds() // 3600)
    if index < 0 or index >= len(quality):
        raise ProviderDataStoreError("household-load quality is misaligned")
    return quality[index]


def quality_slice(
    quality: tuple[IntervalQuality, ...],
    start_time: datetime,
    data_start: datetime,
    count: int,
) -> tuple[IntervalQuality, ...]:
    """Slice aligned quality metadata while preserving the valid default."""
    if not quality:
        return ()
    offset = int((as_utc(start_time) - as_utc(data_start)).total_seconds() // 3600)
    selected = quality[offset : offset + count]
    if len(selected) != count:
        raise ProviderDataStoreError("household-load quality is misaligned")
    return tuple(selected)


def as_utc(value: datetime) -> datetime:
    """Return a timezone-aware timestamp in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderDataStoreError(
            "household-load timestamps must include a timezone"
        )
    return value.astimezone(timezone.utc)
