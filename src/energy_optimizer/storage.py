"""Durable storage for validated normalized provider data."""

from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    HouseholdLoadData,
)


class ProviderDataStoreError(RuntimeError):
    """Raised when normalized provider data cannot be stored or recovered."""


logger = logging.getLogger(__name__)


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


class ProviderDataStore:
    """Store normalized provider models as individually replaceable JSON files.

    Each primary file contains the normalized model directly. A backup contains
    the previous valid value, or the initial value after the first write. Both
    files are validated when read so a damaged primary can be recovered safely.
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

        if isinstance(model, HouseholdLoadData):
            _household_load_points(model)
            existing = self.load(key, adapter)
            if isinstance(existing, HouseholdLoadData):
                model = cast(
                    ModelT,
                    merge_household_load_history(existing, model),
                )

        payload = adapter.dump_json(model)
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

    def load(
        self,
        key: ProviderDataKey,
        adapter: TypeAdapter[ModelT],
    ) -> ModelT | None:
        """Load and validate a normalized model, recovering a damaged primary."""
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
        return (
            self.directory / f"{filename}.json",
            self.directory / f"{filename}.json.bak",
        )

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
