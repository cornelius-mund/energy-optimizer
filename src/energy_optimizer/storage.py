"""Durable storage for validated normalized provider data."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from energy_optimizer.household_load_store import (
    HouseholdLoadHistory,
    HouseholdLoadStore,
    bounded_household_load,
    encode_household_records,
    household_load_records,
    merge_household_load_history,
)
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    HouseholdLoadData,
)
from energy_optimizer.storage_errors import ProviderDataStoreError

logger = logging.getLogger(__name__)
HOUSEHOLD_LOAD_COMPACTION_THRESHOLD = HOUSEHOLD_LOAD_MAX_VALUES + 1_024


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

# Keep the old private test hooks available while the implementation lives in the
# focused household-load module.
_household_load_records = household_load_records
_encode_household_records = encode_household_records


class ProviderDataStore:
    """Store normalized provider models as individually replaceable files.

    Non-household-load models contain the normalized model directly as JSON.
    Household-load history is delegated to its append-friendly NDJSON store.
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
                self._save_household_load(key, bounded_household_load(model)),
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
        """Delegate household-load persistence to its focused store."""
        merged = self._household_load_store().save(key, incoming)
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
        """Delegate household-load compaction to its focused store."""
        self._household_load_store().compact(key, previous, current)

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
        if key.data_type == "household-load":
            history = self._load_household_history(key)
            return cast(ModelT | None, history.model if history is not None else None)

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

    def load_household_load_range(
        self,
        key: ProviderDataKey,
        start_time: datetime,
        end_time: datetime,
    ) -> HouseholdLoadData | None:
        """Load retained household-load points within a half-open range."""
        if key.data_type != "household-load":
            raise ProviderDataStoreError(
                "household-load range queries require the household-load data type"
            )
        return self._household_load_store().load_range(key, start_time, end_time)

    def _household_load_store(self) -> HouseholdLoadStore:
        """Build a helper with the store's current atomic I/O methods."""
        return HouseholdLoadStore(
            self.directory,
            atomic_write=self._atomic_write,
            append=self._append,
            compaction_threshold=lambda: HOUSEHOLD_LOAD_COMPACTION_THRESHOLD,
        )

    def _paths(self, key: ProviderDataKey) -> tuple[Path, Path]:
        filename = f"{key.data_type}-{key.digest()}"
        extension = ".ndjson" if key.data_type == "household-load" else ".json"
        return (
            self.directory / f"{filename}{extension}",
            self.directory / f"{filename}{extension}.bak",
        )

    def _load_household_history(
        self,
        key: ProviderDataKey,
    ) -> HouseholdLoadHistory | None:
        """Delegate household-load recovery to its focused store."""
        return self._household_load_store().load_history(key)

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


__all__ = [
    "ProviderDataKey",
    "ProviderDataStore",
    "ProviderDataStoreError",
    "merge_household_load_history",
]
