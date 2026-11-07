"""Tests for the multi-plane Open-Meteo PV forecast provider."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import OpenMeteoPvConfiguration
from energy_optimizer.providers.open_meteo_pv import (
    OpenMeteoPvError,
    OpenMeteoPvImporter,
)

START = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=3)
NOW = START + timedelta(minutes=20)
WEST = {"name": "West", "tilt_degrees": 22, "azimuth_degrees": 90, "peak_power_kw": 10}
FACADE = {
    "name": "Facade",
    "tilt_degrees": 80,
    "azimuth_degrees": 0,
    "peak_power_kw": 2,
}
WEST_IRRADIANCE = (500.0, 600.0, 700.0)
FACADE_IRRADIANCE = (100.0, 200.0, 300.0)

Handler = Callable[[httpx.Request], httpx.Response]
Importer = Callable[..., OpenMeteoPvImporter]


def configuration(**overrides: Any) -> OpenMeteoPvConfiguration:
    return OpenMeteoPvConfiguration.model_validate(
        {
            "latitude": 52.52,
            "longitude": 13.41,
            "planes": [WEST, FACADE],
            "performance_factor": 0.8,
            "timeout_seconds": 5,
            **overrides,
        }
    )


def irradiance_payload(
    values: tuple[float | None, ...], first_hour_start: datetime = START
) -> dict[str, Any]:
    """Label each value with the end of its hour, as Open-Meteo does."""
    labels = [
        (first_hour_start + timedelta(hours=offset + 1)).strftime("%Y-%m-%dT%H:%M")
        for offset in range(len(values))
    ]
    return {
        "utc_offset_seconds": 0,
        "hourly": {"time": labels, "global_tilted_irradiance": list(values)},
    }


def by_tilt(payloads: Mapping[str, dict[str, Any]]) -> Handler:
    """Answer each plane's request with the payload for its tilt."""
    return lambda request: httpx.Response(
        200, json=payloads[request.url.params["tilt"]]
    )


def two_planes(
    west: tuple[float | None, ...] = WEST_IRRADIANCE,
    facade: tuple[float | None, ...] = FACADE_IRRADIANCE,
) -> Handler:
    return by_tilt({"22": irradiance_payload(west), "80": irradiance_payload(facade)})


@pytest.fixture
def importer() -> Iterator[Importer]:
    with ExitStack() as clients:

        def build(handler: Handler, **overrides: Any) -> OpenMeteoPvImporter:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            clients.enter_context(client)
            return OpenMeteoPvImporter(configuration(**overrides), client)

        yield build


