"""The plug-in reminder against the fake Home Assistant behind real HTTP."""

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from energy_optimizer.config import (
    EvChargingConfiguration,
    EvHomeHoursConfiguration,
    EvPlugInReminderConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.control.ev_controller import (
    EV_SCHEDULE_ADAPTER,
    EV_SCHEDULE_KEY,
    EvSchedule,
)
from energy_optimizer.control.plug_in_reminder import (
    PlugInReminder,
    current_night,
    planned_night_charge,
)
from energy_optimizer.storage import ProviderDataStore
from fake_world import (
    EV_PLUG_ENTITY,
    EV_SOC_ENTITY,
    HOME_ASSISTANT_TOKEN,
    FakeWorld,
    running_fake_world,
)

BERLIN = ZoneInfo("Europe/Berlin")
NIGHT = EvHomeHoursConfiguration(start=time(22), end=time(6))
REMIND_AT = time(21)
ONE_HOUR = timedelta(hours=1)
MONDAY_EVENING = datetime(2026, 11, 9, 21, 30, tzinfo=BERLIN)
PHONES = ["notify.mobile_app_s24", "notify.mobile_app_sm_s921b"]


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=BERLIN)


@pytest.mark.parametrize(
    ("now", "night_of"),
    [
        (local(9, 21, 0), 9),
        (local(9, 23, 30), 9),
        (local(10, 2, 0), 9),
        (local(10, 5, 59), 9),
        (local(9, 20, 59), None),
        (local(10, 6, 0), None),
    ],
)
def test_the_night_runs_from_the_reminder_time_to_the_end_of_the_home_hours(
    now: datetime, night_of: int | None
) -> None:
    night = current_night(now.astimezone(UTC), NIGHT, REMIND_AT, BERLIN)

    assert (None if night is None else night.starts_on.day) == night_of


def _schedule(charging: dict[datetime, float]) -> EvSchedule:
    first = local(9, 18)
    starts = tuple(first + ONE_HOUR * index for index in range(16))
    return EvSchedule(
        updated_at=first.astimezone(UTC),
        hour_starts=tuple(start.astimezone(UTC) for start in starts),
        charge_kw=tuple(charging.get(start, 0.0) for start in starts),
    )


def test_the_energy_planned_for_the_rest_of_the_night_is_summed() -> None:
    schedule = _schedule({local(9, 19): 11.0, local(10, 1): 11.0, local(10, 3): 4.1})
    night = current_night(MONDAY_EVENING, NIGHT, REMIND_AT, BERLIN)
    assert night is not None

    charge = planned_night_charge(schedule, night, MONDAY_EVENING)

    assert charge is not None
    assert charge.energy_kwh == pytest.approx(15.1)
    assert charge.first_hour == local(10, 1)


def _home_assistant(world_url: str) -> HomeAssistantConfiguration:
    return HomeAssistantConfiguration.model_validate(
        {
            "base_url": world_url,
            "token": HOME_ASSISTANT_TOKEN,
            "timeout_seconds": 5,
            "ev": {
                "state_of_charge": {"entity_id": EV_SOC_ENTITY, "unit": "%"},
                "plugged_in": {"entity_id": EV_PLUG_ENTITY},
                "capacity_kwh": 60,
                "minimum_charge_kw": 1.4,
                "maximum_charge_kw": 11,
            },
        }
    )


SETTINGS = EvPlugInReminderConfiguration(
    services=PHONES, message="EV9 anstecken: {energy_kwh:.0f} kWh ab {start}"
)


@pytest.fixture
def world() -> Iterator[tuple[str, FakeWorld]]:
    with running_fake_world() as (url, fake):
        fake.states[EV_PLUG_ENTITY] = ("off", {})
        yield url, fake


def _reminder(
    world_url: str, store: ProviderDataStore, now: datetime = MONDAY_EVENING
) -> PlugInReminder:
    return PlugInReminder(
        _home_assistant(world_url),
        NIGHT,
        SETTINGS,
        BERLIN,
        store,
        clock=lambda: now.astimezone(UTC),
    )


def _notifications(world: FakeWorld) -> list[tuple[str, dict[str, Any]]]:
    return [call for call in world.service_calls if call[0].startswith("notify.")]


def test_an_unplugged_car_that_charges_tonight_is_reminded_once_per_phone(
    world: tuple[str, FakeWorld], tmp_path: Path
) -> None:
    world_url, fake = world
    store = ProviderDataStore(tmp_path)
    store.save(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER, _schedule({local(10, 1): 11.0}))

    assert _reminder(world_url, store).remind_due() is True
    assert _reminder(world_url, store, local(9, 23)).remind_due() is False

    notifications = _notifications(fake)
    assert [service for service, _ in notifications] == PHONES
    assert notifications[0][1] == {
        "title": "EV",
        "message": "EV9 anstecken: 11 kWh ab 01:00",
    }


def test_a_plugged_in_car_is_not_reminded(
    world: tuple[str, FakeWorld], tmp_path: Path
) -> None:
    world_url, fake = world
    fake.states[EV_PLUG_ENTITY] = ("on", {})
    store = ProviderDataStore(tmp_path)
    store.save(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER, _schedule({local(10, 1): 11.0}))

    assert _reminder(world_url, store).remind_due() is False
    assert _notifications(fake) == []


def test_no_reminder_when_the_plan_does_not_charge_tonight(
    world: tuple[str, FakeWorld], tmp_path: Path
) -> None:
    world_url, fake = world
    store = ProviderDataStore(tmp_path)
    store.save(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER, _schedule({local(9, 19): 11.0}))

    assert _reminder(world_url, store).remind_due() is False
    assert _notifications(fake) == []


def test_a_rejected_notification_is_retried_on_the_next_check(
    world: tuple[str, FakeWorld], tmp_path: Path
) -> None:
    world_url, fake = world
    store = ProviderDataStore(tmp_path)
    store.save(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER, _schedule({local(10, 1): 11.0}))
    rejected = PlugInReminder(
        _home_assistant(world_url).model_copy(
            update={"base_url": "http://127.0.0.1:9"}
        ),
        NIGHT,
        SETTINGS,
        BERLIN,
        store,
        clock=lambda: MONDAY_EVENING.astimezone(UTC),
    )

    assert rejected.remind_due() is False
    assert _reminder(world_url, store).remind_due() is True


def test_the_reminder_needs_home_hours_and_notify_services() -> None:
    with pytest.raises(ValidationError, match="needs home_hours"):
        EvChargingConfiguration.model_validate(
            {"strategy": "daily_minimum", "plug_in_reminder": {"services": PHONES}}
        )
    with pytest.raises(ValidationError, match="not a notify service"):
        EvPlugInReminderConfiguration(services=["light.turn_on"])
    with pytest.raises(ValidationError, match="may only use"):
        EvPlugInReminderConfiguration(services=PHONES, message="{unknown}")
