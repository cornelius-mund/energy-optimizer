"""Durable storage for validated normalized provider data."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import TypeVar, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    HouseholdLoadData,
    SourceMetadata,
)


class ProviderDataStoreError(RuntimeError):
    """Raised when normalized provider data cannot be stored or recovered."""


logger = logging.getLogger(__name__)
HOUSEHOLD_LOAD_COMPACTION_THRESHOLD = HOUSEHOLD_LOAD_MAX_VALUES + 1_024
_HOUSEHOLD_LOAD_RECORD_FIELDS = frozenset(
    {
        "latest_observation_at",
        "load_kw",
        "retrieved_at",
        "schema_version",
        "source",
        "timestamp",
        "unit",
    }
)


@dataclass(frozen=True)
class ProviderDataKey:
    """Identify one normalized data record without wrapping its stored payload."""

    data_type: str
    provider: str
    entity_id: str | None = None

    def digest(self) -> str:
        """Return a stable filename component for this provider identity."""
        identity = "\0".join(
            (self.data_type, self.provider, self.entity_id or "")
        ).encode()
        return hashlib.sha256(identity).hexdigest()


ModelT = TypeVar("ModelT")


@dataclass(frozen=True)
class _HouseholdLoadRecord:
    """One self-contained normalized household-load observation."""

    timestamp: datetime
    load_kw: float
    schema_version: str
    unit: str
    source: dict[str, str | None]
    retrieved_at: datetime
    latest_observation_at: datetime


@dataclass(frozen=True)
class _HouseholdLoadHistory:
    """Parsed household-load history and its physical record count."""

    model: HouseholdLoadData
    record_count: int
    ignored_incomplete_final_line: bool = False


class ProviderDataStore:
    """Store normalized provider models as individually replaceable files.

    Non-household-load models contain the normalized model directly as JSON.
    Household-load history uses append-friendly NDJSON and is compacted when
    duplicate or expired records exceed the configured physical threshold.
    Primary files and backups are validated when read so damaged data can be
    recovered safely.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(
        self,
        key: ProviderDataKey,
        adapter: TypeAdapter[ModelT],
        data: object,
    ) -> ModelT:
        """Validate and atomically save one normalized provider data model."""
        logger.debug(
            "event=persistence_save_started component=storage operation=save "
            "data_type=%s provider=%s entity_id=%s",
            key.data_type,
            key.provider,
            key.entity_id,
        )
        try:
            model = adapter.validate_python(data)
        except ValidationError as error:
            logger.error(
                "event=persistence_save_failed component=storage operation=save "
                "data_type=%s provider=%s entity_id=%s error_type=ValidationError",
                key.data_type,
                key.provider,
                key.entity_id,
            )
            raise ProviderDataStoreError(
                "normalized provider data failed validation before storage"
            ) from error

        if isinstance(model, HouseholdLoadData) and key.data_type == "household-load":
            return cast(
                ModelT,
                self._save_household_load(key, _bounded_household_load(model)),
            )

        payload = adapter.dump_json(model, indent=2) + b"\n"
        primary_path, backup_path = self._paths(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.error(
                "event=persistence_save_failed component=storage operation=save "
                "data_type=%s provider=%s entity_id=%s error_type=OSError",
                key.data_type,
                key.provider,
                key.entity_id,
            )
            raise ProviderDataStoreError(
                f"could not create normalized provider-data directory "
                f"{self.directory}: {error}"
            ) from error

        current = self._read_valid(primary_path, adapter)
        backup = self._read_valid(backup_path, adapter)
        if current is not None:
            try:
                self._atomic_write(backup_path, current[0])
            except ProviderDataStoreError:
                logger.error(
                    "event=persistence_save_failed component=storage operation=save "
                    "data_type=%s provider=%s entity_id=%s error_type=WriteError",
                    key.data_type,
                    key.provider,
                    key.entity_id,
                    exc_info=True,
                )
                raise
        elif backup is None:
            try:
                self._atomic_write(backup_path, payload)
            except ProviderDataStoreError:
                logger.error(
                    "event=persistence_save_failed component=storage operation=save "
                    "data_type=%s provider=%s entity_id=%s error_type=WriteError",
                    key.data_type,
                    key.provider,
                    key.entity_id,
                    exc_info=True,
                )
                raise
        try:
            self._atomic_write(primary_path, payload)
        except ProviderDataStoreError:
            logger.error(
                "event=persistence_save_failed component=storage operation=save "
                "data_type=%s provider=%s entity_id=%s error_type=WriteError",
                key.data_type,
                key.provider,
                key.entity_id,
                exc_info=True,
            )
            raise
        logger.info(
            "event=persistence_succeeded component=storage operation=save "
            "data_type=%s provider=%s entity_id=%s record_count=%s",
            key.data_type,
            key.provider,
            key.entity_id,
            len(model.load_kw) if isinstance(model, HouseholdLoadData) else "unknown",
        )
        return model

    def _save_household_load(
        self,
        key: ProviderDataKey,
        incoming: HouseholdLoadData,
    ) -> HouseholdLoadData:
        """Append household-load observations and compact when necessary."""
        incoming_records = _household_load_records(incoming)
        primary_path, backup_path = self._paths(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not create normalized provider-data directory "
                f"{self.directory}: {error}"
            ) from error

        history = self._load_household_history(key)
        if history is None:
            payload = _encode_household_records(incoming_records)
            try:
                self._atomic_write(backup_path, payload)
                self._atomic_write(primary_path, payload)
            except ProviderDataStoreError:
                logger.error(
                    "event=persistence_save_failed component=storage operation=save "
                    "data_type=%s provider=%s entity_id=%s error_type=WriteError",
                    key.data_type,
                    key.provider,
                    key.entity_id,
                    exc_info=True,
                )
                raise
            merged = incoming
        else:
            if history.model.source != incoming.source:
                raise ProviderDataStoreError(
                    "household-load history source identity does not match "
                    "incoming data"
                )
            merged = merge_household_load_history(history.model, incoming)
            if history.ignored_incomplete_final_line:
                self._compact_household_load(key, history.model, history.model)
                history = _HouseholdLoadHistory(
                    model=history.model,
                    record_count=len(_household_load_points(history.model)),
                )
            backup, _ = self._read_household_file(backup_path)
            if backup is None:
                self._atomic_write(
                    backup_path,
                    _encode_household_records(_household_load_records(history.model)),
                )
            incoming_payload = _encode_household_records(incoming_records)
            self._append(primary_path, incoming_payload)
            if history.record_count + len(incoming_records) > (
                HOUSEHOLD_LOAD_COMPACTION_THRESHOLD
            ):
                self._compact_household_load(key, history.model, merged)

        logger.info(
            "event=persistence_succeeded component=storage operation=save "
            "data_type=%s provider=%s entity_id=%s record_count=%s",
            key.data_type,
            key.provider,
            key.entity_id,
            len(merged.load_kw),
        )
        return merged

    def _compact_household_load(
        self,
        key: ProviderDataKey,
        previous: HouseholdLoadData,
        current: HouseholdLoadData,
    ) -> None:
        """Replace an NDJSON history with its bounded latest observations."""
        primary_path, backup_path = self._paths(key)
        previous_payload = _encode_household_records(_household_load_records(previous))
        current_payload = _encode_household_records(_household_load_records(current))
        self._atomic_write(backup_path, previous_payload)
        self._atomic_write(primary_path, current_payload)

    def load(
        self,
        key: ProviderDataKey,
        adapter: TypeAdapter[ModelT],
    ) -> ModelT | None:
        """Load and validate a normalized model, recovering a damaged primary."""
        if key.data_type == "household-load":
            load_started_at = perf_counter()
            logger.info(
                "event=persistence_load_started component=storage operation=load "
                "data_type=%s provider=%s entity_id=%s format=ndjson",
                key.data_type,
                key.provider,
                key.entity_id,
            )
            history = self._load_household_history(key)
            logger.info(
                "event=persistence_load_completed component=storage operation=load "
                "data_type=%s provider=%s entity_id=%s format=ndjson status=%s "
                "record_count=%s retained_count=%s duration_seconds=%.3f",
                key.data_type,
                key.provider,
                key.entity_id,
                "restored" if history is not None else "empty",
                history.record_count if history is not None else 0,
                len(history.model.load_kw) if history is not None else 0,
                perf_counter() - load_started_at,
            )
            return cast(ModelT | None, history.model if history is not None else None)

        logger.debug(
            "event=persistence_load_started component=storage operation=load "
            "data_type=%s provider=%s entity_id=%s",
            key.data_type,
            key.provider,
            key.entity_id,
        )
        primary_path, backup_path = self._paths(key)
        primary = self._read_valid(primary_path, adapter)
        if primary is not None:
            return primary[1]

        backup = self._read_valid(backup_path, adapter)
        if backup is not None:
            logger.warning(
                "event=persistence_recovered component=storage operation=load "
                "data_type=%s provider=%s entity_id=%s source=backup",
                key.data_type,
                key.provider,
                key.entity_id,
            )
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._atomic_write(primary_path, backup[0])
            except OSError, ProviderDataStoreError:
                logger.error(
                    "event=persistence_recovery_failed component=storage "
                    "operation=load "
                    "data_type=%s provider=%s entity_id=%s error_type=WriteError",
                    key.data_type,
                    key.provider,
                    key.entity_id,
                    exc_info=True,
                )
                raise
            return backup[1]

        if not primary_path.exists() and not backup_path.exists():
            return None
        logger.error(
            "event=persistence_recovery_failed component=storage operation=load "
            "data_type=%s provider=%s entity_id=%s error_type=InvalidData",
            key.data_type,
            key.provider,
            key.entity_id,
        )
        raise ProviderDataStoreError(
            f"normalized provider data is invalid and cannot be recovered for "
            f"{key.data_type}/{key.provider}/{key.entity_id or 'default'}"
        )

    def _paths(self, key: ProviderDataKey) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}"
        extension = ".ndjson" if key.data_type == "household-load" else ".json"
        return (
            self.directory / f"{filename}{extension}",
            self.directory / f"{filename}{extension}.bak",
        )

    def _legacy_paths(self, key: ProviderDataKey) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}"
        return (
            self.directory / f"{filename}.json",
            self.directory / f"{filename}.json.bak",
        )

    def _load_household_history(
        self,
        key: ProviderDataKey,
    ) -> _HouseholdLoadHistory | None:
        """Read NDJSON, recover its backup, or migrate legacy JSON."""
        primary_path, backup_path = self._paths(key)
        primary, primary_exists = self._read_household_file(primary_path)
        if primary is not None:
            return primary

        backup, backup_exists = self._read_household_file(backup_path)
        if backup is not None:
            logger.warning(
                "event=persistence_recovered component=storage operation=load "
                "data_type=%s provider=%s entity_id=%s source=backup",
                key.data_type,
                key.provider,
                key.entity_id,
            )
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._atomic_write(
                    primary_path,
                    _encode_household_records(_household_load_records(backup.model)),
                )
            except OSError, ProviderDataStoreError:
                logger.error(
                    "event=persistence_recovery_failed component=storage "
                    "operation=load data_type=%s provider=%s entity_id=%s "
                    "error_type=WriteError",
                    key.data_type,
                    key.provider,
                    key.entity_id,
                    exc_info=True,
                )
                raise
            return backup

        if primary_exists or backup_exists:
            raise ProviderDataStoreError(
                f"normalized provider data is invalid and cannot be recovered for "
                f"{key.data_type}/{key.provider}/{key.entity_id or 'default'}"
            )

        return self._migrate_legacy_household_load(key)

    def _read_household_file(
        self,
        path: Path,
    ) -> tuple[_HouseholdLoadHistory | None, bool]:
        """Read one NDJSON file, tolerating only a truncated final line."""
        read_started_at = perf_counter()
        logger.info(
            "event=persistence_ndjson_read_started component=storage operation=load "
            "path=%s",
            path.name,
        )
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None, False
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not read normalized provider data from {path}: {error}"
            ) from error

        try:
            history = _parse_household_history(payload, path)
        except ProviderDataStoreError as error:
            logger.warning(
                "event=persistence_ndjson_invalid component=storage operation=load "
                "path=%s error_type=%s file_size_bytes=%s duration_seconds=%.3f",
                path.name,
                error.__class__.__name__,
                len(payload),
                perf_counter() - read_started_at,
            )
            return None, True
        logger.info(
            "event=persistence_ndjson_parsed component=storage operation=load "
            "path=%s record_count=%s retained_count=%s "
            "ignored_incomplete_final_line=%s file_size_bytes=%s "
            "duration_seconds=%.3f",
            path.name,
            history.record_count,
            len(history.model.load_kw),
            history.ignored_incomplete_final_line,
            len(payload),
            perf_counter() - read_started_at,
        )
        return history, True

    def _migrate_legacy_household_load(
        self,
        key: ProviderDataKey,
    ) -> _HouseholdLoadHistory | None:
        """Migrate the old monolithic JSON primary and backup files."""
        migration_started_at = perf_counter()
        legacy_primary_path, legacy_backup_path = self._legacy_paths(key)
        logger.info(
            "event=persistence_migration_started component=storage operation=migrate "
            "data_type=%s provider=%s entity_id=%s",
            key.data_type,
            key.provider,
            key.entity_id,
        )
        legacy_primary, primary_exists = self._read_legacy_household_file(
            legacy_primary_path
        )
        legacy_backup, backup_exists = self._read_legacy_household_file(
            legacy_backup_path
        )
        if legacy_primary is None and legacy_backup is None:
            if primary_exists or backup_exists:
                raise ProviderDataStoreError(
                    f"normalized provider data is invalid and cannot be recovered for "
                    f"{key.data_type}/{key.provider}/{key.entity_id or 'default'}"
                )
            logger.info(
                "event=persistence_migration_completed component=storage "
                "operation=migrate data_type=%s provider=%s entity_id=%s "
                "status=not_needed record_count=0 duration_seconds=%.3f",
                key.data_type,
                key.provider,
                key.entity_id,
                perf_counter() - migration_started_at,
            )
            return None

        current_source = legacy_primary or legacy_backup
        assert current_source is not None
        current = _bounded_household_load(current_source)
        backup = _bounded_household_load(legacy_backup or current)
        primary_path, backup_path = self._paths(key)
        current_payload = _encode_household_records(_household_load_records(current))
        backup_payload = _encode_household_records(_household_load_records(backup))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._atomic_write(backup_path, backup_payload)
            self._atomic_write(primary_path, current_payload)
            for path in (legacy_primary_path, legacy_backup_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        except (OSError, ProviderDataStoreError) as error:
            raise ProviderDataStoreError(
                f"could not migrate normalized provider data for {key.data_type}/"
                f"{key.provider}/{key.entity_id or 'default'}: {error}"
            ) from error
        history = _HouseholdLoadHistory(
            model=current,
            record_count=len(_household_load_points(current)),
        )
        logger.info(
            "event=persistence_migration_completed component=storage "
            "operation=migrate data_type=%s provider=%s entity_id=%s "
            "record_count=%s duration_seconds=%.3f",
            key.data_type,
            key.provider,
            key.entity_id,
            history.record_count,
            perf_counter() - migration_started_at,
        )
        return history

    @staticmethod
    def _read_legacy_household_file(
        path: Path,
    ) -> tuple[HouseholdLoadData | None, bool]:
        """Read one old monolithic household-load JSON file."""
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None, False
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not read normalized provider data from {path}: {error}"
            ) from error
        try:
            return TypeAdapter(HouseholdLoadData).validate_json(payload), True
        except ValueError, ValidationError:
            return None, True

    @staticmethod
    def _read_valid(
        path: Path,
        adapter: TypeAdapter[ModelT],
    ) -> tuple[bytes, ModelT] | None:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            logger.error(
                "event=persistence_read_failed component=storage operation=read "
                "path=%s error_type=OSError",
                path,
            )
            raise ProviderDataStoreError(
                f"could not read normalized provider data from {path}: {error}"
            ) from error

        try:
            model = adapter.validate_json(payload)
        except ValueError, ValidationError:
            return None
        return payload, model

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary_path = self.directory / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temporary_path.open("xb") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise ProviderDataStoreError(
                f"could not atomically write normalized provider data to {path}: "
                f"{error}"
            ) from error

    @staticmethod
    def _append(path: Path, payload: bytes) -> None:
        """Append complete NDJSON records and sync them to durable storage."""
        try:
            needs_separator = path.exists() and path.stat().st_size > 0
            with path.open("ab") as history_file:
                if needs_separator:
                    with path.open("rb") as existing_file:
                        existing_file.seek(-1, os.SEEK_END)
                        if existing_file.read(1) not in (b"\n", b"\r"):
                            history_file.write(b"\n")
                history_file.write(payload)
                history_file.flush()
                os.fsync(history_file.fileno())
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not append normalized provider data to {path}: {error}"
            ) from error