def test_fetch_requests_each_plane_with_its_tilt_and_azimuth(
    importer: Importer,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return two_planes()(request)

    importer(handler).fetch(START, END, now=NOW)

    assert [request.url.path for request in requests] == ["/v1/forecast"] * 2
    parameters = [dict(request.url.params) for request in requests]
    assert [(p["tilt"], p["azimuth"]) for p in parameters] == [
        ("22", "90"),
        ("80", "0"),
    ]
    assert all(p["hourly"] == "global_tilted_irradiance" for p in parameters)
    assert all(p["timezone"] == "UTC" for p in parameters)
    assert all(p["forecast_days"] == "2" for p in parameters)
    assert all(
        (p["latitude"], p["longitude"]) == ("52.52", "13.41") for p in parameters
    )
    assert requests[0].extensions["timeout"]["read"] == 5.0


def test_fetch_sums_planes_and_applies_the_performance_factor(
    importer: Importer,
) -> None:
    data = importer(two_planes()).fetch(START, END, now=NOW)

    expected = tuple(
        (west / 1000 * 10 + facade / 1000 * 2) * 0.8
        for west, facade in zip(WEST_IRRADIANCE, FACADE_IRRADIANCE, strict=True)
    )
    assert data.generation_kw == pytest.approx(expected)
    assert data.start_time == START
    assert data.expires_at == END
    assert data.interval_minutes == 60
    assert data.unit == "kW"
    assert data.source.provider == "open-meteo"
    assert data.source.entity_id == "pv_generation"
    assert data.retrieved_at == NOW


def test_fetch_assigns_each_value_to_the_hour_before_its_label(
    importer: Importer,
) -> None:
    payload = irradiance_payload((0.0, 1000.0), first_hour_start=START)
    assert payload["hourly"]["time"][1] == "2026-06-01T12:00"

    data = importer(lambda _: httpx.Response(200, json=payload), planes=[WEST]).fetch(
        START, START + timedelta(hours=2), now=NOW
    )

    assert data.generation_kw == pytest.approx((0.0, 10 * 0.8))


def test_fetch_without_end_covers_hours_every_plane_reports(
    importer: Importer,
) -> None:
    provider = importer(two_planes(facade=FACADE_IRRADIANCE[:2]))

    data = provider.fetch(START, now=NOW)

    assert len(data.generation_kw) == 2
    assert data.expires_at == START + timedelta(hours=2)


def test_fetch_without_end_stops_at_the_first_missing_value(
    importer: Importer,
) -> None:
    provider = importer(two_planes(west=(500.0, None, 700.0)))

    data = provider.fetch(START, now=NOW)

    assert data.expires_at == START + timedelta(hours=1)


def test_fetch_rejects_a_requested_hour_without_irradiance(
    importer: Importer,
) -> None:
    provider = importer(two_planes(west=(500.0, None, 700.0)))

    with pytest.raises(OpenMeteoPvError, match="does not cover the requested hour"):
        provider.fetch(START, END, now=NOW)


def test_fetch_rejects_a_forecast_that_starts_after_the_requested_start(
    importer: Importer,
) -> None:
    provider = importer(two_planes())

    with pytest.raises(OpenMeteoPvError, match="requested start"):
        provider.fetch(START - timedelta(hours=1), now=NOW)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"utc_offset_seconds": 3600, "hourly": {}}, "UTC timestamps"),
        ({"utc_offset_seconds": 0}, "hourly object"),
        ({"utc_offset_seconds": 0, "hourly": {"time": []}}, "hourly time"),
        (
            {
                "utc_offset_seconds": 0,
                "hourly": {
                    "time": ["2026-06-01T11:00"],
                    "global_tilted_irradiance": [],
                },
            },
            "differ in length",
        ),
        (
            {
                "utc_offset_seconds": 0,
                "hourly": {"time": ["noon"], "global_tilted_irradiance": [1.0]},
            },
            "invalid timestamp",
        ),
        (
            {
                "utc_offset_seconds": 0,
                "hourly": {
                    "time": ["2026-06-01T11:00"],
                    "global_tilted_irradiance": [-5.0],
                },
            },
            "irradiance",
        ),
    ],
)
def test_fetch_rejects_malformed_payloads(
    importer: Importer, payload: Any, message: str
) -> None:
    provider = importer(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(OpenMeteoPvError, match=message):
        provider.fetch(START, now=NOW)


@pytest.mark.parametrize(
    ("status", "message"), [(429, "rate limit"), (500, "HTTP 500")]
)
def test_fetch_reports_http_errors(
    importer: Importer, status: int, message: str
) -> None:
    provider = importer(lambda _: httpx.Response(status, json={}))

    with pytest.raises(OpenMeteoPvError, match=message):
        provider.fetch(START, now=NOW)


def test_fetch_reports_timeouts(importer: Importer) -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(OpenMeteoPvError, match="timed out"):
        importer(time_out).fetch(START, now=NOW)


def test_fetch_rejects_naive_start_times(importer: Importer) -> None:
    with pytest.raises(OpenMeteoPvError, match="timezone"):
        importer(two_planes()).fetch(START.replace(tzinfo=None), now=NOW)


def test_forecast_is_fresh_until_it_ages_or_expires(importer: Importer) -> None:
    provider = importer(two_planes(), max_data_age_seconds=3600)
    data = provider.fetch(START, END, now=NOW)

    assert provider.is_fresh(data, now=NOW + timedelta(minutes=30))
    assert not provider.is_fresh(data, now=NOW + timedelta(hours=2))
    assert not provider.is_fresh(data, now=END + timedelta(minutes=1))
