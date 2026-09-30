"""Append-friendly durable storage for normalized household-load data."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from energy_optimizer.household_load_records import (
    HouseholdLoadHistory,
    as_utc,
    bounded_household_load,
    bridge_household_load_gap,
    encode_household_load,
    encode_household_records,
    household_load_points,
    household_load_records,
    merge_household_load_history,
    parse_household_history,
    slice_household_load,
)
from energy_optimizer.legacy_quality import upgrade_legacy_quality
from energy_optimizer.providers.interfaces import HouseholdLoadData
from energy_optimizer.storage_errors import ProviderDataStoreError

if TYPE_CHECKING:
    from energy_optimizer.storage import ProviderDataKey

logger = logging.getLogger(__name__)
_LEGACY_ADAPTER = TypeAdapter(HouseholdLoadData)


@dataclass(frozen=True)
class HouseholdLoadStore:
    """Persist household-load history independently from generic JSON storage."""

    directory: Path
    atomic_write: Callable[[Path, bytes], None]
    append: Callable[[Path, bytes], None]
    compaction_threshold: Callable[[], int]

    def save(
        self, key: ProviderDataKey, incoming: HouseholdLoadData
    ) -> HouseholdLoadData:
        """Append household-load observations and compact when necessary.

        Hours between the persisted history and the incoming range are appended
        as excluded hours too, so the file itself stays contiguous.
        """
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
            self.atomic_write(backup_path, payload)
            self.atomic_write(primary_path, payload)
            return incoming

        if history.model.source != incoming.source:
            raise ProviderDataStoreError(
                "household-load history source identity does not match incoming data"
            )
        bridged = bridge_household_load_gap(history.model, incoming)
        if bridged is not incoming:
            incoming_records = household_load_records(bridged)
        merged = merge_household_load_history(history.model, bridged)
        record_count = history.record_count
        if history.ignored_incomplete_final_line:
            self.compact(key, history.model, history.model)
            record_count = len(household_load_points(history.model))
        backup, _ = self._read_household_file(backup_path)
        if backup is None:
            self.atomic_write(backup_path, encode_household_load(history.model))
        self.append(primary_path, encode_household_records(incoming_records))
        if record_count + len(incoming_records) > self.compaction_threshold():
            self.compact(key, history.model, merged)
        return merged

    def compact(
        self,
        key: ProviderDataKey,
        previous: HouseholdLoadData,
        current: HouseholdLoadData,
    ) -> None:
        """Replace an NDJSON history with its bounded latest observations."""
        primary_path, backup_path = self._paths(key)
        previous_payload = encode_household_load(previous)
        current_payload = encode_household_load(current)
        self.atomic_write(backup_path, previous_payload)
        self.atomic_write(primary_path, current_payload)

    def load_range(
        self, key: ProviderDataKey, start_time: datetime, end_time: datetime
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

    def load_history(self, key: ProviderDataKey) -> HouseholdLoadHistory | None:
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
                self.atomic_write(primary_path, encode_household_load(backup.model))
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

        return self._migrate_legacy(key)

    def _paths(
        self, key: ProviderDataKey, extension: str = ".ndjson"
    ) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}{extension}"
        return self.directory / filename, self.directory / f"{filename}.bak"

    def _read_household_file(
        self, path: Path
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

    def _migrate_legacy(self, key: ProviderDataKey) -> HouseholdLoadHistory | None:
        """Migrate the old monolithic JSON primary and backup files."""
        migration_started_at = perf_counter()
        legacy_primary_path, legacy_backup_path = self._paths(key, ".json")
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
        legacy_primary, primary_exists = self._read_legacy_file(legacy_primary_path)
        legacy_backup, backup_exists = self._read_legacy_file(legacy_backup_path)
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
        current_payload = encode_household_load(current)
        backup_payload = encode_household_load(backup)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.atomic_write(backup_path, backup_payload)
            self.atomic_write(primary_path, current_payload)
            for path in (legacy_primary_path, legacy_backup_path):
                path.unlink(missing_ok=True)
        except (OSError, ProviderDataStoreError) as error:
            raise ProviderDataStoreError(
                f"could not migrate normalized provider data for {key.data_type}/"
                f"{key.provider}/{key.entity_id or 'default'}: {error}"
            ) from error
        record_count = len(household_load_points(current))
        logger.info(
            "event=persistence_migration_completed component=storage "
            "operation=migrate data_type=%s provider=%s entity_id=%s "
            "record_count=%s duration_seconds=%.3f",
            key.data_type,
            key.provider,
            key.entity_id,
            record_count,
            perf_counter() - migration_started_at,
        )
        return HouseholdLoadHistory(model=current, record_count=record_count)

    @staticmethod
    def _read_legacy_file(path: Path) -> tuple[HouseholdLoadData | None, bool]:
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
            return upgrade_legacy_quality(_LEGACY_ADAPTER.validate_json(payload)), True
        except ValueError, ValidationError:
            return None, True
