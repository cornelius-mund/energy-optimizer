"""The whole chain: fake Home Assistant and providers, real service, real solver."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.conftest import _configuration, running_service
from energy_optimizer.config import TariffConfiguration
from fake_world import (
    BATTERY_SOC_ENTITY,
    BATTERY_SOC_PERCENT,
    EV_PLUG_ENTITY,
    EV_SOC_ENTITY,
    EV_SOC_PERCENT,
    HOME_ASSISTANT_TOKEN,
    HOUSEHOLD_ENERGY_ENTITY,
    FakeWorld,
    ev_history_soc_percent,
    ev_plugged_in_share,
    forecast_price_eur_per_mwh,
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
EV_CAPACITY_KWH = 60.0
EV_TARGET_PERCENT = 60.0
EV_MAXIMUM_CHARGE_KW = 4.0
EV_EFFICIENCY = 0.9
HOURS_UNTIL_EV_DEADLINE = 12
PLANNING_SOURCES = [
    "household_load",
    "pv_generation",
    "electricity_prices",
    "battery",
    "ev",
]


EV_DEADLINE = datetime.now(UTC) + timedelta(hours=HOURS_UNTIL_EV_DEADLINE)


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
    home_assistant["ev"] = {
        "state_of_charge": {"entity_id": EV_SOC_ENTITY, "unit": "%"},
        "plugged_in": {"entity_id": EV_PLUG_ENTITY},
        "capacity_kwh": EV_CAPACITY_KWH,
        "maximum_charge_kw": EV_MAXIMUM_CHARGE_KW,
        "charge_efficiency": EV_EFFICIENCY,
        "control": {
            "enabled": True,
            "set_power": {
                "service": "number.set_value",
                "entity_id": "number.easee_dynamic_limit",
            },
            "enable_entity_id": "switch.easee_enabled",
            "poll_interval_seconds": 1,
        },
    }
    day_target = {
        "minimum_soc_percent": EV_TARGET_PERCENT,
        "deadline": EV_DEADLINE.strftime("%H:00"),
    }
    configuration["ev_charging"] = {
        "weekly": {
            day: day_target
            for day in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
        }
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
        consumed = hour["load_kw"] + hour["grid_export_kw"] + hour["ev"]["charge_kw"]
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
    ev_energy_kwh = (
        EV_CAPACITY_KWH * (EV_TARGET_PERCENT - EV_SOC_PERCENT) / 100 / EV_EFFICIENCY
    )
    immediate_charging_cost = 0.0
    for hour in schedule:
        charge_kw = min(EV_MAXIMUM_CHARGE_KW, ev_energy_kwh)
        immediate_charging_cost += charge_kw * _gross_price(hour)
        ev_energy_kwh -= charge_kw
    planned_cost = sum(h["import_cost_eur"] - h["export_revenue_eur"] for h in schedule)

    assert plan["objective_eur"] == pytest.approx(planned_cost, abs=1e-6)
    assert plan["objective_eur"] < unplanned_cost + immediate_charging_cost - 0.01


def _deadline_index(schedule: list[dict[str, Any]]) -> int:
    start = datetime.fromisoformat(schedule[0]["start_time"])
    deadline = EV_DEADLINE.replace(minute=0, second=0, microsecond=0)
    return int((deadline - start) / timedelta(hours=1))


def test_the_car_is_charged_to_its_target_before_the_deadline_in_cheap_hours(
    chain: tuple[str, FakeWorld],
) -> None:
    plan = _wait_for_plan(chain[0])
    schedule = plan["schedule"]
    deadline_index = _deadline_index(schedule)
    target_kwh = EV_CAPACITY_KWH * EV_TARGET_PERCENT / 100

    assert plan["ev_shortfall_kwh"] == pytest.approx(0.0, abs=TOLERANCE)
    assert schedule[deadline_index - 1]["ev"]["state_of_charge_kwh"] >= (
        target_kwh - TOLERANCE
    )
    previous = EV_CAPACITY_KWH * EV_SOC_PERCENT / 100
    for index, hour in enumerate(schedule):
        charge_kw = hour["ev"]["charge_kw"]
        assert -TOLERANCE <= charge_kw <= EV_MAXIMUM_CHARGE_KW + TOLERANCE
        if index >= deadline_index:
            assert charge_kw == pytest.approx(0.0, abs=TOLERANCE)
        assert hour["ev"]["state_of_charge_kwh"] == pytest.approx(
            previous + charge_kw * EV_EFFICIENCY, abs=1e-4
        )
        previous = hour["ev"]["state_of_charge_kwh"]

    before_deadline = schedule[:deadline_index]
    charged_kwh = sum(h["ev"]["charge_kw"] for h in before_deadline)
    average_charging_price = (
        sum(h["ev"]["charge_kw"] * _gross_price(h) for h in before_deadline)
        / charged_kwh
    )
    average_price = sum(_gross_price(h) for h in before_deadline) / deadline_index
    assert average_charging_price < average_price


def _chain_with(
    tmp_path_factory: pytest.TempPathFactory, name: str, ev_charging: dict[str, Any]
) -> Iterator[tuple[str, FakeWorld]]:
    root = tmp_path_factory.mktemp(name)
    with running_fake_world() as (world_url, world):
        configuration = _chain_configuration(world_url, root / "provider-data")
        configuration["ev_charging"] = ev_charging
        with running_service(configuration, root) as service_url:
            yield service_url, world


@pytest.fixture(scope="module")
def immediate_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    yield from _chain_with(
        tmp_path_factory,
        "immediate-chain",
        {"strategy": "immediate", "minimum_soc_percent": EV_TARGET_PERCENT},
    )


SOFT_TARGET_PERCENT = 80.0
SOFT_VALUE_EUR_PER_KWH = 0.25


@pytest.fixture(scope="module")
def soft_target_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    yield from _chain_with(
        tmp_path_factory,
        "soft-target-chain",
        {
            "strategy": "minimum_plus_target",
            "minimum_soc_percent": EV_TARGET_PERCENT,
            "target_soc_percent": SOFT_TARGET_PERCENT,
            "value_eur_per_kwh": SOFT_VALUE_EUR_PER_KWH,
            "deadline": EV_DEADLINE.strftime("%H:00"),
        },
    )


@pytest.fixture(scope="module")
def surplus_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    yield from _chain_with(
        tmp_path_factory,
        "surplus-chain",
        {"strategy": "surplus", "minimum_soc_percent": EV_TARGET_PERCENT},
    )


def test_the_immediate_strategy_charges_at_full_power_from_the_first_hour(
    immediate_chain: tuple[str, FakeWorld],
) -> None:
    plan = _wait_for_plan(immediate_chain[0])
    charged_kw = [hour["ev"]["charge_kw"] for hour in plan["schedule"]]
    needed_kwh = EV_CAPACITY_KWH * (EV_TARGET_PERCENT - EV_SOC_PERCENT) / 100
    full_power_hours = int(needed_kwh / EV_EFFICIENCY // EV_MAXIMUM_CHARGE_KW)

    assert plan["ev_shortfall_kwh"] == pytest.approx(0.0, abs=TOLERANCE)
    assert charged_kw[:full_power_hours] == pytest.approx(
        [EV_MAXIMUM_CHARGE_KW] * full_power_hours
    )
    assert sum(charged_kw) * EV_EFFICIENCY == pytest.approx(needed_kwh)
    assert all(kw == pytest.approx(0.0, abs=TOLERANCE) for kw in charged_kw[6:])


def test_the_surplus_strategy_charges_only_from_pv_that_the_household_leaves(
    surplus_chain: tuple[str, FakeWorld],
) -> None:
    plan = _wait_for_plan(surplus_chain[0])
    charged = [h for h in plan["schedule"] if h["ev"]["charge_kw"] > TOLERANCE]

    assert charged
    for hour in plan["schedule"]:
        surplus_kw = max(pv_kw(_hour_of_day(hour)) - hour["load_kw"], 0.0)
        assert hour["ev"]["charge_kw"] <= surplus_kw + TOLERANCE
    assert max(h["ev"]["state_of_charge_kwh"] for h in plan["schedule"]) <= (
        EV_CAPACITY_KWH * EV_TARGET_PERCENT / 100 + TOLERANCE
    )
    assert plan["ev_shortfall_kwh"] == pytest.approx(0.0, abs=TOLERANCE)


def test_the_soft_target_charges_cheap_hours_above_the_minimum_but_never_beyond_it(
    soft_target_chain: tuple[str, FakeWorld],
) -> None:
    plan = _wait_for_plan(soft_target_chain[0])
    schedule = plan["schedule"]
    deadline_index = _deadline_index(schedule)
    minimum_kwh = EV_CAPACITY_KWH * EV_TARGET_PERCENT / 100
    limit_kwh = EV_CAPACITY_KWH * SOFT_TARGET_PERCENT / 100
    initial_kwh = EV_CAPACITY_KWH * EV_SOC_PERCENT / 100

    assert plan["ev_shortfall_kwh"] == pytest.approx(0.0, abs=TOLERANCE)
    assert schedule[deadline_index - 1]["ev"]["state_of_charge_kwh"] >= (
        minimum_kwh - TOLERANCE
    )
    assert max(h["ev"]["state_of_charge_kwh"] for h in schedule) <= (
        limit_kwh + TOLERANCE
    )
    worth_it = SOFT_VALUE_EUR_PER_KWH * EV_EFFICIENCY
    expensive_stored_kwh = sum(
        h["ev"]["charge_kw"] * EV_EFFICIENCY
        for h in schedule
        if _gross_price(h) >= worth_it
    )
    assert expensive_stored_kwh <= minimum_kwh - initial_kwh + TOLERANCE


def test_the_running_service_hands_the_charger_back_while_no_plan_covers_the_hour(
    chain: tuple[str, FakeWorld],
) -> None:
    service_url, world = chain
    _wait_for_plan(service_url)
    deadline = time.monotonic() + 10
    while not world.service_calls and time.monotonic() < deadline:
        time.sleep(0.2)

    # The first plan starts at the next full hour, so the running hour is not
    # planned yet and the charger keeps its own behaviour: enabled at full power.
    assert world.service_calls[:2] == [
        ("homeassistant.turn_on", {"entity_id": "switch.easee_enabled"}),
        (
            "number.set_value",
            {
                "entity_id": "number.easee_dynamic_limit",
                "value": round(EV_MAXIMUM_CHARGE_KW * 1000 / (3 * 230)),
            },
        ),
    ]


def test_the_plan_reports_how_the_load_was_forecast(
    chain: tuple[str, FakeWorld],
) -> None:
    forecast = _wait_for_plan(chain[0])["load_forecast"]

    assert forecast["method"] in {"seven_day_mean", "weekday_profile"}
    assert forecast["mean_absolute_error_kw"] == pytest.approx(0.0, abs=1e-6)


WEEK_PV_FORECAST_DAYS = 4
PUBLISHED_PRICE_HOURS = 48


@pytest.fixture(scope="module")
def week_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    root = tmp_path_factory.mktemp("week-chain")
    with running_fake_world() as (world_url, world):
        configuration = _chain_configuration(world_url, root / "provider-data")
        del configuration["forecast_solar"]
        configuration["open_meteo_pv"] = {
            "latitude": 52.52,
            "longitude": 13.41,
            "planes": [
                {
                    "name": "South roof",
                    "tilt_degrees": 30,
                    "azimuth_degrees": 0,
                    "peak_power_kw": 5,
                }
            ],
            "performance_factor": 0.8,
            "forecast_days": WEEK_PV_FORECAST_DAYS,
            "base_url": world_url,
        }
        configuration["epex_predictor"] = {"base_url": world_url}
        configuration["orchestration"]["sources"]["electricity_price_forecast"] = {
            "interval_seconds": 600
        }
        with running_service(configuration, root) as service_url:
            yield service_url, world


def test_forecast_prices_extend_the_plan_beyond_the_published_day_ahead_prices(
    week_chain: tuple[str, FakeWorld],
) -> None:
    service_url, world = week_chain

    plan = _wait_for_plan(service_url)

    assert "/prices" in world.requests
    assert plan["price_forecast_from"] is not None
    forecast_from = datetime.fromisoformat(plan["price_forecast_from"])
    schedule = plan["schedule"]
    assert len(schedule) > PUBLISHED_PRICE_HOURS
    tariff = TariffConfiguration.model_validate(TARIFF)
    for hour in schedule:
        starts = datetime.fromisoformat(hour["start_time"])
        market_eur_per_mwh = (
            forecast_price_eur_per_mwh(starts.hour)
            if starts >= forecast_from
            else market_price_eur_per_mwh(starts.hour)
        )
        expected_cost = hour["grid_import_kw"] * tariff.import_price_eur_per_kwh(
            market_eur_per_mwh / 1000, starts.hour
        )
        assert hour["import_cost_eur"] == pytest.approx(expected_cost, abs=TOLERANCE)


DAILY_MINIMUM_PERCENT = 30.0
HOME_HOURS_OF_DAY = (12, 13)
MORNING_HOUR = 7


@pytest.fixture(scope="module")
def daily_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    root = tmp_path_factory.mktemp("daily-chain")
    with running_fake_world() as (world_url, world):
        configuration = _chain_configuration(world_url, root / "provider-data")
        del configuration["forecast_solar"]
        configuration["open_meteo_pv"] = {
            "latitude": 52.52,
            "longitude": 13.41,
            "planes": [
                {
                    "name": "South roof",
                    "tilt_degrees": 30,
                    "azimuth_degrees": 0,
                    "peak_power_kw": 5,
                }
            ],
            "performance_factor": 0.8,
            "forecast_days": WEEK_PV_FORECAST_DAYS,
            "base_url": world_url,
        }
        configuration["epex_predictor"] = {"base_url": world_url}
        configuration["influxdb"] = {"base_url": world_url, "history_days": 14}
        configuration["ev_charging"] = {
            "strategy": "daily_minimum",
            "home_hours": {"start": "12:00", "end": "14:00"},
        }
        sources = configuration["orchestration"]["sources"]
        sources["electricity_price_forecast"] = {"interval_seconds": 600}
        sources["ev_history"] = {"interval_seconds": 600}
        with running_service(configuration, root) as service_url:
            yield service_url, world


def _wait_for_daily_plan(service_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        plan = _wait_for_plan(service_url)
        if any(hour["ev"]["consumption_kwh"] > 0 for hour in plan["schedule"]):
            return plan
        time.sleep(0.5)
    raise AssertionError("the plan never used the learned driving pattern")


def test_every_morning_keeps_the_daily_minimum_with_the_learned_driving(
    daily_chain: tuple[str, FakeWorld],
) -> None:
    service_url, world = daily_chain

    plan = _wait_for_daily_plan(service_url)

    assert world.influx_queries
    schedule = plan["schedule"]
    mornings = [
        hour
        for hour in schedule
        if datetime.fromisoformat(hour["start_time"]).hour == MORNING_HOUR - 1
    ]
    assert len(mornings) >= 2
    for hour in mornings:
        assert hour["ev"]["state_of_charge_percent"] >= DAILY_MINIMUM_PERCENT - 1e-4
    home_hours = [
        hour
        for hour in schedule
        if datetime.fromisoformat(hour["start_time"]).hour in HOME_HOURS_OF_DAY
    ]
    assert home_hours
    assert all(hour["ev"]["expected_plugged_in"] for hour in home_hours)
    away = [hour for hour in schedule[24:] if not hour["ev"]["expected_plugged_in"]]
    assert away
    assert all(
        ev_plugged_in_share(datetime.fromisoformat(hour["start_time"]).hour) == 0.0
        for hour in away
    )
    assert all(hour["ev"]["charge_kw"] == pytest.approx(0.0, abs=1e-6) for hour in away)
    learned_kwh_per_day = (
        (ev_history_soc_percent(MORNING_HOUR - 1) - ev_history_soc_percent(12))
        / 100
        * EV_CAPACITY_KWH
    )
    driving = [hour["ev"]["consumption_kwh"] for hour in schedule]
    assert max(driving) == pytest.approx(learned_kwh_per_day, rel=1e-6)
    assert plan["ev_shortfall_kwh"] == pytest.approx(0.0, abs=1e-6)
    for hour in schedule:
        car = hour["ev"]
        car_can_take_more = (
            car["expected_plugged_in"]
            and car["charge_kw"] < EV_MAXIMUM_CHARGE_KW - 1e-4
            and car["state_of_charge_percent"] < 100 - 1e-4
            and hour["grid_export_kw"] > 1e-4
        )
        assert not car_can_take_more


FEED_IN_CT_PER_KWH = 7.0


@pytest.fixture(scope="module")
def feed_in_chain(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FakeWorld]]:
    root = tmp_path_factory.mktemp("feed-in-chain")
    with running_fake_world() as (world_url, world):
        configuration = _chain_configuration(world_url, root / "provider-data")
        configuration["tariff"] = {**TARIFF, "feed_in_ct_per_kwh": FEED_IN_CT_PER_KWH}
        with running_service(configuration, root) as service_url:
            yield service_url, world


def test_exports_earn_the_fixed_feed_in_tariff_and_the_battery_is_not_sold(
    feed_in_chain: tuple[str, FakeWorld],
) -> None:
    schedule = _wait_for_plan(feed_in_chain[0])["schedule"]

    for hour in schedule:
        assert hour["export_revenue_eur"] == pytest.approx(
            hour["grid_export_kw"] * FEED_IN_CT_PER_KWH / 100, abs=1e-6
        )
        sells_battery_energy = (
            hour["battery"]["discharge_kw"] > 1e-6 and hour["grid_export_kw"] > 1e-6
        )
        assert not sells_battery_energy
