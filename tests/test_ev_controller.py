"""The EV controller applies the plan to the wall box through Home Assistant."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HomeAssistantEvServiceConfiguration,
)
from energy_optimizer.control.ev_controller import (
    EV_SCHEDULE_ADAPTER,
    EV_SCHEDULE_KEY,
    ControlOutcome,
    EvController,
    EvSchedule,
    merge_schedule,
)
from energy_optimizer.storage import ProviderDataStore
from fake_world import HOME_ASSISTANT_TOKEN, FakeWorld, running_fake_world

HOUR = timedelta(hours=1)
NOON = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
LIMIT_ENTITY = "number.easee_dynamic_limit"
SWITCH_ENTITY = "switch.easee_enabled"
SET_VALUE = "number.set_value"


def configuration(
    world_url: str, token: str = HOME_ASSISTANT_TOKEN, **control: Any
) -> HomeAssistantConfiguration:
    return HomeAssistantConfiguration.model_validate(
        {
            "base_url": world_url,
            "token": token,
            "timeout_seconds": 5,
            "ev": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "plugged_in": {"entity_id": "binary_sensor.cable"},
                "capacity_kwh": 60,
                "maximum_charge_kw": 11,
                "control": {
                    "enabled": True,
                    "set_power": {"service": SET_VALUE, "entity_id": LIMIT_ENTITY},
                    "enable_entity_id": SWITCH_ENTITY,
                    **control,
                },
            },
        }
    )


@pytest.fixture
def world() -> Iterator[tuple[str, FakeWorld]]:
    with running_fake_world() as (url, fake):
        yield url, fake


@pytest.fixture
def store(tmp_path: Path) -> ProviderDataStore:
    return ProviderDataStore(tmp_path)


def save_schedule(
    store: ProviderDataStore, charge_kw: tuple[float, ...], updated_at: datetime = NOON
) -> None:
    store.save(
        EV_SCHEDULE_KEY,
        EV_SCHEDULE_ADAPTER,
        EvSchedule(
            updated_at=updated_at,
            hour_starts=tuple(NOON + HOUR * i for i in range(len(charge_kw))),
            charge_kw=charge_kw,
        ),
    )


def controller(
    world_url: str,
    store: ProviderDataStore,
    now: list[datetime],
    **control: Any,
) -> EvController:
    return EvController(
        configuration(world_url, **control), store, clock=lambda: now[0]
    )


def test_the_planned_power_is_converted_to_amps_and_the_charger_is_enabled(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (4.0, 0.0))
    now = [NOON + timedelta(minutes=20)]

    outcome = controller(url, store, now).apply_due()

    assert outcome is ControlOutcome.APPLIED
    assert fake.service_calls == [
        ("homeassistant.turn_on", {"entity_id": SWITCH_ENTITY}),
        (SET_VALUE, {"entity_id": LIMIT_ENTITY, "value": 6}),
    ]


def test_an_unchanged_power_sends_nothing_again(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (4.0, 0.0))
    now = [NOON + timedelta(minutes=20)]
    ev_controller = controller(url, store, now)
    ev_controller.apply_due()
    calls = len(fake.service_calls)

    now[0] = NOON + timedelta(minutes=50)

    assert ev_controller.apply_due() is ControlOutcome.UNCHANGED
    assert len(fake.service_calls) == calls


def test_the_next_hour_without_charging_pauses_the_charger(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (4.0, 0.0))
    now = [NOON + timedelta(minutes=20)]
    ev_controller = controller(url, store, now)
    ev_controller.apply_due()
    fake.service_calls.clear()

    now[0] = NOON + HOUR + timedelta(minutes=1)

    assert ev_controller.apply_due() is ControlOutcome.APPLIED
    assert fake.service_calls == [
        ("homeassistant.turn_off", {"entity_id": SWITCH_ENTITY})
    ]


@pytest.mark.parametrize(
    ("unit", "phases", "expected"),
    [("A", 1, 17), ("A", 3, 6), ("W", 3, 4000), ("kW", 3, 4.0)],
)
def test_the_power_is_expressed_in_the_unit_of_the_service(
    world: tuple[str, FakeWorld],
    store: ProviderDataStore,
    unit: str,
    phases: int,
    expected: float,
) -> None:
    url, fake = world
    save_schedule(store, (4.0,))
    set_power = {
        "service": SET_VALUE,
        "entity_id": LIMIT_ENTITY,
        "unit": unit,
        "phases": phases,
    }

    controller(url, store, [NOON], set_power=set_power).apply_due()

    assert fake.service_calls[-1] == (
        SET_VALUE,
        {"entity_id": LIMIT_ENTITY, "value": pytest.approx(expected)},
    )


def test_a_device_service_gets_the_device_and_the_extra_data(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (11.0,))
    set_power = {
        "service": "easee.set_charger_dynamic_limit",
        "device_id": "charger-device",
        "data_field": "current",
        "extra_data": {"time_to_live": 60},
        "phases": 3,
    }

    controller(
        url, store, [NOON], set_power=set_power, enable_entity_id=None
    ).apply_due()

    assert fake.service_calls == [
        (
            "easee.set_charger_dynamic_limit",
            {"time_to_live": 60, "device_id": "charger-device", "current": 16},
        )
    ]


@pytest.mark.parametrize(
    "target",
    [{}, {"entity_id": LIMIT_ENTITY, "device_id": "charger-device"}],
    ids=["no-target", "two-targets"],
)
def test_the_power_service_needs_exactly_one_target(target: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly one of entity_id or device_id"):
        HomeAssistantEvServiceConfiguration.model_validate(
            {"service": SET_VALUE, **target}
        )


def test_without_a_switch_a_pause_sets_the_limit_to_zero(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (0.0,))

    controller(url, store, [NOON], enable_entity_id=None).apply_due()

    assert fake.service_calls == [(SET_VALUE, {"entity_id": LIMIT_ENTITY, "value": 0})]


def test_a_stale_plan_hands_the_charger_back_at_full_power(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (0.0, 0.0, 0.0), updated_at=NOON)
    now = [NOON + timedelta(hours=2, minutes=1)]

    outcome = controller(url, store, now).apply_due()

    assert outcome is ControlOutcome.FELL_BACK
    assert fake.service_calls == [
        ("homeassistant.turn_on", {"entity_id": SWITCH_ENTITY}),
        (SET_VALUE, {"entity_id": LIMIT_ENTITY, "value": 16}),
    ]


def test_without_any_plan_the_charger_is_handed_back_once(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    ev_controller = controller(url, store, [NOON])

    first = ev_controller.apply_due()
    second = ev_controller.apply_due()

    assert (first, second) == (ControlOutcome.FELL_BACK, ControlOutcome.UNCHANGED)
    assert len(fake.service_calls) == 2


def test_an_hour_the_plan_does_not_cover_hands_the_charger_back(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, _ = world
    save_schedule(store, (4.0,))
    now = [NOON + HOUR * 3]

    assert controller(url, store, now).apply_due() is ControlOutcome.FELL_BACK


def test_a_rejected_command_is_reported_and_retried_on_the_next_run(
    world: tuple[str, FakeWorld], store: ProviderDataStore
) -> None:
    url, fake = world
    save_schedule(store, (4.0,))
    ev_controller = EvController(
        configuration(url, token="wrong"), store, clock=lambda: NOON
    )

    assert ev_controller.apply_due() is ControlOutcome.FAILED
    assert fake.service_calls == []

    ev_controller.home_assistant = configuration(url)
    assert ev_controller.apply_due() is ControlOutcome.APPLIED


def test_a_new_plan_wins_for_its_hours_and_earlier_hours_are_kept() -> None:
    previous = EvSchedule(
        updated_at=NOON,
        hour_starts=(NOON, NOON + HOUR, NOON + 2 * HOUR),
        charge_kw=(1.0, 2.0, 3.0),
    )

    merged = merge_schedule(
        previous, NOON + timedelta(minutes=70), (NOON + 2 * HOUR,), (9.0,)
    )

    assert merged.hour_starts == (NOON, NOON + HOUR, NOON + 2 * HOUR)
    assert merged.charge_kw == (1.0, 2.0, 9.0)
    assert merged.power_at(NOON + timedelta(minutes=90)) == 2.0
