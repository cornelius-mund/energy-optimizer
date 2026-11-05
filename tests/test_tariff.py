"""The grid tariff turns exchange prices into the prices on the bill."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from energy_optimizer.config import Configuration, TariffConfiguration
from energy_optimizer.optimization.plan_generation import PlanGenerator
from energy_optimizer.storage import ProviderDataStore
from test_plan_generation import (
    FIRST_PLANNED_HOUR,
    flat_history,
    prices,
    pv_forecast,
    snapshot,
)

WESTNETZ_MODULE_3 = {
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


def tariff(**overrides: object) -> TariffConfiguration:
    return TariffConfiguration.model_validate({**WESTNETZ_MODULE_3, **overrides})


@pytest.mark.parametrize(
    ("local_hour", "grid_fee_ct"), [(3, 0.95), (10, 9.53), (17, 15.65), (22, 9.53)]
)
def test_the_gross_price_adds_the_grid_fee_of_the_hour_fixed_components_and_vat(
    local_hour: int, grid_fee_ct: float
) -> None:
    gross = tariff().import_price_eur_per_kwh(0.10, local_hour)

    net_ct = 10.0 + grid_fee_ct + 1.0 + 2.946 + 1.32 + 2.05
    assert gross == pytest.approx(net_ct * 1.19 / 100)


def test_a_negative_market_price_is_reduced_by_the_fixed_components_only() -> None:
    assert tariff().import_price_eur_per_kwh(-0.05, 3) == pytest.approx(
        (-5.0 + 0.95 + 7.316) * 1.19 / 100
    )


@pytest.mark.parametrize(
    "bands",
    [
        [{"start_hour": 0, "end_hour": 23, "fee_ct_per_kwh": 1.0}],
        [
            {"start_hour": 0, "end_hour": 24, "fee_ct_per_kwh": 1.0},
            {"start_hour": 5, "end_hour": 6, "fee_ct_per_kwh": 2.0},
        ],
        [{"start_hour": 5, "end_hour": 5, "fee_ct_per_kwh": 1.0}],
    ],
)
def test_bands_that_do_not_cover_the_day_exactly_once_are_rejected(
    bands: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        tariff(grid_fee_bands=bands)


def configuration_with(tariff_settings: dict[str, object]) -> Configuration:
    return Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "timezone": "Europe/Berlin",
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "tariff": tariff_settings,
        }
    )


def test_the_plan_prices_imports_with_the_tariff_of_the_local_hour(
    tmp_path: Path,
) -> None:
    generator = PlanGenerator(
        configuration_with(WESTNETZ_MODULE_3),
        ProviderDataStore(tmp_path),
    )
    hours = 3
    market = [0.10] * hours

    problem = generator.planning_problem(
        snapshot(
            household_load=flat_history(),
            pv_generation=pv_forecast([0.0] * hours),
            electricity_prices=prices(market),
        )
    )

    local_hours = [
        (FIRST_PLANNED_HOUR + timedelta(hours=i))
        .astimezone(ZoneInfo("Europe/Berlin"))
        .hour
        for i in range(hours)
    ]
    assert problem.import_price_eur_per_kwh == pytest.approx(
        tuple(tariff().import_price_eur_per_kwh(0.10, hour) for hour in local_hours)
    )
    assert problem.export_price_eur_per_kwh == pytest.approx(tuple(market))
    assert FIRST_PLANNED_HOUR == datetime(2026, 3, 10, 13, tzinfo=UTC)
