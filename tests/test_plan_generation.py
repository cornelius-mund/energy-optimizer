"""Plan generation from provider snapshots, solved by the real HiGHS solver."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from energy_optimizer.config import Configuration
from energy_optimizer.optimization import PlanStatus
from energy_optimizer.optimization.plan_generation import (
    PLAN_ADAPTER,
    PLAN_KEY,
    PlanGenerationError,
    PlanGenerator,
    load_forecast_kw,
)
from energy_optimizer.orchestration import ProviderDataSnapshot
from energy_optimizer.providers.interfaces import (
    BatteryData,
    ElectricityPriceData,
    HouseholdLoadData,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataStore

NOW = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)
FIRST_PLANNED_HOUR = datetime(2026, 3, 10, 13, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
SOURCE = SourceMetadata(provider="test")


def household_load(
    load_kw: Sequence[float | None], start_time: datetime
) -> HouseholdLoadData:
    return HouseholdLoadData(
        schema_version="1",
        start_time=start_time,
        interval_minutes=60,
        load_kw=tuple(load_kw),
        unit="kW",
        source=SOURCE,
        retrieved_at=NOW,
        latest_observation_at=NOW,
    )


def flat_history(load_kw: float = 1.0, days: int = 8) -> HouseholdLoadData:
    return household_load(
        [load_kw] * 24 * days, datetime(2026, 3, 10, tzinfo=UTC) - timedelta(days=days)
    )


def pv_forecast(generation_kw: list[float]) -> PvGenerationData:
    return PvGenerationData(
        schema_version="1",
        start_time=FIRST_PLANNED_HOUR,
        interval_minutes=60,
        generation_kw=tuple(generation_kw),
        unit="kW",
        source=SOURCE,
        retrieved_at=NOW,
        expires_at=FIRST_PLANNED_HOUR + ONE_HOUR * len(generation_kw),
    )


def prices(import_prices: list[float]) -> ElectricityPriceData:
    return ElectricityPriceData(
        schema_version="1",
        timestamps=tuple(
            FIRST_PLANNED_HOUR + ONE_HOUR * i for i in range(len(import_prices))
        ),
        interval_minutes=60,
        import_price_eur_per_kwh=tuple(import_prices),
        export_price_eur_per_kwh=tuple(import_prices),
        unit="EUR/kWh",
        source=SOURCE,
        retrieved_at=NOW,
        expires_at=FIRST_PLANNED_HOUR + ONE_HOUR * len(import_prices),
    )


def battery(initial_soc_kwh: float = 5.0, efficiency: float = 0.81) -> BatteryData:
    return BatteryData(
        schema_version="1",
        start_time=NOW,
        interval_minutes=60,
        state_of_charge_kwh=(initial_soc_kwh,),
        capacity_kwh=10.0,
        minimum_soc_kwh=0.0,
        maximum_soc_kwh=10.0,
        initial_soc_kwh=initial_soc_kwh,
        maximum_charge_kw=3.0,
        maximum_discharge_kw=1.0,
        battery_efficiency=efficiency,
        unit="kWh",
        power_unit="kW",
        source=SOURCE,
        retrieved_at=NOW,
        latest_observation_at=NOW,
    )


def snapshot(**data: object) -> ProviderDataSnapshot:
    return ProviderDataSnapshot(captured_at=NOW, data=data)


@pytest.fixture
def configuration() -> Configuration:
    return Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "timezone": "UTC",
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> ProviderDataStore:
    return ProviderDataStore(tmp_path / "provider-data")


def test_the_load_forecast_is_the_mean_of_the_same_hour_over_the_last_seven_days() -> (
    None
):
    start = datetime(2026, 3, 1, tzinfo=UTC)
    older_day_kw = [100.0] * 24
    recent_days_kw = [float(hour) + day for day in range(7) for hour in range(24)]
    history = household_load(older_day_kw + recent_days_kw, start)

    forecast = load_forecast_kw(
        history,
        [datetime(2026, 3, 9, 5, tzinfo=UTC), datetime(2026, 3, 9, 6, tzinfo=UTC)],
        ZoneInfo("UTC"),
    )

    assert forecast == pytest.approx((5.0 + 3.0, 6.0 + 3.0))


def test_excluded_hours_do_not_contribute_to_the_load_forecast() -> None:
    values: list[float | None] = [2.0 for _ in range(24 * 7)]
    values[5] = None
    values[24 + 5] = 4.0
    history = household_load(values, datetime(2026, 3, 3, tzinfo=UTC))

    forecast = load_forecast_kw(
        history, [datetime(2026, 3, 10, 5, tzinfo=UTC)], ZoneInfo("UTC")
    )

    assert forecast == pytest.approx(((2.0 * 5 + 4.0) / 6,))


def test_an_hour_of_day_without_any_sample_cannot_be_forecast() -> None:
    values: list[float | None] = [1.0 for _ in range(24 * 7)]
    for day in range(7):
        values[day * 24 + 5] = None
    history = household_load(values, datetime(2026, 3, 3, tzinfo=UTC))

    with pytest.raises(PlanGenerationError, match="local hour 5"):
        load_forecast_kw(
            history, [datetime(2026, 3, 10, 5, tzinfo=UTC)], ZoneInfo("UTC")
        )


def test_the_forecast_uses_the_local_hour_of_day() -> None:
    values = [float(hour) for _ in range(7) for hour in range(24)]
    history = household_load(values, datetime(2026, 3, 3, tzinfo=UTC))

    forecast = load_forecast_kw(
        history, [datetime(2026, 3, 10, 5, tzinfo=UTC)], ZoneInfo("Europe/Berlin")
    )

    assert forecast == pytest.approx((5.0,))


def test_the_plan_covers_the_hours_with_prices_and_a_pv_forecast(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    generator = PlanGenerator(configuration, store)

    stored = generator(
        snapshot(
            household_load=flat_history(1.0),
            pv_generation=pv_forecast([0.0, 3.0, 0.0, 0.0]),
            electricity_prices=prices([0.30, 0.20, 0.10, 0.40, 0.50]),
        )
    )

    plan = stored.plan
    assert plan.status is PlanStatus.OPTIMAL
    assert stored.start_time == FIRST_PLANNED_HOUR
    assert len(plan.hours) == 4
    expected_cost = 1.0 * 0.30 + (1.0 - 3.0) * 0.20 + 1.0 * 0.10 + 1.0 * 0.40
    assert plan.objective_eur == pytest.approx(expected_cost)


def test_the_battery_is_planned_with_the_round_trip_split_over_both_legs(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    generator = PlanGenerator(configuration, store)

    stored = generator(
        snapshot(
            household_load=flat_history(1.0),
            pv_generation=pv_forecast([0.0, 0.0]),
            electricity_prices=prices([0.10, 0.50]),
            battery=battery(),
        )
    )

    first, second = stored.plan.hours
    assert first.battery is not None and second.battery is not None
    assert stored.battery_capacity_kwh == 10.0
    # Every kWh drawn from the battery costs 1 / 0.81 kWh charged at the grid.
    assert second.battery.discharge_kw == pytest.approx(1.0)
    assert first.battery.charge_kw == pytest.approx(1.0 / 0.81)
    assert first.battery.state_of_charge_kwh == pytest.approx(5.0 + 0.9 / 0.81)
    assert second.battery.state_of_charge_kwh == pytest.approx(5.0)


def test_the_latest_optimal_plan_is_persisted(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    stored = PlanGenerator(configuration, store)(
        snapshot(
            household_load=flat_history(),
            pv_generation=pv_forecast([0.0]),
            electricity_prices=prices([0.30]),
        )
    )

    assert store.load(PLAN_KEY, PLAN_ADAPTER) == stored


def test_a_plan_the_solver_cannot_satisfy_is_an_error_and_keeps_the_last_plan(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    generator = PlanGenerator(configuration, store)
    good = generator(
        snapshot(
            household_load=flat_history(1.0),
            pv_generation=pv_forecast([0.0]),
            electricity_prices=prices([0.30]),
        )
    )

    with pytest.raises(PlanGenerationError, match="infeasible"):
        generator(
            snapshot(
                household_load=flat_history(11.0),
                pv_generation=pv_forecast([0.0]),
                electricity_prices=prices([0.30]),
            )
        )

    assert store.load(PLAN_KEY, PLAN_ADAPTER) == good


def test_prices_and_pv_forecast_without_a_common_hour_cannot_be_planned(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    generator = PlanGenerator(configuration, store)
    late_pv = replace(pv_forecast([1.0]), start_time=FIRST_PLANNED_HOUR + ONE_HOUR * 9)

    with pytest.raises(PlanGenerationError, match="do not overlap"):
        generator(
            snapshot(
                household_load=flat_history(),
                pv_generation=late_pv,
                electricity_prices=prices([0.30]),
            )
        )


def test_missing_provider_data_is_named(
    configuration: Configuration, store: ProviderDataStore
) -> None:
    with pytest.raises(PlanGenerationError, match="electricity_prices"):
        PlanGenerator(configuration, store)(
            snapshot(household_load=flat_history(), pv_generation=pv_forecast([0.0]))
        )
