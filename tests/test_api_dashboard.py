"""Dashboard HTTP API tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_dashboard_is_served_by_the_application(client: TestClient) -> None:
    with client as test_client:
        response = test_client.get("/dashboard/")

    assert response.status_code == 200
    assert "Energy dashboard" in response.text


def test_dashboard_serves_its_static_assets(client: TestClient) -> None:
    with client as test_client:
        html = test_client.get("/dashboard/")
        javascript = test_client.get("/dashboard/app.js")
        stylesheet = test_client.get("/dashboard/styles.css")

    assert html.status_code == 200
    assert javascript.status_code == 200
    assert stylesheet.status_code == 200


def test_dashboard_root_redirects_to_the_trailing_slash_path(
    client: TestClient,
) -> None:
    with client as test_client:
        response = test_client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/"


def test_missing_dashboard_assets_return_service_unavailable(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("energy_optimizer.api.FRONTEND_DIRECTORY", tmp_path / "missing")

    try:
        dashboard_redirect()
    except HTTPException as error:
        assert error.status_code == 503
        assert "dashboard assets" in str(error.detail)
    else:
        raise AssertionError("missing dashboard assets must not redirect")


def test_frontend_directory_can_be_configured_for_installed_deployments(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_FRONTEND_DIRECTORY", str(tmp_path))

    assert configured_frontend_directory() == tmp_path


def test_forecast_dashboard_returns_pv_series_and_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    configuration = persistence_configuration
    configuration.write_text(
        configuration.read_text(encoding="utf-8")
        + "forecast_solar:\n"
        + "  latitude: 52.52\n"
        + "  longitude: 13.41\n"
        + "  declination_degrees: 35\n"
        + "  azimuth_degrees: 0\n"
        + "  peak_power_kw: 8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    store = tmp_path / "provider-data"
    ProviderDataStore(store).save(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        TypeAdapter(PvGenerationData),
        PvGenerationData(
            schema_version="1",
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            interval_minutes=60,
            generation_kw=(1.0, 2.0),
            unit="kW",
            source=SourceMetadata(provider="forecast.solar", entity_id="pv_generation"),
            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1"
    assert body["status"] in {"validated", "stale"}
    assert len(body["series"]) == 1
    assert body["series"][0]["scenario_kind"] == "forecast"
    assert body["series"][0]["values"] == [1.0, 2.0]


def test_efficiency_dashboard_returns_component_series(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    configuration = persistence_configuration
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path / "provider-data")
    store.save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="ok",
            inverter_charge_efficiency=0.9,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.85,
            round_trip_efficiency=0.612,
            history_start=start,
            history_end=start + timedelta(hours=2),
            battery_throughput_kwh=5,
            charge_throughput_kwh=10,
            discharge_throughput_kwh=10,
            complete_cycle_count=1,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=start,
            latest_observation_at=start,
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "efficiency",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T03:00:00+00:00",
            },
        )

    assert response.status_code == 200
    series = {item["id"]: item for item in response.json()["series"]}
    assert set(series) == {
        "inverter_charge_efficiency_actual",
        "inverter_discharge_efficiency_actual",
        "battery_efficiency_actual",
        "round_trip_efficiency_actual",
    }
    assert series["battery_efficiency_actual"]["values"] == [0.85]


def test_efficiency_dashboard_marks_default_component_values(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path / "provider-data")
    store.save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="insufficient_data",
            inverter_charge_efficiency=0.95,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.85,
            round_trip_efficiency=0.646,
            history_start=start,
            history_end=start + timedelta(hours=2),
            battery_throughput_kwh=5,
            charge_throughput_kwh=0.05,
            discharge_throughput_kwh=10,
            complete_cycle_count=1,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=start,
            latest_observation_at=start,
            defaulted_components=("inverter_charge_efficiency",),
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "efficiency",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T03:00:00+00:00",
            },
        )

    assert response.status_code == 200
    series = {item["id"]: item for item in response.json()["series"]}
    assert series["battery_efficiency_actual"]["is_default"] is False
    assert series["inverter_discharge_efficiency_actual"]["is_default"] is False
    assert series["inverter_charge_efficiency_actual"]["is_default"] is True
    assert series["inverter_charge_efficiency_actual"]["values"] == [0.95]
    assert series["round_trip_efficiency_actual"]["values"] == [0.646]
    assert series["round_trip_efficiency_actual"]["is_default"] is False


def test_efficiency_dashboard_returns_history_values_for_any_requested_range(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration))
    history_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path / "provider-data")
    store.save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="ok",
            inverter_charge_efficiency=0.9,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.85,
            round_trip_efficiency=0.612,
            history_start=history_start,
            history_end=history_start + timedelta(hours=2),
            battery_throughput_kwh=5,
            charge_throughput_kwh=10,
            discharge_throughput_kwh=10,
            complete_cycle_count=1,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=history_start,
            latest_observation_at=history_start,
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "efficiency",
                "start_time": "2026-08-20T00:00:00+00:00",
                "end_time": "2026-08-20T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "validated"
    series = {item["id"]: item for item in body["series"]}
    assert series["inverter_charge_efficiency_actual"]["values"] == [0.9]
    assert (
        series["inverter_charge_efficiency_actual"]["available_start_time"]
        == "2026-01-01T00:00:00Z"
    )


def test_forecast_dashboard_returns_aligned_import_and_export_price_series(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    configuration = persistence_configuration
    configuration.write_text(
        configuration.read_text(encoding="utf-8") + "awattar: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    timestamps = (
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
    )
    ProviderDataStore(tmp_path / "provider-data").save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=timestamps,
            interval_minutes=60,
            import_price_eur_per_kwh=(0.10, 0.12),
            export_price_eur_per_kwh=(0.10, 0.12),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=timestamps[0],
            expires_at=timestamps[-1] + timedelta(hours=1),
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )

    assert response.status_code == 200
    series = {item["id"]: item for item in response.json()["series"]}
    assert series["import_price_forecast"]["timestamps"] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
    ]
    assert series["import_price_forecast"]["values"] == [0.10, 0.12]
    assert series["export_price_forecast"]["values"] == [0.10, 0.12]
    assert series["import_price_forecast"]["unit"] == "EUR/kWh"
    assert series["export_price_forecast"]["unit"] == "EUR/kWh"


def test_forecast_dashboard_preserves_prices_when_pv_coverage_starts_later(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    configuration = persistence_configuration
    configuration.write_text(
        configuration.read_text(encoding="utf-8")
        + "forecast_solar:\n"
        + "  latitude: 52.52\n"
        + "  longitude: 13.41\n"
        + "  declination_degrees: 35\n"
        + "  azimuth_degrees: 0\n"
        + "  peak_power_kw: 8\n"
        + "awattar: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = (start, start + timedelta(hours=1))
    store = ProviderDataStore(tmp_path / "provider-data")
    store.save(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        TypeAdapter(PvGenerationData),
        PvGenerationData(
            schema_version="1",
            start_time=start + timedelta(hours=2),
            interval_minutes=60,
            generation_kw=(1.0, 2.0),
            unit="kW",
            source=SourceMetadata(provider="forecast.solar", entity_id="pv_generation"),
            retrieved_at=start,
            expires_at=start + timedelta(hours=5),
        ),
    )
    store.save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=timestamps,
            interval_minutes=60,
            import_price_eur_per_kwh=(0.10, 0.12),
            export_price_eur_per_kwh=(0.10, 0.12),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=start,
            expires_at=start + timedelta(hours=2),
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T04:00:00+00:00",
            },
        )

    assert response.status_code == 200
    series = {item["id"]: item for item in response.json()["series"]}
    assert series["import_price_forecast"]["values"][:2] == [0.10, 0.12]
    assert series["export_price_forecast"]["values"][:2] == [0.10, 0.12]
    assert series["pv_generation_forecast"]["values"][:2] == [None, None]


def test_forecast_dashboard_does_not_fabricate_an_empty_price_direction(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    configuration = persistence_configuration
    configuration.write_text(
        configuration.read_text(encoding="utf-8") + "awattar: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ProviderDataStore(tmp_path / "provider-data").save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=(start,),
            interval_minutes=60,
            import_price_eur_per_kwh=(0.10,),
            export_price_eur_per_kwh=(),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=start,
            expires_at=start + timedelta(hours=2),
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["series"]] == [
        "import_price_forecast"
    ]


def test_forecast_dashboard_reports_unavailable_without_persistence(
    client: TestClient,
) -> None:
    with client as test_client:
        response = test_client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["series"] == []


def test_dashboard_contract_separates_actual_and_plan_scenarios(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        actual_response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "actual",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )
        plan_response = client.get(
            "/api/v1/dashboard/data",
            params={
                "scenario_kind": "plan",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )

    assert actual_response.status_code == 200
    assert actual_response.json()["series"][0]["scenario_kind"] == "actual"
    assert plan_response.status_code == 200
    assert plan_response.json()["series"] == []
    assert plan_response.json()["plan_summary"]["status"] == "unavailable"
