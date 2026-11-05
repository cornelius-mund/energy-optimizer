"""EV inputs of the plan: Home Assistant state, deadline and specification."""

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from energy_optimizer.config import (
    Configuration,
    EvChargingConfiguration,
    HomeAssistantConfiguration,
    HomeAssistantEvConfiguration,
)
from energy_optimizer.optimization.plan_generation import (
    PlanGenerator,
    deadline_hours,
    immediate_charging,
)
from energy_optimizer.providers.home_assistant_ev import (
    HomeAssistantError,
    HomeAssistantEvImporter,
)
from energy_optimizer.storage import ProviderDataStore
from test_plan_generation import (
    FIRST_PLANNED_HOUR,
    NOW,
    flat_history,
    prices,
    pv_forecast,
    snapshot,
)

BERLIN = ZoneInfo("Europe/Berlin")
EV_SETTINGS = {
    "state_of_charge": {"entity_id": "sensor.ev_soc", "unit": "%"},
    "plugged_in": {"entity_id": "binary_sensor.ev_cable"},
    "capacity_kwh": 60,
    "minimum_charge_kw": 1.4,
    "maximum_charge_kw": 4.0,
}


def hours_from(first: datetime, count: int) -> list[datetime]:
    return [first + timedelta(hours=i) for i in range(count)]


def test_the_deadline_is_counted_in_planned_hours_until_the_next_local_time() -> None:
    horizon = hours_from(FIRST_PLANNED_HOUR, 24)

    assert deadline_hours(time(7, 0), horizon, BERLIN) == 17
    assert deadline_hours(time(14, 30), horizon, BERLIN) == 1


def test_a_deadline_that_has_passed_today_moves_to_tomorrow() -> None:
    horizon = hours_from(FIRST_PLANNED_HOUR, 30)

    assert deadline_hours(time(14, 0), horizon, BERLIN) == 24


def test_a_deadline_beyond_the_horizon_is_not_planned() -> None:
    assert (
        deadline_hours(time(7, 0), hours_from(FIRST_PLANNED_HOUR, 10), BERLIN) is None
    )


def ev_responses(state_of_charge: str = "50", cable: str = "on") -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.rsplit("/", 1)[-1]
        state = state_of_charge if entity == "sensor.ev_soc" else cable
        return httpx.Response(
            200,
            json={
                "entity_id": entity,
                "state": state,
                "last_updated": NOW.isoformat(),
                "attributes": {},
            },
        )

    return httpx.MockTransport(handle)


def importer(transport: httpx.MockTransport) -> HomeAssistantEvImporter:
    configuration = HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "ev": EV_SETTINGS,
        }
    )
    return HomeAssistantEvImporter(configuration, httpx.Client(transport=transport))


def test_the_importer_converts_the_state_of_charge_and_reads_the_plug() -> None:
    data = importer(ev_responses("50", "on")).fetch(now=NOW)

    assert data.state_of_charge_kwh == pytest.approx(30.0)
    assert data.plugged_in is True
    assert data.maximum_charge_kw == 4.0
    assert data.charge_efficiency == 0.9


def test_a_cable_that_is_off_means_the_car_is_not_plugged_in() -> None:
    assert importer(ev_responses(cable="off")).fetch(now=NOW).plugged_in is False


@pytest.mark.parametrize("state", ["unavailable", "abc", "130"])
def test_an_unusable_state_of_charge_is_rejected(state: str) -> None:
    with pytest.raises(HomeAssistantError):
        importer(ev_responses(state_of_charge=state)).fetch(now=NOW)


def test_a_minimum_charge_power_above_the_maximum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HomeAssistantEvConfiguration.model_validate(
            {**EV_SETTINGS, "minimum_charge_kw": 5.0}
        )


def configuration() -> Configuration:
    return Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "timezone": "Europe/Berlin",
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "ev_charging": {"minimum_soc_percent": 80, "deadline": "07:00"},
        }
    )


def test_the_plan_charges_the_car_in_the_cheapest_hours_before_the_deadline(
    tmp_path: Path,
) -> None:
    hours = 18
    cheap_hour = 5
    market = [0.30] * hours
    market[cheap_hour] = 0.05
    car = importer(ev_responses("70", "on")).fetch(now=NOW)

    stored = PlanGenerator(configuration(), ProviderDataStore(tmp_path))(
        snapshot(
            household_load=flat_history(0.0),
            pv_generation=pv_forecast([0.0] * hours),
            electricity_prices=prices(market),
            ev=car,
        )
    )

    charged = [hour.ev.charge_kw for hour in stored.plan.hours if hour.ev]
    assert stored.ev_capacity_kwh == 60.0
    assert stored.plan.ev_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
    # 70 % to 80 % of 60 kWh is 6 kWh stored, i.e. 6 / 0.9 kWh from the grid.
    assert sum(charged) == pytest.approx(6 / 0.9)
    assert charged[cheap_hour] == pytest.approx(4.0)
    assert stored.plan.objective_eur == pytest.approx(
        4.0 * 0.05 + (6 / 0.9 - 4.0) * 0.30
    )


def test_a_car_that_is_not_plugged_in_is_planned_with_a_shortfall(
    tmp_path: Path,
) -> None:
    hours = 18
    car = importer(ev_responses("70", "off")).fetch(now=NOW)

    stored = PlanGenerator(configuration(), ProviderDataStore(tmp_path))(
        snapshot(
            household_load=flat_history(0.0),
            pv_generation=pv_forecast([0.0] * hours),
            electricity_prices=prices([0.30] * hours),
            ev=car,
        )
    )

    assert stored.plan.ev_shortfall_kwh == pytest.approx(6.0)
    assert sum(h.ev.charge_kw for h in stored.plan.hours if h.ev) == 0.0


def immediate_schedule(
    soc: str, target_percent: float, cable: str = "on", hours: int = 8
) -> tuple[float, ...]:
    car = importer(ev_responses(soc, cable)).fetch(now=NOW)
    specification = immediate_charging(car, target_percent / 100 * 60, hours)
    assert specification.fixed_charge_kw is not None
    return specification.fixed_charge_kw


def test_immediate_charging_uses_full_power_until_the_target_is_stored() -> None:
    schedule = immediate_schedule("50", 80)

    assert schedule == pytest.approx((4.0,) * 5 + (0.0,) * 3)


def test_immediate_charging_ends_with_the_remaining_energy() -> None:
    schedule = immediate_schedule("50", 55)

    assert schedule == pytest.approx((3 / 0.9,) + (0.0,) * 7)


def test_immediate_charging_rounds_a_tiny_remainder_up_to_the_charger_minimum() -> None:
    assert immediate_schedule("50", 50.5)[0] == pytest.approx(1.4)


def test_immediate_charging_never_charges_an_unplugged_car() -> None:
    assert immediate_schedule("50", 80, cable="off") == (0.0,) * 8


def test_the_deadline_strategy_requires_a_deadline() -> None:
    with pytest.raises(ValidationError):
        EvChargingConfiguration.model_validate({"minimum_soc_percent": 80})