def merge_household_load_history(
    existing: HouseholdLoadData,
    incoming: HouseholdLoadData,
) -> HouseholdLoadData:
    """Merge hourly household-load data, preferring the incoming values."""
    existing_points = _household_load_points(existing)
    incoming_points = _household_load_points(incoming)
    points = existing_points | incoming_points
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

    return HouseholdLoadData(
        schema_version=incoming.schema_version,
        start_time=timestamps[0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in ordered_points),
        unit=incoming.unit,
        source=incoming.source,
        retrieved_at=_as_utc(incoming.retrieved_at),
        latest_observation_at=_as_utc(incoming.latest_observation_at),
    )


def _household_load_records(
    data: HouseholdLoadData,
) -> tuple[_HouseholdLoadRecord, ...]:
    """Convert a validated model into self-contained NDJSON records."""
    points = _household_load_points(data)
    source = {
        "provider": data.source.provider,
        "entity_id": data.source.entity_id,
    }
    return tuple(
        _HouseholdLoadRecord(
            timestamp=timestamp,
            load_kw=value,
            schema_version=data.schema_version,
            unit=data.unit,
            source=source,
            retrieved_at=_as_utc(data.retrieved_at),
            latest_observation_at=_as_utc(data.latest_observation_at),
        )
        for timestamp, value in sorted(points.items())
    )


