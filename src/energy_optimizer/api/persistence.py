"""Shared persistence mapping for provider-backed API submissions."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter

from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)


def persist_provider_data(
    store: ProviderDataStore | None,
    key: ProviderDataKey,
    data: object,
    adapter: TypeAdapter[Any],
    *,
    data_label: str,
) -> object:
    """Persist provider data and translate storage failures to HTTP errors."""
    if store is None:
        raise RuntimeError("save_provider_data requires a configured store")
    try:
        persisted = store.save(key, adapter, data)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not persist {data_label} provider data: {error}",
        ) from error
    return persisted


def load_provider_data(
    store: ProviderDataStore | None,
    key: ProviderDataKey,
    adapter: TypeAdapter[Any],
    *,
    data_label: str,
) -> object:
    """Load one provider model or return a consistent API error."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    try:
        provider_data = store.load(key, adapter)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover {data_label} provider data: {error}",
        ) from error
    if provider_data is None:
        raise HTTPException(
            status_code=404,
            detail=f"no persisted {data_label} provider data is available",
        )
    return provider_data
