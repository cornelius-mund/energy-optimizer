"""Dashboard HTTP API tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from pytest import MonkeyPatch

from energy_optimizer.api import app, configured_frontend_directory, dashboard_redirect
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyData,
    ElectricityPriceData,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FORECAST_SOLAR_CONFIGURATION = """\
forecast_solar:
  latitude: 52.52
  longitude: 13.41
  declination_degrees: 35
  azimuth_degrees: 0
  peak_power_kw: 8
"""
AWATTAR_CONFIGURATION = "awattar: {}\n"
BATTERY_EFFICIENCY = BatteryEfficiencyData(
    schema_version="1",
    status="ok",
    inverter_charge_efficiency=0.9,
    inverter_discharge_efficiency=0.8,
    battery_efficiency=0.85,
    round_trip_efficiency=0.612,
    history_start=START,
    history_end=START + timedelta(hours=2),
    battery_throughput_kwh=5,
    charge_throughput_kwh=10,
    discharge_throughput_kwh=10,
    complete_cycle_count=1,
    unit="ratio",
    source=SourceMetadata(provider="home-assistant", entity_id="battery_efficiency"),
    retrieved_at=START,
    latest_observation_at=START,
)


def hours_after_start(hours: int) -> datetime:
    return START + timedelta(hours=hours)


@pytest.fixture
def store(persistence_configuration: Path, tmp_path: Path) -> ProviderDataStore:
    return ProviderDataStore(tmp_path / "provider-data")


def append_configuration(configuration: Path, text: str) -> None:
    configuration.write_text(
        configuration.read_text(encoding="utf-8") + text, encoding="utf-8"
    )


def save_battery_efficiency(store: ProviderDataStore, **changes: Any) -> None:
    store.save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        replace(BATTERY_EFFICIENCY, **changes),
    )


def save_pv_generation(
    store: ProviderDataStore, start_hours: int, expires_hours: int
) -> None:
    store.save(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        TypeAdapter(PvGenerationData),
        PvGenerationData(
            schema_version="1",
            start_time=hours_after_start(start_hours),
            interval_minutes=60,
            generation_kw=(1.0, 2.0),
            unit="kW",
            source=SourceMetadata(provider="forecast.solar", entity_id="pv_generation"),
            retrieved_at=START,
            expires_at=hours_after_start(expires_hours),
        ),
    )


def save_prices(
    store: ProviderDataStore,
    import_prices: tuple[float, ...],
    export_prices: tuple[float, ...],
) -> None:
    store.save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=tuple(
                hours_after_start(hour) for hour in range(len(import_prices))
            ),
            interval_minutes=60,
            import_price_eur_per_kwh=import_prices,
            export_price_eur_per_kwh=export_prices,
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=START,
            expires_at=hours_after_start(2),
        ),
    )


def get_dashboard_data(
    scenario_kind: str, window_hours: int, start_time: datetime = START
) -> dict[str, Any]:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": scenario_kind,
                "start_time": start_time.isoformat(),
                "end_time": (start_time + timedelta(hours=window_hours)).isoformat(),
            },
        )

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_dashboard_is_served_by_the_application(client: TestClient) -> None:
    with client:
        response = client.get("/dashboard/")

    assert response.status_code == 200
    assert "Energy dashboard" in response.text


def test_dashboard_serves_its_static_assets(client: TestClient) -> None:
    paths = ("/dashboard/", "/dashboard/app.js", "/dashboard/styles.css")

    with client:
        statuses = [client.get(path).status_code for path in paths]

    assert statuses == [200, 200, 200]


def test_dashboard_root_redirects_to_the_trailing_slash_path(
    client: TestClient,
) -> None:
    with client:
        response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("", "UTC"), ("timezone: Europe/Berlin\n", "Europe/Berlin")],
    ids=["default-utc", "configured"],
)
def test_dashboard_settings_return_the_timezone(
    minimal_configuration: Path, configured: str, expected: str
) -> None:
    minimal_configuration.write_text(
        configured + minimal_configuration.read_text(), encoding="utf-8"
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/settings")

    assert response.status_code == 200
    assert response.json() == {"timezone": expected}


def test_missing_dashboard_assets_return_service_unavailable(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("energy_optimizer.api.FRONTEND_DIRECTORY", tmp_path / "missing")

    with pytest.raises(HTTPException) as raised:
        dashboard_redirect()

    assert raised.value.status_code == 503
    assert "dashboard assets" in str(raised.value.detail)


def test_frontend_directory_can_be_configured_for_installed_deployments(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_FRONTEND_DIRECTORY", str(tmp_path))

    assert configured_frontend_directory() == tmp_path


def test_forecast_dashboard_returns_pv_series_and_metadata(
    persistence_configuration: Path, store: ProviderDataStore
) -> None:
    append_configuration(persistence_configuration, FORECAST_SOLAR_CONFIGURATION)
    save_pv_generation(store, start_hours=0, expires_hours=3)

    body = get_dashboard_data("forecast", window_hours=2)

    assert body["schema_version"] == "1"
    assert body["status"] in {"validated", "stale"}
    assert len(body["series"]) == 1
    assert body["series"][0]["scenario_kind"] == "forecast"
    assert body["series"][0]["values"] == [1.0, 2.0]


def test_efficiency_dashboard_returns_component_series(
    store: ProviderDataStore,
) -> None:
    save_battery_efficiency(store)

    body = get_dashboard_data("efficiency", window_hours=3)

    series = {item["id"]: item for item in body["series"]}
    assert set(series) == {
        "inverter_charge_efficiency_actual",
        "inverter_discharge_efficiency_actual",
        "battery_efficiency_actual",
        "round_trip_efficiency_actual",
    }
    assert series["battery_efficiency_actual"]["values"] == [0.85]


def test_efficiency_dashboard_marks_default_component_values(
    store: ProviderDataStore,
) -> None:
    save_battery_efficiency(
        store,
        status="insufficient_data",
        inverter_charge_efficiency=0.95,
        round_trip_efficiency=0.646,
        charge_throughput_kwh=0.05,
        defaulted_components=("inverter_charge_efficiency",),
    )

    body = get_dashboard_data("efficiency", window_hours=3)

    series = {item["id"]: item for item in body["series"]}
    assert series["battery_efficiency_actual"]["is_default"] is False
    assert series["inverter_discharge_efficiency_actual"]["is_default"] is False
    assert series["inverter_charge_efficiency_actual"]["is_default"] is True
    assert series["inverter_charge_efficiency_actual"]["values"] == [0.95]
    assert series["battery_efficiency_actual"]["calculation_status"] == "calculated"
    assert (
        series["inverter_charge_efficiency_actual"]["calculation_status"] == "defaulted"
    )
    assert (
        series["round_trip_efficiency_actual"]["calculation_status"]
        == "calculated_with_defaults"
    )
    assert series["round_trip_efficiency_actual"]["values"] == [0.646]
    assert series["round_trip_efficiency_actual"]["is_default"] is False
    metrics = {item["id"]: item for item in body["metrics"]}
    assert metrics["battery_throughput"] == {
        "id": "battery_throughput",
        "label": "Battery throughput",
        "value": 5.0,
        "unit": "kWh",
    }
    assert metrics["completed_battery_cycles"]["label"] == "Completed battery cycles"
    assert not any("throughput" in diagnostic for diagnostic in body["diagnostics"])


def test_efficiency_dashboard_keeps_full_throughput_precision(
    store: ProviderDataStore,
) -> None:
    """Leave two-decimal display rounding to the UI; the API value is unrounded."""
    save_battery_efficiency(
        store,
        inverter_charge_efficiency=0.95,
        round_trip_efficiency=0.646,
        battery_throughput_kwh=350.123456,
        charge_throughput_kwh=0.0,
        discharge_throughput_kwh=5.0,
        complete_cycle_count=2,
    )

    body = get_dashboard_data("efficiency", window_hours=3)

    metrics = {item["id"]: item for item in body["metrics"]}
    assert metrics["battery_throughput"]["value"] == 350.123456
    assert metrics["inverter_charge_throughput"]["value"] == 0.0
    assert metrics["inverter_discharge_throughput"]["value"] == 5.0
    assert metrics["completed_battery_cycles"]["value"] == 2
    assert {item["unit"] for item in metrics.values()} == {"kWh", "cycles"}


def test_efficiency_dashboard_pairs_fallback_metadata_with_each_status(
    store: ProviderDataStore,
) -> None:
    """Expose the fallback flag for every status that displays the fallback ratio."""
    save_battery_efficiency(
        store,
        status="invalid",
        inverter_charge_efficiency=0.95,
        inverter_discharge_efficiency=0.95,
        battery_efficiency=0.95,
        round_trip_efficiency=0.857,
        battery_throughput_kwh=0,
        charge_throughput_kwh=0,
        discharge_throughput_kwh=0.05,
        complete_cycle_count=0,
        defaulted_components=(
            "battery_efficiency",
            "inverter_charge_efficiency",
            "inverter_discharge_efficiency",
        ),
        component_statuses={
            "battery_efficiency": "unavailable",
            "inverter_charge_efficiency": "invalid",
            "inverter_discharge_efficiency": "defaulted",
            "round_trip_efficiency": "calculated_with_defaults",
        },
    )

    body = get_dashboard_data("efficiency", window_hours=3)

    assert {
        item["id"]: (item["calculation_status"], item["is_default"])
        for item in body["series"]
    } == {
        "battery_efficiency_actual": ("unavailable", True),
        "inverter_charge_efficiency_actual": ("invalid", True),
        "inverter_discharge_efficiency_actual": ("defaulted", True),
        "round_trip_efficiency_actual": ("calculated_with_defaults", False),
    }


def test_efficiency_dashboard_returns_history_values_for_any_requested_range(
    store: ProviderDataStore,
) -> None:
    save_battery_efficiency(store)

    body = get_dashboard_data(
        "efficiency",
        window_hours=1,
        start_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert body["status"] == "validated"
    series = {item["id"]: item for item in body["series"]}
    assert series["inverter_charge_efficiency_actual"]["values"] == [0.9]
    assert (
        series["inverter_charge_efficiency_actual"]["available_start_time"]
        == "2026-01-01T00:00:00Z"
    )


def test_forecast_dashboard_returns_aligned_import_and_export_price_series(
    persistence_configuration: Path, store: ProviderDataStore
) -> None:
    append_configuration(persistence_configuration, AWATTAR_CONFIGURATION)
    save_prices(store, import_prices=(0.10, 0.12), export_prices=(0.10, 0.12))

    body = get_dashboard_data("forecast", window_hours=2)

    series = {item["id"]: item for item in body["series"]}
    assert series["import_price_forecast"]["timestamps"] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
    ]
    assert series["import_price_forecast"]["values"] == [0.10, 0.12]
    assert series["export_price_forecast"]["values"] == [0.10, 0.12]
    assert series["import_price_forecast"]["unit"] == "EUR/kWh"
    assert series["export_price_forecast"]["unit"] == "EUR/kWh"


def test_forecast_dashboard_preserves_prices_when_pv_coverage_starts_later(
    persistence_configuration: Path, store: ProviderDataStore
) -> None:
    append_configuration(
        persistence_configuration, FORECAST_SOLAR_CONFIGURATION + AWATTAR_CONFIGURATION
    )
    save_pv_generation(store, start_hours=2, expires_hours=5)
    save_prices(store, import_prices=(0.10, 0.12), export_prices=(0.10, 0.12))

    body = get_dashboard_data("forecast", window_hours=4)

    series = {item["id"]: item for item in body["series"]}
    assert series["import_price_forecast"]["values"][:2] == [0.10, 0.12]
    assert series["export_price_forecast"]["values"][:2] == [0.10, 0.12]
    assert series["pv_generation_forecast"]["values"][:2] == [None, None]


def test_forecast_dashboard_does_not_fabricate_an_empty_price_direction(
    persistence_configuration: Path, store: ProviderDataStore
) -> None:
    append_configuration(persistence_configuration, AWATTAR_CONFIGURATION)
    save_prices(store, import_prices=(0.10,), export_prices=())

    body = get_dashboard_data("forecast", window_hours=1)

    assert [item["id"] for item in body["series"]] == ["import_price_forecast"]


@pytest.mark.usefixtures("minimal_configuration")
def test_forecast_dashboard_reports_unavailable_without_persistence() -> None:
    body = get_dashboard_data("forecast", window_hours=1)

    assert body["status"] == "unavailable"
    assert body["series"] == []


def test_dashboard_contract_separates_actual_and_plan_scenarios(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        responses = {
            kind: client.get(
                "/api/v1/dashboard/data",
                params={
                    "scenario_kind": kind,
                    "start_time": "2026-01-01T00:00:00+00:00",
                    "end_time": "2026-01-01T02:00:00+00:00",
                },
            )
            for kind in ("actual", "plan")
        }

    assert responses["actual"].status_code == 200
    assert responses["actual"].json()["series"][0]["scenario_kind"] == "actual"
    assert responses["plan"].status_code == 200
    assert responses["plan"].json()["series"] == []
    assert responses["plan"].json()["plan_summary"]["status"] == "unavailable"
