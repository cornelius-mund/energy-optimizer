"""Tests for the direct Forecast.Solar PV forecast provider."""

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx
import pytest

from energy_optimizer.config import ForecastSolarConfiguration
from energy_optimizer.providers.forecast_solar import (
    ForecastSolarError,
    ForecastSolarImporter,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)


def configuration(**overrides: Any) -> ForecastSolarConfiguration:
    values: dict[str, Any] = {
        "latitude": 52.52,
        "longitude": 13.41,
        "declination_degrees": 35,
        "azimuth_degrees": 0,
        "peak_power_kw": 8,
        "timeout_seconds": 5,
        "max_data_age_seconds": 7200,
    }
    values.update(overrides)
    return ForecastSolarConfiguration.model_validate(values)


def forecast_payload(
    *,
    periods: Mapping[str, object] | None = None,
    timezone_name: str = "UTC",
) -> dict[str, object]:
    return {
        "result": {
            "watts": {
                "2026-01-01 00:00:00": 0,
                "2026-01-01 01:00:00": 0,
                "2026-01-01 02:00:00": 0,
                "2026-01-01 03:00:00": 0,
                "2026-01-01 04:00:00": 0,
            },
            "watt_hours_period": periods
            or {
                "2026-01-01 00:00:00": 0,
                "2026-01-01 01:00:00": 1000,
                "2026-01-01 02:00:00": 2000,
                "2026-01-01 03:00:00": 3000,
                "2026-01-01 04:00:00": 4000,
            },
        },
        "message": {"info": {"timezone": timezone_name}},
    }


def importer(
    handler: httpx.MockTransport | httpx.BaseTransport,
    **configuration_overrides: Any,
) -> tuple[ForecastSolarImporter, httpx.Client]:
    client = httpx.Client(transport=handler)
    return (
        ForecastSolarImporter(configuration(**configuration_overrides), client),
        client,
    )


def test_fetch_builds_public_url_and_normalizes_period_energy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/estimate/52.52/13.41/35/0/8"
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        return httpx.Response(200, json=forecast_payload())

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.interval_minutes == 60
    assert data.generation_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.unit == "kW"
    assert data.source.provider == "forecast.solar"
    assert data.source.entity_id == "pv_generation"
    assert data.retrieved_at == NOW
    assert data.expires_at == END


def test_fetch_converts_irregular_periods_to_hourly_values() -> None:
    payload = forecast_payload(
        periods={
            "2026-01-01 00:00:00": 0,
            "2026-01-01 00:30:00": 500,
            "2026-01-01 01:00:00": 1000,
            "2026-01-01 02:00:00": 2000,
            "2026-01-01 03:00:00": 3000,
            "2026-01-01 04:00:00": 4000,
        }
    )
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert data.generation_kw == (1.5, 2.0, 3.0, 4.0)


def test_fetch_accepts_the_two_day_public_forecast_horizon() -> None:
    periods = {
        (START + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S"): (
            1000 if hour == 1 else 0
        )
        for hour in range(49)
    }
    payload = forecast_payload(periods=periods)
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=48), now=NOW)
    finally:
        client.close()

    assert len(data.generation_kw) == 48
    assert data.generation_kw[0] == 1.0


def test_fetch_rejects_a_default_horizon_with_only_one_local_date() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=forecast_payload()))
    )
    try:
        with pytest.raises(ForecastSolarError, match="following day"):
            provider.fetch(START, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "rate limit"), (503, "HTTP 503")],
)
def test_fetch_reports_http_failures(status: int, message: str) -> None:
    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(status)))
    try:
        with pytest.raises(ForecastSolarError, match=message):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_fetch_reports_timeout() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(ForecastSolarError, match="timed out"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        {"result": {}, "message": {"info": {"timezone": "UTC"}}},
        {
            "result": {
                "watt_hours_period": {
                    "2026-01-01 00:00:00": 0,
                    "2026-01-01 01:00:00": 1000,
                }
            },
            "message": {"info": {"timezone": "UTC"}},
        },
    ],
)
def test_fetch_reports_malformed_or_incomplete_payload(payload: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=payload)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(ForecastSolarError):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    "periods",
    [
        {
            "2026-01-01 00:00:00": 0,
            "2026-01-01 01:00:00": -1,
            "2026-01-01 02:00:00": 0,
            "2026-01-01 03:00:00": 0,
            "2026-01-01 04:00:00": 0,
        },
        {
            "2026-01-01 00:00:00": 0,
            "2026-01-01 01:00:00": 1000,
            "2026-01-01 03:00:00": 2000,
            "2026-01-01 04:00:00": 0,
        },
    ],
)
def test_fetch_rejects_invalid_period_values_or_gaps(
    periods: dict[str, object],
) -> None:
    payload = forecast_payload(periods=periods)
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(ForecastSolarError):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_freshness_requires_coverage_and_retrieval_age() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=forecast_payload()))
    )
    try:
        data = provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert provider.is_fresh(data, now=datetime(2026, 1, 1, 1, 59, tzinfo=timezone.utc))
    assert not provider.is_fresh(data, now=END)
    provider.configuration = configuration(max_data_age_seconds=60)
    assert not provider.is_fresh(
        data,
        now=datetime(2026, 1, 1, 1, 31, tzinfo=timezone.utc),
    )
