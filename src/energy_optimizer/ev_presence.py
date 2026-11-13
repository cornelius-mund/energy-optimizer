"""Persistent owner overrides for hourly EV charging availability."""

from datetime import UTC, datetime
from threading import RLock
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

PRESENCE_KEY = ProviderDataKey("ev-presence", "energy-optimizer", "current")
PRESENCE_LOCK = RLock()


class EvPresenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_time: datetime
    end_time: datetime
    present: bool = Field(strict=True)

    @field_validator("start_time", "end_time")
    @classmethod
    def whole_utc_hour(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("presence times must include a time zone")
        value = value.astimezone(UTC)
        if value.minute or value.second or value.microsecond:
            raise ValueError("presence times must be aligned to whole UTC hours")
        return value

    @model_validator(mode="after")
    def ordered_interval(self) -> "EvPresenceInput":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self


class EvPresence(EvPresenceInput):
    id: UUID = Field(default_factory=uuid4)


class EvPresenceList(BaseModel):
    overrides: tuple[EvPresence, ...] = ()


PRESENCE_ADAPTER = TypeAdapter(EvPresenceList)


def active_presence(store: ProviderDataStore, now: datetime) -> tuple[EvPresence, ...]:
    saved = store.load(PRESENCE_KEY, PRESENCE_ADAPTER) or EvPresenceList()
    return tuple(
        sorted(
            (item for item in saved.overrides if item.end_time > now),
            key=lambda item: (item.start_time, str(item.id)),
        )
    )


def add_presence(
    store: ProviderDataStore, entry: EvPresenceInput, now: datetime
) -> EvPresence:
    if entry.end_time <= now:
        raise ValueError("the override must end in the future")
    with PRESENCE_LOCK:
        pending = active_presence(store, now)
        if len(pending) >= 100:
            raise ValueError("at most 100 active presence overrides are allowed")
        if any(
            entry.start_time < item.end_time and item.start_time < entry.end_time
            for item in pending
        ):
            raise ValueError(
                "overlapping overrides are not allowed; "
                "remove or split the existing interval"
            )
        created = EvPresence(**entry.model_dump())
        store.save(
            PRESENCE_KEY,
            PRESENCE_ADAPTER,
            EvPresenceList(overrides=(*pending, created)),
        )
        return created


def remove_presence(store: ProviderDataStore, entry_id: UUID, now: datetime) -> bool:
    with PRESENCE_LOCK:
        pending = active_presence(store, now)
        remaining = tuple(item for item in pending if item.id != entry_id)
        if len(pending) == len(remaining):
            return False
        store.save(PRESENCE_KEY, PRESENCE_ADAPTER, EvPresenceList(overrides=remaining))
        return True


def overridden_availability(
    learned: tuple[bool, ...], hours: list[datetime], entries: tuple[EvPresence, ...]
) -> tuple[bool, ...]:
    """Override availability only; preserve independently learned driving energy."""
    return tuple(
        next(
            (
                entry.present
                for entry in entries
                if entry.start_time <= hour < entry.end_time
            ),
            available,
        )
        for hour, available in zip(hours, learned, strict=True)
    )
