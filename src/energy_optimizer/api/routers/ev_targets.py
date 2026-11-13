"""Manage dated EV requirements and replan from already-collected inputs."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from energy_optimizer.ev_targets import (
    EvTarget,
    EvTargetInput,
    active_targets,
    add_target,
    remove_target,
)
from energy_optimizer.storage import ProviderDataStore
from energy_optimizer.storage_errors import ProviderDataStoreError

router = APIRouter()


class EvTargetResponse(BaseModel):
    targets: tuple[EvTarget, ...]
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


def _response(request: Request, *, replan: bool = False) -> EvTargetResponse:
    now = datetime.now(UTC)
    targets = active_targets(_store(request), now)
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
    return EvTargetResponse(targets=targets, planning_status=status, detail=detail)


@router.get("/api/v1/ev/targets", response_model=EvTargetResponse)
def list_targets(request: Request) -> EvTargetResponse:
    try:
        return _response(request)
    except ProviderDataStoreError as error:
        raise HTTPException(503, "EV requirements could not be read") from error


@router.post("/api/v1/ev/targets", response_model=EvTargetResponse, status_code=201)
def create_target(target: EvTargetInput, request: Request) -> EvTargetResponse:
    try:
        add_target(_store(request), target, datetime.now(UTC))
        return _response(request, replan=True)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except ProviderDataStoreError as error:
        raise HTTPException(
            503, "EV requirements could not be saved or read"
        ) from error


@router.delete("/api/v1/ev/targets/{target_id}", response_model=EvTargetResponse)
def delete_target(target_id: UUID, request: Request) -> EvTargetResponse:
    try:
        if not remove_target(_store(request), target_id, datetime.now(UTC)):
            raise HTTPException(404, "active EV requirement not found")
        return _response(request, replan=True)
    except ProviderDataStoreError as error:
        raise HTTPException(
            503, "EV requirements could not be removed or read"
        ) from error
