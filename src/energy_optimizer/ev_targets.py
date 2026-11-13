"""Persistent, dated minimum charge requirements for the configured vehicle."""

from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

TARGET_KEY = ProviderDataKey("ev-targets", "energy-optimizer", "current")
TARGET_LOCK = RLock()
MAX_ACTIVE_TARGETS = 100


class EvTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    deadline: datetime
    minimum_soc_percent: float = Field(ge=0, le=100)

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must include a time zone")
        return value.astimezone(UTC)


class EvTarget(EvTargetInput):
    id: UUID = Field(default_factory=uuid4)


class EvTargets(BaseModel):
    targets: tuple[EvTarget, ...] = ()


TARGET_ADAPTER = TypeAdapter(EvTargets)


def active_targets(store: ProviderDataStore, now: datetime) -> tuple[EvTarget, ...]:
    saved = store.load(TARGET_KEY, TARGET_ADAPTER) or EvTargets()
    return tuple(
        sorted(
            (target for target in saved.targets if target.deadline > now),
            key=lambda target: (target.deadline, str(target.id)),
        )
    )


def add_target(
    store: ProviderDataStore, target: EvTargetInput, now: datetime
) -> EvTarget:
    if target.deadline <= now:
        raise ValueError("deadline must be in the future")
    with TARGET_LOCK:
        targets = active_targets(store, now)
        if len(targets) >= MAX_ACTIVE_TARGETS:
            raise ValueError("at most 100 active EV requirements are allowed")
        created = EvTarget(**target.model_dump())
        store.save(TARGET_KEY, TARGET_ADAPTER, EvTargets(targets=(*targets, created)))
        return created


def remove_target(store: ProviderDataStore, target_id: UUID, now: datetime) -> bool:
    with TARGET_LOCK:
        targets = active_targets(store, now)
        remaining = tuple(target for target in targets if target.id != target_id)
        if len(remaining) == len(targets):
            return False
        store.save(TARGET_KEY, TARGET_ADAPTER, EvTargets(targets=remaining))
        return True


def target_hour(deadline: datetime, start: datetime, hours: int) -> int | None:
    """Complete charging by the hour boundary preceding a partial-hour deadline."""
    if deadline > start + timedelta(hours=hours):
        return None
    return max(0, int((deadline - start).total_seconds() // 3600))
