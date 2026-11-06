"""The whole chain: fake Home Assistant and providers, real service, real solver."""

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.conftest import _configuration, running_service
from energy_optimizer.config import TariffConfiguration
from fake_world import (
    BATTERY_SOC_ENTITY,
    BATTERY_SOC_PERCENT,
    HOME_ASSISTANT_TOKEN,
    HOUSEHOLD_ENERGY_ENTITY,
    FakeWorld,
    household_load_kw,
    market_price_eur_per_mwh,
    pv_kw,
    running_fake_world,
)

pytestmark = pytest.mark.e2e

BATTERY_CAPACITY_KWH = 10.0
BATTERY_EFFICIENCY = 0.81
INITIAL_SOC_KWH = BATTERY_CAPACITY_KWH * BATTERY_SOC_PERCENT / 100
MINIMUM_SOC_KWH = 1.0
TOLERANCE = 1e-5
PLAN_TIMEOUT_SECONDS = 90
TARIFF = {
    "grid_fee_bands": [
        {"start_hour": 0, "end_hour": 7, "fee_ct_per_kwh": 0.95},
        {"start_hour": 7, "end_hour": 15, "fee_ct_per_kwh": 9.53},
        {"start_hour": 15, "end_hour": 20, "fee_ct_per_kwh": 15.65},
        {"start_hour": 20, "end_hour": 24, "fee_ct_per_kwh": 9.53},
    ],
    "supplier_surcharge_ct_per_kwh": 1.0,
    "levies_ct_per_kwh": 2.946,
    "concession_fee_ct_per_kwh": 1.32,
    "electricity_tax_ct_per_kwh": 2.05,
    "vat_rate": 0.19,
}
PLANNING_SOURCES = ["household_load", "pv_generation", "electricity_prices", "battery"]


def _chain_configuration(world_url: str, data_directory: Path) -> dict[str, Any]:
    configuration = _configuration(data_directory)
    configuration["timezone"] = "UTC"
    home_assistant = configuration["home_assistant"]
    home_assistant["base_url"] = world_url
    home_assistant["token"] = HOME_ASSISTANT_TOKEN
    home_assistant["household_load"] = {
        "terms": [
            {
                "operation": "add",
                "entities": [
                    {
                        "entity_id": HOUSEHOLD_ENERGY_ENTITY,
                        "state_class": "total_increasing",
                        "unit": "kWh",
                    }
                ],
            }
        ]
    }
    home_assistant["max_data_age_seconds"] = 7200
    home_assistant["battery"] = {
        "state_of_charge": {"entity_id": BATTERY_SOC_ENTITY, "unit": "%"},
        "capacity": BATTERY_CAPACITY_KWH,
        "minimum_soc": 10,
        "maximum_soc": 100,
        "maximum_charge": 4,
        "maximum_discharge": 4,
        "battery_efficiency": BATTERY_EFFICIENCY,
    }
    del home_assistant["grid_import"], home_assistant["grid_export"]
    configuration["forecast_solar"]["base_url"] = world_url
    configuration["awattar"] = {"base_url": f"{world_url}/awattar"}
    configuration["tariff"] = TARIFF
    configuration["orchestration"] = {
        "enabled": True,
        "sources": {
            name: {"interval_seconds": 600, "history_lookback_seconds": 3600}
            for name in PLANNING_SOURCES
        },
        "optimization": {"enabled": True, "required_sources": PLANNING_SOURCES},
    }
    return configuration


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, FakeWorld]]:
    root = tmp_path_factory.mktemp("chain")
    with running_fake_world() as (world_url, world):
        configuration = _chain_configuration(world_url, root / "provider-data")
        with running_service(configuration, root) as service_url:
            yield service_url, world


