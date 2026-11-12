"""Weekly EV patterns learned from history and the daily-minimum plan inputs."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from energy_optimizer.config import EvChargingConfiguration, EvHomeHoursConfiguration
from energy_optimizer.optimization import (
    BatterySpecification,
    EvSpecification,
    PlanStatus,
)
from energy_optimizer.optimization.ev_patterns import (
    EvPatterns,
    driving_consumption,
    expected_plugged_in,
    learn_ev_patterns,
)
from energy_optimizer.optimization.plan_generation import (
    daily_minimum_charging,
    morning_minimums,
    stored_ev_energy_value,
)
from energy_optimizer.providers.interfaces import (
    EvData,
    EvHistoryData,
    SourceMetadata,
)
from test_optimization import TOLERANCE, problem, solve

UTC_ZONE = ZoneInfo("UTC")
ONE_HOUR = timedelta(hours=1)
MONDAY = datetime(2026, 11, 2, tzinfo=UTC)
CAPACITY_KWH = 100.0
HOME_FROM_HOUR = 17
HOME_UNTIL_HOUR = 7


def _home(hour_of_day: int) -> bool:
    return hour_of_day >= HOME_FROM_HOUR or hour_of_day < HOME_UNTIL_HOUR


def _two_weeks_of_commuting() -> EvHistoryData:
    """Away 07:00-17:00 on weekdays, driving 10 % on Mondays and 20 % otherwise;
    the car is recharged by midnight."""
    soc: list[float | None] = []
    plugged: list[bool | None] = []
    for index in range(14 * 24):
        moment = MONDAY + ONE_HOUR * index
        weekday_trip = moment.weekday() < 5
        drop = 10.0 if moment.weekday() == 0 else 20.0
        at_home = _home(moment.hour) or not weekday_trip
        soc.append(80.0 - drop if weekday_trip and 7 <= moment.hour < 23 else 80.0)
        plugged.append(at_home)
    return EvHistoryData(
        schema_version="1",
        start_time=MONDAY,
        interval_minutes=60,
        state_of_charge_percent=tuple(soc),
        plugged_in=tuple(plugged),
        source=SourceMetadata(provider="test"),
        retrieved_at=MONDAY + timedelta(days=14),
    )


def test_the_driving_energy_per_weekday_is_learned_from_state_of_charge_drops() -> None:
    patterns = learn_ev_patterns(_two_weeks_of_commuting(), CAPACITY_KWH, UTC_ZONE)

    assert patterns.consumption_kwh_by_weekday[0] == pytest.approx(10.0)
    assert patterns.consumption_kwh_by_weekday[2] == pytest.approx(20.0)
    assert patterns.consumption_kwh_by_weekday[6] == pytest.approx(0.0)


def test_the_plug_in_share_per_weekday_hour_is_learned() -> None:
    patterns = learn_ev_patterns(_two_weeks_of_commuting(), CAPACITY_KWH, UTC_ZONE)

    assert patterns.plugged_in_share[(1, 12)] == 0.0
    assert patterns.plugged_in_share[(1, 20)] == 1.0
    assert patterns.plugged_in_share[(5, 12)] == 1.0


def test_unknown_hours_have_no_pattern() -> None:
    history = replace(
        _two_weeks_of_commuting(),
        state_of_charge_percent=(None,) * 14 * 24,
        plugged_in=(None,) * 14 * 24,
    )

    assert learn_ev_patterns(history, CAPACITY_KWH, UTC_ZONE).is_empty()


def _hours(first: datetime, count: int) -> list[datetime]:
    return [first + ONE_HOUR * index for index in range(count)]


COMMUTER = EvPatterns(
    consumption_kwh_by_weekday={weekday: 15.0 for weekday in range(5)},
    plugged_in_share={
        (weekday, hour): float(_home(hour) or weekday >= 5)
        for weekday in range(7)
        for hour in range(24)
    },
)


def test_the_current_plug_state_holds_until_the_pattern_changes() -> None:
    tuesday_noon = MONDAY + timedelta(days=1, hours=12)

    plugged = expected_plugged_in(COMMUTER, _hours(tuesday_noon, 8), True)

    assert plugged == (True,) * 8


def test_the_pattern_decides_after_its_first_change() -> None:
    tuesday_evening = MONDAY + timedelta(days=1, hours=20)

    plugged = expected_plugged_in(COMMUTER, _hours(tuesday_evening, 14), False)

    assert plugged[:11] == (False,) * 11
    assert plugged[11:] == (False, False, False)


def test_driving_is_deducted_when_the_car_comes_home() -> None:
    tuesday_morning = MONDAY + timedelta(days=1, hours=6)
    hours = _hours(tuesday_morning, 14)
    plugged = expected_plugged_in(COMMUTER, hours, True)

    consumption = driving_consumption(COMMUTER, hours, plugged)

    assert consumption[11] == pytest.approx(15.0)
    assert sum(consumption) == pytest.approx(15.0)


def test_without_an_arrival_driving_is_deducted_at_six_in_the_evening() -> None:
    always_home = EvPatterns(consumption_kwh_by_weekday={1: 5.0}, plugged_in_share={})
    tuesday_morning = MONDAY + timedelta(days=1, hours=6)
    hours = _hours(tuesday_morning, 24)

    consumption = driving_consumption(always_home, hours, (True,) * 24)

    assert consumption[12] == pytest.approx(5.0)


def test_every_morning_inside_the_plan_has_its_minimum() -> None:
    charging = EvChargingConfiguration(strategy="daily_minimum")
    sunday_evening = MONDAY + timedelta(days=6, hours=20)

    minimums = morning_minimums(
        charging, _hours(sunday_evening, 48), UTC_ZONE, CAPACITY_KWH
    )

    assert minimums == ((11, 30.0), (35, 30.0))


def test_weekday_overrides_change_the_minimum_and_the_time() -> None:
    charging = EvChargingConfiguration.model_validate(
        {
            "strategy": "daily_minimum",
            "weekly": {"monday": {"minimum_soc_percent": 60, "deadline": "06:00"}},
        }
    )
    sunday_evening = MONDAY + timedelta(days=6, hours=20)

    minimums = morning_minimums(
        charging, _hours(sunday_evening, 48), UTC_ZONE, CAPACITY_KWH
    )

    assert minimums == ((10, 60.0), (35, 30.0))
    assert charging.day_targets()[3] == (30.0, time(7))


def _ev(
    state_of_charge_kwh: float,
    plugged_in: bool,
    charge_efficiency: float = 1.0,
) -> EvData:
    return EvData(
        schema_version="1",
        start_time=MONDAY,
        state_of_charge_kwh=state_of_charge_kwh,
        capacity_kwh=CAPACITY_KWH,
        plugged_in=plugged_in,
        maximum_charge_kw=11.0,
        charge_efficiency=charge_efficiency,
        unit="kWh",
        source=SourceMetadata(provider="test"),
        retrieved_at=MONDAY,
        latest_observation_at=MONDAY,
    )


def _daily_plan(
    ev: EvSpecification, prices: tuple[float, ...]
) -> list[tuple[float, float]]:
    hours = len(prices)
    plan = solve(replace(problem((0.0,) * hours, prices), ev=ev))
    assert plan.status is PlanStatus.OPTIMAL
    return [
        (hour.ev.charge_kw, hour.ev.state_of_charge_kwh)
        for hour in plan.hours
        if hour.ev is not None
    ]


def test_charging_moves_to_the_cheaper_day_while_each_morning_keeps_its_minimum() -> (
    None
):
    sunday_evening = MONDAY + timedelta(days=6, hours=20)
    hours = _hours(sunday_evening, 48)
    expensive_first_night = tuple(
        0.40 if index < 24 else 0.10 for index in range(len(hours))
    )
    specification = daily_minimum_charging(
        _ev(state_of_charge_kwh=40.0, plugged_in=True),
        EvChargingConfiguration(strategy="daily_minimum"),
        hours,
        UTC_ZONE,
        COMMUTER,
    )

    schedule = _daily_plan(specification, expensive_first_night)

    first_night_charge = sum(power for power, _ in schedule[:11])
    assert first_night_charge == pytest.approx(0.0, abs=TOLERANCE)
    assert schedule[10][1] >= 30.0 - TOLERANCE
    assert schedule[34][1] >= 30.0 - TOLERANCE
    assert sum(power for power, _ in schedule[24:]) > 0


def test_a_minimum_the_car_cannot_reach_is_reported_as_shortfall() -> None:
    sunday_evening = MONDAY + timedelta(days=6, hours=20)
    hours = _hours(sunday_evening, 12)
    specification = daily_minimum_charging(
        _ev(state_of_charge_kwh=10.0, plugged_in=False),
        EvChargingConfiguration(strategy="daily_minimum"),
        hours,
        UTC_ZONE,
        EvPatterns(consumption_kwh_by_weekday={}, plugged_in_share={}),
    )

    plan = solve(replace(problem((0.0,) * 12, (0.2,) * 12), ev=specification))

    assert plan.status is PlanStatus.OPTIMAL
    assert plan.ev_shortfall_kwh == pytest.approx(20.0)


ALWAYS_HOME = EvPatterns(consumption_kwh_by_weekday={}, plugged_in_share={})
FEED_IN_EUR_PER_KWH = 0.07
IMPORT_EUR_PER_KWH = 0.30
CAR_EFFICIENCY = 0.9
BATTERY_LEG_EFFICIENCY = 0.9


def _valued_car(state_of_charge_kwh: float, hours: int) -> EvSpecification:
    sunday_noon = MONDAY + timedelta(days=6, hours=12)
    return daily_minimum_charging(
        _ev(
            state_of_charge_kwh=state_of_charge_kwh,
            plugged_in=True,
            charge_efficiency=CAR_EFFICIENCY,
        ),
        EvChargingConfiguration(strategy="daily_minimum"),
        _hours(sunday_noon, hours),
        UTC_ZONE,
        ALWAYS_HOME,
        stored_ev_energy_value((IMPORT_EUR_PER_KWH,) * hours),
    )


def test_stored_energy_in_the_car_is_worth_the_cheapest_import_price() -> None:
    assert stored_ev_energy_value((0.35, 0.28, 0.41)) == pytest.approx(0.28)
    assert stored_ev_energy_value((-0.02, 0.30)) == 0.0
    assert stored_ev_energy_value(()) == 0.0


def test_pv_surplus_goes_into_the_car_instead_of_the_grid() -> None:
    plan = solve(
        replace(
            problem(
                (1.0, 1.0),
                (IMPORT_EUR_PER_KWH,) * 2,
                pv_forecast_kw=(7.0, 0.0),
                export_price_eur_per_kwh=(FEED_IN_EUR_PER_KWH,) * 2,
            ),
            ev=_valued_car(state_of_charge_kwh=50.0, hours=2),
        )
    )

    sunny = plan.hours[0]
    assert sunny.ev is not None
    assert sunny.ev.charge_kw == pytest.approx(6.0)
    assert sunny.grid_export_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert plan.hours[1].ev is not None
    assert plan.hours[1].ev.charge_kw == pytest.approx(0.0, abs=TOLERANCE)


def test_the_car_is_not_charged_from_the_grid_just_to_store_energy() -> None:
    plan = solve(
        replace(
            problem((1.0,) * 3, (IMPORT_EUR_PER_KWH,) * 3),
            ev=_valued_car(state_of_charge_kwh=50.0, hours=3),
        )
    )

    assert all(
        hour.ev is not None and hour.ev.charge_kw == pytest.approx(0.0, abs=TOLERANCE)
        for hour in plan.hours
    )


def test_small_pv_surplus_charges_the_car_without_drawing_from_the_battery() -> None:
    full_battery = BatterySpecification(
        minimum_soc_kwh=0.0,
        maximum_soc_kwh=8.0,
        initial_soc_kwh=8.0,
        final_soc_kwh=0.0,
        maximum_charge_kw=5.0,
        maximum_discharge_kw=5.0,
        charge_efficiency=BATTERY_LEG_EFFICIENCY,
        discharge_efficiency=BATTERY_LEG_EFFICIENCY,
        terminal_value_eur_per_kwh=IMPORT_EUR_PER_KWH * BATTERY_LEG_EFFICIENCY,
    )
    plan = solve(
        replace(
            problem(
                (0.0,),
                (IMPORT_EUR_PER_KWH,),
                pv_forecast_kw=(3.0,),
                export_price_eur_per_kwh=(FEED_IN_EUR_PER_KWH,),
                battery=full_battery,
            ),
            ev=_valued_car(state_of_charge_kwh=50.0, hours=1),
        )
    )

    (hour,) = plan.hours
    assert hour.ev is not None and hour.battery is not None
    assert hour.ev.charge_kw == pytest.approx(3.0)
    assert hour.battery.discharge_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert hour.grid_import_kw == pytest.approx(0.0, abs=TOLERANCE)
    assert hour.grid_export_kw == pytest.approx(0.0, abs=TOLERANCE)


NIGHT = EvHomeHoursConfiguration(start=time(22), end=time(6))
RARELY_PLUGGED_AT_NIGHT = EvPatterns(
    consumption_kwh_by_weekday={},
    plugged_in_share={
        (weekday, hour): 0.3 for weekday in range(7) for hour in range(24)
    },
)


def test_home_hours_may_wrap_midnight() -> None:
    assert NIGHT.contains(time(22))
    assert NIGHT.contains(time(3))
    assert not NIGHT.contains(time(6))
    assert not NIGHT.contains(time(12))
    assert EvHomeHoursConfiguration(start=time(9), end=time(17)).contains(time(9))


def test_home_hours_need_a_start_and_an_end_that_differ() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        EvHomeHoursConfiguration(start=time(22), end=time(22))


def test_the_car_can_charge_in_its_home_hours_whatever_its_plug_state() -> None:
    evening = MONDAY + timedelta(hours=20)
    hours = _hours(evening, 12)

    plugged = expected_plugged_in(RARELY_PLUGGED_AT_NIGHT, hours, False, NIGHT.contains)

    assert plugged == (False,) * 2 + (True,) * 8 + (False,) * 2


def test_without_home_hours_the_plug_history_decides() -> None:
    evening = MONDAY + timedelta(hours=20)

    plugged = expected_plugged_in(RARELY_PLUGGED_AT_NIGHT, _hours(evening, 12), False)

    assert plugged == (False,) * 12
