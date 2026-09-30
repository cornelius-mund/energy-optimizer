"""Historic multi-asset actuals served through the unified dashboard contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from energy_optimizer.api import app, historic
from energy_optimizer.api.schemas import DashboardSeries, SourceMetadata
from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    HomeAssistantBatteryEfficiencyImporter,
)
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyHistoryData,
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
    PvGenerationData,
)
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)
from home_assistant_fixtures import (
    home_assistant_history_payload,
    home_assistant_jittery_total_readings,
    import_and_build,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUSEHOLD_KEY = ProviderDataKey("household-load", "home-assistant", "household_load")
GRID_KEY = ProviderDataKey("grid-flow", "home-assistant", "grid_flow")
PRICE_HISTORY_KEY = ProviderDataKey("electricity-price-history", "awattar.de", "de")
PRICE_FORECAST_KEY = ProviderDataKey("electricity-prices", "awattar.de", "de")
BATTERY_HISTORY_KEY = ProviderDataKey(
    "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
)
ENERGY_ENTITY = (
    "{entity_id: %s, state_class: total_increasing, unit: kWh, operation: add}"
)


def hours(*offsets: int) -> list[datetime]:
    return [START + timedelta(hours=offset) for offset in offsets]


def iso(*offsets: int) -> list[str]:
    return [value.isoformat().replace("+00:00", "Z") for value in hours(*offsets)]


@dataclass
class Environment:
    """A running application with an isolated provider-data directory."""

    client: TestClient
    store: ProviderDataStore
    directory: Path

    def get_actual(
        self, start: str = "2026-01-01T00:00:00Z", end: str = "2026-01-01T04:00:00Z"
    ) -> Any:
        response = self.client.get(
            "/api/v1/dashboard/data",
            params={"scenario_kind": "actual", "start_time": start, "end_time": end},
        )
        assert response.status_code == 200, response.text
        return response.json()


def write_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    persistence: bool = True,
    household: bool = True,
    grid: bool = True,
    battery: str | None = "calculated",
    prices: bool = True,
    home_assistant_max_age: float | None = None,
    price_max_age: float | None = None,
    battery_interval_seconds: int | None = None,
) -> Path:
    lines = [
        "time_resolution_minutes: 60",
        "grid: {maximum_import_kw: 10, maximum_export_kw: 10}",
        "solver: {name: highs, time_limit_seconds: 60}",
    ]
    if persistence:
        lines.append(f"persistence: {{directory: {tmp_path / 'provider-data'}}}")
    if prices:
        lines += [
            "awattar:",
            "  max_data_age_seconds: "
            + ("null" if price_max_age is None else str(price_max_age)),
        ]
    if household or grid or battery:
        lines += [
            "home_assistant:",
            "  base_url: http://homeassistant.test:8123",
            "  token: test-token",
            "  timeout_seconds: 5",
        ]
        if home_assistant_max_age is not None:
            lines.append(f"  max_data_age_seconds: {home_assistant_max_age}")
    if household:
        lines += [
            "  household_load_entities:",
            f"    - {ENERGY_ENTITY % 'sensor.household_energy'}",
        ]
    if grid:
        lines += [
            "  grid_import_entities:",
            f"    - {ENERGY_ENTITY % 'sensor.grid_import'}",
            "  grid_export_entities:",
            f"    - {ENERGY_ENTITY % 'sensor.grid_export'}",
        ]
    if battery:
        lines += [
            "  battery:",
            "    state_of_charge: {entity_id: sensor.battery_soc, unit: '%'}",
            "    capacity: 10",
            "    minimum_soc: 5",
            "    maximum_soc: 100",
            "    maximum_charge: 4",
            "    maximum_discharge: 4",
        ]
        if battery == "live":
            lines.append("    battery_efficiency: 0.9")
        if battery == "calculated":
            lines += [
                "    efficiency_calculation:",
                "      state_of_charge: {entity_id: sensor.battery_soc, unit: '%'}",
            ]
            for leg in ("battery", "inverter_charge", "inverter_discharge"):
                lines += [
                    f"      {leg}:",
                    f"        energy_in: [{ENERGY_ENTITY % f'sensor.{leg}_in'}]",
                    f"        energy_out: [{ENERGY_ENTITY % f'sensor.{leg}_out'}]",
                ]
    if battery_interval_seconds is not None:
        lines += [
            "orchestration:",
            "  enabled: false",
            "  sources:",
            f"    battery_efficiency: {{interval_seconds: {battery_interval_seconds}}}",
        ]
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(path))
    return path


def exclusion(
    hour: int,
    reason: ExclusionReason = "counter_decrease",
    entity_id: str = "sensor.grid_import",
) -> HourExclusion:
    hour_start = START + timedelta(hours=hour)
    return HourExclusion(
        hour_start,
        (
            ExclusionCause.of(
                reason,
                f"{entity_id} is excluded in hour {hour}",
                entity_id,
                [ExcludedDataPoint(hour_start, state="2", unit="kWh")],
            ),
        ),
    )


def seed_household(
    store: ProviderDataStore,
    values: tuple[float | None, ...] = (1.0, 2.0, 3.0, 4.0),
    *,
    start: datetime = START,
    observed: datetime = START,
    excluded: tuple[int, ...] = (),
) -> None:
    """Persist household load; the ``excluded`` offsets are hours without value."""
    store.save(
        HOUSEHOLD_KEY,
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            load_kw=tuple(
                None if index in excluded else value
                for index, value in enumerate(values)
            ),
            unit="kW",
            source=ProviderSourceMetadata("home-assistant", "household_load"),
            retrieved_at=observed,
            latest_observation_at=observed,
            exclusions=tuple(
                exclusion(offset, entity_id="sensor.household_energy")
                for offset in excluded
            ),
        ),
    )


def grid_flow(
    values: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5),
    *,
    observed: datetime = START,
    excluded: tuple[int, ...] = (),
) -> GridFlowData:
    """Build grid flow; the ``excluded`` offsets are hours without both values."""
    return GridFlowData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        import_kw=tuple(
            None if index in excluded else value for index, value in enumerate(values)
        ),
        export_kw=tuple(
            None if index in excluded else value / 10
            for index, value in enumerate(values)
        ),
        unit="kW",
        source=ProviderSourceMetadata("home-assistant", "grid_flow"),
        retrieved_at=observed,
        latest_observation_at=observed,
        exclusions=tuple(exclusion(offset) for offset in excluded),
    )


def seed_grid(store: ProviderDataStore, data: GridFlowData | None = None) -> None:
    store.save(GRID_KEY, TypeAdapter(GridFlowData), data or grid_flow())


def price_history(
    start: datetime = START,
    count: int = 4,
    *,
    retrieved: datetime = START,
    skip: tuple[int, ...] = (),
) -> ElectricityPriceData:
    stamps = tuple(
        start + timedelta(hours=index) for index in range(count) if index not in skip
    )
    return ElectricityPriceData(
        schema_version="1",
        timestamps=stamps,
        interval_minutes=60,
        import_price_eur_per_kwh=tuple(
            0.30 + index / 100 for index in range(len(stamps))
        ),
        export_price_eur_per_kwh=tuple(
            0.10 + index / 100 for index in range(len(stamps))
        ),
        unit="EUR/kWh",
        source=ProviderSourceMetadata("awattar.de", "de"),
        retrieved_at=retrieved,
        expires_at=retrieved + timedelta(hours=48),
    )


def seed_prices(
    store: ProviderDataStore,
    data: ElectricityPriceData | None = None,
    key: ProviderDataKey = PRICE_HISTORY_KEY,
) -> None:
    store.save(key, TypeAdapter(ElectricityPriceData), data or price_history())


def battery_history(
    *,
    intervals: int = 4,
    observed: datetime = START,
    excluded: tuple[int, ...] = (),
) -> BatteryEfficiencyHistoryData:
    """Build aligned history; an excluded hour has no energy in any leg.

    Following the importer, the state of charge at both boundaries of an excluded
    hour (values ``k`` and ``k + 1``) is ``None`` as well.
    """
    energy = tuple(None if index in excluded else 0.5 for index in range(intervals))
    boundaries = set(excluded) | {index + 1 for index in excluded}
    return BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        battery_energy_in_kwh=energy,
        battery_energy_out_kwh=energy,
        inverter_charge_energy_in_kwh=energy,
        inverter_charge_energy_out_kwh=energy,
        inverter_discharge_energy_in_kwh=energy,
        inverter_discharge_energy_out_kwh=energy,
        state_of_charge_percent=tuple(
            None if index in boundaries else 20.0 + 10 * index
            for index in range(intervals + 1)
        ),
        unit="kWh",
        source=ProviderSourceMetadata("home-assistant", "battery_efficiency_history"),
        retrieved_at=observed,
        latest_observation_at=observed,
        exclusions=tuple(
            exclusion(offset, entity_id="sensor.battery_in") for offset in excluded
        ),
    )


def seed_battery(
    store: ProviderDataStore, data: BatteryEfficiencyHistoryData | None = None
) -> None:
    store.save(
        BATTERY_HISTORY_KEY,
        TypeAdapter(BatteryEfficiencyHistoryData),
        data or battery_history(),
    )


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Yield a fully configured application; tests choose what data to seed."""
    write_configuration(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield Environment(
            client, ProviderDataStore(tmp_path / "provider-data"), tmp_path
        )


def by_id(body: Any) -> dict[str, Any]:
    return {item["id"]: item for item in body["series"]}


def availability(body: Any) -> dict[str, Any]:
    return {item["asset"]: item for item in body["assets"]}


def corrupt(directory: Path, key: ProviderDataKey) -> None:
    """Damage both the primary and backup file of one persisted record."""
    for path in (directory / "provider-data").glob(f"{key.data_type}-{key.digest()}*"):
        path.write_text("{corrupt\n", encoding="utf-8")


def test_actual_scenario_returns_every_available_asset_series(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store)
    seed_prices(environment.store)
    seed_battery(environment.store)

    body = environment.get_actual()

    assert body["schema_version"] == "1"
    assert body["status"] == "validated"
    assert body["interval_minutes"] == 60
    assert body["requested_start_time"] == "2026-01-01T00:00:00Z"
    assert body["requested_end_time"] == "2026-01-01T04:00:00Z"
    assert [item["id"] for item in body["series"]] == [
        "household_load_actual",
        "grid_import_actual",
        "grid_export_actual",
        "import_price_actual",
        "export_price_actual",
        "battery_state_of_charge_actual",
    ]
    series = by_id(body)
    expected = {
        "household_load_actual": ("household_load", "kW", [1.0, 2.0, 3.0, 4.0]),
        "grid_import_actual": ("grid_import", "kW", [0.5, 1.5, 2.5, 3.5]),
        "grid_export_actual": ("grid_export", "kW", [0.05, 0.15, 0.25, 0.35]),
        "import_price_actual": ("import_price", "EUR/kWh", [0.30, 0.31, 0.32, 0.33]),
        "export_price_actual": ("export_price", "EUR/kWh", [0.10, 0.11, 0.12, 0.13]),
        "battery_state_of_charge_actual": (
            "battery_state_of_charge",
            "%",
            [20.0, 30.0, 40.0, 50.0],
        ),
    }
    for series_id, (data_type, unit, values) in expected.items():
        item = series[series_id]
        assert item["scenario_kind"] == "actual"
        assert item["data_type"] == data_type
        assert item["unit"] == unit
        assert item["timestamps"] == iso(0, 1, 2, 3)
        assert item["values"] == pytest.approx(values)
        assert item["missing_intervals"] == []
        assert item["validation_status"] == "valid"
        assert item["freshness"] == "unknown"
        assert item["requested_start_time"] == "2026-01-01T00:00:00Z"
        assert item["requested_end_time"] == "2026-01-01T04:00:00Z"
        assert item["retrieved_at"] == "2026-01-01T00:00:00Z"
        assert item["available_start_time"] == "2026-01-01T00:00:00Z"
    assert series["household_load_actual"]["source"] == {
        "provider": "home-assistant",
        "entity_id": "household_load",
    }
    assert series["grid_import_actual"]["source"]["entity_id"] == "grid_flow"
    assert series["import_price_actual"]["source"] == {
        "provider": "awattar.de",
        "entity_id": "de",
    }
    assert series["battery_state_of_charge_actual"]["source"]["entity_id"] == (
        "battery_efficiency_history"
    )
    # Four interval samples plus the closing boundary sample are retained.
    assert series["battery_state_of_charge_actual"]["available_end_time"] == (
        "2026-01-01T05:00:00Z"
    )
    assert series["household_load_actual"]["available_end_time"] == (
        "2026-01-01T04:00:00Z"
    )


def test_availability_identifies_every_asset_and_why_absent_ones_are_missing(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store)
    seed_prices(environment.store)
    seed_battery(environment.store)

    body = environment.get_actual()
    assets = availability(body)

    assert list(assets) == [
        "household_load",
        "pv_generation",
        "grid_flow",
        "electricity_prices",
        "battery",
        "electric_vehicle",
        "heat_pump",
    ]
    assert assets["household_load"] == {
        "asset": "household_load",
        "status": "available",
        "series_ids": ["household_load_actual"],
        "reason": None,
    }
    assert assets["grid_flow"]["series_ids"] == [
        "grid_import_actual",
        "grid_export_actual",
    ]
    for absent, reason in (
        ("pv_generation", "no historic PV-generation importer is available"),
        ("electric_vehicle", "no electric-vehicle importer is available"),
        ("heat_pump", "no heat-pump importer is available"),
    ):
        assert assets[absent]["status"] == "not_configured"
        assert assets[absent]["series_ids"] == []
        assert reason in assets[absent]["reason"]
    # Assets that are simply not installed are not warnings.
    assert body["diagnostics"] == []


def test_series_are_aligned_to_requested_hours_and_keep_explicit_gaps(
    environment: Environment,
) -> None:
    seed_household(environment.store, (1.0, 2.0), start=START + timedelta(hours=1))
    seed_grid(environment.store)

    body = environment.get_actual(end="2026-01-01T05:00:00Z")

    household = by_id(body)["household_load_actual"]
    assert household["timestamps"] == iso(0, 1, 2, 3, 4)
    assert household["values"] == [None, 1.0, 2.0, None, None]
    assert household["missing_intervals"] == iso(0, 3, 4)
    assert household["available_start_time"] == "2026-01-01T01:00:00Z"
    assert household["available_end_time"] == "2026-01-01T03:00:00Z"
    grid = by_id(body)["grid_import_actual"]
    assert grid["values"] == [0.5, 1.5, 2.5, 3.5, None]
    assert grid["missing_intervals"] == iso(4)
    assert body["status"] == "partial"


def test_a_range_wholly_outside_retained_history_is_empty_not_invalid(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store)
    seed_prices(environment.store)
    seed_battery(environment.store)

    body = environment.get_actual("2026-03-01T00:00:00Z", "2026-03-01T02:00:00Z")

    assert body["status"] == "empty"
    assert all(value is None for item in body["series"] for value in item["values"])
    assert {name: item["status"] for name, item in availability(body).items()} == {
        "household_load": "empty",
        "pv_generation": "not_configured",
        "grid_flow": "empty",
        "electricity_prices": "empty",
        "battery": "empty",
        "electric_vehicle": "not_configured",
        "heat_pump": "not_configured",
    }
    assert "no household-load observations exist" in " ".join(body["diagnostics"])


def test_partially_covered_assets_do_not_hide_the_valid_ones(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store, grid_flow((7.0,)))

    body = environment.get_actual()

    assert body["status"] == "partial"
    assert by_id(body)["household_load_actual"]["values"] == [1.0, 2.0, 3.0, 4.0]
    assert by_id(body)["grid_import_actual"]["values"] == [7.0, None, None, None]
    assert by_id(body)["grid_import_actual"]["missing_intervals"] == iso(1, 2, 3)


def test_unconfigured_and_unseeded_assets_do_not_invalidate_configured_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(tmp_path, monkeypatch, grid=False, battery=None, prices=False)
    with TestClient(app) as client:
        store = ProviderDataStore(tmp_path / "provider-data")
        seed_household(store)
        environment = Environment(client, store, tmp_path)

        body = environment.get_actual()

    assert body["status"] == "validated"
    assert [item["id"] for item in body["series"]] == ["household_load_actual"]
    statuses = {name: item["status"] for name, item in availability(body).items()}
    assert statuses["household_load"] == "available"
    assert {statuses[name] for name in statuses if name != "household_load"} == {
        "not_configured"
    }
    assert availability(body)["grid_flow"]["reason"] == (
        "no Home Assistant grid import and export entities are configured"
    )
    assert availability(body)["electricity_prices"]["reason"] == (
        "no electricity-price provider is configured"
    )
    assert availability(body)["battery"]["reason"] == (
        "no Home Assistant battery is configured"
    )


def test_battery_history_imported_from_total_counters_with_a_dip_is_served(
    environment: Environment,
) -> None:
    """Import, persist and serve battery history with a 1 Wh counter decrease.

    All legs use ``total`` counters without ``last_reset``. The counters dip by
    1 Wh in hour 1. Nothing is tolerated or repaired: hour 1 is excluded in every
    leg and in the state of charge around it, and the other hours are imported,
    persisted, and served.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.battery_soc":
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id,
                    [
                        (f"2026-01-01T{hour:02d}:00:00+00:00", str(20 + 10 * hour))
                        for hour in range(5)
                    ],
                    unit="%",
                    state_class="measurement",
                ),
            )
        return httpx.Response(
            200,
            json=home_assistant_history_payload(
                entity_id,
                home_assistant_jittery_total_readings(3200.0, 1),
                state_class="total",
            ),
        )

    leg = {
        "energy_in": [
            {
                "entity_id": "sensor.charging_battery_energy",
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        "energy_out": [
            {
                "entity_id": "sensor.discharging_battery_energy",
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    }
    configuration = HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "battery": {
                "state_of_charge": {"entity_id": "sensor.battery_soc", "unit": "%"},
                "capacity": 10,
                "minimum_soc": 5,
                "maximum_soc": 100,
                "maximum_charge": 4,
                "maximum_discharge": 4,
                "efficiency_calculation": {
                    "state_of_charge": {
                        "entity_id": "sensor.battery_soc",
                        "unit": "%",
                    },
                    "battery": leg,
                    "inverter_charge": leg,
                    "inverter_discharge": leg,
                },
            },
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = import_and_build(
            HomeAssistantBatteryEfficiencyImporter(configuration),
            client,
            START,
            START + timedelta(hours=4),
            now=START,
        )
    finally:
        client.close()
    for leg_values in (
        history.battery_energy_in_kwh,
        history.battery_energy_out_kwh,
        history.inverter_charge_energy_in_kwh,
        history.inverter_charge_energy_out_kwh,
        history.inverter_discharge_energy_in_kwh,
        history.inverter_discharge_energy_out_kwh,
    ):
        assert leg_values == (1.0, None, 1.0, 1.0)
    assert history.state_of_charge_percent == (20.0, None, None, 50.0, 60.0)
    assert [item.hour_start for item in history.exclusions] == hours(1)
    seed_battery(environment.store, history)

    body = environment.get_actual()

    assert availability(body)["battery"]["status"] == "available"
    state_of_charge = by_id(body)["battery_state_of_charge_actual"]
    assert state_of_charge["values"] == [20.0, None, None, 50.0]
    assert state_of_charge["missing_intervals"] == iso(1, 2)
    assert state_of_charge["validation_status"] == "valid"
    persisted = ProviderDataStore(environment.directory / "provider-data").load(
        BATTERY_HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData)
    )
    assert persisted == history
    excluded_hours = environment.client.get(
        "/api/v1/dashboard/excluded-hours",
        params={
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T04:00:00Z",
        },
    ).json()
    assert [
        (item["hour_start"], item["source"]) for item in excluded_hours["hours"]
    ] == [("2026-01-01T01:00:00Z", "battery_efficiency")]
    causes = excluded_hours["hours"][0]["causes"]
    assert {(cause["reason"], cause["entity_id"]) for cause in causes} == {
        (reason, entity_id)
        for reason in ("counter_decrease", "step_after_decrease")
        for entity_id in (
            "sensor.charging_battery_energy",
            "sensor.discharging_battery_energy",
        )
    }


def test_battery_without_efficiency_calculation_explains_missing_state_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(tmp_path, monkeypatch, battery="live")
    with TestClient(app) as client:
        environment = Environment(
            client, ProviderDataStore(tmp_path / "provider-data"), tmp_path
        )
        seed_household(environment.store)

        body = environment.get_actual()

    assert availability(body)["battery"]["status"] == "not_configured"
    assert "battery.efficiency_calculation" in availability(body)["battery"]["reason"]


def test_configured_assets_without_persisted_data_are_reported_unavailable(
    environment: Environment,
) -> None:
    body = environment.get_actual()

    assert body["status"] == "unavailable"
    assert body["series"] == []
    assets = availability(body)
    assert assets["household_load"]["status"] == "unavailable"
    assert assets["household_load"]["reason"] == (
        "no persisted household-load provider data is available"
    )
    for name in ("grid_flow", "electricity_prices", "battery"):
        assert assets[name]["status"] == "unavailable"
        assert "no persisted" in assets[name]["reason"]
    assert len(body["diagnostics"]) == 4


def test_missing_persistence_is_reported_for_every_configured_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(tmp_path, monkeypatch, persistence=False)
    with TestClient(app) as client:
        body = Environment(
            client, ProviderDataStore(tmp_path / "unused"), tmp_path
        ).get_actual()

    assert body["status"] == "unavailable"
    assert body["series"] == []
    assets = availability(body)
    assert {
        assets[name]["status"]
        for name in ("household_load", "grid_flow", "electricity_prices", "battery")
    } == {"unavailable"}
    assert "persistence is not configured" in assets["household_load"]["reason"]
    assert "persistence is not configured" in assets["battery"]["reason"]


def test_an_installation_without_assets_explains_that_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(
        tmp_path,
        monkeypatch,
        persistence=False,
        household=False,
        grid=False,
        battery=None,
        prices=False,
    )
    with TestClient(app) as client:
        body = Environment(
            client, ProviderDataStore(tmp_path / "unused"), tmp_path
        ).get_actual()

    assert body["status"] == "unavailable"
    assert body["diagnostics"] == ["no historic energy asset is configured"]


@pytest.mark.parametrize(
    ("asset", "key", "series_ids"),
    [
        ("grid_flow", GRID_KEY, {"grid_import_actual", "grid_export_actual"}),
        (
            "electricity_prices",
            PRICE_HISTORY_KEY,
            {"import_price_actual", "export_price_actual"},
        ),
        ("battery", BATTERY_HISTORY_KEY, {"battery_state_of_charge_actual"}),
    ],
)
def test_corrupt_persistence_is_withheld_and_isolated_from_valid_assets(
    environment: Environment,
    asset: str,
    key: ProviderDataKey,
    series_ids: set[str],
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store)
    seed_prices(environment.store)
    seed_battery(environment.store)
    corrupt(environment.directory, key)

    body = environment.get_actual()

    assert series_ids.isdisjoint(by_id(body))
    assert "household_load_actual" in by_id(body)
    assert availability(body)[asset]["status"] == "invalid"
    assert availability(body)[asset]["series_ids"] == []
    assert "invalid" in availability(body)[asset]["reason"]
    assert any(asset.replace("_", " ") in item for item in body["diagnostics"])
    assert body["status"] != "invalid"


def test_corrupt_household_persistence_is_never_returned_as_valid_actuals(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    for path in (environment.directory / "provider-data").glob("household-load-*"):
        path.write_text("invalid\n", encoding="utf-8")

    body = environment.get_actual()

    assert body["status"] == "invalid"
    assert body["series"] == []
    assert availability(body)["household_load"]["status"] == "invalid"
    assert "could not recover household-load provider data" in body["diagnostics"][0]


def test_only_corrupt_data_reports_an_invalid_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(
        tmp_path, monkeypatch, household=False, battery=None, prices=False
    )
    with TestClient(app) as client:
        environment = Environment(
            client, ProviderDataStore(tmp_path / "provider-data"), tmp_path
        )
        seed_grid(environment.store)
        corrupt(tmp_path, GRID_KEY)

        body = environment.get_actual()

    assert body["status"] == "invalid"
    assert body["series"] == []


@pytest.mark.parametrize(
    ("key", "adapter", "record"),
    [
        (
            GRID_KEY,
            TypeAdapter(GridFlowData),
            GridFlowData(
                "1",
                START,
                60,
                (1.0, 2.0),
                (1.0,),
                "kW",
                ProviderSourceMetadata("home-assistant", "grid_flow"),
                START,
                START,
            ),
        ),
        (
            # Import is excluded, export is not: excluded hours must be excluded
            # for both channels together.
            GRID_KEY,
            TypeAdapter(GridFlowData),
            GridFlowData(
                "1",
                START,
                60,
                (None, 2.0),
                (1.0, 2.0),
                "kW",
                ProviderSourceMetadata("home-assistant", "grid_flow"),
                START,
                START,
                exclusions=(exclusion(0),),
            ),
        ),
        (
            # An hour without values has no explaining exclusion.
            GRID_KEY,
            TypeAdapter(GridFlowData),
            GridFlowData(
                "1",
                START,
                60,
                (None, 2.0),
                (None, 2.0),
                "kW",
                ProviderSourceMetadata("home-assistant", "grid_flow"),
                START,
                START,
            ),
        ),
        (
            PRICE_HISTORY_KEY,
            TypeAdapter(ElectricityPriceData),
            ElectricityPriceData(
                "1",
                (START,),
                60,
                (0.3,),
                (),
                "EUR/kWh",
                ProviderSourceMetadata("awattar.de", "de"),
                START,
                START,
            ),
        ),
        (
            BATTERY_HISTORY_KEY,
            TypeAdapter(BatteryEfficiencyHistoryData),
            BatteryEfficiencyHistoryData(
                "1",
                START,
                60,
                (0.5, 0.5),
                (0.5, 0.5),
                (0.5, 0.5),
                (0.5, 0.5),
                (0.5, 0.5),
                (0.5, 0.5),
                (20.0, 30.0),
                "kWh",
                ProviderSourceMetadata("home-assistant", "battery_efficiency_history"),
                START,
                START,
            ),
        ),
    ],
)
def test_structurally_inconsistent_records_are_withheld_as_invalid(
    environment: Environment,
    key: ProviderDataKey,
    adapter: TypeAdapter[Any],
    record: object,
) -> None:
    seed_household(environment.store)
    directory = environment.directory / "provider-data"
    (directory / f"{key.data_type}-{key.digest()}.json").write_bytes(
        adapter.dump_json(record)
    )

    body = environment.get_actual()

    assert "household_load_actual" in by_id(body)
    invalid = [item for item in body["assets"] if item["status"] == "invalid"]
    assert len(invalid) == 1
    assert invalid[0]["series_ids"] == []


def test_a_damaged_primary_is_recovered_from_its_backup_and_served(
    environment: Environment,
) -> None:
    seed_grid(environment.store)
    seed_grid(environment.store, grid_flow((9.0, 9.0, 9.0, 9.0), observed=START))
    path = (
        environment.directory / "provider-data" / f"grid-flow-{GRID_KEY.digest()}.json"
    )
    path.write_text("{corrupt", encoding="utf-8")

    body = environment.get_actual()

    assert availability(body)["grid_flow"]["status"] == "available"
    assert by_id(body)["grid_import_actual"]["values"][0] == pytest.approx(0.5)


def test_a_failed_recovery_write_is_reported_instead_of_serving_unverified_data(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_household(environment.store)
    seed_grid(environment.store)
    seed_grid(environment.store)
    path = (
        environment.directory / "provider-data" / f"grid-flow-{GRID_KEY.digest()}.json"
    )
    path.write_text("{corrupt", encoding="utf-8")

    def fail(*_: object) -> None:
        raise ProviderDataStoreError("disk is read-only")

    monkeypatch.setattr(ProviderDataStore, "_atomic_write", fail)

    body = environment.get_actual()

    assert availability(body)["grid_flow"]["status"] == "invalid"
    assert "grid_import_actual" not in by_id(body)
    assert "household_load_actual" in by_id(body)
    # The technical cause is logged for operators, not echoed to API clients.
    assert "disk is read-only" not in " ".join(body["diagnostics"])


@pytest.mark.parametrize("threshold", [1.0, 1e9])
def test_polling_freshness_never_invalidates_historical_actuals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threshold: float
) -> None:
    write_configuration(
        tmp_path,
        monkeypatch,
        home_assistant_max_age=threshold,
        price_max_age=threshold,
        battery_interval_seconds=int(threshold),
    )
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        environment = Environment(
            client, ProviderDataStore(tmp_path / "provider-data"), tmp_path
        )
        seed_household(environment.store, observed=now if threshold > 1 else START)
        seed_grid(
            environment.store, grid_flow(observed=now if threshold > 1 else START)
        )
        seed_prices(
            environment.store, price_history(retrieved=now if threshold > 1 else START)
        )
        seed_battery(
            environment.store, battery_history(observed=now if threshold > 1 else START)
        )

        body = environment.get_actual()

    expected = "stale" if threshold == 1.0 else "fresh"
    assert body["status"] == ("stale" if expected == "stale" else "validated")
    assert {item["freshness"] for item in body["series"]} == {expected}
    assert {item["validation_status"] for item in body["series"]} == {"valid"}
    assert by_id(body)["household_load_actual"]["values"] == [1.0, 2.0, 3.0, 4.0]
    if expected == "stale":
        assert {
            availability(body)[name]["status"]
            for name in ("household_load", "grid_flow", "electricity_prices", "battery")
        } == {"stale"}
        assert "polling threshold" in " ".join(body["diagnostics"])


def test_excluded_hours_are_null_gaps_only_in_the_series_they_belong_to(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(1,))
    seed_grid(environment.store, grid_flow(excluded=(2,)))
    seed_prices(environment.store)
    seed_battery(environment.store, battery_history(excluded=(3,)))

    body = environment.get_actual()

    series = by_id(body)
    assert series["household_load_actual"]["values"] == [1.0, None, 3.0, 4.0]
    assert series["household_load_actual"]["missing_intervals"] == iso(1)
    assert series["grid_import_actual"]["values"] == [0.5, 1.5, None, 3.5]
    assert series["grid_import_actual"]["missing_intervals"] == iso(2)
    assert series["grid_export_actual"]["values"] == pytest.approx(
        [0.05, 0.15, None, 0.35]
    )
    assert series["grid_export_actual"]["missing_intervals"] == iso(2)
    # Hour 3 is excluded, so the boundary values 3 and 4 are unavailable.
    assert series["battery_state_of_charge_actual"]["values"] == [
        20.0,
        30.0,
        40.0,
        None,
    ]
    assert series["battery_state_of_charge_actual"]["missing_intervals"] == iso(3)
    assert series["import_price_actual"]["missing_intervals"] == []
    assert series["export_price_actual"]["missing_intervals"] == []
    assert {item["validation_status"] for item in series.values()} == {"valid"}
    assert availability(body)["household_load"]["status"] == "available"
    assert availability(body)["grid_flow"]["status"] == "available"
    assert availability(body)["battery"]["status"] == "available"
    assert body["status"] == "partial"


def test_a_range_of_only_excluded_hours_is_empty_not_invalid(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(0, 1, 2, 3))
    seed_grid(environment.store, grid_flow(excluded=(0, 1, 2, 3)))

    body = environment.get_actual()

    household = by_id(body)["household_load_actual"]
    assert household["values"] == [None, None, None, None]
    assert household["missing_intervals"] == iso(0, 1, 2, 3)
    assert by_id(body)["grid_import_actual"]["values"] == [None] * 4
    assert availability(body)["household_load"]["status"] == "empty"
    assert availability(body)["grid_flow"]["status"] == "empty"
    assert "no household-load observations exist" in " ".join(body["diagnostics"])


def test_excluded_hours_outside_the_requested_range_are_not_reported(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(3,))
    seed_grid(environment.store, grid_flow(excluded=(3,)))

    body = environment.get_actual(end="2026-01-01T03:00:00Z")

    assert by_id(body)["household_load_actual"]["values"] == [1.0, 2.0, 3.0]
    assert by_id(body)["household_load_actual"]["missing_intervals"] == []
    assert by_id(body)["grid_import_actual"]["values"] == [0.5, 1.5, 2.5]
    assert by_id(body)["grid_import_actual"]["missing_intervals"] == []
    assert body["status"] == "validated"


def test_suspect_hours_of_persisted_legacy_history_are_served_as_gaps(
    environment: Environment,
) -> None:
    """Grid-flow and battery files of earlier versions flag hours with quality."""
    suspect = {"status": "suspect", "reason": "counter_reset", "entity_id": "s.grid"}
    valid = {"status": "valid", "reason": None, "entity_id": None}
    directory = environment.directory / "provider-data"
    directory.mkdir(exist_ok=True)
    common = {
        "schema_version": "1",
        "start_time": START.isoformat(),
        "interval_minutes": 60,
        "retrieved_at": START.isoformat(),
        "latest_observation_at": START.isoformat(),
    }
    legacy_grid = {
        **common,
        "import_kw": [0.5, 1.5, 2.5, 3.5],
        "export_kw": [0.05, 0.15, 0.25, 0.35],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "grid_flow"},
        "quality": [valid, suspect, valid, valid],
    }
    legacy_battery = {
        **common,
        **{
            name: [0.5] * 4
            for name in (
                "battery_energy_in_kwh",
                "battery_energy_out_kwh",
                "inverter_charge_energy_in_kwh",
                "inverter_charge_energy_out_kwh",
                "inverter_discharge_energy_in_kwh",
                "inverter_discharge_energy_out_kwh",
            )
        },
        "state_of_charge_percent": [20.0, 30.0, 40.0, 50.0, 60.0],
        "unit": "kWh",
        "source": {
            "provider": "home-assistant",
            "entity_id": "battery_efficiency_history",
        },
        "quality": [valid, valid, suspect, valid],
    }
    for key, payload in (
        (GRID_KEY, legacy_grid),
        (BATTERY_HISTORY_KEY, legacy_battery),
    ):
        (directory / f"{key.data_type}-{key.digest()}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    body = environment.get_actual()

    series = by_id(body)
    assert series["grid_import_actual"]["values"] == [0.5, None, 2.5, 3.5]
    assert series["grid_export_actual"]["values"] == pytest.approx(
        [0.05, None, 0.25, 0.35]
    )
    assert series["grid_import_actual"]["missing_intervals"] == iso(1)
    state_of_charge = series["battery_state_of_charge_actual"]
    # The suspect hour 2 has no state of charge at its own boundary.
    assert state_of_charge["values"][0] == 20.0
    assert state_of_charge["values"][2] is None
    assert iso(2)[0] in state_of_charge["missing_intervals"]
    assert {item["validation_status"] for item in series.values()} == {"valid"}
    excluded_hours = environment.client.get(
        "/api/v1/dashboard/excluded-hours",
        params={
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T04:00:00Z",
        },
    ).json()
    assert [
        (item["hour_start"], item["source"], cause["reason"])
        for item in excluded_hours["hours"]
        for cause in item["causes"]
    ] == [
        ("2026-01-01T01:00:00Z", "grid_flow", "flagged_by_earlier_version"),
        ("2026-01-01T02:00:00Z", "battery_efficiency", "flagged_by_earlier_version"),
    ]


def test_price_history_exposes_only_completed_hours_as_actuals(
    environment: Environment,
) -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    seed_prices(
        environment.store, price_history(now - timedelta(hours=2), 5, retrieved=now)
    )
    start = now - timedelta(hours=2)

    body = environment.get_actual(
        start.isoformat().replace("+00:00", "Z"),
        (now + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
    )

    prices = by_id(body)["import_price_actual"]
    assert [value is not None for value in prices["values"]] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert prices["available_end_time"] == now.isoformat().replace("+00:00", "Z")
    assert body["status"] == "partial"


def test_price_history_gaps_stay_explicit(environment: Environment) -> None:
    seed_prices(environment.store, price_history(count=4, skip=(1,)))

    prices = by_id(environment.get_actual())["import_price_actual"]

    assert prices["values"] == [0.30, None, 0.31, 0.32]
    assert prices["missing_intervals"] == iso(1)


def test_forecast_and_plan_data_never_appear_in_the_actual_scenario(
    environment: Environment,
) -> None:
    seed_household(environment.store)
    seed_prices(environment.store, price_history(), PRICE_FORECAST_KEY)
    environment.store.save(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        TypeAdapter(PvGenerationData),
        PvGenerationData(
            "1",
            START,
            60,
            (1.0, 2.0),
            "kW",
            ProviderSourceMetadata("forecast.solar", "pv_generation"),
            START,
            START + timedelta(hours=3),
        ),
    )

    body = environment.get_actual()

    assert [item["id"] for item in body["series"]] == ["household_load_actual"]
    assert {item["scenario_kind"] for item in body["series"]} == {"actual"}
    assert availability(body)["electricity_prices"]["status"] == "unavailable"
    assert availability(body)["pv_generation"]["status"] == "not_configured"
    forecast = environment.client.get(
        "/api/v1/dashboard/data",
        params={
            "scenario_kind": "forecast",
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T02:00:00Z",
        },
    ).json()
    assert forecast["assets"] == []


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-01-01T00:30:00Z", "2026-01-01T02:00:00Z", "aligned to the hour"),
        ("2026-01-01T00:00:00Z", "2026-01-01T02:15:00Z", "aligned to the hour"),
        ("2026-01-01T02:00:00Z", "2026-01-01T02:00:00Z", "later than start_time"),
        ("2026-01-01T03:00:00Z", "2026-01-01T02:00:00Z", "later than start_time"),
        ("2026-01-01T00:00:00", "2026-01-01T02:00:00Z", "include a timezone"),
        ("2026-01-01T00:00:00Z", "2036-02-01T00:00:00Z", "must not exceed"),
    ],
)
def test_range_rules_match_the_dashboard_contract(
    environment: Environment, start: str, end: str, message: str
) -> None:
    response = environment.client.get(
        "/api/v1/dashboard/data",
        params={"scenario_kind": "actual", "start_time": start, "end_time": end},
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_range_boundaries_are_half_open_and_normalized_to_utc(
    environment: Environment,
) -> None:
    seed_household(environment.store)

    body = environment.get_actual(
        "2026-01-01T02:00:00+01:00", "2026-01-01T04:00:00+01:00"
    )

    household = by_id(body)["household_load_actual"]
    assert household["timestamps"] == iso(1, 2)
    assert household["values"] == [2.0, 3.0]


def test_additional_asset_loaders_use_the_same_envelope_for_every_asset_type(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Future PV, EV, and heat-pump importers only register a loader."""
    seed_household(environment.store)

    def loader(asset: str, series_id: str, unit: str) -> historic.HistoricAssetLoader:
        def load(context: historic.HistoricReadContext) -> historic.HistoricAssetResult:
            series = DashboardSeries(
                id=series_id,
                data_type=asset,
                scenario_kind="actual",
                timestamps=hours(0, 1),
                values=[1.0, None],
                unit=unit,
                source=SourceMetadata(provider="test", entity_id=asset),
                requested_start_time=context.start,
                requested_end_time=context.end,
                available_start_time=START,
                available_end_time=START + timedelta(hours=2),
                retrieved_at=START,
                freshness="unknown",
                validation_status="valid",
                missing_intervals=hours(1),
            )
            return historic.HistoricAssetResult(asset, "available", (series,))

        return load

    monkeypatch.setattr(
        historic,
        "HISTORIC_ASSET_LOADERS",
        (
            historic.load_household_load,
            loader("pv_generation", "pv_generation_actual", "kW"),
            loader("electric_vehicle", "electric_vehicle_state_of_charge_actual", "%"),
            loader("heat_pump", "heat_pump_load_actual", "kW"),
        ),
    )

    body = environment.get_actual("2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")

    assert [item["asset"] for item in body["assets"]] == [
        "household_load",
        "pv_generation",
        "electric_vehicle",
        "heat_pump",
    ]
    assert {item["status"] for item in body["assets"]} == {"available"}
    assert list(by_id(body)) == [
        "household_load_actual",
        "pv_generation_actual",
        "electric_vehicle_state_of_charge_actual",
        "heat_pump_load_actual",
    ]
    assert by_id(body)["pv_generation_actual"]["scenario_kind"] == "actual"


def test_an_unexpected_asset_data_error_is_isolated_to_that_asset(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_household(environment.store)

    def broken(context: historic.HistoricReadContext) -> historic.HistoricAssetResult:
        raise ValueError("cannot represent this data")

    monkeypatch.setattr(
        historic,
        "HISTORIC_ASSET_LOADERS",
        (historic.load_household_load, broken),
    )

    body = environment.get_actual()

    assert "household_load_actual" in by_id(body)
    assert availability(body)["broken"]["status"] == "invalid"
    assert "cannot represent this data" not in " ".join(body["diagnostics"])


def test_actual_dashboard_is_reachable_without_specifying_a_scenario(
    environment: Environment,
) -> None:
    seed_household(environment.store)

    response = environment.client.get(
        "/api/v1/dashboard/data",
        params={
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T02:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["series"][0]["scenario_kind"] == "actual"