def _wait_for_plan(service_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = httpx.get(f"{service_url}/plan", timeout=5)
        if response.status_code == 200:
            plan: dict[str, Any] = response.json()
            return plan
        time.sleep(0.5)
    raise AssertionError("the service did not create a plan in time")


def _market_price(hour: dict[str, Any]) -> float:
    return market_price_eur_per_mwh(_hour_of_day(hour)) / 1000


def _gross_price(hour: dict[str, Any]) -> float:
    return TariffConfiguration.model_validate(TARIFF).import_price_eur_per_kwh(
        _market_price(hour), _hour_of_day(hour)
    )


def _hour_of_day(hour: dict[str, Any]) -> int:
    return datetime.fromisoformat(hour["start_time"]).hour


def test_the_service_plans_from_what_home_assistant_and_the_providers_serve(
    chain: tuple[str, FakeWorld],
) -> None:
    service_url, world = chain

    plan = _wait_for_plan(service_url)

    assert plan["status"] == "optimal"
    schedule = plan["schedule"]
    assert len(schedule) >= 24
    start = datetime.fromisoformat(schedule[0]["start_time"])
    for index, hour in enumerate(schedule):
        assert datetime.fromisoformat(hour["start_time"]) == start + timedelta(
            hours=index
        )
    requested = {path.split("/")[1] for path in world.requests}
    assert {"api", "awattar", "estimate"} <= requested


def test_each_planned_hour_balances_and_uses_the_forecast_inputs(
    chain: tuple[str, FakeWorld],
) -> None:
    schedule = _wait_for_plan(chain[0])["schedule"]

    for hour in schedule:
        hour_of_day = _hour_of_day(hour)
        battery = hour["battery"]
        assert hour["load_kw"] == pytest.approx(household_load_kw(hour_of_day))
        assert hour["pv_generation_kw"] + hour["pv_curtailment_kw"] == pytest.approx(
            pv_kw(hour_of_day)
        )
        supplied = hour["pv_generation_kw"] + hour["grid_import_kw"]
        consumed = hour["load_kw"] + hour["grid_export_kw"]
        assert supplied + battery["discharge_kw"] == pytest.approx(
            consumed + battery["charge_kw"], abs=TOLERANCE
        )
        assert min(battery["charge_kw"], battery["discharge_kw"]) <= TOLERANCE


def test_the_battery_dispatch_respects_its_limits_and_buys_cheap_energy(
    chain: tuple[str, FakeWorld],
) -> None:
    schedule = _wait_for_plan(chain[0])["schedule"]
    leg_efficiency = BATTERY_EFFICIENCY**0.5

    previous = INITIAL_SOC_KWH
    for hour in schedule:
        battery = hour["battery"]
        expected = (
            previous
            + battery["charge_kw"] * leg_efficiency
            - battery["discharge_kw"] / leg_efficiency
        )
        assert battery["state_of_charge_kwh"] == pytest.approx(expected, abs=1e-4)
        assert MINIMUM_SOC_KWH - TOLERANCE <= battery["state_of_charge_kwh"]
        assert battery["state_of_charge_kwh"] <= BATTERY_CAPACITY_KWH + TOLERANCE
        previous = battery["state_of_charge_kwh"]
    assert previous >= INITIAL_SOC_KWH - TOLERANCE

    discharged = [h for h in schedule if h["battery"]["discharge_kw"] > TOLERANCE]
    assert discharged
    cheapest_gross = min(_gross_price(h) for h in schedule)
    assert all(_gross_price(h) > cheapest_gross for h in discharged)


def test_imports_are_priced_with_the_grid_tariff_and_exports_at_the_market(
    chain: tuple[str, FakeWorld],
) -> None:
    schedule = _wait_for_plan(chain[0])["schedule"]

    for hour in schedule:
        assert hour["import_cost_eur"] == pytest.approx(
            hour["grid_import_kw"] * _gross_price(hour), abs=1e-6
        )
        assert hour["export_revenue_eur"] == pytest.approx(
            hour["grid_export_kw"] * _market_price(hour), abs=1e-6
        )


def test_the_plan_costs_less_than_buying_everything_at_the_tariff(
    chain: tuple[str, FakeWorld],
) -> None:
    plan = _wait_for_plan(chain[0])
    schedule = plan["schedule"]

    unplanned_cost = sum(
        max(h["load_kw"] - pv_kw(_hour_of_day(h)), 0) * _gross_price(h)
        - max(pv_kw(_hour_of_day(h)) - h["load_kw"], 0) * _market_price(h)
        for h in schedule
    )
    planned_cost = sum(h["import_cost_eur"] - h["export_revenue_eur"] for h in schedule)

    assert plan["objective_eur"] == pytest.approx(planned_cost, abs=1e-6)
    assert plan["objective_eur"] < unplanned_cost - 0.01