def _encode_household_records(
    records: tuple[_HouseholdLoadRecord, ...],
) -> bytes:
    """Encode complete household-load records as newline-delimited JSON."""
    return b"".join(
        json.dumps(
            {
                "timestamp": record.timestamp.isoformat(),
                "load_kw": record.load_kw,
                "schema_version": record.schema_version,
                "unit": record.unit,
                "source": record.source,
                "retrieved_at": record.retrieved_at.isoformat(),
                "latest_observation_at": record.latest_observation_at.isoformat(),
            },
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def _bounded_household_load(data: HouseholdLoadData) -> HouseholdLoadData:
    """Trim an incoming or migrated model to the retained hourly window."""
    all_points = sorted(_household_load_points(data).items())
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
        retrieved_at=_as_utc(data.retrieved_at),
        latest_observation_at=_as_utc(data.latest_observation_at),
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


def _parse_household_history(
    payload: bytes,
    path: Path,
) -> _HouseholdLoadHistory:
    """Parse NDJSON and reconstruct the latest contiguous retained history."""
    records: list[_HouseholdLoadRecord] = []
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
            records.append(_parse_household_record(value))
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
    timestamps = [timestamp for timestamp, _ in ordered_points]
    if any(
        later - earlier != _HOUR for earlier, later in zip(timestamps, timestamps[1:])
    ):
        raise ProviderDataStoreError(
            "household-load history must contain contiguous hourly timestamps"
        )
    latest = records[-1]
    model = HouseholdLoadData(
        schema_version="1",
        start_time=timestamps[0],
        interval_minutes=60,
        load_kw=tuple(value for _, value in ordered_points),
        unit="kW",
        source=SourceMetadata(
            provider=first_source["provider"] or "",
            entity_id=first_source["entity_id"],
        ),
        retrieved_at=latest.retrieved_at,
        latest_observation_at=latest.latest_observation_at,
    )
    return _HouseholdLoadHistory(
        model=model,
        record_count=len(records),
        ignored_incomplete_final_line=ignored_incomplete_final_line,
    )


def _parse_household_record(value: object) -> _HouseholdLoadRecord:
    """Validate one decoded NDJSON household-load record."""
    if not isinstance(value, dict) or set(value) != _HOUSEHOLD_LOAD_RECORD_FIELDS:
        raise ValueError("record fields do not match the NDJSON contract")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"provider", "entity_id"}:
        raise ValueError("record source does not match the normalized contract")
    provider = source["provider"]
    entity_id = source["entity_id"]
    if not isinstance(provider, str) or not isinstance(entity_id, (str, type(None))):
        raise ValueError("record source values are invalid")
    load_kw = value["load_kw"]
    if isinstance(load_kw, bool) or not isinstance(load_kw, (int, float)):
        raise ValueError("record load is invalid")
    load_value = float(load_kw)
    if not math.isfinite(load_value) or load_value < 0:
        raise ValueError("record load is invalid")
    timestamp = _parse_record_timestamp(value["timestamp"], require_hour=True)
    retrieved_at = _parse_record_timestamp(value["retrieved_at"])
    latest_observation_at = _parse_record_timestamp(value["latest_observation_at"])
    if value["schema_version"] != "1" or value["unit"] != "kW":
        raise ValueError("record schema or unit is invalid")
    return _HouseholdLoadRecord(
        timestamp=timestamp,
        load_kw=load_value,
        schema_version="1",
        unit="kW",
        source={"provider": provider, "entity_id": entity_id},
        retrieved_at=retrieved_at,
        latest_observation_at=latest_observation_at,
    )


def _parse_record_timestamp(value: object, *, require_hour: bool = False) -> datetime:
    """Parse and normalize one persisted timestamp."""
    if not isinstance(value, str):
        raise ValueError("record timestamp is not a string")
    timestamp = _as_utc(datetime.fromisoformat(value))
    if require_hour and (timestamp.minute or timestamp.second or timestamp.microsecond):
        raise ValueError("record timestamp is not hourly aligned")
    return timestamp


def _household_load_points(data: HouseholdLoadData) -> dict[datetime, float]:
    """Validate and index one normalized hourly household-load series."""
    start_time = _as_utc(data.start_time)
    if start_time.minute or start_time.second or start_time.microsecond:
        raise ProviderDataStoreError(
            "household-load start_time must be aligned to the UTC hour"
        )
    if data.interval_minutes != 60 or data.unit != "kW":
        raise ProviderDataStoreError("household-load data must use hourly kW values")
    _as_utc(data.retrieved_at)
    _as_utc(data.latest_observation_at)
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


_HOUR = timedelta(hours=1)


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware timestamp in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderDataStoreError(
            "household-load timestamps must include a timezone"
        )
    return value.astimezone(timezone.utc)
