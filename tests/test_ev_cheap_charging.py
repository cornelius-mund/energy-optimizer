"""The car's stored energy is worth the gross price at the reference spot price,
divided by the charging losses. Grid charging then pays only below that price,
and PV surplus is stored instead of exported."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_optimizer.config import EvChargingConfiguration, TariffConfiguration
from energy_optimizer.optimization import PlanStatus
from energy_optimizer.optimization.ev_patterns import NO_PATTERNS
from energy_optimizer.optimization.plan_generation import (
    daily_minimum_charging,
    reference_stored_value,
)
from test_ev_patterns import _ev
from test_optimization import TOLERANCE, problem, solve

UTC_ZONE = ZoneInfo("UTC")
SUNDAY_EVENING = datetime(2026, 11, 8, 20, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
CHARGE_EFFICIENCY = 0.9
FEED_IN_EUR_PER_KWH = 0.07
TARIFF = TariffConfiguration.model_validate(
    {
        "grid_fee_bands": [{"start_hour": 0, "end_hour": 24, "fee_ct_per_kwh": 9.53}],
        "supplier_surcharge_ct_per_kwh": 1.0,
        "levies_ct_per_kwh": 2.946,
        "concession_fee_ct_per_kwh": 1.32,
        "electricity_tax_ct_per_kwh": 2.05,
        "vat_rate": 0.19,
    }
)


def _hours(count: int) -> list[datetime]:
    return [SUNDAY_EVENING + ONE_HOUR * index for index in range(count)]


def _reference_value() -> float:
    value = reference_stored_value(
        _ev(
            state_of_charge_kwh=40.0,
            plugged_in=True,
            charge_efficiency=CHARGE_EFFICIENCY,
        ),
        EvChargingConfiguration(
            strategy="daily_minimum", cheap_market_price_ct_per_kwh=1
        ),
        TARIFF,
        local_hour=21,
    )
    assert value is not None
    return value


def _gross(spot_ct_per_kwh: float) -> float:
    return TARIFF.import_price_eur_per_kwh(spot_ct_per_kwh / 100, 21)


def test_the_reference_value_is_the_gross_price_at_one_ct_spot_over_the_losses() -> (
    None
):
    assert _reference_value() == pytest.approx(_gross(1.0) / CHARGE_EFFICIENCY)
    assert _reference_value() == pytest.approx(0.2359, abs=1e-4)


def test_without_a_reference_price_there_is_no_value_override() -> None:
    value = reference_stored_value(
        _ev(state_of_charge_kwh=40.0, plugged_in=True),
        EvChargingConfiguration(strategy="daily_minimum"),
        TARIFF,
        local_hour=21,
    )

    assert value is None


def _charge_kw_per_hour(
    import_gross: tuple[float, ...],
    pv_forecast_kw: tuple[float, ...] | None = None,
    load_kw: tuple[float, ...] | None = None,
) -> list[float]:
    hours = _hours(len(import_gross))
    specification = daily_minimum_charging(
        _ev(
            state_of_charge_kwh=40.0,
            plugged_in=True,
            charge_efficiency=CHARGE_EFFICIENCY,
        ),
        EvChargingConfiguration(strategy="daily_minimum"),
        hours,
        UTC_ZONE,
        NO_PATTERNS,
        stored_value_eur_per_kwh=_reference_value(),
    )
    plan = solve(
        replace(
            problem(
                load_kw or (0.0,) * len(import_gross),
                import_gross,
                pv_forecast_kw=pv_forecast_kw,
                export_price_eur_per_kwh=(FEED_IN_EUR_PER_KWH,) * len(import_gross),
            ),
            ev=specification,
        )
    )
    assert plan.status is PlanStatus.OPTIMAL
    return [hour.ev.charge_kw for hour in plan.hours if hour.ev is not None]


def test_the_car_charges_from_the_grid_below_one_ct_spot() -> None:
    charge = _charge_kw_per_hour((_gross(0.5),))

    assert charge[0] > 1.0


def test_the_car_does_not_charge_from_the_grid_at_three_ct_spot() -> None:
    charge = _charge_kw_per_hour((_gross(3.0),))

    assert charge[0] == pytest.approx(0.0, abs=TOLERANCE)


def test_pv_surplus_is_stored_in_the_car_instead_of_exported() -> None:
    charge = _charge_kw_per_hour((_gross(3.0),), pv_forecast_kw=(3.0,), load_kw=(1.0,))

    assert charge[0] == pytest.approx(2.0, abs=TOLERANCE)
