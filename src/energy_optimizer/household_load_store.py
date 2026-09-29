"""Append-friendly durable storage for normalized household-load data."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Protocol

from pydantic import TypeAdapter, ValidationError

from energy_optimizer.household_load_records import (
    HouseholdLoadHistory,
    HouseholdLoadRecord,
    as_utc,
    bounded_household_load,
    encode_household_records,
    household_load_points,
    household_load_quality_points,
    household_load_records,
    merge_household_load_history,
    parse_household_history,
    quality_at,
    quality_slice,
    slice_household_load,
)
from energy_optimizer.providers.interfaces import (
    HouseholdLoadData,
)
from energy_optimizer.storage_errors import ProviderDataStoreError

logger = logging.getLogger(__name__)


class ProviderDataKeyLike(Protocol):
    """The provider-key behavior required by the household-load store."""

    @property
    def data_type(self) -> str:
        """Return the provider data type."""

    @property
    def provider(self) -> str:
        """Return the provider name."""

    @property
    def entity_id(self) -> str | None:
        """Return the optional provider entity identifier."""

    def digest(self) -> str:
        """Return the stable identity digest used in storage filenames."""


AtomicWrite = Callable[[Path, bytes], None]
Append = Callable[[Path, bytes], None]
CompactionThreshold = Callable[[], int]


class HouseholdLoadStore:
    """Persist household-load history independently from generic JSON storage."""

    def __init__(
        self,
        directory: Path,
        *,
        atomic_write: AtomicWrite,
        append: Append,
        compaction_threshold: CompactionThreshold,
    ) -> None:
        self.directory = directory
        self._atomic_write = atomic_write
        self._append = append
        self._compaction_threshold = compaction_threshold

    def save(
        self,
        key: ProviderDataKeyLike,
        incoming: HouseholdLoadData,
    ) -> HouseholdLoadData:
        """Append household-load observations and compact when necessary."""
        incoming_records = household_load_records(incoming)
        primary_path, backup_path = self._paths(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not create normalized provider-data directory "
                f"{self.directory}: {error}"
            ) from error

        history = self.load_history(key)
        if history is None:
            payload = encode_household_records(incoming_records)
            self._atomic_write(backup_path, payload)
            self._atomic_write(primary_path, payload)
            merged = incoming
        else:
            if history.model.source != incoming.source:
                raise ProviderDataStoreError(
                    "household-load history source identity does not match "
                    "incoming data"
                )
            merged = merge_household_load_history(history.model, incoming)
            if history.ignored_incomplete_final_line:
                self.compact(key, history.model, history.model)
                history = HouseholdLoadHistory(
                    model=history.model,
                    record_count=len(household_load_points(history.model)),
                )
            backup, _ = self._read_household_file(backup_path)
            if backup is None:
                self._atomic_write(
                    backup_path,
                    encode_household_records(household_load_records(history.model)),
                )
            incoming_payload = encode_household_records(incoming_records)
            self._append(primary_path, incoming_payload)
            if history.record_count + len(incoming_records) > (
                self._compaction_threshold()
            ):
                self.compact(key, history.model, merged)

        return merged

    def compact(
        self,
        key: ProviderDataKeyLike,
        previous: HouseholdLoadData,
        current: HouseholdLoadData,
    ) -> None:
        """Replace an NDJSON history with its bounded latest observations."""
        primary_path, backup_path = self._paths(key)
        previous_payload = encode_household_records(household_load_records(previous))
        current_payload = encode_household_records(household_load_records(current))
        self._atomic_write(backup_path, previous_payload)
        self._atomic_write(primary_path, current_payload)

    def load(self, key: ProviderDataKeyLike) -> HouseholdLoadData | None:
        """Load household-load history, recovering it or migrating legacy JSON."""
        history = self.load_history(key)
        return history.model if history is not None else None

    def load_range(
        self,
        key: ProviderDataKeyLike,
        start_time: datetime,
        end_time: datetime,
    ) -> HouseholdLoadData | None:
        """Load retained household-load points within a half-open range."""
        start = as_utc(start_time)
        end = as_utc(end_time)
        if end <= start:
            raise ProviderDataStoreError("household-load range must be non-empty")

        history = self.load_history(key)
        if history is None:
            return None
        return slice_household_load(history.model, start, end)

    def load_history(self, key: ProviderDataKeyLike) -> HouseholdLoadHistory | None:
        """Read NDJSON, recover its backup, or migrate legacy JSON."""
        started_at = perf_counter()
        primary_path, backup_path = self._paths(key)
        primary, primary_exists = self._read_household_file(primary_path)
        if primary is not None:
            return primary

        backup, backup_exists = self._read_household_file(backup_path)
        if backup is not None:
            logger.warning(
                "event=persistence_recovered component=storage operation=load "
                "data_type=%s provider=%s entity_id=%s source=backup "
                "record_count=%s duration_seconds=%.3f",
                key.data_type,
                key.provider,
                key.entity_id,
                backup.record_count,
                perf_counter() - started_at,
            )
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._atomic_write(
                    primary_path,
                    encode_household_records(household_load_records(backup.model)),
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

    def _paths(self, key: ProviderDataKeyLike) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}"
        return (
            self.directory / f"{filename}.ndjson",
            self.directory / f"{filename}.ndjson.bak",
        )

    def _legacy_paths(self, key: ProviderDataKeyLike) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}"
        return (
            self.directory / f"{filename}.json",
            self.directory / f"{filename}.json.bak",
        )

    def _read_household_file(
        self,
        path: Path,
    ) -> tuple[HouseholdLoadHistory | None, bool]:
        """Read one NDJSON file, tolerating only a truncated final line."""
        read_started_at = perf_counter()
        logger.debug(
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
            history = parse_household_history(payload, path)
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
        logger.debug(
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
        key: ProviderDataKeyLike,
    ) -> HouseholdLoadHistory | None:
        """Migrate the old monolithic JSON primary and backup files."""
        migration_started_at = perf_counter()
        legacy_primary_path, legacy_backup_path = self._legacy_paths(key)
        # A store without any data reaches this method on every read, so only a
        # real migration is announced; otherwise INFO output would repeat.
        if legacy_primary_path.exists() or legacy_backup_path.exists():
            logger.info(
                "event=persistence_migration_started component=storage "
                "operation=migrate data_type=%s provider=%s entity_id=%s",
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
            return None

        current_source = legacy_primary or legacy_backup
        assert current_source is not None
        current = bounded_household_load(current_source)
        backup = bounded_household_load(legacy_backup or current)
        primary_path, backup_path = self._paths(key)
        current_payload = encode_household_records(household_load_records(current))
        backup_payload = encode_household_records(household_load_records(backup))
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
        history = HouseholdLoadHistory(
            model=current,
            record_count=len(household_load_points(current)),
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
            return HouseholdLoadDataAdapter.validate_json(payload), True
        except ValueError, ValidationError:
            return None, True


HouseholdLoadDataAdapter = TypeAdapter(HouseholdLoadData)


__all__ = [
    "Append",
    "AtomicWrite",
    "CompactionThreshold",
    "HouseholdLoadDataAdapter",
    "HouseholdLoadHistory",
    "HouseholdLoadRecord",
    "HouseholdLoadStore",
    "ProviderDataKeyLike",
    "as_utc",
    "bounded_household_load",
    "encode_household_records",
    "household_load_points",
    "household_load_quality_points",
    "household_load_records",
    "merge_household_load_history",
    "parse_household_history",
    "quality_at",
    "quality_slice",
    "slice_household_load",
]
