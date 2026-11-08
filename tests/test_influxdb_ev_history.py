"""Tests for the EV history read from the Home Assistant InfluxDB."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEvConfiguration, InfluxDbConfiguration
from energy_optimizer.providers.influxdb_ev_history import (
    InfluxDbError,
    InfluxDbEvHistoryImporter,
    InfluxSeries,
    merge_ev_history,
)

NOW = datetime(2026, 11, 8, 20, 30, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
WINDOW_END = datetime(2026, 11, 8, 20, tzinfo=UTC)
CAPACITY_KWH = 100.0

Handler = Callable[[httpx.Request], httpx.Response]


def influxdb(**overrides: Any) -> InfluxDbConfiguration:
    return InfluxDbConfiguration.model_validate(
        {"base_url": "http://influx.test:8086", **overrides}
    )


def ev(unit: str = "%") -> HomeAssistantEvConfiguration:
    return HomeAssistantEvConfiguration.model_validate(
        {
            "state_of_charge": {"entity_id": "sensor.ev_soc", "unit": unit},
            "plugged_in": {"entity_id": "binary_sensor.ev_cable"},
            "capacity_kwh": CAPACITY_KWH,
            "minimum_charge_kw": 1.4,
            "maximum_charge_kw": 11,
        }
    )


def influx_answer(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "results": [
            {
                "statement_id": 0,
                "series": [{"name": "x", "columns": [], "values": rows}],
            }
        ]
    }


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


class FakeInflux:
    """Answers each query from per-entity hourly values and records the queries."""

    def __init__(self, values: dict[str, dict[datetime, float | None]]) -> None:
        self.values = values
        self.queries: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parameters = dict(request.url.params)
        self.queries.append(parameters)
        entity = next(name for name in self.values if f"'{name}'" in parameters["q"])
        rows = [[epoch(hour), value] for hour, value in self.values[entity].items()]
        return httpx.Response(200, json=influx_answer(rows))


def importer(
    handler: Handler, unit: str = "%", **overrides: Any
) -> InfluxDbEvHistoryImporter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return InfluxDbEvHistoryImporter(influxdb(**overrides), ev(unit), client)


def test_the_last_state_of_every_hour_is_imported() -> None:
    first = WINDOW_END - 3 * ONE_HOUR
    fake = FakeInflux(
        {
            "ev_soc": {first: 40.0, first + ONE_HOUR: 55.5, first + 2 * ONE_HOUR: None},
            "ev_cable": {first: 1.0, first + ONE_HOUR: 0.2, first + 2 * ONE_HOUR: 0.6},
        }
    )

    data = importer(fake).fetch(first, WINDOW_END, now=NOW)

    assert data.start_time == first
    assert data.state_of_charge_percent == (40.0, 55.5, None)
    assert data.plugged_in == (True, False, True)
    assert data.retrieved_at == NOW


def test_the_query_reads_the_entity_from_its_unit_measurement_per_hour() -> None:
    fake = FakeInflux({"ev_soc": {}, "ev_cable": {}})

    importer(fake, username="reader", password="secret").fetch(
        WINDOW_END - ONE_HOUR, WINDOW_END, now=NOW
    )

    soc_query, cable_query = fake.queries
    assert soc_query["db"] == "homeassistant"
    assert soc_query["epoch"] == "s"
    assert (soc_query["u"], soc_query["p"]) == ("reader", "secret")
    assert soc_query["q"] == (
        'SELECT last("value") FROM "autogen"."%" '
        "WHERE \"entity_id\" = 'ev_soc' "
        "AND time >= '2026-11-08T19:00:00Z' AND time < '2026-11-08T20:00:00Z' "
        "GROUP BY time(1h) fill(previous)"
    )
    assert '"autogen"."binary_sensor.ev_cable"' in cable_query["q"]


def test_old_hours_come_from_the_long_term_policy_and_recent_ones_from_raw() -> None:
    fake = FakeInflux({"ev_soc": {}, "ev_cable": {}})
    start = NOW - timedelta(days=100)

    importer(fake).fetch(start, WINDOW_END, now=NOW)

    soc_queries = [query["q"] for query in fake.queries if "'ev_soc'" in query["q"]]
    long_term, raw = soc_queries
    assert 'SELECT last("mean_value") FROM "downsample_rp"."%"' in long_term
    assert 'SELECT last("value") FROM "autogen"."%"' in raw
    boundary = "2026-08-11T20:00:00Z"
    assert f"time < '{boundary}'" in long_term
    assert f"time >= '{boundary}'" in raw


def test_the_plug_history_can_come_from_another_entity_than_the_live_state() -> None:
    hour = WINDOW_END - ONE_HOUR
    fake = FakeInflux({"ev_soc": {}, "ev_charge_port": {hour: 1.0}})

    data = importer(fake, ev_plugged_in_entity_id="binary_sensor.ev_charge_port").fetch(
        hour, WINDOW_END, now=NOW
    )

    assert data.plugged_in == (True,)
    assert '"autogen"."binary_sensor.ev_charge_port"' in fake.queries[1]["q"]


def test_a_state_of_charge_in_kwh_is_converted_to_percent() -> None:
    hour = WINDOW_END - ONE_HOUR
    fake = FakeInflux({"ev_soc": {hour: 25.0}, "ev_cable": {}})

    data = importer(fake, unit="kWh").fetch(hour, WINDOW_END, now=NOW)

    assert data.state_of_charge_percent == (25.0 / CAPACITY_KWH * 100,)


def test_unitless_entities_are_stored_under_their_entity_id() -> None:
    assert InfluxSeries.for_entity("binary_sensor.cable", None) == InfluxSeries(
        measurement="binary_sensor.cable", entity_tag="cable"
    )
    assert InfluxSeries.for_entity("sensor.soc", "%") == InfluxSeries(
        measurement="%", entity_tag="soc"
    )


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, {"error": "authorization failed"}, "rejected the credentials"),
        (500, {}, "HTTP 500"),
        (200, {"results": [{"error": "measurement not found"}]}, "rejected the query"),
        (200, {"results": []}, "must contain results"),
        (200, influx_answer([[epoch(NOW), "high"]]), "must be numeric"),
        (200, influx_answer([["yesterday", 1.0]]), "epoch seconds"),
        (200, influx_answer([[epoch(NOW)]]), "a time and one value"),
    ],
)
def test_an_unusable_answer_is_rejected(
    status: int, body: dict[str, Any], message: str
) -> None:
    handler: Handler = lambda _: httpx.Response(status, json=body)  # noqa: E731

    with pytest.raises(InfluxDbError, match=message):
        importer(handler).fetch(WINDOW_END - ONE_HOUR, WINDOW_END, now=NOW)


def test_an_empty_window_is_rejected() -> None:
    with pytest.raises(InfluxDbError, match="must not be empty"):
        importer(FakeInflux({})).fetch(WINDOW_END, WINDOW_END, now=NOW)


def test_a_refresh_overlays_the_persisted_history_and_keeps_its_length() -> None:
    first = WINDOW_END - 4 * ONE_HOUR
    persisted_fake = FakeInflux(
        {
            "ev_soc": {first + ONE_HOUR * i: 10.0 * i for i in range(3)},
            "ev_cable": {first + ONE_HOUR * i: 1.0 for i in range(3)},
        }
    )
    persisted = importer(persisted_fake).fetch(first, first + 3 * ONE_HOUR, now=NOW)
    update_fake = FakeInflux(
        {
            "ev_soc": {first + 2 * ONE_HOUR: 25.0, first + 3 * ONE_HOUR: 30.0},
            "ev_cable": {first + 2 * ONE_HOUR: 0.0, first + 3 * ONE_HOUR: 0.0},
        }
    )
    update = importer(update_fake).fetch(first + 2 * ONE_HOUR, WINDOW_END, now=NOW)

    merged = merge_ev_history(persisted, update, keep_hours=3)

    assert merged.start_time == first + ONE_HOUR
    assert merged.state_of_charge_percent == (10.0, 25.0, 30.0)
    assert merged.plugged_in == (True, False, False)


def test_the_history_turns_stale_after_its_maximum_age() -> None:
    history_importer = importer(FakeInflux({"ev_soc": {}, "ev_cable": {}}))
    data = history_importer.fetch(WINDOW_END - ONE_HOUR, WINDOW_END, now=NOW)

    assert history_importer.is_fresh(data, now=NOW + timedelta(hours=1))
    assert not history_importer.is_fresh(data, now=NOW + timedelta(hours=3))
