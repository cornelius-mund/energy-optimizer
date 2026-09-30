"""Tests for the direct Forecast.Solar PV forecast provider."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any

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

Handler = Callable[[httpx.Request], httpx.Response]
Importer = Callable[..., ForecastSolarImporter]


def configuration(**overrides: Any) -> ForecastSolarConfiguration:
    return ForecastSolarConfiguration.model_validate(
        {
            "latitude": 52.52,
            "longitude": 13.41,
            "declination_degrees": 35,
            "azimuth_degrees": 0,
            "peak_power_kw": 8,
            "timeout_seconds": 5,
            "max_data_age_seconds": 7200,
            **overrides,
        }
    )


def hourly(*values: int, first: datetime = START) -> dict[str, int]:
    """Key consecutive hourly values by their Forecast.Solar timestamps."""
    return {
        (first + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S"): value
        for hour, value in enumerate(values)
    }


def response(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"result": result, "message": {"info": {"timezone": "UTC"}}}


def forecast_payload(periods: Mapping[str, int] | None = None) -> dict[str, Any]:
    return response(
        {
            "watts": hourly(0, 0, 0, 0, 0),
            "watt_hours_period": periods or hourly(0, 1000, 2000, 3000, 4000),
        }
    )


def respond(status: int = 200, **content: Any) -> Handler:
    """Answer every request with the same response."""
    return lambda _: httpx.Response(status, **content)


@pytest.fixture
def importer() -> Iterator[Importer]:
    with ExitStack() as clients:

        def build(handler: Handler, **overrides: Any) -> ForecastSolarImporter:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            clients.enter_context(client)
            return ForecastSolarImporter(configuration(**overrides), client)

        yield build


def test_fetch_builds_public_url_and_normalizes_period_energy(
    importer: Importer,
) -> None:
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

    data = importer(handler).fetch(START, END, now=NOW)

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.interval_minutes == 60
    assert data.generation_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.unit == "kW"
    assert data.source.provider == "forecast.solar"
    assert data.source.entity_id == "pv_generation"
    assert data.retrieved_at == NOW
    assert data.expires_at == END


def test_fetch_converts_irregular_periods_to_hourly_values(
    importer: Importer,
) -> None:
    periods = {
        **hourly(0),
        "2026-01-01 00:30:00": 500,
        **hourly(1000, 2000, 3000, 4000, first=START + timedelta(hours=1)),
    }
    provider = importer(respond(json=forecast_payload(periods)))

    data = provider.fetch(START, END, now=NOW)

    assert data.generation_kw == (1.5, 2.0, 3.0, 4.0)


def test_fetch_accepts_the_two_day_public_forecast_horizon(
    importer: Importer,
) -> None:
    provider = importer(respond(json=forecast_payload(hourly(0, 1000, *[0] * 47))))

    data = provider.fetch(START, START + timedelta(hours=48), now=NOW)

    assert len(data.generation_kw) == 48
    assert data.generation_kw[0] == 1.0


def test_fetch_rejects_a_default_horizon_with_only_one_local_date(
    importer: Importer,
) -> None:
    provider = importer(respond(json=forecast_payload()))

    with pytest.raises(ForecastSolarError, match="following day"):
        provider.fetch(START, now=NOW)


@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "rate limit"), (503, "HTTP 503")],
)
def test_fetch_reports_http_failures(
    importer: Importer, status: int, message: str
) -> None:
    with pytest.raises(ForecastSolarError, match=message):
        importer(respond(status)).fetch(START, END, now=NOW)


def test_fetch_reports_timeout(importer: Importer) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(ForecastSolarError, match="timed out"):
        importer(handler).fetch(START, END, now=NOW)


@pytest.mark.parametrize(
    "content",
    [
        {"content": b"not-json"},
        {"json": response({})},
        {"json": response({"watt_hours_period": hourly(0, 1000)})},
    ],
    ids=["not-json", "empty-result", "missing-watts"],
)
def test_fetch_reports_malformed_or_incomplete_payload(
    importer: Importer, content: dict[str, Any]
) -> None:
    with pytest.raises(ForecastSolarError):
        importer(respond(**content)).fetch(START, END, now=NOW)


@pytest.mark.parametrize(
    "periods",
    [
        hourly(0, -1, 0, 0, 0),
        {**hourly(0, 1000), **hourly(2000, 0, first=START + timedelta(hours=3))},
    ],
    ids=["negative-energy", "missing-hour"],
)
def test_fetch_rejects_invalid_period_values_or_gaps(
    importer: Importer, periods: dict[str, int]
) -> None:
    provider = importer(respond(json=forecast_payload(periods)))

    with pytest.raises(ForecastSolarError):
        provider.fetch(START, END, now=NOW)


def test_freshness_requires_coverage_and_retrieval_age(importer: Importer) -> None:
    provider = importer(respond(json=forecast_payload()))
    data = provider.fetch(START, END, now=NOW)

    assert provider.is_fresh(data, now=datetime(2026, 1, 1, 1, 59, tzinfo=timezone.utc))
    assert not provider.is_fresh(data, now=END)
    provider.configuration = configuration(max_data_age_seconds=60)
    assert not provider.is_fresh(
        data,
        now=datetime(2026, 1, 1, 1, 31, tzinfo=timezone.utc),
    )
