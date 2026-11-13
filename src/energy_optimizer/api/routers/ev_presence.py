"""HTTP management of dated vehicle presence overrides."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from energy_optimizer.ev_presence import (
    EvPresence,
    EvPresenceInput,
    active_presence,
    add_presence,
    remove_presence,
)
from energy_optimizer.storage import ProviderDataStore
from energy_optimizer.storage_errors import ProviderDataStoreError

router = APIRouter()


class EvPresenceResponse(BaseModel):
    overrides: tuple[EvPresence, ...]
    planning_status: str = "unchanged"
    detail: str | None = None


def _store(request: Request) -> ProviderDataStore:
    configuration = request.app.state.configuration
    if configuration.home_assistant is None or configuration.home_assistant.ev is None:
        raise HTTPException(503, "EV configuration is required")
    store: ProviderDataStore | None = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(503, "provider data persistence is not configured")
    return store


def _response(request: Request, *, replan: bool = False) -> EvPresenceResponse:
    now = datetime.now(UTC)
    entries = active_presence(_store(request), now)
    status, detail = "unchanged", None
    if replan:
        orchestrator = getattr(request.app.state, "orchestrator", None)
        if orchestrator is None:
            status, detail = (
                "unavailable",
                "Saved; scheduled optimization is not enabled.",
            )
        else:
            status, detail = orchestrator.replan(now)
    return EvPresenceResponse(overrides=entries, planning_status=status, detail=detail)


@router.get("/api/v1/ev/presence", response_model=EvPresenceResponse)
def list_presence(request: Request) -> EvPresenceResponse:
    try:
        return _response(request)
    except ProviderDataStoreError as error:
        raise HTTPException(503, "EV presence overrides could not be read") from error


@router.post("/api/v1/ev/presence", response_model=EvPresenceResponse, status_code=201)
def create_presence(entry: EvPresenceInput, request: Request) -> EvPresenceResponse:
    try:
        add_presence(_store(request), entry, datetime.now(UTC))
        return _response(request, replan=True)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except ProviderDataStoreError as error:
        raise HTTPException(
            503, "EV presence overrides could not be saved or read"
        ) from error


@router.delete("/api/v1/ev/presence/{entry_id}", response_model=EvPresenceResponse)
def delete_presence(entry_id: UUID, request: Request) -> EvPresenceResponse:
    try:
        if not remove_presence(_store(request), entry_id, datetime.now(UTC)):
            raise HTTPException(404, "active EV presence override not found")
        return _response(request, replan=True)
    except ProviderDataStoreError as error:
        raise HTTPException(
            503, "EV presence overrides could not be removed or read"
        ) from error
