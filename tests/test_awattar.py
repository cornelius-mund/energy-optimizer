"""Tests for the aWATTar Germany electricity-price provider."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import AwattarConfiguration
from energy_optimizer.providers.awattar import AwattarError, AwattarImporter

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)

Handler = Callable[[httpx.Request], httpx.Response]
Importer = Callable[..., AwattarImporter]


def configuration(**overrides: Any) -> AwattarConfiguration:
    return AwattarConfiguration.model_validate(
        {"timeout_seconds": 5, "max_data_age_seconds": 7200, **overrides}
    )


def epoch_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def payload(
    *,
    prices: tuple[float, ...] = (100.0, 120.0),
    unit: str = "Eur/MWh",
    first: datetime = START,
) -> dict[str, Any]:
    return {
        "data": [
            {
                "start_timestamp": epoch_milliseconds(first + timedelta(hours=index)),
                "end_timestamp": epoch_milliseconds(first + timedelta(hours=index + 1)),
                "marketprice": price,
                "unit": unit,
            }
            for index, price in enumerate(prices)
        ]
    }


def respond(status: int = 200, **content: Any) -> Handler:
    """Answer every request with the same response."""
    return lambda _: httpx.Response(status, **content)


def recording(requests: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload())

    return handler


@pytest.fixture
def importer() -> Iterator[Importer]:
    with ExitStack() as clients:

        def build(handler: Handler, **overrides: Any) -> AwattarImporter:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            clients.enter_context(client)
            return AwattarImporter(configuration(**overrides), client)

        yield build


def test_fetch_normalizes_market_prices_and_source(importer: Importer) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/marketdata":
            return httpx.Response(200, json=payload())
        return httpx.Response(404)

    data = importer(handler).fetch(START, now=NOW)

    assert data.timestamps == (START + timedelta(hours=1),)
    assert data.import_price_eur_per_kwh == (0.12,)
    assert data.export_price_eur_per_kwh == (0.12,)
    assert data.unit == "EUR/kWh"
    assert data.source.provider == "awattar.de"
    assert data.source.entity_id == "de"
    assert data.expires_at == START + timedelta(hours=2)


def test_fetch_keeps_all_intervals_when_retrieval_is_before_first_hour(
    importer: Importer,
) -> None:
    data = importer(respond(json=payload())).fetch(START, now=START)

    assert data.timestamps == (START, START + timedelta(hours=1))
    assert data.import_price_eur_per_kwh == (0.1, 0.12)
    assert data.export_price_eur_per_kwh == data.import_price_eur_per_kwh


def test_fetch_requests_the_explicit_price_window(importer: Importer) -> None:
    requests: list[httpx.Request] = []
    provider = importer(recording(requests))

    provider.fetch(START, now=NOW)
    explicit_end = START + timedelta(hours=6)
    provider.fetch(START, explicit_end, now=NOW)

    default_window, explicit_window = (dict(r.url.params) for r in requests)
    assert default_window == {
        "start": str(epoch_milliseconds(START)),
        "end": str(epoch_milliseconds(START + timedelta(hours=48))),
    }
    assert explicit_window == {
        "start": str(epoch_milliseconds(START)),
        "end": str(epoch_milliseconds(explicit_end)),
    }


def test_fetch_validates_the_window_before_sending_a_request(
    importer: Importer,
) -> None:
    requests: list[httpx.Request] = []
    provider = importer(recording(requests))

    with pytest.raises(AwattarError, match="aligned to the hour"):
        provider.fetch(START + timedelta(minutes=30), now=NOW)
    with pytest.raises(AwattarError, match="timezone"):
        provider.fetch(datetime(2026, 1, 1), now=NOW)

    assert requests == []


def test_fetch_keeps_prices_that_reach_into_the_next_day(importer: Importer) -> None:
    provider = importer(respond(json=payload(prices=tuple(range(36)))))

    data = provider.fetch(START, now=START - timedelta(minutes=30))

    assert len(data.timestamps) == 36
    assert data.timestamps[0] == START
    assert data.timestamps[-1] == START + timedelta(hours=35)
    assert data.expires_at == START + timedelta(hours=36)


def test_fetch_accepts_a_response_that_ends_at_the_local_end_of_today(
    importer: Importer,
) -> None:
    first = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    now = datetime(2026, 9, 30, 9, 56, tzinfo=timezone.utc)
    provider = importer(respond(json=payload(prices=tuple(range(13)), first=first)))

    data = provider.fetch(first, now=now)

    assert data.timestamps[0] == datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    assert data.timestamps[-1] == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    assert data.expires_at == datetime(2026, 9, 30, 22, tzinfo=timezone.utc)


def test_fetch_ignores_intervals_at_or_after_the_window_end(
    importer: Importer,
) -> None:
    provider = importer(respond(json=payload(prices=(100.0, 120.0, 140.0))))

    data = provider.fetch(START, START + timedelta(hours=2), now=START)

    assert data.timestamps == (START, START + timedelta(hours=1))
    assert data.expires_at == START + timedelta(hours=2)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"data": []},
        {"data": [{"start_timestamp": 0}]},
        payload(unit="EUR/kWh"),
        payload(prices=(200_000.0,)),
    ],
    ids=["empty", "malformed", "unsupported-unit", "out-of-range-price"],
)
def test_fetch_rejects_empty_malformed_unsupported_or_out_of_range_data(
    importer: Importer, bad_payload: dict[str, Any]
) -> None:
    with pytest.raises(AwattarError):
        importer(respond(json=bad_payload)).fetch(START, now=START)


def test_fetch_rejects_gaps_and_transport_failures(importer: Importer) -> None:
    gap = payload()
    for key in ("start_timestamp", "end_timestamp"):
        gap["data"][1][key] += 3_600_000

    with pytest.raises(AwattarError, match="contiguous"):
        importer(respond(json=gap)).fetch(START, now=START)
    with pytest.raises(AwattarError, match="HTTP 503"):
        importer(respond(503)).fetch(START, now=START)


def test_freshness_checks_expiry_and_retrieval_age(importer: Importer) -> None:
    provider = importer(respond(json=payload()))
    data = provider.fetch(START, now=NOW)

    assert provider.is_fresh(data, now=START + timedelta(hours=1, minutes=30))
    assert not provider.is_fresh(data, now=START + timedelta(hours=2))
    provider.configuration = configuration(max_data_age_seconds=1800)
    assert not provider.is_fresh(data, now=START + timedelta(hours=1, minutes=1))
