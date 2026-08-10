"""Durable storage for validated normalized provider data."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError


class ProviderDataStoreError(RuntimeError):
    """Raised when normalized provider data cannot be stored or recovered."""


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
        try:
            model = adapter.validate_python(data)
        except ValidationError as error:
            raise ProviderDataStoreError(
                "normalized provider data failed validation before storage"
            ) from error

        payload = adapter.dump_json(model)
        primary_path, backup_path = self._paths(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderDataStoreError(
                f"could not create normalized provider-data directory "
                f"{self.directory}: {error}"
            ) from error

        current = self._read_valid(primary_path, adapter)
        backup = self._read_valid(backup_path, adapter)
        if current is not None:
            self._atomic_write(backup_path, current[0])
        elif backup is None:
            self._atomic_write(backup_path, payload)
        self._atomic_write(primary_path, payload)
        return model

    def load(
        self,
        key: ProviderDataKey,
        adapter: TypeAdapter[ModelT],
    ) -> ModelT | None:
        """Load and validate a normalized model, recovering a damaged primary."""
        primary_path, backup_path = self._paths(key)
        primary = self._read_valid(primary_path, adapter)
        if primary is not None:
            return primary[1]

        backup = self._read_valid(backup_path, adapter)
        if backup is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._atomic_write(primary_path, backup[0])
            return backup[1]

        if not primary_path.exists() and not backup_path.exists():
            return None
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
