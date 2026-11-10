"""Charge the car to its limit while the market price is at or below a threshold."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from energy_optimizer.config import EvChargingConfiguration
from energy_optimizer.optimization import PlanStatus
from energy_optimizer.optimization.ev_patterns import NO_PATTERNS
from energy_optimizer.optimization.plan_generation import (
    CHEAP_CHARGE_VALUE_EUR_PER_KWH,
    cheap_charge_bonus,
    daily_minimum_charging,
)
from test_ev_patterns import _ev
from test_optimization import TOLERANCE, problem, solve

UTC_ZONE = ZoneInfo("UTC")
SUNDAY_EVENING = datetime(2026, 11, 8, 20, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
THRESHOLD_CT = 1.0
GROSS_IMPORT_EUR_PER_KWH = 0.30


def _hours(count: int) -> list[datetime]:
    return [SUNDAY_EVENING + ONE_HOUR * index for index in range(count)]


def test_the_bonus_marks_only_the_hours_at_or_below_the_threshold() -> None:
    bonus = cheap_charge_bonus((0.02, 0.01, 0.005, 0.30), THRESHOLD_CT)

    assert bonus == (
        0.0,
        CHEAP_CHARGE_VALUE_EUR_PER_KWH,
        CHEAP_CHARGE_VALUE_EUR_PER_KWH,
        0.0,
    )


def test_without_a_threshold_there_is_no_bonus() -> None:
    assert cheap_charge_bonus((0.0, 0.0), None) is None


def test_the_car_charges_to_its_limit_in_cheap_hours_and_not_otherwise() -> None:
    hours = _hours(3)
    market = (0.005, 0.005, 0.20)
    specification = daily_minimum_charging(
        _ev(state_of_charge_kwh=40.0, plugged_in=True),
        EvChargingConfiguration(strategy="daily_minimum"),
        hours,
        UTC_ZONE,
        NO_PATTERNS,
        stored_value_eur_per_kwh=0.0,
        cheap_bonus=cheap_charge_bonus(market, THRESHOLD_CT),
    )

    plan = solve(
        replace(problem((0.0,) * 3, (GROSS_IMPORT_EUR_PER_KWH,) * 3), ev=specification)
    )

    assert plan.status is PlanStatus.OPTIMAL
    cheap_1, cheap_2, expensive = plan.hours
    assert (
        cheap_1.ev is not None and cheap_2.ev is not None and expensive.ev is not None
    )
    assert cheap_2.ev.state_of_charge_kwh == pytest.approx(60.0, abs=TOLERANCE)
    assert expensive.ev.charge_kw == pytest.approx(0.0, abs=TOLERANCE)


def test_a_price_above_the_threshold_does_not_charge_the_car() -> None:
    hours = _hours(2)
    specification = daily_minimum_charging(
        _ev(state_of_charge_kwh=40.0, plugged_in=True),
        EvChargingConfiguration(strategy="daily_minimum"),
        hours,
        UTC_ZONE,
        NO_PATTERNS,
        stored_value_eur_per_kwh=0.0,
        cheap_bonus=cheap_charge_bonus((0.02, 0.02), THRESHOLD_CT),
    )

    plan = solve(
        replace(problem((0.0,) * 2, (GROSS_IMPORT_EUR_PER_KWH,) * 2), ev=specification)
    )

    assert all(
        hour.ev is not None and hour.ev.charge_kw == pytest.approx(0.0, abs=TOLERANCE)
        for hour in plan.hours
    )


def test_the_threshold_needs_the_daily_minimum_strategy() -> None:
    with pytest.raises(ValidationError, match="needs the daily_minimum strategy"):
        EvChargingConfiguration.model_validate(
            {
                "strategy": "deadline",
                "minimum_soc_percent": 80,
                "deadline": "07:00",
                "cheap_market_price_ct_per_kwh": 1,
            }
        )
